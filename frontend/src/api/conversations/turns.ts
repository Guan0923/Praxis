import { isRuntimeTurnNode, normalizeRuntimeNode } from "../../app/runtime/runtimeNodeNormalization";
import type { RuntimeStateNode, RuntimeTreeNode, SidebarThread, TurnTraceResponse } from "../../types";
import { requestJson } from "../transport/request";
import { versionAfterRead, versionReadClock } from "./versionClock";

export interface TurnPage {
  turns: RuntimeTreeNode[];
  next_cursor: string | null;
  has_more: boolean;
}

export async function getTurnPage(sessionId: string, threadId = sessionId, before?: string): Promise<TurnPage> {
  const started = versionReadClock();
  const query = new URLSearchParams({ session_id: sessionId, thread_id: threadId, limit: "5" });
  if (before) query.set("before", before);
  const page = await requestJson<TurnPage>(`/api/turns/history?${query.toString()}`);
  return { ...page, turns: page.turns.map((value) => {
    const turn = normalizeRuntimeNode(value);
    const index = versionAfterRead(turn.session_id, turn.id, started);
    return index !== undefined && isRuntimeTurnNode(turn) && turn.data[index] ? { ...turn, current_data_idx: index } : turn;
  }) };
}

export async function listTurns(sessionId: string, threadId = sessionId): Promise<RuntimeTreeNode[]> {
  return (await getTurnPage(sessionId, threadId)).turns;
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

export interface VersionSelection {
  id: string;
  session_id: string;
  thread_id: string;
  current_data_idx: number;
  revision: number;
}

export async function patchTurnCurrentData(
  turnId: string,
  currentDataIdx: number,
  sessionId?: string,
): Promise<VersionSelection> {
  return requestJson(`/api/turns/${encodeURIComponent(turnId)}/current-data`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, current_data_idx: currentDataIdx }),
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
