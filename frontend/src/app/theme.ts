import { theme, type ThemeConfig } from "antd";
import type { AppearanceMode } from "../api/settings";

export const systemFontStack =
  '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif';

const light = {
  accent: "#a84421", "accent-dark": "#8c361a", "on-accent": "#ffffff",
  bg: "#ffffff", surface: "#ffffff", "sidebar-bg": "#ffffff",
  border: "#e7e7e7", "border-strong": "#c9c9c9", text: "#252525", muted: "#676767",
  disabled: "#979797", selected: "#ededed", hover: "#f3f3f3", "surface-subtle": "#f7f7f7",
  "user-bubble": "#f1f1f1", focus: "#a84421", "accent-soft": "#f6e9e1",
  "accent-border": "#dfbca9", "accent-glow": "rgba(168, 68, 33, 0.10)",
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
    accent: "#e9a07d", "accent-dark": "#f2ba9d", "on-accent": "#2a1911",
    bg: "#1c1c1a", surface: "#262623", "sidebar-bg": "#171715",
    border: "#3b3b35", "border-strong": "#59584e", text: "#efeee8", muted: "#aeaba1",
    disabled: "#78766d", selected: "#36362f", hover: "#2d2d28", "surface-subtle": "#30302a",
    "user-bubble": "#33332c", focus: "#e9a07d", "accent-soft": "#3b2b23",
    "accent-border": "#7b5743", "accent-glow": "rgba(233, 160, 125, 0.12)",
    success: "#87d9a2", "success-bg": "#233a2c", warning: "#ecc17c",
    error: "#ff9da1", "error-bg": "#40272a", "error-border": "#79464b",
    info: "#96c9ed", "info-bg": "#273640", "info-border": "#496879",
    "surface-overlay": "rgba(38, 38, 35, 0.96)",
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
    cssVar: { prefix: "praxis" },
    algorithm: mode === "dark" ? theme.darkAlgorithm : theme.defaultAlgorithm,
    token: {
      colorPrimary: p.accent, colorInfo: p.info, colorSuccess: p.success,
      colorWarning: p.warning, colorError: p.error,
      colorText: p.text, colorTextSecondary: p.muted, colorTextDescription: p.muted,
      colorTextDisabled: p.disabled, colorTextLightSolid: p["on-accent"],
      colorBgBase: p.surface, colorBgLayout: p.bg, colorBgContainer: p.surface,
      colorBgElevated: p.surface, colorBorder: p.border, colorBorderSecondary: p.border,
      controlItemBgActive: p.selected, controlItemBgHover: p.hover,
      borderRadius: 10, controlHeight: 34, fontFamily: systemFontStack,
      fontSize: 14, wireframe: false,
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
