import { createContext, useContext, useEffect, useState, useSyncExternalStore } from "react";
import { ApiError, requestJson } from "../api/transport/request";
import type { FileReference } from "../types";
import type { PendingUpload } from "../pages/chat/contracts";

export interface ReadingPosition { top: number; messageId?: string | null; offset: number; atBottom: boolean }
export interface ViewState {
  session_id: string; thread_id: string; revision: number;
  draft: string; references: FileReference[]; uploads: PendingUpload[];
  reading: ReadingPosition | null; expanded: Record<string, boolean>;
}
type Patch = Partial<Pick<ViewState, "draft" | "references" | "uploads" | "reading" | "expanded">>;
interface Entry {
  value: ViewState; confirmed: ViewState; pending: Patch; sending?: Patch;
  loaded: boolean; loading?: Promise<void>; saving?: Promise<void>; timer?: ReturnType<typeof setTimeout>;
  error?: Error; listeners: Set<() => void>;
}
const entries = new Map<string, Entry>();
export const ViewStateContext = createContext("");
export const viewKey = (sessionId?: string, threadId?: string) => sessionId && threadId ? `${sessionId}/${threadId}` : "";
const empty: ViewState = { session_id: "", thread_id: "", revision: 0, draft: "", references: [], uploads: [], reading: null, expanded: {} };

function entry(key: string): Entry {
  let current = entries.get(key);
  if (!current) {
    const [session_id, thread_id] = key.split("/");
    const value = { ...empty, session_id, thread_id };
    current = { value, confirmed: value, pending: {}, loaded: false, listeners: new Set() };
    entries.set(key, current);
  }
  return current;
}
function emit(current: Entry) { for (const listener of current.listeners) listener(); }
function project(current: Entry) { current.value = { ...current.confirmed, ...current.sending, ...current.pending }; emit(current); }
function endpoint(key: string) { return `/api/view-state/${key.split("/").map(encodeURIComponent).join("/")}`; }
export const viewSnapshot = (key: string) => entry(key).value;

export function receiveView(value: ViewState) {
  const current = entry(viewKey(value.session_id, value.thread_id));
  if (current.loaded && value.revision < current.confirmed.revision) return;
  current.loaded = true;
  current.confirmed = value;
  project(current);
}
export function loadView(key: string, force = false): Promise<void> {
  if (!key) return Promise.resolve();
  const current = entry(key);
  if (current.loading) return current.loading;
  if (current.loaded && !force) return Promise.resolve();
  current.loading = requestJson<ViewState>(endpoint(key)).then((value) => {
    if (entries.get(key) === current) receiveView(value);
    current.error = undefined;
  }).catch((error) => {
    current.error = error; project(current);
    if (error instanceof ApiError && (error.status === 404 || error.status === 409)) return;
    throw error;
  })
    .finally(() => { current.loading = undefined; });
  return current.loading;
}
export function patchView(key: string, patch: Patch, delay = 500) {
  if (!key) return;
  const current = entry(key);
  current.pending = { ...current.pending, ...patch };
  project(current);
  if (delay === 1000 && current.timer) return;
  if (current.timer) clearTimeout(current.timer);
  current.timer = setTimeout(() => { current.timer = undefined; void flushView(key); }, delay);
}
export function flushView(key: string): Promise<void> {
  if (!key) return Promise.resolve();
  const current = entry(key);
  if (current.timer) clearTimeout(current.timer);
  current.timer = undefined;
  if (current.saving) return current.saving;
  current.saving = (async () => {
    while (Object.keys(current.pending).length) {
      current.sending = current.pending;
      current.pending = {};
      try {
        const value = await requestJson<ViewState>(endpoint(key), {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(current.sending), operation: { sessionId: current.value.session_id }, keepalive: true,
        });
        if (value.revision >= current.confirmed.revision) current.confirmed = value;
        current.loaded = true;
        current.error = undefined;
        current.sending = undefined;
        project(current);
      } catch (error) {
        current.pending = { ...current.sending, ...current.pending };
        current.sending = undefined;
        current.error = error instanceof Error ? error : new Error(String(error));
        project(current);
        return;
      }
    }
  })().finally(() => { current.saving = undefined; });
  return current.saving;
}
export async function reloadViews() {
  await Promise.all([...entries].filter(([key, value]) => key && value.loaded).map(([key]) => loadView(key, true)));
}
export function flushViews() { return Promise.all([...entries.keys()].map(flushView)); }
export function clearViewStateCache() {
  for (const value of entries.values()) if (value.timer) clearTimeout(value.timer);
  entries.clear();
}
export function useViewState(key: string) {
  const current = entry(key);
  const value = useSyncExternalStore((listener) => { current.listeners.add(listener); return () => { current.listeners.delete(listener); }; }, () => current.value);
  useEffect(() => {
    void loadView(key).catch(() => undefined);
    return () => { void flushView(key); };
  }, [key]);
  return { value, loaded: current.loaded, error: current.error };
}
export function useSavedExpansion(id: string, fallback = false): [boolean, (value: boolean) => void] {
  const key = useContext(ViewStateContext);
  const { value } = useViewState(key);
  const [local, setLocal] = useState(fallback);
  if (!key) return [local, setLocal];
  return [value.expanded[id] ?? fallback, (open) => {
    const current = entry(key);
    patchView(key, { expanded: { ...current.value.expanded, [id]: open } });
  }];
}
export function useSavedExpansionKeys(prefix: string): [string[], (keys: string[]) => void] {
  const key = useContext(ViewStateContext);
  const { value } = useViewState(key);
  const [local, setLocal] = useState<string[]>([]);
  if (!key) return [local, setLocal];
  return [Object.keys(value.expanded).filter((id) => id.startsWith(prefix) && value.expanded[id]), (keys) => {
    const expanded = { ...entry(key).value.expanded };
    for (const id of Object.keys(expanded)) if (id.startsWith(prefix)) delete expanded[id];
    for (const id of keys) expanded[id] = true;
    patchView(key, { expanded });
  }];
}
