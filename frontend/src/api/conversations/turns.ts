import type { RuntimeStateNode, RuntimeTreeNode, SidebarThread, TurnTraceResponse } from "../../types";
import { requestJson } from "../transport/request";

export async function listTurns(sessionId: string): Promise<RuntimeTreeNode[]> {
  return requestJson(`/api/turns?session_id=${encodeURIComponent(sessionId)}`);
}

export function threadTraceDownloadUrl(sessionId: string, threadId: string): string {
  const query = new URLSearchParams({ session_id: sessionId, thread_id: threadId });
  return `/api/turns/trace/export?${query.toString()}`;
}

export async function getTurnTrace(
  turnId: string,
  dataIdx: number,
  signal?: AbortSignal,
  afterSequence?: number,
): Promise<TurnTraceResponse> {
  const query = new URLSearchParams({ data_idx: String(dataIdx) });
  if (afterSequence !== undefined) query.set("after_sequence", String(afterSequence));
  return requestJson(
    `/api/turns/${encodeURIComponent(turnId)}/trace?${query.toString()}`,
    { signal },
  );
}

export async function patchTurnCurrentData(
  turnId: string,
  currentDataIdx: number,
  sessionId?: string,
): Promise<RuntimeStateNode> {
  return requestJson(`/api/turns/${encodeURIComponent(turnId)}/current-data`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current_data_idx: currentDataIdx }),
    operation: { sessionId },
  });
}

export async function forkTurn(turnId: string, sessionId?: string): Promise<{ turn: RuntimeStateNode; sidebar_thread: SidebarThread }> {
  return requestJson(`/api/turns/${encodeURIComponent(turnId)}/fork`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
    operation: { sessionId },
  });
}

export async function compactTurn(turnId: string, sessionId?: string): Promise<RuntimeStateNode> {
  return requestJson(`/api/turns/${encodeURIComponent(turnId)}/compact`, {
    method: "POST",
    operation: { sessionId },
  });
}
