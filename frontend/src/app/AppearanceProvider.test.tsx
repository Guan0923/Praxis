import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EditorView } from "codemirror";
import { undo } from "@codemirror/commands";
import { getSettings, updateAppearanceConfig } from "../api/settings";
import { AppearanceProvider } from "./AppearanceProvider";
import { AppearanceSettingsSection } from "../components/settings/AppearanceSettingsSection";
import { fallbackSettings } from "../components/settings/contracts";
import UserSettingsModal from "../components/UserSettingsModal";
import CodeEditor from "../components/rightPanel/CodeEditor";

vi.mock("../api/settings", async (importOriginal) => ({
  ...await importOriginal<typeof import("../api/settings")>(),
  getSettings: vi.fn(),
  updateAppearanceConfig: vi.fn(),
  updateProfile: vi.fn(),
}));

const profile = { display_name: "本地用户", agent_preferences: "" };
const sandboxHealth = {
  phase: "healthy" as const, installed: true, code: null, detail: null, checking: false,
  autoRecoveryPhase: "idle" as const, nextRetryAt: null, manualRepairing: false,
  check: vi.fn(), notifyUserBackendRequest: vi.fn(), repairManually: vi.fn(),
};

describe("appearance", () => {
  beforeEach(() => {
    Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
    vi.clearAllMocks();
    vi.mocked(getSettings).mockResolvedValue(fallbackSettings(profile));
    vi.mocked(updateAppearanceConfig).mockImplementation(async (mode) => ({ mode }));
  });
  afterEach(() => { cleanup(); document.documentElement.removeAttribute("data-appearance"); });

  it("waits for the saved theme before mounting the application", async () => {
    let resolve!: (value: ReturnType<typeof fallbackSettings>) => void;
    vi.mocked(getSettings).mockReturnValue(new Promise((done) => { resolve = done; }));
    render(<AppearanceProvider><div>application content</div></AppearanceProvider>);
    expect(screen.queryByText("application content")).not.toBeInTheDocument();
    await act(async () => resolve({ ...fallbackSettings(profile), appearance_config: { mode: "dark" } }));
    expect(screen.getByText("application content")).toBeInTheDocument();
    expect(document.documentElement.dataset.appearance).toBe("dark");
  });

  it("keeps the app usable with a warning when startup settings fail", async () => {
    vi.mocked(getSettings).mockRejectedValue(new Error("offline"));
    render(<AppearanceProvider><div>application content</div></AppearanceProvider>);
    expect(await screen.findByText("application content")).toBeInTheDocument();
    expect(screen.getByText(/设置读取失败/)).toBeInTheDocument();
    expect(document.documentElement.dataset.appearance).toBe("light");
  });

  it("applies immediately, disables pending changes, and rolls back a failed save", async () => {
    let reject!: (error: Error) => void;
    vi.mocked(updateAppearanceConfig).mockReturnValue(new Promise((_resolve, fail) => { reject = fail; }));
    render(<AppearanceProvider><AppearanceSettingsSection /></AppearanceProvider>);
    await userEvent.click(await screen.findByText("深色"));
    expect(document.documentElement.dataset.appearance).toBe("dark");
    expect(screen.getByRole("radio", { name: /浅色/ })).toBeDisabled();
    expect(updateAppearanceConfig).toHaveBeenCalledTimes(1);
    expect(updateAppearanceConfig).toHaveBeenCalledWith("dark");
    await act(async () => reject(new Error("offline")));
    expect(document.documentElement.dataset.appearance).toBe("light");
    expect(screen.getByText(/外观保存失败/)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /浅色/ })).not.toBeDisabled();
  });

  it("has no save footer and does not save or discard another settings draft", async () => {
    const onClose = vi.fn();
    render(<AppearanceProvider><UserSettingsModal open profile={profile} onClose={onClose}
      onProfileChange={vi.fn()} sandboxHealth={sandboxHealth} /></AppearanceProvider>);
    const name = await screen.findByRole("textbox", { name: "用户名" });
    await userEvent.clear(name);
    await userEvent.type(name, "未保存的名字");
    await userEvent.click(screen.getByRole("menuitem", { name: "外观" }));
    expect(screen.queryByRole("button", { name: "保存" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByText("深色"));
    await waitFor(() => expect(updateAppearanceConfig).toHaveBeenCalledWith("dark"));
    await userEvent.click(screen.getByRole("menuitem", { name: "个人简介" }));
    expect(screen.getByRole("textbox", { name: "用户名" })).toHaveValue("未保存的名字");
    expect(screen.getByRole("button", { name: "保存" })).toBeInTheDocument();
    const { updateProfile } = await import("../api/settings");
    expect(updateProfile).not.toHaveBeenCalled();
    expect(document.documentElement.dataset.appearance).toBe("dark");
  });

  it("changes a live editor theme without losing its document, selection or undo history", async () => {
    const onChange = vi.fn();
    const { container } = render(<AppearanceProvider><AppearanceSettingsSection />
      <CodeEditor filename="notes.txt" value="original" newline={"\n"} onChange={onChange} />
    </AppearanceProvider>);
    await waitFor(() => expect(container.querySelector(".cm-editor")).not.toBeNull());
    const element = container.querySelector<HTMLElement>(".cm-editor")!;
    const view = EditorView.findFromDOM(element)!;
    act(() => view.dispatch({ changes: { from: 8, insert: " draft" }, selection: { anchor: 11 } }));
    await userEvent.click(screen.getByText("深色"));
    expect(EditorView.findFromDOM(container.querySelector(".cm-editor")!)).toBe(view);
    expect(view.state.doc.toString()).toBe("original draft");
    expect(view.state.selection.main.anchor).toBe(11);
    expect(view.state.facet(EditorView.darkTheme)).toBe(true);
    act(() => { undo(view); });
    expect(view.state.doc.toString()).toBe("original");
  });
});
