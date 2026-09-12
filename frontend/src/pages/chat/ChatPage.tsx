import { ApiError } from "../../api/transport/request";
import { ViewStateContext, useViewState, viewKey } from "../../app/viewState";
import { getTurnPage } from "../../api/conversations/turns";
import { withTurnPage } from "../../app/conversationProjection";
import { ErrorDisplay } from "../../components/ErrorDisplay";
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { App as AntApp, FloatButton } from "antd";
import { VerticalAlignBottomOutlined } from "@ant-design/icons";
import { bindSessionOperationResources, sessionFileContentUrl, submitDecision } from "../../api";
import { parseCommand } from "../../commands";
import {
  commandKeyAction,
  commandSuggestions,
  commandTrigger,
  nextCommandIndex,
  type CommandTrigger,
} from "../../commands/completion";
import { fileKeyAction } from "../../commands/fileCompletion";
import Composer from "./Composer";
import { latestTodoList } from "./todoPanel";
import { messagesBeforeRewind, projectTurnPath, pruneTurnDescendants } from "../../app/runtime/runtimeDetailProjection";
import { isRuntimeTurnNode } from "../../app/runtime/runtimeNodeNormalization";
import type {
  ChatMessage,
  ChatMode,
  Conversation,
  DecisionRequest,
  FileReference,
  RuntimeStateNode,
} from "../../types";
import { ChatMessageList } from "./ChatMessageList";
import ConversationTimeline from "./ConversationTimeline";
import TracePage from "./TracePage";
import { composerAction, type ChatPageProps } from "./contracts";
import { useComposerFiles } from "./useComposerFiles";
import { useMessageEditing } from "./useMessageEditing";
import { useRuntimeControls } from "./useRuntimeControls";
import { useAgentThreadView } from "./useAgentThreadView";
import { ChatToolbar, type ChatMainView } from "./ChatToolbar";
import { useChatCommands } from "./useChatCommands";
import { useChatScroll } from "./useChatScroll";
import { useQueuedMessageFlow } from "./useQueuedMessageFlow";
import { useResponsiveChatLayout } from "./useResponsiveChatLayout";
import { useSessionOwnership } from "../../app/useSessionOwnership";
import { ButtonTooltipContext } from "../../components/ButtonTooltipContext";

export { composerAction } from "./contracts";
export { CHAT_COMPACT_WIDTH } from "./useResponsiveChatLayout";

