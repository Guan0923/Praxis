import type { QueuedMessage } from "../../app/types";
import type { FileReference } from "../../types";
import { requestJson, requestVoid } from "../transport/request";

function queueUrl(threadId: string, messageId?: string): string {
  const base = `/api/sidebar-threads/${encodeURIComponent(threadId)}/queued-messages`;
  return messageId ? `${base}/${encodeURIComponent(messageId)}` : base;
}

export async function listQueuedMessages(threadId: string): Promise<QueuedMessage[]> {
  return requestJson(queueUrl(threadId));
}

export async function createQueuedMessage(
  threadId: string,
  id: string,
  content: string,
  references: FileReference[] = [],
  sessionId?: string,
): Promise<QueuedMessage> {
  return requestJson(queueUrl(threadId), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, content, references }),
    operation: { sessionId, dedupeKey: `queued-message:create:${id}` },
  });
}

export async function updateQueuedMessage(
  threadId: string,
  messageId: string,
  content: string,
  references: FileReference[] = [],
  sessionId?: string,
): Promise<QueuedMessage> {
  return requestJson(queueUrl(threadId, messageId), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, references }),
    operation: { sessionId },
  });
}

export async function deleteQueuedMessage(threadId: string, messageId: string, sessionId?: string): Promise<void> {
  await requestVoid(queueUrl(threadId, messageId), { method: "DELETE", operation: { sessionId } });
}
