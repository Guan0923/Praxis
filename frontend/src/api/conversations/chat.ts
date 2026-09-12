import { readErrorReport, type ErrorReport } from "../errorReport";
import { apiErrorFrom } from "../transport/request";
import type { ChatMode, FileReference, PermissionMode, ReasoningEffort, RuntimeConfigModel, RuntimeStateNode, StreamMessage } from "../../types";
import { apiUrl } from "../transport/base";
import { ApiError, jsonBody, requestJson } from "../transport/request";

export interface StreamOptions {
  sessionId: string;
  threadId?: string;
  sourceNodeId?: string;
  mode?: ChatMode;
  permissionMode?: PermissionMode;
  fullAccessAcknowledged?: boolean;
  reasoningEffort?: ReasoningEffort;
  providerName?: string;
  model?: RuntimeConfigModel;
  references?: FileReference[];
  queuedDelivery?: { messageIds: string[] };
  onAccepted?: (turn: RuntimeStateNode) => void;
}

const terminalPattern = /^<SSE id="([^"]+)" type="(success|network|failed)">([\s\S]*)<\/SSE>$/;
const MAX_STREAM_RECONNECTS = 12;
type StreamResult = "completed" | "aborted" | "silent_failed";

export class SseProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SseProtocolError";
  }
}

export class SseExecutionError extends Error {
  constructor(message: string, public readonly error_report?: ErrorReport) {
    super(message);
    this.name = "SseExecutionError";
  }
}

function executionConfig(options: StreamOptions): Record<string, unknown> {
  return {
    permission_mode: options.permissionMode ?? "read_only",
    full_access_acknowledged: Boolean(options.fullAccessAcknowledged),
    running_mode: options.mode ?? "agent",
    provider_name: options.providerName,
    model: options.model,
  };
}

