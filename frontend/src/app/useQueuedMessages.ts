import { useEffect, useRef, useState } from "react";
import { listQueuedMessages } from "../api";
import type { Conversation } from "../types";
import type { QueuedMessage } from "./types";

interface UseQueuedMessagesOptions {
  current: Conversation | null;
  conversations: Conversation[];
  panelConversations: Record<string, Conversation>;
  onError: (message: string) => void;
}

export function useQueuedMessages({
  current,
  conversations,
  panelConversations,
  onError,
}: UseQueuedMessagesOptions) {
  const [queuedMessages, setQueuedMessages] = useState<Map<string, QueuedMessage[]>>(() => new Map());
  const revisions = useRef(new Map<string, number>());

  useEffect(() => {
    const refresh = (event: Event) => {
      const threadId = (event as CustomEvent<string>).detail;
      const targets = new Map([...conversations, ...Object.values(panelConversations), ...(current ? [current] : [])]
        .filter((item) => item.threadId && (!threadId || item.threadId === threadId))
        .map((item) => [item.id, item]));
      for (const target of targets.values()) {
        void refreshQueuedMessages(target.id).catch((error) => onError(String((error as Error).message ?? error)));
      }
    };
    window.addEventListener("praxis-queue-changed", refresh);
    window.addEventListener("praxis-sync-reset", refresh);
    return () => {
      window.removeEventListener("praxis-queue-changed", refresh);
      window.removeEventListener("praxis-sync-reset", refresh);
    };
  }, [current, conversations, panelConversations]);

  useEffect(() => {
    if (!current?.id || !current.threadId) return;
    let active = true;
    const revision = (revisions.current.get(current.id) ?? 0) + 1;
    revisions.current.set(current.id, revision);
    void listQueuedMessages(current.threadId)
      .then((items) => {
        if (!active || revision !== (revisions.current.get(current.id) ?? 0)) return;
        setQueuedMessages((previous) => {
          const next = new Map(previous);
          next.set(current.id, mergeLocalMessages(previous.get(current.id) ?? [], items));
          return next;
        });
      })
      .catch((error) => {
        if (active) onError(String((error as Error).message ?? error));
      });
    return () => { active = false; };
  }, [current?.id, current?.threadId]);

  function updateQueuedMessages(conversationId: string, updater: (items: QueuedMessage[]) => QueuedMessage[]) {
    revisions.current.set(conversationId, (revisions.current.get(conversationId) ?? 0) + 1);
    setQueuedMessages((previous) => {
      const queues = new Map(previous);
      const next = updater(previous.get(conversationId) ?? []);
      if (next.length > 0) queues.set(conversationId, next);
      else queues.delete(conversationId);
      return queues;
    });
  }

  async function refreshQueuedMessages(conversationId: string): Promise<void> {
    const target = conversations.find((item) => item.id === conversationId) ?? panelConversations[conversationId]
      ?? (current?.id === conversationId ? current : undefined);
    if (!target?.threadId) return;
    const revision = (revisions.current.get(conversationId) ?? 0) + 1;
    revisions.current.set(conversationId, revision);
    const items = await listQueuedMessages(target.threadId);
    if (revision !== (revisions.current.get(conversationId) ?? 0)) return;
    setQueuedMessages((previous) => {
      const next = new Map(previous);
      next.set(conversationId, mergeLocalMessages(previous.get(conversationId) ?? [], items));
      return next;
    });
  }

  return { queuedMessages, setQueuedMessages, updateQueuedMessages, refreshQueuedMessages };
}

export function mergeLocalMessages(local: QueuedMessage[], stored: QueuedMessage[]): QueuedMessage[] {
  const localById = new Map(local.map((item) => [item.id, item]));
  const storedIds = new Set(stored.map((item) => item.id));
  return [
    ...stored.map((item) => {
      const pending = localById.get(item.id);
      return { ...item, saving: pending?.saving, error: item.state === "pending" ? pending?.error : undefined };
    }),
    ...local.filter((item) => item.unsaved && !storedIds.has(item.id)),
  ];
}
