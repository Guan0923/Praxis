const closeGuards = new Map<string, () => Promise<boolean>>();
const saveGuards = new Map<string, { sessionId: string; save: () => Promise<boolean> }>();

export function registerFilePanelSave(windowId: string, sessionId: string, save: () => Promise<boolean>): () => void {
  saveGuards.set(windowId, { sessionId, save });
  return () => { saveGuards.delete(windowId); };
}

export async function saveSessionFilePanels(sessionId: string): Promise<boolean> {
  for (const guard of saveGuards.values()) {
    if (guard.sessionId === sessionId && !await guard.save()) return false;
  }
  return true;
}

export function registerFilePanelCloseGuard(windowId: string, guard: () => Promise<boolean>): () => void {
  closeGuards.set(windowId, guard);
  return () => {
    if (closeGuards.get(windowId) === guard) closeGuards.delete(windowId);
  };
}

export async function allowFilePanelClose(windowId: string): Promise<boolean> {
  return await closeGuards.get(windowId)?.() ?? true;
}

export async function allowAllFilePanelsToLeave(): Promise<boolean> {
  for (const guard of closeGuards.values()) {
    if (!await guard()) return false;
  }
  return true;
}
