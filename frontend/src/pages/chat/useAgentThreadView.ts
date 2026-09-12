import type { TurnPage } from "../../api/conversations/turns";
import { reportFromError } from "../../api/errorReport";
import { useContext, useEffect, useMemo, useRef, useState } from "react";
import { UNSAFE_LocationContext, UNSAFE_NavigationContext } from "react-router-dom";
import { ApiError, requestJson } from "../../api/transport/request";
import { chatPath, parseChatPath } from "../../app/conversationNavigation";
import { getTurnPage, sendAgentThreadMessage, streamAgentThread } from "../../api";
import { withLoadedTurns } from "../../app/conversationProjection";
import { applyRuntimeNodeFrame, runtimeNodeAccumulator } from "../../app/runtime/runtimeNodeReducer";
import { isRuntimeTurnNode } from "../../app/runtime/runtimeNodeNormalization";
import type {
  ChatMessage,
  ChatMode,
  Conversation,
  FileReference,
  PermissionMode,
  RuntimeConfigModel,
  RuntimeTreeNode,
} from "../../types";

interface UseAgentThreadViewOptions {
  canonical: Conversation | null;
  enabled: boolean;
  retainedConversationIds?: string[];
  onUpdate: (id: string, updater: (conversation: Conversation) => Conversation) => void;
}

function mergeNodes(current: RuntimeTreeNode[] | undefined, updates: RuntimeTreeNode[]): RuntimeTreeNode[] {
  const nodes = new Map((current ?? []).map((node) => [`${node.session_id}:${node.id}`, node] as const));
  for (const node of updates) nodes.set(`${node.session_id}:${node.id}`, node);
  return [...nodes.values()];
}

