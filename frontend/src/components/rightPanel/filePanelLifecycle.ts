const closeGuards = new Map<string, () => Promise<boolean>>();

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
