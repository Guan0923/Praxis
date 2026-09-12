import { reportFromError } from "../api/errorReport";
import { pauseTurn, SseExecutionError, SseProtocolError, streamAttachedTurn, streamChat, streamResume, streamRewind } from "../api";
import type { ChatMessage, RuntimeStateNode, StreamMessage } from "../types";
import type { ActiveRun, ChatRunRequest } from "./types";
import { integrateRuntimeNodeUpdates, projectRuntimeNode } from "./runtime/runtimeDetailProjection";
import { applyRuntimeNodeFrame, runtimeNodeAccumulator } from "./runtime/runtimeNodeReducer";

export interface RunControllerCallbacks {
  activeRuns: Map<string, ActiveRun>;
  updateLastMessage: (conversationId: string, updater: (message: ChatMessage) => ChatMessage) => void;
  rebindRunSession: (conversationId: string, sessionId: string) => Promise<void>;
  refreshSessions: () => Promise<void>;
  refreshQueuedMessages?: (conversationId: string) => Promise<void>;
  updateConversation?: (conversationId: string, updater: (conversation: import("../types").Conversation) => import("../types").Conversation) => void;
  recoverConversation: (conversationId: string, sessionId: string, turnId?: string) => Promise<void>;
  checkSandboxHealth?: () => Promise<unknown>;
  onControlError?: (message: string) => void;
}

