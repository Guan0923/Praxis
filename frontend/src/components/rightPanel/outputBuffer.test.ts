import { describe, expect, it } from "vitest";
import { TerminalOutputBuffer } from "./outputBuffer";

describe("terminal output buffer", () => {
  it("keeps at most one MiB with an accurate omitted byte count", () => {
    const buffer = new TerminalOutputBuffer();
    buffer.append("H".repeat(512 * 1024));
    buffer.append("M".repeat(1024 * 1024));
    buffer.append("T".repeat(512 * 1024));
    const output = buffer.snapshot();
    expect(buffer.retainedBytes).toBe(1024 * 1024);
    expect(output.startsWith("H".repeat(100))).toBe(true);
    expect(output.endsWith("T".repeat(100))).toBe(true);
    expect(output).toContain("1048576 bytes omitted");
  });
  it("preserves unicode at split and truncation boundaries", () => {
    const buffer = new TerminalOutputBuffer();
    buffer.append("中文".repeat(200000));
    expect(buffer.snapshot()).not.toContain("\ufffd");
    expect(buffer.retainedBytes).toBeLessThanOrEqual(1024 * 1024);
  });
});



it("retains the backend omission count when replaying a bounded snapshot", () => {
  const buffer = new TerminalOutputBuffer();
  buffer.append("H".repeat(512 * 1024));
  buffer.omit(1024 * 1024);
  buffer.append("T".repeat(512 * 1024));
  expect(buffer.retainedBytes).toBe(1024 * 1024);
  expect(buffer.snapshot()).toContain("1048576 bytes omitted");
  expect(buffer.snapshot().match(/bytes omitted/g)).toHaveLength(1);
});
