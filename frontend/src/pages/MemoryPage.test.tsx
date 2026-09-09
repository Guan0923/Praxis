import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import MemorySettingsSection from "./MemoryPage";

const mocks = vi.hoisted(() => ({
  cancelMemoryJob: vi.fn(),
  clearMemories: vi.fn(),
  consolidateMemory: vi.fn(),
  deleteMemory: vi.fn(),
  dryRunMemory: vi.fn(),
  extractMemory: vi.fn(),
  getSettings: vi.fn(),
  listMemoryEvidence: vi.fn(),
  listMemoryInjectionHistory: vi.fn(),
  listMemoryItems: vi.fn(),
  listMemoryJobs: vi.fn(),
  listSidebarThreads: vi.fn(),
  restoreMemory: vi.fn(),
  setMemoryEnabled: vi.fn(),
  updateMemoryConfig: vi.fn(),
}));

vi.mock("../api", () => mocks);

const config = {
  enabled: false,
  disable_on_external_context: true,
  extraction_model: "",
  consolidation_model: "",
  retrieval_limit: 40,
  injection_max_items: 8,
  injection_max_tokens: 1200,
  injection_max_bytes: 8192,
};

const item = {
  memory_id: "memory_a",
  kind: "semantic",
  title: "Concise reports",
  content: "The user prefers concise technical reports.",
  summary: "Concise",
  scope: "global",
  project_id: null,
  confidence: 0.9,
  tags: ["preference"],
  status: "active",
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
  deleted_at: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  mocks.getSettings.mockResolvedValue({ memory_config: config });
  mocks.listMemoryItems.mockResolvedValue([item]);
  mocks.listMemoryJobs.mockResolvedValue([]);
  mocks.listSidebarThreads.mockResolvedValue([]);
  mocks.listMemoryInjectionHistory.mockResolvedValue([]);
  mocks.listMemoryEvidence.mockResolvedValue([]);
  mocks.updateMemoryConfig.mockImplementation(async (value) => value);
  mocks.setMemoryEnabled.mockResolvedValue({ ...item, status: "disabled" });
  mocks.clearMemories.mockResolvedValue(undefined);
});

describe("Memory settings management", () => {
  afterEach(() => cleanup());

  it("loads memory state and updates the single switch", async () => {
    const user = userEvent.setup();
    render(<MemorySettingsSection />);

    expect(await screen.findByText("Concise reports")).toBeInTheDocument();
    const memorySwitch = screen.getByRole("switch", { name: "启用记忆" });
    await user.click(memorySwitch);
    await waitFor(() => expect(mocks.updateMemoryConfig).toHaveBeenCalledWith({ ...config, enabled: true }));
  });

  it("requires the exact confirmation before clearing", async () => {
    const user = userEvent.setup();
    render(<MemorySettingsSection />);
    await screen.findByText("Concise reports");

    await user.click(screen.getByRole("button", { name: /清空全部 Memory/ }));
    const confirm = screen.getByRole("button", { name: "永久清空" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "清空确认文字" }), "CLEAR ALL MEMORIES");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() => expect(mocks.clearMemories).toHaveBeenCalledWith("CLEAR ALL MEMORIES"));
  });

  it("renders project-scoped memory returned by the management API", async () => {
    mocks.listMemoryItems.mockResolvedValue([{ ...item, memory_id: "memory_project", title: "Project preference", scope: "project", project_id: "project_a" }]);
    render(<MemorySettingsSection />);

    expect(await screen.findByText("Project preference")).toBeInTheDocument();
    expect(screen.getByText("项目 project_a")).toBeInTheDocument();
  });
});