export function createRunController(callbacks: RunControllerCallbacks) {
  const pendingPauses = new Set<string>();
  const canReplaceSubscription = new WeakMap<ActiveRun, () => boolean>();

  async function runConversation(request: ChatRunRequest): Promise<void> {
    const previous = callbacks.activeRuns.get(request.conversationId);
    if (previous) {
      if (!request.attach && canReplaceSubscription.get(previous)?.()) {
        // Explicitly replace a sealed subscription, not the backend execution.
        previous.controller.abort();
      } else if (!request.waitForActiveRun) return;
      await previous.settled;
      if (callbacks.activeRuns.has(request.conversationId)) return;
    }
    const controller = new AbortController();
    let releaseSettled!: () => void;
    const settled = new Promise<void>((resolve) => {
      releaseSettled = resolve;
    });
    const active: ActiveRun = { controller, sessionId: request.sessionId, turnId: request.attach ? request.turnId : undefined, settled };
    callbacks.activeRuns.set(request.conversationId, active);
    const accumulator = runtimeNodeAccumulator();
    const pendingTurns = new Map<string, RuntimeStateNode>();
    let finalTurn: RuntimeStateNode | undefined;
    canReplaceSubscription.set(active, () => finalTurn !== undefined && finalTurn.status !== "running");
    let admissionAccepted = false;
    let pendingActiveTurnId: string | undefined;
    let forcePathProjection = false;
    let scheduledFrame: number | undefined;
    let scheduledWithAnimationFrame = false;

    const cancelScheduledFrame = () => {
      if (scheduledFrame === undefined) return;
      if (scheduledWithAnimationFrame && typeof globalThis.cancelAnimationFrame === "function") {
        globalThis.cancelAnimationFrame(scheduledFrame);
      } else {
        globalThis.clearTimeout(scheduledFrame);
      }
      scheduledFrame = undefined;
      scheduledWithAnimationFrame = false;
    };

    const flushPendingFrames = () => {
      cancelScheduledFrame();
      if (!pendingActiveTurnId || pendingTurns.size === 0) return;
      const turns = [...pendingTurns.values()];
      const activeTurnId = pendingActiveTurnId;
      const reproject = forcePathProjection;
      pendingTurns.clear();
      pendingActiveTurnId = undefined;
      forcePathProjection = false;
      callbacks.updateConversation?.(
        request.conversationId,
        (conversation) => integrateRuntimeNodeUpdates(conversation, turns, activeTurnId, reproject),
      );
    };

    const scheduleFrameFlush = () => {
      if (scheduledFrame !== undefined) return;
      if (typeof globalThis.requestAnimationFrame === "function") {
        scheduledWithAnimationFrame = true;
        scheduledFrame = globalThis.requestAnimationFrame(() => {
          scheduledFrame = undefined;
          scheduledWithAnimationFrame = false;
          flushPendingFrames();
        });
      } else {
        scheduledFrame = window.setTimeout(() => {
          scheduledFrame = undefined;
          flushPendingFrames();
        }, 0);
      }
    };

    const onMessage = (message: StreamMessage) => {
      if (callbacks.activeRuns.get(request.conversationId)?.controller !== controller) return;
      admissionAccepted = true;
      let turn: RuntimeStateNode;
      try {
        if (message.type === "turn.snapshot") {
          // Every in-memory reconnect begins with a fresh SQLite
          // authority snapshot. Rebase this Turn before applying subsequent
          // connection-local revisions; other continuation Turns keep their
          // own accumulators.
          const key = `${message.turn.session_id}:${message.turn.id}`;
          accumulator.nodes.delete(key);
          accumulator.revisions.delete(key);
        }
        turn = applyRuntimeNodeFrame(accumulator, message);
      } catch (error) {
        throw new SseProtocolError(String((error as Error).message ?? error));
      }
      const key = `${turn.session_id}:${turn.id}`;
      finalTurn = turn;
      active.turnId = turn.id;
      if (message.type === "turn.snapshot") request.onBaseline?.(turn);
      pendingTurns.set(key, turn);
      pendingActiveTurnId = turn.id;
      forcePathProjection ||= message.type === "turn.snapshot"
        || message.patch?.current_data_idx !== undefined
        || message.operations?.some((operation) => operation.op === "append_message") === true;
      scheduleFrameFlush();
      if (turn.status !== "running") {
        // Publish the Turn state now; the same SSE may still carry continuation Turns.
        flushPendingFrames();
      }
    };

    const options = {
      sessionId: request.sessionId,
      threadId: request.threadId ?? request.sessionId,
      sourceNodeId: request.sourceNodeId,
      mode: request.mode,
      permissionMode: request.permissionMode,
      fullAccessAcknowledged: request.permissionMode === "full_access",
      reasoningEffort: request.reasoningEffort,
      providerName: request.providerName,
      model: request.model,
      references: request.references,
      queuedDelivery: request.queuedDelivery,
      onAccepted: (turn?: RuntimeStateNode) => {
        admissionAccepted = true;
        if (!turn) {
          request.onAccepted?.();
          return;
        }
        active.turnId = turn.id;
        finalTurn = turn;
        callbacks.updateConversation?.(
          request.conversationId,
          (conversation) => integrateRuntimeNodeUpdates(conversation, [turn], turn.id, true),
        );
        request.onAccepted?.(turn);
      },
    } as const;

    try {
      const result = request.attach
        ? await streamAttachedTurn(
          request.turnId ?? request.sourceNodeId ?? "",
          onMessage,
          controller.signal,
          request.sessionId,
        )
        : request.resume
          ? await streamResume(
            request.sessionId,
            onMessage,
            controller.signal,
            request.permissionMode,
            request.reasoningEffort,
            request.sourceNodeId,
            request.providerName,
            request.model,
            request.mode,
            request.permissionMode === "full_access",
          )
          : request.rewindTurnId
            ? await streamRewind(request.rewindTurnId, request.prompt ?? "", onMessage, controller.signal, options)
            : await streamChat(request.prompt ?? "", onMessage, controller.signal, options);
      flushPendingFrames();
      if (result === "aborted") return;
      if (result === "silent_failed") {
        const recoveryTurnId = active.turnId ?? request.turnId ?? request.sourceNodeId;
        await callbacks.recoverConversation(request.conversationId, request.sessionId, recoveryTurnId).catch(() => undefined);
        callbacks.updateLastMessage(request.conversationId, (item) => ({
          ...item,
          error: undefined,
          status: "failed",
          running: false,
          decision: undefined,
        }));
        return;
      }
      if (request.attach && finalTurn?.status === "running") {
        await callbacks.recoverConversation(
          request.conversationId,
          request.sessionId,
          finalTurn.id,
        ).catch(() => undefined);
        return;
      }
      if (finalTurn) {
        const projection = projectRuntimeNode(finalTurn);
        callbacks.updateLastMessage(request.conversationId, (item) => ({
          ...item,
          content: projection.content || item.content,
          error: projection.errorSuppressed ? undefined : projection.error,
          status: finalTurn?.status,
          running: false,
          decision: undefined,
        }));
      }
    } catch (error) {
      flushPendingFrames();
      if (!admissionAccepted) request.onAdmissionRejected?.();
      if (request.rewindTurnId && !admissionAccepted && !finalTurn) {
        if (!controller.signal.aborted) {
          callbacks.onControlError?.(String((error as Error).message ?? error));
        }
        return;
      }
      if (!finalTurn && !request.attach && !controller.signal.aborted) {
        await callbacks.checkSandboxHealth?.().catch(() => undefined);
      }
      if (!admissionAccepted && !request.attach && !request.resume && !request.rewindTurnId) {
        throw error;
      }
      const protocolError = error instanceof SseProtocolError;
      if (error instanceof SseExecutionError) {
        if (finalTurn) {
          const stopped: RuntimeStateNode = {
            ...finalTurn, status: "failed",
            data: finalTurn.data.map((version) => version.map((message) => ({
              ...message, content: message.content.map((item) => item.status === "running" ? { ...item, status: "failed" as const } : item),
            }))),
          };
          callbacks.updateConversation?.(request.conversationId, (conversation) =>
            integrateRuntimeNodeUpdates(conversation, [stopped], stopped.id, true));
        }
        callbacks.updateLastMessage(request.conversationId, (item) => ({
          ...item, error: error.message, error_report: reportFromError(error), status: "failed", running: false, decision: undefined,
          items: item.items?.map((entry) => entry.status === "running" ? { ...entry, status: "failed" } : entry),
        }));
        return;
      }
      if (protocolError) {
        controller.abort();
        const recoveryTurnId = active.turnId ?? request.turnId ?? request.sourceNodeId;
        if (recoveryTurnId && !request.attach) {
          await pauseTurn(recoveryTurnId, request.sessionId).catch(() => undefined);
        }
        await callbacks.recoverConversation(request.conversationId, request.sessionId, recoveryTurnId).catch(() => undefined);
      }
      if (request.attach) {
        if (!protocolError) {
          const recoveryTurnId = active.turnId ?? request.turnId ?? request.sourceNodeId;
          await callbacks.recoverConversation(request.conversationId, request.sessionId, recoveryTurnId).catch(() => undefined);
        }
        return;
      }
      if (protocolError || !controller.signal.aborted) {
        callbacks.updateLastMessage(request.conversationId, (item) => ({
          ...item,
          error: String((error as Error).message ?? error),
          error_report: reportFromError(error),
          running: protocolError ? false : finalTurn?.status === "running",
          decision: undefined,
        }));
      }
    } finally {
      cancelScheduledFrame();
      canReplaceSubscription.delete(active);
      if (callbacks.activeRuns.get(request.conversationId) === active) {
        callbacks.activeRuns.delete(request.conversationId);
      }
      releaseSettled();
      const reportRefreshError = (error: unknown) => {
        callbacks.onControlError?.(String((error as Error).message ?? error));
      };
      void Promise.resolve().then(() => callbacks.refreshSessions()).catch(reportRefreshError);
      void Promise.resolve().then(() => callbacks.refreshQueuedMessages?.(request.conversationId)).catch(reportRefreshError);
    }
  }

  function stopConversation(turn: Pick<RuntimeStateNode, "id" | "session_id" | "status">): void {
    if (turn.status !== "running") return;
    const key = `${turn.session_id}:${turn.id}`;
    if (pendingPauses.has(key)) return;
    pendingPauses.add(key);
    void pauseTurn(turn.id, turn.session_id).catch((error) => {
      callbacks.onControlError?.(`暂停失败，请重试：${String((error as Error).message ?? error)}`);
    }).finally(() => pendingPauses.delete(key));
  }

  return { runConversation, stopConversation };
}
