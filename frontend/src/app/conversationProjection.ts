import type { TurnPage } from "../api/conversations/turns";
import { projectTurnPath } from "./runtime/runtimeDetailProjection";
import { isRuntimeTurnNode } from "./runtime/runtimeNodeNormalization";
import type { Conversation, RuntimeTreeNode } from "../types";

export function withLoadedTurns(
  conversation: Conversation,
  nodes: RuntimeTreeNode[],
  activeTurnId: string | null = conversation.activeTurnId ?? null,
): Conversation {
  if (activeTurnId && !nodes.some((node) => node.id === activeTurnId && isRuntimeTurnNode(node))) {
    throw new Error("Current Turn is missing; reload conversation history.");
  }
  const map = new Map(nodes.map((node) => [`${node.session_id}:${node.id}`, node] as const));
  const projected = activeTurnId ? projectTurnPath(map, activeTurnId, true) : [];
  const hiddenPrefix = conversation.hiddenBeforeTurnId ? `${conversation.hiddenBeforeTurnId}:message:` : null;
  let hiddenIndex = -1;
  if (hiddenPrefix) {
    for (let index = projected.length - 1; index >= 0; index -= 1) {
      if (projected[index].id.startsWith(hiddenPrefix)) {
        hiddenIndex = index;
        break;
      }
    }
  }
  const delivered = new Set(projected.map((message) => message.deliveryId).filter(Boolean));
  const pending = conversation.messages.filter((message) => message.role === "user"
    && (message.pending || message.error) && message.deliveryId && !delivered.has(message.deliveryId));
  return {
    ...conversation,
    runtimeNodes: nodes,
    activeTurnId: activeTurnId ?? undefined,
    lastNodeId: activeTurnId ?? undefined,
    messages: [...(hiddenIndex >= 0 ? projected.slice(hiddenIndex + 1) : projected), ...pending],
    messagesLoaded: true,
  };
}

export function withTurnPage(conversation: Conversation, page: TurnPage, append = false): Conversation {
  const retained = append ? conversation.runtimeNodes ?? []
    : (conversation.runtimeNodes ?? []).filter((node) => node.thread_id !== (conversation.threadId ?? conversation.sessionId));
  const nodes = new Map(retained.map((node) => [`${node.session_id}:${node.id}`, node]));
  for (const node of page.turns) {
    const key = `${node.session_id}:${node.id}`;
    if (!append || !nodes.has(key)) nodes.set(key, node);
  }
  return {
    ...withLoadedTurns(conversation, [...nodes.values()], append ? conversation.activeTurnId ?? null : page.current_turn_id),
    historyCursor: page.next_cursor,
    historyHasMore: page.has_more,
  };
}
