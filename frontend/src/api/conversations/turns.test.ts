import { afterEach, describe, expect, it, vi } from "vitest";
import { requestJson } from "../transport/request";
import { getTurnTrace } from "./turns";

vi.mock("../transport/request", () => ({ requestJson: vi.fn() }));

afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("getTurnTrace", () => {
  it("addresses one Session Thread Turn version directly", async () => {
    vi.mocked(requestJson).mockResolvedValue({ context: null, items: [], last_sequence: 0 });
    await getTurnTrace("session-a", "thread-a", "turn-a", 2, undefined, 7);
    expect(requestJson).toHaveBeenCalledWith(
      "/api/turns/turn-a/trace?session_id=session-a&thread_id=thread-a&data_idx=2&after_sequence=7",
      { signal: expect.any(AbortSignal) },
    );
  });

  it("fails a request that never returns instead of loading forever", async () => {
    vi.useFakeTimers();
    vi.mocked(requestJson).mockImplementation(
      (_url, init) => new Promise((_resolve, reject) => {
        init?.signal?.addEventListener(
          "abort",
          () => reject(new DOMException("aborted", "AbortError")),
          { once: true },
        );
      }),
    );
    const request = getTurnTrace("session-a", "thread-a", "turn-a", 0);
    const rejection = expect(request).rejects.toThrow("Trace 请求超时。");
    await vi.advanceTimersByTimeAsync(10_000);
    await rejection;
  });
});