export function useAgentThreadView({ canonical, enabled, onUpdate, retainedConversationIds }: UseAgentThreadViewOptions) {
  const router = useChatRoute();
  const [historyByThread, setHistoryByThread] = useState<Record<string, Pick<Conversation, "activeTurnId" | "historyCursor" | "historyHasMore">>>({});
  const [selectedByRootThread, setSelectedByRootThread] = useState<Record<string, string>>({});
  const [pendingByThread, setPendingByThread] = useState<Record<string, ChatMessage[]>>({});
  const [treeInvalidation, setTreeInvalidation] = useState(0);
  const [streamError, setStreamError] = useState<Error | string | null>(null);
  const [validTargets, setValidTargets] = useState<Set<string>>(() => new Set());
  const updateRef = useRef(onUpdate);
  const observers = useRef(new Map<string, { controller: AbortController; owner: string; restart: () => void }>());
  const rootTreeStateByThread = useRef<Record<string, string>>({});
  updateRef.current = onUpdate;
  useEffect(() => () => {
    for (const observer of observers.current.values()) observer.controller.abort();
    observers.current.clear();
  }, []);
  useEffect(() => {
    const restart = () => {
      for (const observer of [...observers.current.values()]) {
        observer.controller.abort(); observer.restart();
      }
    };
    window.addEventListener("praxis-sync-reset", restart);
    return () => window.removeEventListener("praxis-sync-reset", restart);
  }, []);
  useEffect(() => {
    if (!retainedConversationIds) return;
    const retained = new Set(retainedConversationIds);
    if (canonical?.id) retained.add(canonical.id);
    for (const [key, observer] of observers.current) if (!retained.has(observer.owner)) {
      observer.controller.abort(); observers.current.delete(key);
    }
  }, [retainedConversationIds]);

  const sessionId = canonical?.sessionId;
  const rootThreadId = canonical?.threadId;
  const selectedThreadId = rootThreadId
    ? (enabled && router?.route?.threadId === rootThreadId ? router.route.agentId ?? rootThreadId : selectedByRootThread[rootThreadId] ?? rootThreadId)
    : canonical?.threadId;
  const isSubagent = Boolean(enabled && sessionId && selectedThreadId && selectedThreadId !== rootThreadId);
  const targetKey = `${sessionId}:${rootThreadId}:${selectedThreadId}`;
  useEffect(() => { setStreamError(null); }, [targetKey]);
  const rootTurnState = (canonical?.runtimeNodes ?? [])
    .filter(isRuntimeTurnNode)
    .filter((node) => node.thread_id === rootThreadId)
    .map((node) => `${node.id}:${node.status}`)
    .join("|");
  const lastCanonicalMessage = canonical?.messages[canonical.messages.length - 1];
  const rootTreeState = `${rootTurnState}#${canonical?.messages.length ?? 0}:${lastCanonicalMessage?.running ? "running" : "idle"}`;

  useEffect(() => {
    if (!rootThreadId) return;
    const previous = rootTreeStateByThread.current[rootThreadId];
    rootTreeStateByThread.current[rootThreadId] = rootTreeState;
    if (previous !== undefined && previous !== rootTreeState) {
      setTreeInvalidation((current) => current + 1);
    }
  }, [rootThreadId, rootTreeState]);

  const viewConversation = useMemo(() => {
    if (!canonical || !isSubagent || !selectedThreadId) return canonical;
    if (!validTargets.has(targetKey)) return null;
    const history = historyByThread[`${sessionId}:${selectedThreadId}`];
    if (!history) return null;
    const turns = (canonical.runtimeNodes ?? []).filter((node) => node.thread_id === selectedThreadId);
    const projected = withLoadedTurns(
      { ...canonical, ...history, threadId: selectedThreadId, hiddenBeforeTurnId: undefined, messages: [] },
      turns,
      history.activeTurnId ?? null,
    );
    return { ...projected, runtimeNodes: canonical.runtimeNodes };
  }, [canonical, isSubagent, selectedThreadId, sessionId, historyByThread, validTargets, targetKey]);

  const viewRef = useRef(viewConversation);
  viewRef.current = viewConversation;

  const pendingKey = sessionId && selectedThreadId ? `${sessionId}:${selectedThreadId}` : "";
  const canonicalDeliveryIds = useMemo(
    () => new Set((viewConversation?.messages ?? []).flatMap((item) => item.deliveryId ? [item.deliveryId] : [])),
    [viewConversation?.messages],
  );
  const pendingMessages = (pendingByThread[pendingKey] ?? []).filter(
    (item) => !item.deliveryId || !canonicalDeliveryIds.has(item.deliveryId),
  );
  const displayConversation = viewConversation && pendingMessages.length > 0
    ? { ...viewConversation, messages: [...viewConversation.messages, ...pendingMessages] }
    : viewConversation;

  useEffect(() => {
    if (!pendingKey || canonicalDeliveryIds.size === 0) return;
    setPendingByThread((current) => {
      const existing = current[pendingKey];
      if (!existing?.some((item) => item.deliveryId && canonicalDeliveryIds.has(item.deliveryId))) return current;
      return {
        ...current,
        [pendingKey]: existing.filter((item) => !item.deliveryId || !canonicalDeliveryIds.has(item.deliveryId)),
      };
    });
  }, [canonicalDeliveryIds, pendingKey]);

  useEffect(() => {
    if (!enabled || !isSubagent || !canonical?.id || !sessionId || !selectedThreadId) return;
    const observerKey = `${sessionId}:${selectedThreadId}`;
    if (observers.current.has(observerKey)) return;
    const conversationId = canonical.id;
    const startObserver = () => {
    const controller = new AbortController();
    observers.current.set(observerKey, { controller, owner: conversationId, restart: startObserver });
    let retryMs = 500;
    let lastEventId = "";
    let streamRevision = 0;

    const commitNodes = (nodes: RuntimeTreeNode[]) => {
      updateRef.current(conversationId, (current) => ({
        ...current,
        runtimeNodes: mergeNodes(current.runtimeNodes, nodes),
      }));
    };

    const reload = async () => {
      const revision = streamRevision;
      const page = await getTurnPage(sessionId, selectedThreadId);
      if (controller.signal.aborted || revision !== streamRevision) return;
      updateRef.current(conversationId, (current) => ({ ...current,
        runtimeNodes: mergeNodes((current.runtimeNodes ?? []).filter((node) => node.thread_id !== selectedThreadId), page.turns),
      }));
      setHistoryByThread((current) => ({ ...current, [observerKey]: {
        activeTurnId: page.current_turn_id ?? undefined, historyCursor: page.next_cursor, historyHasMore: page.has_more,
      } }));
    };

    const run = async () => {
      let validated = false;
      while (!controller.signal.aborted) {
        const accumulator = runtimeNodeAccumulator();
        try {
          if (!validated) {
            await requestJson(`/api/conversation-target/${encodeURIComponent(sessionId)}/${encodeURIComponent(rootThreadId!)}/${encodeURIComponent(selectedThreadId)}`);
            if (controller.signal.aborted) return;
            validated = true;
            setValidTargets((current) => new Set([...current, targetKey]));
          }
          await reload();
          const result = await streamAgentThread(sessionId, selectedThreadId, (event) => {
            if (event.type === "thread.ready") {
              setStreamError(null);
              retryMs = 500;
              return;
            }
            if (event.type === "turn.terminal") {
              setTreeInvalidation((current) => current + 1);
              void reload().catch((error) => { if (!controller.signal.aborted) setStreamError(error); });
              return;
            }
            if (event.type === "turn.snapshot") {
              const key = `${event.turn.session_id}:${event.turn.id}`;
              accumulator.nodes.delete(key);
              accumulator.revisions.delete(key);
            }
            const turn = applyRuntimeNodeFrame(accumulator, event);
            streamRevision += 1;
            commitNodes([turn]);
            if (turn.thread_id === selectedThreadId && turn.id === event.current_turn_id) setHistoryByThread((current) => ({ ...current,
              [observerKey]: { ...current[observerKey], activeTurnId: event.current_turn_id ?? undefined },
            }));
          }, controller.signal, lastEventId, (cursor) => {
            lastEventId = cursor;
          });
          if (result === "aborted" || result === "evicted") return;
          throw new Error("Agent Thread SSE ended unexpectedly.");
        } catch (error) {
          if (controller.signal.aborted) return;
          setStreamError(error instanceof Error ? error : String(error));
          if (error instanceof ApiError && (error.status === 404 || error.status === 409)) return;
          await new Promise<void>((resolve) => globalThis.setTimeout(resolve, retryMs));
          retryMs = Math.min(5_000, retryMs * 2);
        }
      }
    };
    void run();
    };
    startObserver();
    if (!retainedConversationIds) return () => {
      observers.current.get(observerKey)?.controller.abort(); observers.current.delete(observerKey);
    };
  }, [canonical?.id, enabled, isSubagent, selectedThreadId, sessionId]);

  function applyHistoryPage(page: TurnPage, append = true) {
    if (!canonical || !selectedThreadId || viewRef.current?.id !== canonical.id
      || viewRef.current?.threadId !== selectedThreadId
      || viewRef.current?.activeTurnId !== viewConversation?.activeTurnId
      || viewRef.current?.historyCursor !== viewConversation?.historyCursor) return;
    updateRef.current(canonical.id, (current) => current.messagesLoaded === false ? current : {
      ...current, runtimeNodes: append ? mergeNodes(page.turns, current.runtimeNodes ?? []) : mergeNodes(current.runtimeNodes, page.turns),
    });
    setHistoryByThread((current) => ({ ...current, [`${sessionId}:${selectedThreadId}`]: { activeTurnId: append ? viewRef.current?.activeTurnId : page.current_turn_id ?? undefined, historyCursor: page.next_cursor, historyHasMore: page.has_more } }));
  }

  function selectThread(threadId: string) {
    if (!rootThreadId) return;
    if (enabled && router && sessionId) {
      router.navigate(chatPath(sessionId, rootThreadId, threadId));
      return;
    }
    setSelectedByRootThread((current) => ({ ...current, [rootThreadId]: threadId }));
  }

  async function sendMessage(values: {
    content: string;
    references?: FileReference[];
    mode: ChatMode;
    permissionMode: PermissionMode;
    providerName?: string;
    model?: RuntimeConfigModel;
  }, retry?: ChatMessage) {
    if (!sessionId || !selectedThreadId || !isSubagent) throw new Error("当前没有选中的 Subagent Thread。");
    const message: ChatMessage = {
      id: retry?.id ?? `pending:${crypto.randomUUID()}`,
      role: "user",
      content: values.content,
      events: [],
      references: values.references,
      pending: true,
      timelineSource: "steering",
    };
    setPendingByThread((current) => ({
      ...current,
      [pendingKey]: [...(current[pendingKey] ?? []).filter((item) => item.id !== message.id), message],
    }));
    try {
      const response = await sendAgentThreadMessage(selectedThreadId, {
        sessionId,
        ...values,
        fullAccessAcknowledged: values.permissionMode === "full_access",
      });
      setPendingByThread((current) => ({
        ...current,
        [pendingKey]: (current[pendingKey] ?? []).map((item) => item.id === message.id ? { ...item, deliveryId: response.delivery_id } : item),
      }));
      return response;
    } catch (error) {
      setPendingByThread((current) => ({
        ...current,
        [pendingKey]: (current[pendingKey] ?? []).map((item) => item.id === message.id
          ? { ...item, pending: false, error: String((error as Error).message ?? error), error_report: reportFromError(error) } : item),
      }));
      throw error;
    }
  }

  return {
    conversation: displayConversation,
    rootThreadId,
    selectedThreadId,
    isSubagent,
    treeInvalidation,
    streamError,
    selectThread,
    applyHistoryPage,
    sendMessage,
  };
}

function useChatRoute() {
  const location = useContext(UNSAFE_LocationContext);
  const navigation = useContext(UNSAFE_NavigationContext);
  const route = location ? parseChatPath(location.location.pathname) : null;
  const lastChatRoute = useRef(route);
  // Hidden chat content must keep its thread until another chat URL is selected.
  if (location && !["/benchmark", "/trash"].includes(location.location.pathname)) lastChatRoute.current = route;
  return location && navigation ? { route: lastChatRoute.current,
    navigate: (path: string) => navigation.navigator.push(path) } : null;
}
