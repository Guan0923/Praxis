import { theme, type ThemeConfig } from "antd";
import type { AppearanceMode } from "../api/settings";

export const systemFontStack =
  '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif';

const light = {
  accent: "#262626", "accent-dark": "#444444", "on-accent": "#ffffff",
  bg: "#f7f7f7", surface: "#ffffff", "sidebar-bg": "#f7f7f7",
  border: "#e3e3e3", "border-strong": "#c8c8c8", text: "#242424", muted: "#707070",
  disabled: "#999999", selected: "#eaeaea", hover: "#f0f0f0", "surface-subtle": "#f5f5f5",
  "user-bubble": "#f1f1f1", focus: "#595959", "accent-soft": "#ededed",
  "accent-border": "#b7b7b7", "accent-glow": "rgba(38, 38, 38, 0.16)",
  success: "#16803d", "success-bg": "#edf8f0", warning: "#a96408",
  error: "#c52b32", "error-bg": "#fff1f0", "error-border": "#efb7b9",
  info: "#246899", "info-bg": "#edf5fa", "info-border": "#b5d3e6",
  "surface-overlay": "rgba(255, 255, 255, 0.94)",
  shadow: "rgba(0, 0, 0, 0.12)", "shadow-soft": "rgba(0, 0, 0, 0.06)",
  "shadow-strong": "rgba(0, 0, 0, 0.16)", "shimmer-highlight": "#ffffff",
};

export const palettes: Record<AppearanceMode, typeof light> = {
  light,
  dark: {
    accent: "#aad9bc", "accent-dark": "#c2e8cf", "on-accent": "#18271d",
    bg: "#191a1b", surface: "#202122", "sidebar-bg": "#191a1b",
    border: "#383a3b", "border-strong": "#535657", text: "#e5e5e5", muted: "#a3a5a6",
    disabled: "#737779", selected: "#303334", hover: "#2b2d2e", "surface-subtle": "#27292a",
    "user-bubble": "#2e3031", focus: "#aad9bc", "accent-soft": "#293c30",
    "accent-border": "#587761", "accent-glow": "rgba(170, 217, 188, 0.18)",
    success: "#87d9a2", "success-bg": "#233a2c", warning: "#ecc17c",
    error: "#ff9da1", "error-bg": "#40272a", "error-border": "#79464b",
    info: "#96c9ed", "info-bg": "#273640", "info-border": "#496879",
    "surface-overlay": "rgba(32, 33, 34, 0.96)",
    shadow: "rgba(0, 0, 0, 0.28)", "shadow-soft": "rgba(0, 0, 0, 0.18)",
    "shadow-strong": "rgba(0, 0, 0, 0.4)", "shimmer-highlight": "#e5e5e5",
  },
};

export function applyAppearance(mode: AppearanceMode) {
  const root = document.documentElement;
  root.dataset.appearance = mode;
  root.style.colorScheme = mode;
  for (const [name, value] of Object.entries(palettes[mode])) root.style.setProperty(`--${name}`, value);
}

export function appearanceTheme(mode: AppearanceMode): ThemeConfig {
  const p = palettes[mode];
  return {
    cssVar: { prefix: "mini-agent" },
    algorithm: mode === "dark" ? theme.darkAlgorithm : theme.defaultAlgorithm,
    token: {
      colorPrimary: p.accent, colorInfo: p.info, colorSuccess: p.success,
      colorWarning: p.warning, colorError: p.error,
      colorText: p.text, colorTextSecondary: p.muted, colorTextDescription: p.muted,
      colorTextDisabled: p.disabled, colorTextLightSolid: p["on-accent"],
      colorBgBase: p.surface, colorBgLayout: p.bg, colorBgContainer: p.surface,
      colorBgElevated: p.surface, colorBorder: p.border, colorBorderSecondary: p.border,
      controlItemBgActive: p.selected, controlItemBgHover: p.hover,
      borderRadius: 10, fontFamily: systemFontStack,
    },
  };
}

export function terminalTheme(mode: AppearanceMode) {
  const p = palettes[mode];
  return {
    background: p.surface, foreground: p.text, cursor: p.accent, cursorAccent: p["on-accent"],
    selectionBackground: p.selected, selectionForeground: p.text,
    black: mode === "dark" ? "#898d8f" : "#242424", brightBlack: p.muted,
    red: p.error, brightRed: p.error, green: p.success, brightGreen: p.success,
    yellow: p.warning, brightYellow: p.warning, blue: p.info, brightBlue: p.info,
    magenta: mode === "dark" ? "#d9afea" : "#88439e",
    brightMagenta: mode === "dark" ? "#e6c7f2" : "#743b85",
    cyan: mode === "dark" ? "#8bd3db" : "#236e78",
    brightCyan: mode === "dark" ? "#ace3e8" : "#256571",
    white: mode === "dark" ? "#cccccc" : "#555555", brightWhite: p.text,
  };
}
