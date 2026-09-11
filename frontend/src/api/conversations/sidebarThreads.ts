import type { SidebarThread } from "../../types";
import { requestJson, requestVoid } from "../transport/request";

export type SidebarThreadSort = "created_at" | "recent_activity";

export interface SidebarThreadOrderResult {
  ordered_thread_ids: string[];
}

export async function listSidebarThreads(state: "active" | "archived" | "deleted" | "all" = "active"): Promise<SidebarThread[]> {
  return requestJson(`/api/sidebar-threads?state=${encodeURIComponent(state)}`);
}

export async function createSidebarThread(title = "新对话", clientId?: string): Promise<SidebarThread> {
  return requestJson("/api/sidebar-threads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, client_id: clientId }),
    operation: { group: "conversations:create", dedupeKey: "conversation:create" },
  });
}

export async function updateSidebarThreadOrder(
  projectId: string | null,
  order: { orderedThreadIds: string[] } | { sortBy: SidebarThreadSort },
): Promise<SidebarThreadOrderResult> {
  return requestJson("/api/sidebar-threads/order", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      project_id: projectId,
      ...(order && "orderedThreadIds" in order
        ? { ordered_thread_ids: order.orderedThreadIds }
        : { sort_by: order.sortBy }),
    }),
    operation: { group: `sidebar-order:${projectId ?? "ungrouped"}` },
  });
}

export async function renameSidebarThread(threadId: string, title: string, sessionId?: string): Promise<SidebarThread> {
  return requestJson(`/api/sidebar-threads/${encodeURIComponent(threadId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
    operation: { sessionId },
  });
}

export async function archiveSidebarThread(threadId: string, sessionId?: string): Promise<SidebarThread> {
  return requestJson(`/api/sidebar-threads/${encodeURIComponent(threadId)}/archive`, { method: "POST", operation: { sessionId } });
}

export async function restoreSidebarThread(threadId: string, sessionId?: string): Promise<SidebarThread> {
  return requestJson(`/api/sidebar-threads/${encodeURIComponent(threadId)}/restore`, { method: "POST", operation: { sessionId } });
}

export async function deleteSidebarThread(threadId: string, sessionId: string): Promise<void> {
  return requestVoid(`/api/sidebar-threads/${encodeURIComponent(threadId)}?session_id=${encodeURIComponent(sessionId)}`, { method: "DELETE", operation: { sessionId } });
}
