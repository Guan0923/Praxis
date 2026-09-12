import { describe, expect, it, vi } from "vitest";
import { requestJson } from "../api/transport/request";
import { flushView, loadView, patchView, receiveView, useViewState, type ViewState } from "./viewState";
import { act, renderHook } from "@testing-library/react";

vi.mock("../api/transport/request", () => ({ requestJson: vi.fn() }));
const saved = (key: string, revision = 1): ViewState => ({ session_id: key, thread_id: key, revision, draft: "saved", references: [], uploads: [], reading: null, expanded: {} });

describe("backend view state", () => {
  it("retains newer typing across delayed confirmation", async () => {
    receiveView(saved("typing"));
    const { result } = renderHook(() => useViewState("typing/typing"));
    let finish!: (value: ViewState) => void;
    vi.mocked(requestJson).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    vi.mocked(requestJson).mockResolvedValueOnce({ ...saved("typing", 3), draft: "newer" });
    act(() => patchView("typing/typing", { draft: "first" }, 10000));
    let flush!: Promise<void>;
    act(() => { flush = flushView("typing/typing"); });
    act(() => patchView("typing/typing", { draft: "newer" }, 10000));
    await act(async () => { finish({ ...saved("typing", 2), draft: "first" }); await flush; });
    expect(result.current.value.draft).toBe("newer");
  });
  it("keeps failed writes in memory and reports them", async () => {
    receiveView(saved("failed"));
    const { result } = renderHook(() => useViewState("failed/failed"));
    vi.mocked(requestJson).mockRejectedValueOnce(new Error("offline"));
    act(() => patchView("failed/failed", { draft: "unsaved" }, 10000));
    await act(() => flushView("failed/failed"));
    expect(result.current.value.draft).toBe("unsaved");
    expect(result.current.error?.message).toBe("offline");
    vi.mocked(requestJson).mockResolvedValueOnce({ ...saved("failed", 2), draft: "unsaved" });
    await act(() => flushView("failed/failed"));
  });
  it("reuses loaded state without another read", async () => {
    receiveView(saved("cached"));
    vi.mocked(requestJson).mockClear();
    await loadView("cached/cached");
    expect(requestJson).not.toHaveBeenCalled();
  });
});
