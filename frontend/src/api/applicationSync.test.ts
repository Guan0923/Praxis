import { afterEach, describe, expect, it, vi } from "vitest";
import { subscribeApplicationEvents } from "./applicationSync";

class Stream {
  static OPEN = 1;
  static CLOSED = 2;
  static instances: Stream[] = [];
  readyState = Stream.OPEN;
  onmessage?: (message: { data: string; lastEventId: string }) => void;
  onerror?: () => void;
  constructor(readonly url: string) { Stream.instances.push(this); }
  close() { this.readyState = Stream.CLOSED; }
  emit(type: string, cursor: string) { this.onmessage?.({ data: JSON.stringify({ type }), lastEventId: cursor }); }
}

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); Stream.instances = []; });

describe("application synchronization", () => {
  it("marks silent disconnects and resumes from the last processed position", async () => {
    vi.useFakeTimers(); vi.stubGlobal("EventSource", Stream);
    const status = vi.fn();
    const stop = subscribeApplicationEvents(async () => {}, status, vi.fn());
    Stream.instances[0].emit("sync.ready", "epoch:4");
    await vi.advanceTimersByTimeAsync(0);
    expect(status).toHaveBeenLastCalledWith(true);
    await vi.advanceTimersByTimeAsync(6000);
    expect(status).toHaveBeenLastCalledWith(false);
    expect(Stream.instances[1].url).toContain("cursor=epoch%3A4");
    stop();
  });

  it("does not confirm synchronization when applying an event fails", async () => {
    vi.useFakeTimers(); vi.stubGlobal("EventSource", Stream);
    const status = vi.fn();
    const error = vi.fn();
    const stop = subscribeApplicationEvents(async () => { throw new Error("snapshot failed"); }, status, error);
    Stream.instances[0].emit("sync.reset", "epoch:5");
    Stream.instances[0].emit("sync.ready", "epoch:5");
    await vi.advanceTimersByTimeAsync(1000);
    expect(error).toHaveBeenCalledOnce();
    expect(status).not.toHaveBeenCalledWith(true);
    expect(Stream.instances[1].url).not.toContain("cursor=");
    stop();
  });
});
