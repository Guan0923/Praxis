import type { TurnPage } from "../api/conversations/turns";
import { projectTurnPath } from "./runtime/runtimeDetailProjection";
import { isRuntimeTurnNode } from "./runtime/runtimeNodeNormalization";
import type { Conversation, RuntimeTreeNode } from "../types";

export function withLoadedTurns(
  conversation: Conversation,
  nodes: RuntimeTreeNode[],
  preferredActiveTurnId?: string,
): Conversation {
  const threadId = conversation.threadId ?? conversation.sessionId;
  const threadNodes = nodes.filter(isRuntimeTurnNode).filter((node) => node.thread_id === threadId);
  const selected = threadNodes.find((node) => node.id === (preferredActiveTurnId ?? conversation.activeTurnId));
  const parentIds = new Set(threadNodes.map((node) => node.parent_id).filter(Boolean));
  const leaves = threadNodes
    .filter((node) => !parentIds.has(node.id))
    .sort((left, right) => left.timestamp.localeCompare(right.timestamp));
  const fallback = leaves[leaves.length - 1];
  const activeTurnId = selected?.id ?? fallback?.id;
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
  let earliest = activeTurnId ? nodes.find((node) => node.id === activeTurnId) : undefined;
  const visited = new Set<string>();
  while (earliest && isRuntimeTurnNode(earliest) && earliest.parent_id && !visited.has(earliest.id)) {
    visited.add(earliest.id);
    const parent = map.get(`${earliest.parent_session_id}:${earliest.parent_id}`);
    if (!parent) break;
    earliest = parent;
  }
  const historyCursor = earliest && isRuntimeTurnNode(earliest) && earliest.parent_id && nodes.filter(isRuntimeTurnNode).length >= 5
    ? JSON.stringify({ thread: threadId, head: activeTurnId, session: earliest.parent_session_id, turn: earliest.parent_id }) : null;
  return {
    ...conversation,
    historyCursor,
    historyHasMore: historyCursor !== null,
    runtimeNodes: nodes,
    activeTurnId,
    lastNodeId: activeTurnId,
    messages: hiddenIndex >= 0 ? projected.slice(hiddenIndex + 1) : projected,
    messagesLoaded: true,
  };
}

export function withRefreshedTurns(conversation: Conversation, refreshed: RuntimeTreeNode[]): Conversation {
  if (!conversation.messagesLoaded) return withLoadedTurns(conversation, refreshed);
  const nodes = new Map((conversation.runtimeNodes ?? []).map((node) => [node.id, node]));
  for (const node of refreshed) nodes.set(node.id, node);
  return {
    ...withLoadedTurns(conversation, [...nodes.values()]),
    historyCursor: conversation.historyCursor,
    historyHasMore: conversation.historyHasMore,
  };
}

export function withTurnPage(conversation: Conversation, page: TurnPage, append = false): Conversation {
  const nodes = new Map((append ? conversation.runtimeNodes ?? [] : []).map((node) => [node.id, node]));
  for (const node of page.turns) {
    if (!append || !nodes.has(node.id)) nodes.set(node.id, node);
  }
  return {
    ...withLoadedTurns(conversation, [...nodes.values()]),
    historyCursor: page.next_cursor,
    historyHasMore: page.has_more,
  };
}
