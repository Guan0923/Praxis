import { useEffect, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import { createQueuedMessage, deleteQueuedMessage, steerTurn } from "../../api";
import type { QueuedMessage } from "../../app/types";
import { completionToken } from "../../commands/fileCompletion";
import type { ChatMessage, Conversation, FileReference, RuntimeStateNode } from "../../types";
import type { FileMentionEditorHandle } from "./FileMentionEditor";
import type { PendingUpload } from "./contracts";

interface QueuedRunRequest {
  conversationId: string;
  sessionId: string;
  sourceNodeId: string | null;
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
  const takingOut = useRef(new Set<string>());
  const [takeOutConversationId, setTakeOutConversationId] = useState<string | null>(null);

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
    const item: QueuedMessage = {
      id: crypto.randomUUID(),
      thread_id: conversation.threadId,
      content: prompt,
      references: itemReferences ?? [],
      state: "pending",
      created_at: now,
      updated_at: now,
      saving: true,
    };
    updateQueue((items) => [...items, item]);
    clearComposer();
    setPendingUploads([]);
    await saveQueuedMessage(item);
  }

  async function saveQueuedMessage(item: QueuedMessage): Promise<QueuedMessage | undefined> {
    updateQueue((items) => items.map((candidate) => candidate.id === item.id ? { ...item, saving: true, error: undefined } : candidate));
    try {
      const stored = await createQueuedMessage(item.thread_id, item.id, item.content, item.references, conversation?.sessionId);
      updateQueue((items) => items.map((candidate) => candidate.id === item.id ? stored : candidate));
      return stored;
    } catch (error) {
      updateQueue((items) => items.map((candidate) => candidate.id === item.id
        ? { ...item, saving: false, error: String((error as Error).message ?? error) }
        : candidate));
      return undefined;
    }
  }

  async function editQueuedMessage(item: QueuedMessage) {
    if (isSubagent || item.state !== "pending" || item.saving || takingOut.current.size > 0 || !conversation?.threadId) return;
    const currentPrompt = input.trim();
    const currentReferences = collectedReferences();
    if (currentPrompt || currentReferences.length > 0) {
      onWarning("输入框有内容，无法修改队列消息");
      return;
    }
    takingOut.current.add(item.id);
    setTakeOutConversationId(conversation.id);
    updateQueue((items) => items.map((candidate) => candidate.id === item.id ? { ...candidate, saving: true } : candidate));
    try {
      if (!item.error) await deleteQueuedMessage(item.thread_id, item.id, conversation.sessionId);
      updateQueue((items) => items.filter((candidate) => candidate.id !== item.id));
      // Keep inline file mentions and separately uploaded attachments editable.
      const inlineReferences = item.references.filter((reference) => reference.source !== "upload" || item.content.includes(completionToken(reference.display_path)));
      const missingTokens = inlineReferences.map((reference) => completionToken(reference.display_path))
        .filter((token) => !item.content.includes(token));
      const prompt = [item.content, ...missingTokens].filter(Boolean).join(" ");
      if (flow.current === state) editorRef.current?.restore(prompt, inlineReferences);
      setInput(prompt);
      setReferences(inlineReferences);
      setPendingUploads(item.references.filter((reference) => !inlineReferences.includes(reference)).map((reference) => ({
        uid: crypto.randomUUID(), name: reference.display_path, isImage: /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(reference.display_path),
        status: "done", percent: 100, path: reference.path, displayPath: reference.display_path,
      })));
      window.setTimeout(() => {
        if (flow.current === state) editorRef.current?.focus();
      }, 0);
    } catch (error) {
      updateQueue((items) => items.map((candidate) => candidate.id === item.id ? { ...candidate, saving: false } : candidate));
      onWarning(String((error as Error).message ?? error));
    } finally {
      takingOut.current.delete(item.id);
      setTakeOutConversationId(null);
    }
  }

  async function sendQueuedMessage(item: QueuedMessage) {
    if (item.state !== "pending" || item.saving || takingOut.current.has(item.id)) return;
    const stored = item.error ? await saveQueuedMessage(item) : item;
    if (stored) await submitSteering([stored]);
  }

  async function deleteMessage(item: QueuedMessage) {
    if (isSubagent || item.state !== "pending" || item.saving || takingOut.current.has(item.id) || !conversation?.threadId) return;
    try {
      if (!item.error) await deleteQueuedMessage(conversation.threadId, item.id, conversation.sessionId);
      updateQueue((items) => items.filter((candidate) => candidate.id !== item.id));
    } catch (error) {
      onSetLast({ error: String((error as Error).message ?? error) });
    }
  }

  async function submitSteering(items: QueuedMessage[]) {
    items = items.filter((item) => !takingOut.current.has(item.id));
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
    const pendingItems = items.filter((item) => item.state === "pending" && !item.saving && !item.error && !takingOut.current.has(item.id));
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

  return {
    deleteQueuedMessage: deleteMessage, editQueuedMessage, pauseOrSteer, queueCurrentPrompt, sendQueuedMessage,
    takingOutMessage: takeOutConversationId !== null && takeOutConversationId === conversation?.id,
  };
}
