import { describe, expect, it } from "vitest";
import { effectiveDisplayMode } from "./displayMode";

describe("display modes", () => {
  it.each(["minimal", "medium", "verbose"] as const)("preserves %s", (mode) => {
    expect(effectiveDisplayMode(mode)).toBe(mode);
  });
});
