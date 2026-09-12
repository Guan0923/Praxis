import { apiUrl } from "./transport/base";
import type { VersionSelection } from "./conversations/turns";
import type { ViewState } from "../app/viewState";

export type ApplicationEvent =
  | { type: "sync.reset"; reason?: "startup" | "restart" | "expired" }
  | { type: "sync.ready" | "sync.heartbeat" | "catalog.changed" }
  | { type: "session.changed" | "panel.changed"; session_id: string }
  | { type: "version.changed"; selection: VersionSelection }
  | { type: "view.changed"; view: ViewState };

export function subscribeApplicationEvents(onEvent: (event: ApplicationEvent) => Promise<void>, onStatus: (connected: boolean) => void, onError: (error: unknown) => void) {
  let source: EventSource;
  let retry: ReturnType<typeof setTimeout> | undefined;
  let disposed = false;
  let queue = Promise.resolve();
  let cursor = "";
  let lastSeen = Date.now();
  const connect = () => {
    lastSeen = Date.now();
    const stream = new EventSource(apiUrl(`/api/application-events${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`));
    source = stream;
    stream.onerror = () => onStatus(false);
    stream.onmessage = (message) => {
      lastSeen = Date.now();
      queue = queue.then(async () => {
        if (disposed || source !== stream || stream.readyState === EventSource.CLOSED) return;
        const event = JSON.parse(message.data) as ApplicationEvent;
        await onEvent(event);
        if (disposed || source !== stream) return;
        cursor = message.lastEventId;
        if (event.type === "sync.ready" && source.readyState === EventSource.OPEN) onStatus(true);
      }).catch((error) => {
        if (disposed || source !== stream) return;
        onStatus(false); onError(error);
        stream.close();
        cursor = "";
        // A failed snapshot must not advance the observation cursor.
        retry = setTimeout(connect, 1000);
      });
    };
  };
  connect();
  const watchdog = setInterval(() => {
    if (Date.now() - lastSeen < 6000) return;
    onStatus(false);
    source.close();
    if (retry) clearTimeout(retry);
    connect();
  }, 2000);
  return () => { disposed = true; clearInterval(watchdog); if (retry) clearTimeout(retry); source.close(); };
}
