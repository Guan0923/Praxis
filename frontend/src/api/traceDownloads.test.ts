import { describe, expect, it } from "vitest";
import { benchmarkTraceDownloadUrl } from "./benchmarks";
import { threadTraceDownloadUrl } from "./conversations/turns";

describe("trace download URLs", () => {
  it("encodes thread query values without adding a version or cursor", () => {
    const url = new URL(threadTraceDownloadUrl("session & one", "thread/中文"), "http://localhost");
    expect([...url.searchParams]).toEqual([["session_id", "session & one"], ["thread_id", "thread/中文"]]);
  });

  it("encodes benchmark path segments", () => {
    expect(benchmarkTraceDownloadUrl("run/one", "test?#"))
      .toBe("/benchmark/runs/run%2Fone/tasks/test%3F%23/trace/export");
  });
});
