import type { RightPanelPayload, RightPanelWindow, RuntimeStateNode } from "../types";
import { jsonBody, requestJson, requestVoid } from "./transport/request";
import { browserWindowGeneration, browserWindowId } from "./transport/operationControl";

export interface CreatedSideChat {
  window: RightPanelWindow;
  anchor: RuntimeStateNode;
}

export interface CreatedTerminal {
  window: RightPanelWindow;
  terminal: {
    id: string;
    terminal_type: string;
    terminal_label: string;
    cwd: string;
    last_sequence: number;
    exit_code: number | null;
    alive: boolean;
  };
}

export interface CreatedFilesWindow {
  window: RightPanelWindow;
}

const base = (sessionId: string) => `/api/right-panel/${encodeURIComponent(sessionId)}`;
const scoped = (url: string, threadId?: string) => threadId ? `${url}?thread_id=${encodeURIComponent(threadId)}` : url;

export function getRightPanel(sessionId: string, threadId?: string): Promise<RightPanelPayload> {
  return requestJson(scoped(base(sessionId), threadId));
}

export function updateRightPanel(
  sessionId: string,
  patch: Partial<Pick<RightPanelPayload["state"], "width" | "collapsed" | "active_window_id">>,
  threadId?: string,
): Promise<RightPanelPayload> {
  return requestJson(scoped(base(sessionId), threadId), { ...jsonBody(patch), method: "PATCH" });
}

export function createSideChat(sessionId: string, sourceTurnId: string, threadId?: string): Promise<CreatedSideChat> {
  return requestJson(scoped(`${base(sessionId)}/side-chats`, threadId), {
    method: "POST",
    ...jsonBody({ source_turn_id: sourceTurnId }),
  });
}

export function createPanelTerminal(sessionId: string, sourceTurnId: string, threadId?: string): Promise<CreatedTerminal> {
  return requestJson(scoped(`${base(sessionId)}/terminals`, threadId), {
    method: "POST",
    ...jsonBody({ source_turn_id: sourceTurnId }),
  });
}

export function createFilesWindow(sessionId: string, threadId?: string): Promise<CreatedFilesWindow> {
  return requestJson(scoped(`${base(sessionId)}/files`, threadId), { method: "POST" });
}

export function renameRightPanelWindow(sessionId: string, windowId: string, title: string, threadId?: string): Promise<RightPanelWindow> {
  return requestJson(scoped(`${base(sessionId)}/windows/${encodeURIComponent(windowId)}`, threadId), {
    ...jsonBody({ title }),
    method: "PATCH",
  });
}

export async function closeRightPanelWindow(sessionId: string, windowId: string, threadId?: string): Promise<void> {
  await requestVoid(scoped(`${base(sessionId)}/windows/${encodeURIComponent(windowId)}`, threadId), { method: "DELETE" });
}

export function terminalWebSocketUrl(terminalId: string, afterSequence: number): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const baseUrl = `${scheme}://${window.location.host}`;
  const params = new URLSearchParams({ after_sequence: String(afterSequence), window_id: browserWindowId() });
  const generation = browserWindowGeneration();
  if (generation !== null) params.set("generation", String(generation));
  return `${baseUrl}/api/right-panel/terminals/${encodeURIComponent(terminalId)}/ws?${params}`;
}