async function streamEndpoint(
  url: string,
  body: Record<string, unknown> | undefined,
  expectedTurnId: string,
  onMessage: (message: StreamMessage) => void,
  signal: AbortSignal,
): Promise<StreamResult> {
  let lastEventId = "";
  let reconnects = 0;
  let latestStatus: string | undefined;
  let failureReport: ErrorReport | undefined;

  const waitToReconnect = async (): Promise<boolean> => {
    if (signal.aborted) return false;
    if (reconnects >= MAX_STREAM_RECONNECTS) return false;
    const delay = Math.min(2_000, 250 * (2 ** Math.min(reconnects, 3)));
    reconnects += 1;
    return new Promise<boolean>((resolve) => {
      const onAbort = () => {
        globalThis.clearTimeout(timer);
        resolve(false);
      };
      const timer = globalThis.setTimeout(() => {
        signal.removeEventListener("abort", onAbort);
        resolve(true);
      }, delay);
      signal.addEventListener("abort", onAbort, { once: true });
    });
  };

  for (;;) {
    let response: Response;
    try {
      response = await fetch(apiUrl(url), {
        method: body ? "POST" : "GET",
        cache: "no-store",
        headers: {
          ...(body ? { "Content-Type": "application/json" } : {}),
          ...(lastEventId ? { "Last-Event-ID": lastEventId } : {}),
        },
        ...(body ? { body: JSON.stringify(body) } : {}),
        signal,
      });
    } catch (error) {
      if ((error as Error).name === "AbortError" || signal.aborted) return "aborted";
      if (await waitToReconnect()) continue;
      throw error;
    }
    if (!response.ok || !response.body) {
      if (response.status === 503 && await waitToReconnect()) continue;
      throw await apiErrorFrom(response);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let terminal: RegExpMatchArray | null = null;
    let receivedFrame = false;
    try {
      for (;;) {
        const next = await reader.read();
        if (next.done) {
          buffer += decoder.decode().replace(/\r\n/g, "\n");
        } else {
          buffer += decoder.decode(next.value, { stream: true }).replace(/\r\n/g, "\n");
        }
        let boundary: number;
        while ((boundary = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          let blockEventId = "";
          const dataLines: string[] = [];
          for (const line of block.split("\n")) {
            if (line.startsWith("id: ")) blockEventId = line.slice(4);
            else if (line.startsWith("data: ")) dataLines.push(line.slice(6));
          }
          if (dataLines.length === 0) continue;
          const payload = dataLines.join("\n");
          const matched = payload.match(terminalPattern);
          if (matched) {
            if (terminal) throw new SseProtocolError("SSE stream contains more than one terminal envelope");
            terminal = matched;
          } else {
            if (terminal) throw new SseProtocolError("SSE frame arrived after the terminal envelope");
            let frame: StreamMessage;
            try {
              frame = JSON.parse(payload) as StreamMessage;
            } catch (error) {
              throw new SseProtocolError(`Invalid SSE JSON: ${String((error as Error).message ?? error)}`);
            }
            if ((frame as { type: string }).type === "turn.error") {
              failureReport = readErrorReport((frame as unknown as { error_report?: unknown }).error_report);
              continue;
            }
            if (frame.type !== "turn.snapshot" && frame.type !== "turn.delta") {
              throw new SseProtocolError(`Unsupported SSE frame: ${String((frame as { type?: unknown }).type)}`);
            }
            if (!receivedFrame) {
              if (frame.type !== "turn.snapshot") throw new SseProtocolError("SSE stream must begin with a Turn snapshot");
              if (frame.turn.id !== expectedTurnId) {
                throw new SseProtocolError("SSE baseline id does not match the requested Turn");
              }
            }
            receivedFrame = true;
            latestStatus = frame.type === "turn.snapshot" ? frame.turn.status : frame.patch?.status ?? latestStatus;
            onMessage(frame);
          }
          if (blockEventId) lastEventId = blockEventId;
        }
        if (next.done) break;
      }
    } catch (error) {
      if ((error as Error).name === "AbortError" || signal.aborted) return "aborted";
      if (error instanceof SseProtocolError) throw error;
      if (await waitToReconnect()) continue;
      throw error;
    } finally {
      reader.releaseLock();
    }
    if (signal.aborted) return "aborted";
    if (!terminal) {
      if (lastEventId && await waitToReconnect()) continue;
      throw new SseProtocolError("SSE stream unexpectedly ended before completion");
    }
    if (terminal[1] !== expectedTurnId) {
      throw new SseProtocolError("SSE terminal id does not match the active Turn");
    }
    if (!receivedFrame) {
      if (terminal[2] === "failed") {
        if (terminal[3]) throw new ApiError(500, terminal[3], undefined, failureReport);
        return "silent_failed";
      }
      throw new SseProtocolError("SSE stream completed without a Turn baseline");
    }
    if (terminal[2] === "network") throw new Error("network");
    if (terminal[2] === "failed" && latestStatus === "running") {
      throw new SseExecutionError(terminal[3] || "Execution stopped before its final state could be saved.", failureReport);
    }
    return "completed";
  }
}

export async function streamChat(
  prompt: string,
  onMessage: (message: StreamMessage) => void,
  signal: AbortSignal,
  options: StreamOptions,
): Promise<StreamResult> {
  const body = {
      session_id: options.sessionId,
      thread_id: options.threadId ?? options.sessionId,
      parent_id: options.sourceNodeId ?? "",
      ...(options.queuedDelivery
        ? { queued_delivery: { message_ids: options.queuedDelivery.messageIds } }
        : {
          message: { role: "user", content: [{ type: "text", text: prompt, ...(options.references?.length ? { references: options.references } : {}) }] },
        }),
      ...executionConfig(options),
    };
  let turn: RuntimeStateNode;
  try {
    turn = await requestJson<RuntimeStateNode>(
      "/api/turns",
      { ...jsonBody(body), signal },
    );
    options.onAccepted?.(turn);
  } catch (error) {
    if ((error as Error).name === "AbortError" || signal.aborted) return "aborted";
    throw error;
  }
  return streamEndpoint(
    `/api/turns/${encodeURIComponent(turn.id)}/stream?session_id=${encodeURIComponent(options.sessionId)}&thread_id=${encodeURIComponent(options.threadId ?? options.sessionId)}`,
    undefined,
    turn.id,
    onMessage,
    signal,
  );
}

export async function streamRewind(
  turnId: string,
  prompt: string,
  onMessage: (message: StreamMessage) => void,
  signal: AbortSignal,
  options: StreamOptions,
): Promise<StreamResult> {
  const receipt = await requestJson<{ turn_id: string; delivery_id: string; status: "accepted" }>(
    `/api/turns/${encodeURIComponent(turnId)}/rewind`,
    { ...jsonBody({
      message: { role: "user", content: [{ type: "text", text: prompt, ...(options.references?.length ? { references: options.references } : {}) }] },
      ...executionConfig(options),
    }), signal, operation: { sessionId: options.sessionId } },
  );
  return streamEndpoint(
    `/api/turns/${encodeURIComponent(turnId)}/stream?session_id=${encodeURIComponent(options.sessionId)}&delivery_id=${encodeURIComponent(receipt.delivery_id)}`,
    undefined,
    turnId,
    onMessage,
    signal,
  );
}

export async function streamResume(
  sessionId: string,
  onMessage: (message: StreamMessage) => void,
  signal: AbortSignal,
  permissionMode: PermissionMode,
  _reasoningEffort: ReasoningEffort = "medium",
  sourceNodeId?: string,
  providerName?: string,
  model?: RuntimeConfigModel,
  mode: ChatMode = "agent",
  fullAccessAcknowledged = false,
): Promise<StreamResult> {
  if (!sourceNodeId) throw new Error("resume requires a Turn id");
  await requestJson<{ turn_id: string; status: "accepted" }>(
    `/api/turns/${encodeURIComponent(sourceNodeId)}/resume`,
    { ...jsonBody({
      permission_mode: permissionMode,
      full_access_acknowledged: fullAccessAcknowledged,
      running_mode: mode,
      provider_name: providerName,
      ...(model ? { model } : {}),
    }), signal, operation: { sessionId } },
  );
  return streamEndpoint(
    `/api/turns/${encodeURIComponent(sourceNodeId)}/stream?session_id=${encodeURIComponent(sessionId)}`,
    undefined,
    sourceNodeId,
    onMessage,
    signal,
  );
}

export async function streamAttachedTurn(
  turnId: string,
  onMessage: (message: StreamMessage) => void,
  signal: AbortSignal,
  sessionId: string,
): Promise<StreamResult> {
  return streamEndpoint(
    `/api/turns/${encodeURIComponent(turnId)}/stream?session_id=${encodeURIComponent(sessionId)}`,
    undefined,
    turnId,
    onMessage,
    signal,
  );
}

export async function pauseTurn(turnId: string, sessionId?: string): Promise<void> {
  await requestJson(`/api/turns/${encodeURIComponent(turnId)}/pause`, {
    method: "POST",
    operation: { sessionId, group: `turn-control:${turnId}` },
  });
}

export async function steerTurn(
  turnId: string,
  deliveryId: string,
  messageIds: string[],
  sessionId?: string,
): Promise<void> {
  await requestJson(`/api/turns/${encodeURIComponent(turnId)}/steer`, {
    ...jsonBody({
      delivery_id: deliveryId,
      message_ids: messageIds,
    }),
    operation: { sessionId, group: `turn-control:${turnId}` },
  });
}
