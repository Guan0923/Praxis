import type { Conversation } from "../types";

export function trimConversationDetails(
  conversations: Conversation[], visited: Map<string, number>, active: Set<string>,
): Conversation[] {
  const idle = conversations.filter((item) => item.messagesLoaded && !active.has(item.id)
    && !item.messages.some((message) => message.running)
    && !item.runtimeNodes?.some((node) => "status" in node && node.status === "running"));
  idle.sort((a, b) => (visited.get(b.id) ?? 0) - (visited.get(a.id) ?? 0));
  const evicted = new Set(idle.slice(5).map((item) => item.id));
  if (!evicted.size) return conversations;
  return conversations.map((item) => evicted.has(item.id) ? {
    ...item, messages: [], runtimeNodes: undefined, messagesLoaded: false,
    historyCursor: undefined, historyHasMore: undefined,
  } : item);
}
