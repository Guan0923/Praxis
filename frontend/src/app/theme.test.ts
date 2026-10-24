import { describe, expect, it } from "vitest";
import { appearanceTheme, palettes, terminalTheme } from "./theme";

function luminance(color: string) {
  const channels = color.slice(1).match(/.{2}/g)!.map((hex) => {
    const value = parseInt(hex, 16) / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}

function contrast(first: string, second: string) {
  const values = [luminance(first), luminance(second)].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
}

describe("workbench theme", () => {
  it("uses pure white light surfaces and neutral grays without a yellow tint", () => {
    expect(palettes.light.bg).toBe("#ffffff");
    expect(palettes.light.surface).toBe("#ffffff");
    expect(palettes.light["sidebar-bg"]).toBe("#ffffff");
    for (const token of ["border", "border-strong", "text", "muted", "selected", "hover", "surface-subtle", "user-bubble"] as const) {
      const channels = palettes.light[token].slice(1).match(/.{2}/g)!;
      expect(new Set(channels).size).toBe(1);
    }
  });
  for (const mode of ["light", "dark"] as const) {
    it(`keeps ${mode} UI and terminal tokens aligned`, () => {
      const palette = palettes[mode];
      const config = appearanceTheme(mode);
      expect(config.token?.colorPrimary).toBe(palette.accent);
      expect(config.token?.colorBgContainer).toBe(palette.surface);
      expect(config.token?.colorText).toBe(palette.text);
      expect(terminalTheme(mode)).toMatchObject({ background: palette.surface, foreground: palette.text, cursor: palette.accent });
    });

    it(`keeps ${mode} primary and secondary text readable`, () => {
      const palette = palettes[mode];
      for (const background of [palette.surface, palette["sidebar-bg"], palette["user-bubble"]]) {
        expect(contrast(palette.text, background)).toBeGreaterThanOrEqual(4.5);
        expect(contrast(palette.muted, background)).toBeGreaterThanOrEqual(4.5);
      }
      expect(contrast(palette["on-accent"], palette.accent)).toBeGreaterThanOrEqual(4.5);
    });
  }
});
