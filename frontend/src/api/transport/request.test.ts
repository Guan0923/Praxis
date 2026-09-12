import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiErrorFrom, requestVoid } from "./request";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("requestVoid", () => {
  it("shows field validation errors", async () => {
    const error = await apiErrorFrom(new Response(JSON.stringify({
      detail: [{ loc: ["body", "url"], msg: "Invalid MCP URL" }],
    }), { status: 422 }));
    expect(error.message).toBe("body.url: Invalid MCP URL");
  });

  it("keeps the report even when detail is structured", async () => {
    const report = { type: "OSError", message: "denied", traceback: "server.py:9", winerror: 5 };
    const error = await apiErrorFrom(new Response(JSON.stringify({
      detail: { message: "generic" }, code: "broker_pipe_unavailable", error_report: report,
    }), { status: 503 }));
    expect(error.message).toBe("OSError: denied");
    expect(error.status).toBe(503);
    expect(error.code).toBe("broker_pipe_unavailable");
    expect(error.error_report).toEqual(report);
  });

  it("accepts a successful 204 response without parsing JSON", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(requestVoid("/api/example", { method: "DELETE" })).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("preserves structured errors from unsuccessful responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: "删除失败", code: "delete_failed" }),
      { status: 409, headers: { "Content-Type": "application/json" } },
    )));

    await expect(requestVoid("/api/example", { method: "DELETE" })).rejects.toEqual(
      new ApiError(409, "删除失败", "delete_failed"),
    );
  });
});