export default function ChatPage({
  active = true,
  showButtonTooltips = true,
  conversation: canonicalConversation,
  agentThreadNavigation = false,
  retainedConversationIds,
  displayMode: configuredDisplayMode,
  providerConfig,
  mode: selectedMode,
  onModeChange = () => undefined,
  onUpdate,
  onNew,
  onNavigate,
  onEnsureSession = async (id) => canonicalConversation?.sessionId ?? id,
  onFork,
  onRewind,
  onSelectSession = async (id) => id,
  onReload = async () => undefined,
  onRefresh = async () => undefined,
  onRun,
  onStopRun,
  queuedMessages = [],
  onQueuedMessagesChange = () => undefined,
  onQueuedMessagesRefresh = async () => undefined,
  sandboxHealth = { phase: "healthy", detail: null },
}: ChatPageProps) {
  const { message } = AntApp.useApp();
  const sendPendingRef = useRef<object | null>(null);
  const [sendPending, setSendPending] = useState(false);
  const createdConversationRef = useRef<string | null>(null);
  useEffect(() => { sendPendingRef.current = null; setSendPending(false); }, [canonicalConversation?.id]);
  const agentThreadView = useAgentThreadView({
    canonical: canonicalConversation,
    enabled: agentThreadNavigation,
    retainedConversationIds,
    onUpdate,
  });
  const conversation = agentThreadView.conversation;
  const savedKey = viewKey(conversation?.sessionId, conversation?.threadId);
  const savedView = useViewState(savedKey);
  const ownership = useSessionOwnership(conversation?.sessionId);
  const sessionReadOnly = Boolean(conversation?.sessionId) && ownership !== "writable";
  useEffect(() => {
    if (!conversation?.sessionId) return;
    bindSessionOperationResources(
      conversation.sessionId,
      [conversation.threadId, ...(conversation.runtimeNodes ?? []).map((node) => node.thread_id)].filter((id): id is string => Boolean(id)),
      (conversation.runtimeNodes ?? []).map((node) => node.id),
    );
  }, [conversation?.sessionId, conversation?.threadId, conversation?.runtimeNodes]);
  const [agentModeByThread, setAgentModeByThread] = useState<Record<string, ChatMode>>({});
  const mode = agentThreadView.isSubagent && agentThreadView.selectedThreadId
    ? agentModeByThread[agentThreadView.selectedThreadId] ?? selectedMode ?? "agent"
    : selectedMode ?? "agent";
  const changeViewMode = useCallback((value: ChatMode) => {
    if (agentThreadView.isSubagent && agentThreadView.selectedThreadId) {
      setAgentModeByThread((current) => ({ ...current, [agentThreadView.selectedThreadId!]: value }));
      return;
    }
    onModeChange(value);
  }, [agentThreadView.isSubagent, agentThreadView.selectedThreadId, onModeChange]);
  const { chatPageRef, compact, isMobile } = useResponsiveChatLayout(active);
  const [compactionPending, setCompactionPending] = useState(false);
  const [activeCommandIndex, setActiveCommandIndex] = useState(0);
  const [commandTriggerState, setCommandTriggerState] = useState<CommandTrigger | null>(null);
  const [commandMenuDismissedFor, setCommandMenuDismissedFor] = useState<string | null>(null);
  const [dismissedTodoPanels, setDismissedTodoPanels] = useState<Set<string>>(() => new Set());
  const [mainView, setMainView] = useState<ChatMainView>("chat");

  const [unboundMessages, setUnboundMessages] = useState<Record<string, ChatMessage>>({});
  const unboundMessage = unboundMessages[conversation?.id ?? ""];
  const messages = unboundMessage
    ? [...(conversation?.messages ?? []), unboundMessage]
    : conversation?.messages ?? [];
  const { chatScrollRef, handleScroll: handleChatScroll, isAtBottom, scrollToBottom, scrollToPosition } = useChatScroll(
    savedKey || conversation?.id,
    messages,
    active,
    { key: savedKey, hasMore: conversation?.historyHasMore, loadEarlier },
  );
  const historyRequestRef = useRef<string | null>(null);
  async function loadEarlier() {
    if (!conversation?.sessionId || conversation.historyHasMore !== true || historyRequestRef.current) return;
    const id = conversation.id;
    const cursor = conversation.historyCursor;
    const head = conversation.activeTurnId;
    historyRequestRef.current = id;
    try {
      const page = await getTurnPage(conversation.sessionId, conversation.threadId, conversation.historyCursor ?? undefined);
      if (agentThreadView.isSubagent) agentThreadView.applyHistoryPage(page);
      else onUpdate(id, (current) => current.messagesLoaded && current.historyCursor === cursor
        && current.activeTurnId === head ? withTurnPage(current, page, true) : current);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        const page = await getTurnPage(conversation.sessionId, conversation.threadId).catch(() => null);
        if (page && agentThreadView.isSubagent) agentThreadView.applyHistoryPage(page, false);
        else if (page) onUpdate(id, (current) => current.messagesLoaded && current.historyCursor === cursor
          && current.activeTurnId === head ? withTurnPage(current, page) : current);
      }
      const failure = error instanceof Error ? error : new Error(String(error));
      void message.error({ content: <ErrorDisplay error={failure} /> });
    } finally {
      historyRequestRef.current = null;
    }
  };
  const handleHistoryScroll = () => {
    handleChatScroll();
    if (chatScrollRef.current && chatScrollRef.current.scrollTop <= 24) void loadEarlier();
  };
  const activeRuntimeNode = conversation?.runtimeNodes?.find(
    (node): node is RuntimeStateNode => isRuntimeTurnNode(node)
      && node.id === conversation.activeTurnId
      && node.session_id === conversation.sessionId
      && node.thread_id === (conversation.threadId ?? conversation.sessionId),
  );
  const busy = !agentThreadView.isSubagent && activeRuntimeNode?.status === "running";
  const sandboxBlocked = sandboxHealth.phase !== "healthy";
  const interactionBusy = busy || compactionPending || sandboxBlocked;
  const projectUnavailable = conversation?.projectId !== undefined && conversation.projectAvailable === false;
  const completionDisabled = compactionPending || sandboxBlocked || projectUnavailable || Boolean(savedKey && !savedView.loaded);
  const composerFiles = useComposerFiles({
    conversationId: conversation?.id,
    sessionId: conversation?.sessionId,
    threadId: conversation?.threadId,
    completionDisabled,
    preserveDraft: (id) => {
      if (!id || createdConversationRef.current !== id) return false;
      createdConversationRef.current = null;
      return true;
    },
    onTextChanged: (change) => {
      setCommandTriggerState(commandTrigger(change.prompt, change.caret));
      setCommandMenuDismissedFor((dismissed) => dismissed === change.prompt ? dismissed : null);
      setActiveCommandIndex(0);
    },
  });
  const {
    input,
    setInput,
    references,
    setReferences,
    pendingUploads,
    setPendingUploads,
    fileCandidates,
    activeFileIndex,
    setActiveFileIndex,
    fileTriggerState,
    fileMenuAvailable,
    setFileMenuDismissedFor,
    editorRef,
    handleEditorChange,
    completeFile,
    handlePickFiles,
    removePendingUpload,
    retryUpload,
    collectedReferences,
    clearComposer,
  } = composerFiles;
  const filteredCommands = commandSuggestions(commandTriggerState?.query ?? "").filter(
    (command) => !agentThreadView.isSubagent || command.name !== "/compact",
  );
  const commandMenuVisible = !completionDisabled
    && !fileMenuAvailable
    && commandTriggerState !== null
    && commandMenuDismissedFor !== input
    && filteredCommands.length > 0;
  // The file menu is mutually exclusive with the slash-command menu and only
  // appears while the caret still sits inside an `@` trigger.
  const fileMenuVisible = fileMenuAvailable;
  const display = configuredDisplayMode ?? "medium";

  const todoTurnId = conversation?.activeTurnId ?? activeRuntimeNode?.id;
  const todo = useMemo(() => latestTodoList(messages, todoTurnId), [messages, todoTurnId]);
  const todoPanelKey = `${conversation?.id ?? "draft"}:${todoTurnId ?? "no-turn"}`;
  const todoCompleted = todo !== null && todo.length > 0 && todo.every((item) => item.status === "completed");
  const visibleTodo = todo?.length && !todoCompleted && !dismissedTodoPanels.has(todoPanelKey) ? todo : null;
  const todoClosable = Boolean(visibleTodo) && !busy;
  const closeTodoPanel = useCallback(() => {
    setDismissedTodoPanels((current) => {
      if (current.has(todoPanelKey)) return current;
      const next = new Set(current);
      next.add(todoPanelKey);
      return next;
    });
  }, [todoPanelKey]);
  const currentThreadId = activeRuntimeNode?.thread_id ?? conversation?.threadId ?? conversation?.sessionId;
  const traceTurns = (conversation?.runtimeNodes ?? []).filter(
    (node): node is RuntimeStateNode => isRuntimeTurnNode(node)
      && node.thread_id === currentThreadId
      && node.id !== conversation?.hiddenBeforeTurnId,
  );
  const hasTurnTree = traceTurns.length > 0;
  const visibleMainView = hasTurnTree ? mainView : "chat";
  useEffect(() => {
    if (!hasTurnTree) setMainView("chat");
  }, [conversation?.id, hasTurnTree]);
  const runtimeControls = useRuntimeControls({
    conversation,
    activeRuntimeNode,
    busy: busy || sandboxBlocked,
    providerConfig,
    mode,
    onModeChange: changeViewMode,
    onFailure: (error) => setLast({ error: String((error as Error).message ?? error) }),
  });
  const {
    permissionMode,
    reasoningEffort,
    runtimeConfigPending,
    openSettingsSelect,
    setOpenSettingsSelect,
    activeUsage,
    usagePercent,
    requestProviderName,
    requestModel,
    changeRunningMode,
    changePermissionMode,
    changeReasoningEffort,
  } = runtimeControls;
  const messageEditing = useMessageEditing({
    conversation,
    interactionBusy: interactionBusy || sessionReadOnly,
    activeRuntimeNode,
    onRewind: agentThreadView.isSubagent ? undefined : onRewind,
    onFork: agentThreadView.isSubagent ? undefined : onFork,
    onUpdate,
    runPrompt,
    onError: (error) => { void message.error({ content: <ErrorDisplay error={error} />, duration: 0 }); },
  });
  const {
    editingMessageId,
    editingDraft,
    setEditingDraft,
    rewindPending,
    editingSubmitting,
    editRef,
    beginEdit,
    cancelEdit,
    saveEdit,
    handleUserBubbleClick,
    forkMessage,
    changeMessageVersion,
    messageVersion,
  } = messageEditing;
  const hasDraft = Boolean(input.trim() || references.length > 0 || pendingUploads.some((upload) => upload.status === "done"));
  const composerActionState = composerAction(
    agentThreadView.isSubagent ? undefined : activeRuntimeNode?.status,
    hasDraft,
    pendingUploads.some((upload) => upload.status === "uploading"),
    queuedMessages.some((item) => item.state === "pending" && !item.saving && !item.error),
  );
  const actionMode = agentThreadView.isSubagent ? "send" : composerActionState.mode;
  const chatCommands = useChatCommands({
    activeCommandIndex,
    filteredCommands,
    editorRef,
    setActiveCommandIndex,
    setCommandMenuDismissedFor,
    setCompactionPending,
    setMainView,
    compactionPending,
    isSubagent: agentThreadView.isSubagent,
    conversation,
    activeRuntimeNode,
    clearComposer,
    onInsert: insert,
    onReload,
    onInfo: (content) => void message.info(content),
  });
  const queuedMessageFlow = useQueuedMessageFlow({
    conversation,
    activeRuntimeNode,
    queuedMessages,
    disabled: sandboxBlocked || sessionReadOnly || agentThreadView.isSubagent,
    input,
    collectedReferences,
    clearComposer,
    editorRef,
    setInput,
    setReferences,
    setPendingUploads,
    onQueuedMessagesChange,
    onQueuedMessagesRefresh,
    onDispatch: ({ conversationId, sessionId, sourceNodeId, messageIds }) => new Promise<void>((resolve, reject) => {
      void dispatchRun(
        conversationId, sessionId, null, false, sourceNodeId, undefined, undefined,
        true, undefined, { messageIds }, () => resolve(),
      ).catch(reject);
    }),
    onWarning: (content) => void message.warning(content),
  });


  function updateLast(updater: (message: ChatMessage) => ChatMessage, conversationId = conversation?.id) {
    if (!conversationId) return;
    onUpdate(conversationId, (current) => {
      const currentMessages = [...current.messages];
      const index = currentMessages.length - 1;
      if (index < 0 || currentMessages[index].role !== "assistant") return current;
      currentMessages[index] = updater(currentMessages[index]);
      return { ...current, messages: currentMessages };
    });
  }

  function setLast(fields: Partial<ChatMessage>, conversationId?: string) {
    updateLast((message) => ({ ...message, ...fields }), conversationId);
  }

  function defaultSourceNodeId(): string | undefined {
    return conversation?.activeTurnId ?? conversation?.lastNodeId;
  }

  async function ensureSession(): Promise<{ conversationId: string; sessionId: string }> {
    if (!conversation) {
      const id = await onNew();
      createdConversationRef.current = id;
      return { conversationId: id, sessionId: id };
    }
    const conversationId = conversation.id;
    const sessionId = await onEnsureSession(conversationId);
    return { conversationId, sessionId };
  }

  async function insert(content: string) {
    const { conversationId } = await ensureSession();
    const message: ChatMessage = { id: crypto.randomUUID(), role: "assistant", content, events: [] };
    onUpdate(conversationId, (current) => ({ ...current, messages: [...current.messages, message] }));
  }

  async function dispatchRun(
    conversationId: string,
    sessionId: string,
    prompt: string | null,
    resume = false,
    sourceNodeId: string | null = defaultSourceNodeId() ?? null,
    references?: FileReference[],
    rewindTurnId?: string,
    waitForActiveRun = false,
    onBaseline?: (turn: RuntimeStateNode) => void,
    queuedDelivery?: { messageIds: string[] },
    onAccepted?: (turn?: RuntimeStateNode) => void,
    onAdmissionRejected?: () => void,
    turnId?: string,
  ) {
    if (sandboxBlocked) throw new Error("沙箱 Broker 尚未确认健康。");
    if (!onRun) throw new Error("ChatPage requires the Turn run controller.");
    await onRun({
      conversationId,
      sessionId,
      prompt,
      resume,
      mode,
      permissionMode,
      reasoningEffort,
      providerName: requestProviderName,
      model: requestModel,
      sourceNodeId: sourceNodeId ?? undefined,
      threadId: conversation?.threadId ?? sessionId,
      turnId,
      references,
      rewindTurnId,
      waitForActiveRun,
      onBaseline,
      queuedDelivery,
      onAccepted,
      onAdmissionRejected,
    });
  }

  async function runPrompt(
    prompt: string,
    target?: { conversationId: string; sessionId: string; sourceNodeId?: string; rewindTurnId?: string },
    references?: FileReference[],
    onAccepted?: () => void,
    retryMessage?: ChatMessage,
  ) {
    const sourceNodeId = target ? target.sourceNodeId ?? null : retryMessage?.sourceNodeId ?? defaultSourceNodeId() ?? null;
    if (!target?.rewindTurnId) {
      const { conversationId, sessionId } = target ?? await ensureSession();
      let released = false;
      const release = () => {
        if (released) return;
        released = true;
        onAccepted?.();
      };
      try {
        await dispatchRun(
          conversationId,
          sessionId,
          prompt,
          false,
          sourceNodeId,
          references,
          undefined,
          Boolean(retryMessage || (activeRuntimeNode && activeRuntimeNode.status !== "running")),
          undefined,
          undefined,
          (turn) => {
            release();
            return turn;
          },
          release,
        );
      } catch (error) {
        release();
        throw error;
      }
      return;
    }

    const userMessage: ChatMessage = { id: crypto.randomUUID(), role: "user", content: prompt, events: [], references, pending: true, sourceNodeId: sourceNodeId ?? undefined };
    const localKey = target?.conversationId ?? conversation?.id ?? "";
    setUnboundMessages((current) => ({ ...current, [localKey]: userMessage }));
    let resolved: { conversationId: string; sessionId: string };
    try {
      resolved = target ?? await ensureSession();
    } catch (error) {
      setUnboundMessages((current) => current[localKey] === userMessage
        ? { ...current, [localKey]: { ...userMessage, pending: false, error: String((error as Error).message ?? error) } }
        : current);
      throw error;
    }
    const { conversationId, sessionId } = resolved;
    setUnboundMessages((current) => {
      if (current[localKey] !== userMessage) return current;
      const next = { ...current };
      delete next[localKey];
      return next;
    });
    const assistantMessage: ChatMessage = { id: `${userMessage.id}:response`, role: "assistant", content: "", events: [], running: true };
    const showSubmittedMessages = () => onUpdate(conversationId, (current) => {
      let visibleMessages = current.messages.filter((item) => item.id !== userMessage.id && item.id !== assistantMessage.id);
      let runtimeNodes = current.runtimeNodes;
      if (target?.rewindTurnId) {
        if (current.runtimeNodes) {
          runtimeNodes = pruneTurnDescendants(current.runtimeNodes, target.rewindTurnId);
          const map = new Map(runtimeNodes.map((node) => [`${node.session_id}:${node.id}`, node] as const));
          visibleMessages = map.has(`${sessionId}:${target.rewindTurnId}`)
            ? messagesBeforeRewind(projectTurnPath(map, target.rewindTurnId, true), target.rewindTurnId)
            : messagesBeforeRewind(current.messages, target.rewindTurnId);
        } else {
          visibleMessages = messagesBeforeRewind(current.messages, target.rewindTurnId);
        }
      }
      const messages = [...visibleMessages, userMessage, assistantMessage];
      return {
        ...current,
        messageCount: messages.filter((message) => message.role === "user" || message.role === "assistant").length,
        updatedAt: new Date().toISOString(),
        messages,
        runtimeNodes,
        activeTurnId: target?.rewindTurnId ?? current.activeTurnId,
        lastNodeId: target?.rewindTurnId ?? current.lastNodeId,
      };
    });
    if (!target?.rewindTurnId) showSubmittedMessages();
    const reject = () => {
      onAccepted?.();
      if (target?.rewindTurnId) return;
      onUpdate(conversationId, (current) => ({
        ...current,
        messages: current.messages.map((item) => item.id === userMessage.id
          ? { ...item, pending: false, error: "发送失败" }
          : item.id === assistantMessage.id ? { ...item, running: false } : item),
      }));
    };
    try {
      await dispatchRun(
        conversationId,
        sessionId,
        prompt,
        false,
        sourceNodeId,
        references,
        target?.rewindTurnId,
        Boolean(retryMessage || (activeRuntimeNode && activeRuntimeNode.status !== "running")),
        undefined,
        undefined,
        (turn) => {
          if (target?.rewindTurnId) showSubmittedMessages();
          onUpdate(conversationId, (current) => ({
            ...current,
            messages: current.messages.map((item) => item.id === userMessage.id ? { ...item, pending: false } : item),
          }));
          onAccepted?.();
          return turn;
        },
        reject,
      );
    } catch (error) {
      reject();
      throw error;
    }
  }

  async function retryFailedMessage(item: ChatMessage) {
    if (sendPendingRef.current || interactionBusy || sessionReadOnly || rewindPending) return;
    const attempt = {};
    sendPendingRef.current = attempt;
    setSendPending(true);
    const release = () => {
      if (sendPendingRef.current === attempt) {
        sendPendingRef.current = null;
        setSendPending(false);
      }
    };
    try {
      if (agentThreadView.isSubagent) {
        await agentThreadView.sendMessage({ content: item.content, references: item.references, mode, permissionMode, providerName: requestProviderName, model: requestModel }, item);
      } else {
        await runPrompt(item.content, undefined, item.references, release, item);
      }
    } catch (error) {
      void message.error({ content: <ErrorDisplay error={error} />, duration: 0 });
    } finally {
      release();
    }
  }

  async function send() {
    if (compactionPending || sandboxBlocked || sessionReadOnly || sendPendingRef.current || rewindPending || queuedMessageFlow.startingTurn || queuedMessageFlow.takingOutMessage) return;
    const attempt = {};
    sendPendingRef.current = attempt;
    setSendPending(true);
    const releaseSend = () => {
      if (sendPendingRef.current === attempt) {
        sendPendingRef.current = null;
        setSendPending(false);
      }
    };
    try {
      const prompt = input.trim();
      // A running assistant no longer blocks the composer: a draft is handed
      // to the in-memory FIFO queue below.  Only an in-progress upload prevents
      // submission because its final reference is not available yet.
      if (pendingUploads.some((upload) => upload.status === "uploading")) return;
      if (actionMode === "resume" && conversation?.sessionId && activeRuntimeNode) {
        await dispatchRun(
          conversation.id,
          conversation.sessionId,
          null,
          true,
          activeRuntimeNode.id,
          undefined,
          undefined,
          true,
          releaseSend,
        );
        return;
      }
      const mergedReferences = collectedReferences();
      if (!prompt && mergedReferences.length === 0) return;
      // Slash commands are control actions, not conversational turns.  They
      // must never be persisted into the running FIFO queue.  Keep command
      // handling ahead of the running branch so `/compact`, `/trace`, etc. remain
      // explicit commands even while an assistant is active.
      const command = parseCommand(prompt);
      if (command && prompt) {
        await chatCommands.executeCommand(command.name);
        return;
      }
      if (agentThreadView.isSubagent) {
        clearComposer();
        setPendingUploads([]);
        try {
          await agentThreadView.sendMessage({
            content: prompt,
            references: mergedReferences.length > 0 ? mergedReferences : undefined,
            mode,
            permissionMode,
            providerName: requestProviderName,
            model: requestModel,
          });
        } catch (error) {
          void message.error({ content: <ErrorDisplay error={error} />, duration: 0 });
        }
        return;
      }
      if (activeRuntimeNode?.status === "running") {
        const queued = queuedMessageFlow.queueCurrentPrompt(prompt, mergedReferences);
        releaseSend();
        await queued;
        return;
      }
      clearComposer();
      setPendingUploads([]);
      await runPrompt(
        prompt,
        undefined,
        mergedReferences.length > 0 ? mergedReferences : undefined,
        releaseSend,
      );
    } catch (error) {
      void message.error({ content: <ErrorDisplay error={error} />, duration: 0 });
    } finally {
      releaseSend();
    }
  }

  async function chooseDecision(request: DecisionRequest, choice: string, options?: { supplement?: string; answers?: Record<string, string[]> }) {
    try {
      await submitDecision(request.decision_id, choice, options, conversation?.sessionId);
      setLast({ decision: undefined });
    } catch (error) {
      setLast({ error: String((error as Error).message ?? error) });
    }
  }

  function stop() {
    if (activeRuntimeNode && onStopRun) {
      onStopRun(activeRuntimeNode);
    }
  }

  function handleComposerKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    const isComposing = event.nativeEvent.isComposing;
    const fileAction = fileKeyAction({ key: event.key, shiftKey: event.shiftKey, isComposing, menuVisible: fileMenuVisible });
    if (fileAction.type === "move") { event.preventDefault(); setActiveFileIndex((current) => nextCommandIndex(current, fileAction.direction, fileCandidates.length)); return; }
    if (fileAction.type === "dismiss") { event.preventDefault(); setFileMenuDismissedFor(input); return; }
    if (fileAction.type === "complete") {
      event.preventDefault();
      if (event.key === "Enter") event.stopPropagation();
      completeFile();
      return;
    }
    const action = commandKeyAction({ key: event.key, shiftKey: event.shiftKey, isComposing, menuVisible: commandMenuVisible });
    if (action.type === "move") { event.preventDefault(); setActiveCommandIndex((current) => nextCommandIndex(current, action.direction, filteredCommands.length)); return; }
    if (action.type === "dismiss") { event.preventDefault(); setCommandMenuDismissedFor(input); return; }
    if (action.type === "complete") { event.preventDefault(); chatCommands.completeCommand(); return; }
    if (action.type === "execute") { event.preventDefault(); void chatCommands.executeSelectedCommand(); return; }
    if (action.type === "send") { event.preventDefault(); void send(); }
  }

  return (
    <ViewStateContext.Provider value={savedKey}>
    <ButtonTooltipContext.Provider value={showButtonTooltips}>
    <div ref={chatPageRef} className={`chat-page${compact ? " chat-page--compact" : ""}`}>
      {savedView.error ? <div role="alert" className="chat-state-error">界面状态尚未保存：{savedView.error.message}</div> : null}
      {agentThreadView.streamError ? <div role="alert" className="chat-state-error"><ErrorDisplay error={agentThreadView.streamError} /></div> : null}
      {currentThreadId ? (
        <ChatToolbar
          visible={hasTurnTree || agentThreadView.isSubagent}
          currentThreadId={currentThreadId}
          conversationTitle={conversation?.title || "新对话"}
          compact={compact}
          mainView={visibleMainView}
          agentThread={agentThreadNavigation && conversation?.sessionId && agentThreadView.rootThreadId && agentThreadView.selectedThreadId ? {
            sessionId: conversation.sessionId,
            rootThreadId: agentThreadView.rootThreadId,
            selectedThreadId: agentThreadView.selectedThreadId,
            invalidation: agentThreadView.treeInvalidation,
            onSelect: agentThreadView.selectThread,
          } : undefined}
          onMainViewChange={setMainView}
        />
      ) : null}
      {visibleMainView === "trace" ? <TracePage key={currentThreadId} turns={traceTurns} /> : <>
      <div className="chat-content">
        <ChatMessageList
          onRetrySend={(item) => void retryFailedMessage(item)}
          messages={messages}
          sessionId={conversation?.sessionId}
          display={display}
          interactionBusy={interactionBusy || sessionReadOnly || rewindPending}
          compactionPending={compactionPending}
          chatScrollRef={chatScrollRef}
          onScroll={handleHistoryScroll}
          onWheel={(event) => { if (event.deltaY < 0 && event.currentTarget.scrollTop <= 24) void loadEarlier(); }}
          editingMessageId={editingMessageId}
          editingDraft={editingDraft}
          editRef={editRef}
          rewindPending={rewindPending}
          editingSubmitting={editingSubmitting}
          canEdit={!agentThreadView.isSubagent && Boolean(onRewind)}
          setEditingDraft={setEditingDraft}
          cancelEdit={cancelEdit}
          saveEdit={saveEdit}
          beginEdit={beginEdit}
          handleUserBubbleClick={handleUserBubbleClick}
          messageVersion={messageVersion}
          changeMessageVersion={changeMessageVersion}
          onDecision={chooseDecision}
          onFork={!agentThreadView.isSubagent && onFork ? forkMessage : undefined}
          sandboxErrorReport={sandboxHealth.error_report}
          sandboxFailure={sandboxHealth.phase === "unhealthy" ? sandboxHealth.detail ?? "健康检查未通过。" : null}
        />
        {!isMobile ? (
          <ConversationTimeline
            key={currentThreadId ?? "no-thread"}
            messages={messages}
            scrollContainerRef={chatScrollRef}
            onNavigate={scrollToPosition}
          />
        ) : null}
        {!isAtBottom ? (
          <FloatButton
            className="chat-scroll-bottom-button"
            icon={<VerticalAlignBottomOutlined />}
            tooltip={showButtonTooltips ? "滚动到底部" : undefined}
            aria-label="滚动到底部"
            onClick={scrollToBottom}
          />
        ) : null}
      </div>
      <Composer
        input={input}
        busy={interactionBusy}
        compact={compact}
        filteredCommands={filteredCommands}
        commandMenuVisible={commandMenuVisible}
        activeCommandIndex={activeCommandIndex}
        mode={mode}
        permissionMode={permissionMode}
        reasoningEffort={reasoningEffort}
        modePending={runtimeConfigPending.mode}
        permissionPending={runtimeConfigPending.permission}
        reasoningPending={runtimeConfigPending.reasoning}
        todos={visibleTodo}
        todoClosable={todoClosable}
        onTodoClose={closeTodoPanel}
        usagePercent={usagePercent}
        usageTotalTokens={activeUsage?.total ?? null}
        usageContextLength={activeUsage?.context}
        openSettingsSelect={openSettingsSelect}
        editorRef={editorRef}
        onEditorChange={handleEditorChange}
        onKeyDown={handleComposerKeyDown}
        onComplete={chatCommands.completeCommand}
        onCommandExecute={(index) => void chatCommands.executeSelectedCommand(index)}
        onActiveCommandChange={setActiveCommandIndex}
        onModeChange={(value) => void changeRunningMode(value)}
        onPermissionChange={(value) => void changePermissionMode(value)}
        onReasoningChange={(value) => void changeReasoningEffort(value)}
        onSettingsSelectChange={setOpenSettingsSelect}
        onStop={actionMode === "steer" ? queuedMessageFlow.sendPendingMessages : stop}
        onSend={() => void send()}
        actionMode={actionMode}
        submitDisabled={sandboxBlocked || projectUnavailable || compactionPending || sessionReadOnly || rewindPending || composerActionState.disabled || (actionMode === "steer" && queuedMessages.some((item) => item.saving)) || ((sendPending || queuedMessageFlow.startingTurn || queuedMessageFlow.takingOutMessage) && (actionMode === "send" || actionMode === "resume"))}
        disabled={sandboxBlocked || projectUnavailable || compactionPending || sessionReadOnly || rewindPending}
        inputDisabled={queuedMessageFlow.takingOutMessage}
        disabledReason={sandboxBlocked
          ? sandboxHealth.phase === "checking" ? "正在检查沙箱 Broker" : "沙箱 Broker 不可用"
          : sessionReadOnly ? ownership === "unknown" ? "正在确认窗口操作权" : "当前 session 正在另一个窗口对话"
          : conversation?.projectAvailable === false ? "项目 cwd 不可用，恢复文件夹后才能运行" : undefined}
        fileCandidates={fileCandidates}
        fileMenuVisible={fileMenuVisible}
        activeFileIndex={activeFileIndex}
        fileMenuQuery={fileTriggerState?.query ?? ""}
        onFileComplete={completeFile}
        onActiveFileChange={setActiveFileIndex}
        onPickFiles={handlePickFiles}
        sessionId={conversation?.sessionId}
        pendingUploads={pendingUploads}
        uploadsUploading={pendingUploads.some((upload) => upload.status === "uploading")}
        uploadsDisabled={sessionReadOnly}
        queuedMessages={agentThreadView.isSubagent ? [] : queuedMessages}
        onQueueSend={agentThreadView.isSubagent ? undefined : queuedMessageFlow.sendQueuedMessage}
        onQueueEdit={agentThreadView.isSubagent ? undefined : queuedMessageFlow.editQueuedMessage}
        onQueueDelete={agentThreadView.isSubagent ? undefined : queuedMessageFlow.deleteQueuedMessage}
        onRemoveUpload={removePendingUpload}
        onRetryUpload={retryUpload}
        onUploadPreview={(index) => {
          const upload = pendingUploads[index];
          if (upload?.path && conversation?.sessionId) {
            window.open(sessionFileContentUrl(conversation.sessionId, "upload", upload.path), "_blank", "noopener");
          }
        }}
      />
      </>}
    </div>
    </ButtonTooltipContext.Provider>
    </ViewStateContext.Provider>
  );
}
