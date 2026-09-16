import type { ManagedFileSource } from "../../types/files";

export interface OpenPanelFile {
  sessionId: string;
  threadId?: string;
  source: ManagedFileSource;
  path: string;
}

export function openPanelFile(target: OpenPanelFile) {
  window.dispatchEvent(new CustomEvent("praxis-open-panel-file", { detail: target }));
}

export function notifyFileSaved(sessionId: string, source: ManagedFileSource, path: string) {
  window.dispatchEvent(new CustomEvent("praxis-file-saved", { detail: { sessionId, source, path } }));
}
