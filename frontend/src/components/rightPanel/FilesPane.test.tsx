import { App, Grid } from "antd";
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import FilesPane from "./FilesPane";

const api = vi.hoisted(() => ({
  getFileRoots: vi.fn(),
  listFileDirectory: vi.fn(),
  readEditorFile: vi.fn(),
  saveEditorFile: vi.fn(),
  createFileEntry: vi.fn(),
  renameFileEntry: vi.fn(),
  moveFileEntry: vi.fn(),
  recycleFileEntry: vi.fn(),
  sessionFileContentUrl: vi.fn(() => "/content"),
}));

vi.mock("../../api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api")>(),
  ...api,
}));

vi.mock("./CodeEditor", () => ({ default: () => <div>editor</div> }));

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(Grid, "useBreakpoint").mockReturnValue({ md: true } as ReturnType<typeof Grid.useBreakpoint>);
  api.getFileRoots.mockResolvedValue([
    { source: "workspace", path: "workspace:", name: "workspace", available: true },
    { source: "project", path: "project:", name: "project", available: false },
  ]);
  api.listFileDirectory.mockResolvedValue([]);
});

afterEach(() => vi.restoreAllMocks());

it("shows workspace and unavailable project roots", async () => {
  render(
    <App>
      <FilesPane active panelWindow={{
        id: "window-files",
        session_id: "session",
        kind: "files",
        title: "文件",
        position: 0,
        created_at: "2026-09-05T00:00:00Z",
        updated_at: "2026-09-05T00:00:00Z",
        thread_id: null,
        anchor_turn_id: null,
        terminal_id: null,
        terminal_type: null,
        cwd: null,
        deleted_at: null,
      }} />
    </App>,
  );

  expect(await screen.findByText("workspace", { exact: true })).toBeInTheDocument();
  expect(screen.getByText("project", { exact: true })).toBeInTheDocument();
});
