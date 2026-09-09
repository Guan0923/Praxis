import { useEffect, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import { createQueuedMessage, deleteQueuedMessage, steerTurn, updateQueuedMessage } from "../../api";
import type { QueuedMessage } from "../../app/types";
import type { ChatMessage, Conversation, FileReference, RuntimeStateNode } from "../../types";
import type { FileMentionEditorHandle } from "./FileMentionEditor";
import type { PendingUpload } from "./contracts";

interface QueuedRunRequest {
  conversationId: string;
  sessionId: string;
  sourceNodeId: string | null;
  deliveryId: string;
  messageIds: string[];
  onBaseline: () => void;
}

interface UseQueuedMessageFlowOptions {
  conversation: Conversation | null;
  activeRuntimeNode?: RuntimeStateNode;
  queuedMessages: QueuedMessage[];
  queueSubmitting: boolean;
  sandboxBlocked: boolean;
  isSubagent: boolean;
  input: string;
  collectedReferences: () => FileReference[];
  clearComposer: () => void;
  editorRef: MutableRefObject<FileMentionEditorHandle | null>;
  setInput: Dispatch<SetStateAction<string>>;
  setReferences: Dispatch<SetStateAction<FileReference[]>>;
  setPendingUploads: Dispatch<SetStateAction<PendingUpload[]>>;
  setQueueSubmitting: Dispatch<SetStateAction<boolean>>;
  onQueuedMessagesChange: (conversationId: string, updater: (items: QueuedMessage[]) => QueuedMessage[]) => void;
  onQueuedMessagesRefresh: (conversationId: string) => Promise<void>;
  onSetLast: (fields: Partial<ChatMessage>) => void;
  onDispatch: (request: QueuedRunRequest) => Promise<void>;
  onStop: () => void;
  onWarning: (content: string) => void;
}

export function useQueuedMessageFlow({
  conversation,
  activeRuntimeNode,
  queuedMessages,
  queueSubmitting,
  sandboxBlocked,
  isSubagent,
  input,
  collectedReferences,
  clearComposer,
  editorRef,
  setInput,
  setReferences,
  setPendingUploads,
  setQueueSubmitting,
  onQueuedMessagesChange,
  onQueuedMessagesRefresh,
  onSetLast,
  onDispatch,
  onStop,
  onWarning,
}: UseQueuedMessageFlowOptions) {
  const flow = useRef({ conversationId: conversation?.id, flushing: false, steering: false, blocked: false });
  if (flow.current.conversationId !== conversation?.id) {
    flow.current = { conversationId: conversation?.id, flushing: false, steering: false, blocked: false };
  }
  const state = flow.current;
  const acknowledgedDeliveryIdsRef = useRef(new Set<string>());
  const [editingQueuedMessageId, setEditingQueuedMessageId] = useState<string | null>(null);

  useEffect(() => {
    setEditingQueuedMessageId(null);
  }, [conversation?.id]);

  useEffect(() => {
    const status = activeRuntimeNode?.status;
    if (status === "running") {
      state.blocked = false;
      return;
    }
    if (
      !sandboxBlocked
      && !state.flushing
      && !state.blocked
      && !isSubagent
      && queuedMessages.some((item) => item.state === "pending" && !item.saving && !item.error)
      && conversation?.id
      && (status === "success" || status === "failed")
    ) {
      state.flushing = true;
      setQueueSubmitting(true);
      void flushQueuedMessages();
    }
  // `queueSubmitting` deliberately retriggers the effect after a completed
  // flush so entries appended while that request was in flight start the next
  // FIFO pass.
  }, [activeRuntimeNode?.id, activeRuntimeNode?.status, queueSubmitting, queuedMessages, conversation?.id, sandboxBlocked]);

  useEffect(() => {
    if (!conversation?.id || !activeRuntimeNode) return;
    const ids = activeRuntimeNode.data[activeRuntimeNode.current_data_idx]
      ?.filter((item) => item.role === "user" && typeof item.delivery_id === "string")
      .map((item) => String(item.delivery_id)) ?? [];
    const fresh = ids.filter((id) => !acknowledgedDeliveryIdsRef.current.has(id));
    if (fresh.length === 0) return;
    fresh.forEach((id) => acknowledgedDeliveryIdsRef.current.add(id));
    void onQueuedMessagesRefresh(conversation.id);
  }, [activeRuntimeNode?.data, activeRuntimeNode?.current_data_idx, conversation?.id, onQueuedMessagesRefresh]);

  function updateQueue(updater: (items: QueuedMessage[]) => QueuedMessage[]) {
    if (conversation?.id) onQueuedMessagesChange(conversation.id, updater);
  }

  async function queueCurrentPrompt(prompt: string, itemReferences?: FileReference[]) {
    if (!prompt.trim() && (!itemReferences || itemReferences.length === 0)) return;
    if (!conversation?.id || !conversation.threadId) return;
    const now = new Date().toISOString();
    const previous = queuedMessages.find((item) => item.id === editingQueuedMessageId);
    const item: QueuedMessage = {
      id: editingQueuedMessageId ?? crypto.randomUUID(),
      thread_id: conversation.threadId,
      content: prompt,
      references: itemReferences ?? [],
      state: "pending",
      created_at: now,
      updated_at: now,
      saving: true,
      editing: Boolean(previous && (!previous.error || previous.editing)),
    };
    updateQueue((items) => editingQueuedMessageId
      ? items.map((candidate) => candidate.id === item.id ? item : candidate)
      : [...items, item]);
    setEditingQueuedMessageId(null);
    clearComposer();
    setPendingUploads([]);
    await saveQueuedMessage(item);
  }

  async function saveQueuedMessage(item: QueuedMessage): Promise<QueuedMessage | undefined> {
    updateQueue((items) => items.map((candidate) => candidate.id === item.id ? { ...item, saving: true, error: undefined } : candidate));
    try {
      const stored = item.editing
        ? await updateQueuedMessage(item.thread_id, item.id, item.content, item.references, conversation?.sessionId)
        : await createQueuedMessage(item.thread_id, item.id, item.content, item.references, conversation?.sessionId);
      updateQueue((items) => items.map((candidate) => candidate.id === item.id ? stored : candidate));
      return stored;
    } catch (error) {
      updateQueue((items) => items.map((candidate) => candidate.id === item.id
        ? { ...item, saving: false, error: String((error as Error).message ?? error) }
        : candidate));
      return undefined;
    }
  }

  function editQueuedMessage(item: QueuedMessage) {
    if (item.state !== "pending" || item.saving) return;
    const currentPrompt = input.trim();
    const currentReferences = collectedReferences();
    if (currentPrompt || currentReferences.length > 0) {
      onWarning("输入框有内容，无法修改队列消息");
      return;
    }
    setEditingQueuedMessageId(item.id);
    editorRef.current?.restore(item.content, item.references);
    setInput(item.content);
    setReferences(item.references ?? []);
    window.setTimeout(() => editorRef.current?.focus(), 0);
  }

  async function sendQueuedMessage(item: QueuedMessage) {
    if (item.state !== "pending" || item.saving) return;
    const stored = item.error ? await saveQueuedMessage(item) : item;
    if (stored) await submitSteering([stored]);
  }

  async function deleteMessage(item: QueuedMessage) {
    if (isSubagent || item.state !== "pending" || item.saving || !conversation?.threadId) return;
    try {
      if (!item.error || item.editing) await deleteQueuedMessage(conversation.threadId, item.id, conversation.sessionId);
      updateQueue((items) => items.filter((candidate) => candidate.id !== item.id));
    } catch (error) {
      onSetLast({ error: String((error as Error).message ?? error) });
    }
  }

  async function submitSteering(items: QueuedMessage[]) {
    if (sandboxBlocked || state.steering || !conversation?.id || activeRuntimeNode?.status !== "running" || items.length === 0) return;
    state.steering = true;
    const ids = new Set(items.map((item) => item.id));
    updateQueue((current) => current.map((item) => ids.has(item.id) ? { ...item, state: "dispatched" } : item));
    try {
      await steerTurn(
        activeRuntimeNode.id,
        crypto.randomUUID(),
        items.map((item) => item.id),
        conversation.sessionId,
      );
    } catch (error) {
      updateQueue((current) => current.map((item) => ids.has(item.id) ? { ...item, state: "pending" } : item));
      onWarning(String((error as Error).message ?? error));
    } finally {
      state.steering = false;
    }
    await onQueuedMessagesRefresh(conversation.id).catch((error) => onWarning(String((error as Error).message ?? error)));
  }

  function pauseOrSteer() {
    const pending = queuedMessages.filter((item) => item.state === "pending" && !item.saving && !item.error);
    if (pending.length > 0 && !state.steering) {
      void submitSteering(pending);
      return;
    }
    onStop();
  }

  async function flushQueuedMessages() {
    const items = queuedMessages.slice();
    if (sandboxBlocked || !conversation?.sessionId || items.length === 0) {
      state.flushing = false;
      setQueueSubmitting(false);
      return;
    }
    const pendingItems = items.filter((item) => item.state === "pending" && !item.saving && !item.error);
    if (pendingItems.length === 0) {
      state.flushing = false;
      setQueueSubmitting(false);
      return;
    }
    let acknowledged = false;
    try {
      await onDispatch({
        conversationId: conversation.id,
        sessionId: conversation.sessionId,
        sourceNodeId: activeRuntimeNode?.id ?? null,
        deliveryId: crypto.randomUUID(),
        messageIds: pendingItems.map((item) => item.id),
        onBaseline: () => {
          if (acknowledged) return;
          acknowledged = true;
          void onQueuedMessagesRefresh(conversation.id);
        },
      });
      await onQueuedMessagesRefresh(conversation.id);
      if (!acknowledged) state.blocked = true;
    } catch (error) {
      onSetLast({ error: String((error as Error).message ?? error), running: false, decision: undefined });
      state.blocked = true;
    } finally {
      state.flushing = false;
      if (flow.current === state) setQueueSubmitting(false);
    }
  }

  return { deleteQueuedMessage: deleteMessage, editQueuedMessage, pauseOrSteer, queueCurrentPrompt, sendQueuedMessage };
}
