import { useEffect, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import { ApiError, createQueuedMessage, deleteQueuedMessage, steerTurn } from "../../api";
import type { QueuedMessage } from "../../app/types";
import { completionToken } from "../../commands/fileCompletion";
import type { Conversation, FileReference, RuntimeStateNode } from "../../types";
import type { FileMentionEditorHandle } from "./FileMentionEditor";
import type { PendingUpload } from "./contracts";

interface QueuedRunRequest {
  conversationId: string;
  sessionId: string;
  sourceNodeId: string | null;
  messageIds: string[];
}

interface UseQueuedMessageFlowOptions {
  conversation: Conversation | null;
  activeRuntimeNode?: RuntimeStateNode;
  queuedMessages: QueuedMessage[];
  disabled: boolean;
  compactionPending: boolean;
  input: string;
  collectedReferences: () => FileReference[];
  clearComposer: () => void;
  editorRef: MutableRefObject<FileMentionEditorHandle | null>;
  setInput: Dispatch<SetStateAction<string>>;
  setReferences: Dispatch<SetStateAction<FileReference[]>>;
  setPendingUploads: Dispatch<SetStateAction<PendingUpload[]>>;
  onQueuedMessagesChange: (conversationId: string, updater: (items: QueuedMessage[]) => QueuedMessage[]) => void;
  onQueuedMessagesRefresh: (conversationId: string) => Promise<void>;
  // Resolves when the backend admits the Turn, not when its SSE connection closes.
  onDispatch: (request: QueuedRunRequest) => Promise<void>;
  onWarning: (content: string) => void;
}

export function useQueuedMessageFlow({
  conversation, activeRuntimeNode, queuedMessages, disabled, compactionPending, input, collectedReferences,
  clearComposer, editorRef, setInput, setReferences, setPendingUploads,
  onQueuedMessagesChange, onQueuedMessagesRefresh, onDispatch, onWarning,
}: UseQueuedMessageFlowOptions) {
  const inFlight = useRef(new Set<string>());
  const starting = useRef<string | null>(null);
  const [startingConversation, setStartingConversation] = useState<string | null>(null);
  const [editingConversation, setEditingConversation] = useState<string | null>(null);
  const currentConversation = useRef(conversation?.id);
  currentConversation.current = conversation?.id;
  const deliveryIds = activeRuntimeNode?.data[activeRuntimeNode.current_data_idx]
    ?.filter((item) => item.role === "user" && item.delivery_id)
    .map((item) => item.delivery_id).join(",");

  useEffect(() => {
    if (conversation?.id && activeRuntimeNode) void refresh();
  }, [conversation?.id, activeRuntimeNode?.id, activeRuntimeNode?.status, deliveryIds]);

  useEffect(() => {
    if (!disabled && !compactionPending && (activeRuntimeNode?.status === "success" || activeRuntimeNode?.status === "failed")) {
      if (queuedMessages.some((item) => item.saving)) return;
      const pending = queuedMessages.filter((item) => item.state === "pending" && !item.saving && !item.error);
      if (pending.length) void dispatchMessages(pending);
    }
  }, [conversation?.id, activeRuntimeNode?.id, activeRuntimeNode?.status, queuedMessages, disabled, compactionPending, startingConversation]);

  function updateQueue(updater: (items: QueuedMessage[]) => QueuedMessage[]) {
    if (conversation?.id) onQueuedMessagesChange(conversation.id, updater);
  }

  async function refresh() {
    if (!conversation?.id) return;
    try {
      await onQueuedMessagesRefresh(conversation.id);
    } catch (error) {
      onWarning(String((error as Error).message ?? error));
    }
  }

  function fail(items: QueuedMessage[], error: unknown) {
    const ids = new Set(items.map((item) => item.id));
    const detail = String((error as Error).message ?? error);
    updateQueue((current) => current.map((item) => ids.has(item.id)
      ? { ...item, state: "pending", saving: false, error: detail } : item));
    onWarning(detail);
  }

  async function queueCurrentPrompt(prompt: string, references: FileReference[] = []) {
    if (!conversation?.threadId || (!prompt.trim() && references.length === 0)) return;
    const now = new Date().toISOString();
    const item: QueuedMessage = {
      id: crypto.randomUUID(), thread_id: conversation.threadId, content: prompt, references,
      state: "pending", created_at: now, updated_at: now, unsaved: true, saving: true,
    };
    updateQueue((items) => [...items, item]);
    clearComposer();
    setPendingUploads([]);
    await save(item);
  }

  async function save(item: QueuedMessage): Promise<QueuedMessage | undefined> {
    inFlight.current.add(item.id);
    updateQueue((items) => items.map((entry) => entry.id === item.id ? { ...entry, saving: true, error: undefined } : entry));
    try {
      const stored = await createQueuedMessage(item.thread_id, item.id, item.content, item.references, conversation?.sessionId);
      updateQueue((items) => items.map((entry) => entry.id === item.id ? stored : entry));
      return stored;
    } catch (error) {
      fail([item], error);
    } finally {
      inFlight.current.delete(item.id);
    }
  }

  function canChange(item: QueuedMessage) {
    return !disabled && item.state === "pending" && !item.saving && !inFlight.current.has(item.id);
  }

  async function remove(item: QueuedMessage): Promise<boolean> {
    inFlight.current.add(item.id);
    updateQueue((items) => items.map((entry) => entry.id === item.id ? { ...entry, saving: true } : entry));
    try {
      if (!item.unsaved) await deleteQueuedMessage(item.thread_id, item.id, conversation?.sessionId);
      updateQueue((items) => items.filter((entry) => entry.id !== item.id));
      return true;
    } catch (error) {
      fail([item], error);
      return false;
    } finally {
      inFlight.current.delete(item.id);
    }
  }

  async function deleteMessage(item: QueuedMessage) {
    if (canChange(item)) await remove(item);
  }

  async function editQueuedMessage(item: QueuedMessage) {
    if (!canChange(item) || editingConversation || !conversation) return;
    if (input.trim() || collectedReferences().length) {
      onWarning("输入框有内容，无法修改队列消息");
      return;
    }
    setEditingConversation(conversation.id);
    try {
      if (!await remove(item)) return;
      // Inline mentions belong in the editor; uploads belong in the attachment list.
      const inline = item.references.filter((ref) => ref.source !== "upload" || item.content.includes(completionToken(ref.display_path)));
      const missing = inline.map((ref) => completionToken(ref.display_path)).filter((token) => !item.content.includes(token));
      const prompt = [item.content, ...missing].filter(Boolean).join(" ");
      if (currentConversation.current === conversation.id) editorRef.current?.restore(prompt, inline);
      setInput(prompt);
      setReferences(inline);
      setPendingUploads(item.references.filter((ref) => !inline.includes(ref)).map((ref) => ({
        uid: crypto.randomUUID(), name: ref.display_path, isImage: /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(ref.display_path),
        status: "done", percent: 100, path: ref.path, displayPath: ref.display_path,
      })));
    } finally {
      setEditingConversation(null);
    }
  }

  async function dispatchMessages(items: QueuedMessage[]) {
    if (compactionPending) return;
    items = items.filter((item) => canChange(item));
    if (!conversation?.sessionId || !items.length || starting.current === conversation.id) return;
    const running = activeRuntimeNode?.status === "running";
    let startsTurn = !running;
    if (!running) {
      starting.current = conversation.id;
      setStartingConversation(conversation.id);
    }
    const ids = new Set(items.map((item) => item.id));
    ids.forEach((id) => inFlight.current.add(id));
    updateQueue((current) => current.map((item) => ids.has(item.id) ? { ...item, state: "dispatched", error: undefined } : item));
    try {
      if (running) {
        try {
          await steerTurn(activeRuntimeNode.id, crypto.randomUUID(), [...ids], conversation.sessionId);
        } catch (error) {
          if (!(error instanceof ApiError) || error.code !== "turn_finished") throw error;
          if (starting.current === conversation.id) {
            await refresh();
            return;
          }
          startsTurn = true;
          starting.current = conversation.id;
          setStartingConversation(conversation.id);
          await onDispatch({
            conversationId: conversation.id, sessionId: conversation.sessionId,
            sourceNodeId: activeRuntimeNode.id, messageIds: [...ids],
          });
        }
      } else {
        await onDispatch({
          conversationId: conversation.id, sessionId: conversation.sessionId,
          sourceNodeId: activeRuntimeNode?.id ?? null, messageIds: [...ids],
        });
      }
      void refresh();
    } catch (error) {
      fail(items, error);
    } finally {
      ids.forEach((id) => inFlight.current.delete(id));
      if (startsTurn) {
        starting.current = null;
        setStartingConversation(null);
      }
    }
  }

  async function sendQueuedMessage(item: QueuedMessage) {
    if (!canChange(item)) return;
    const stored = item.unsaved ? await save(item) : item;
    if (stored) await dispatchMessages([stored]);
  }

  function sendPendingMessages() {
    if (queuedMessages.some((item) => item.saving)) return Promise.resolve();
    return dispatchMessages(queuedMessages.filter((item) => item.state === "pending" && !item.saving && !item.error));
  }

  return {
    deleteQueuedMessage: deleteMessage, editQueuedMessage, queueCurrentPrompt, sendQueuedMessage, sendPendingMessages,
    startingTurn: startingConversation !== null && startingConversation === conversation?.id,
    takingOutMessage: editingConversation !== null && editingConversation === conversation?.id,
  };
}
