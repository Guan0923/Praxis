import { App, Grid } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

const panelWindow = {
  id: "window-files",
  session_id: "session",
  kind: "files" as const,
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
};

function renderPane() {
  return render(
    <App>
      <FilesPane active panelWindow={panelWindow} />
    </App>,
  );
}

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
  renderPane();

  expect(await screen.findByText("workspace", { exact: true })).toBeInTheDocument();
  expect(screen.getByText("project", { exact: true })).toBeInTheDocument();
});

it("expands directories by clicking the row and keeps icons beside names", async () => {
  const longDirectoryName = "directory-with-a-name-that-overflows-the-file-panel";
  api.listFileDirectory.mockImplementation(async (_sessionId, _source, path) => path === "workspace:"
    ? [{
        source: "workspace",
        path: `workspace:${longDirectoryName}`,
        name: longDirectoryName,
        kind: "directory",
        size: null,
        mtime: "2026-09-06T00:00:00Z",
        mime: null,
        is_image: false,
        version: null,
      }]
    : []);
  renderPane();

  const workspace = await screen.findByText("workspace", { exact: true });
  fireEvent.click(workspace);

  const directory = await screen.findByText(longDirectoryName, { exact: true });
  expect(api.listFileDirectory).toHaveBeenCalledWith("session", "workspace", "workspace:");
  const content = directory.closest(".ant-tree-node-content-wrapper");
  expect(content).not.toBeNull();
  expect(content?.querySelector(".ant-tree-iconEle")).not.toBeNull();
  expect(content?.querySelector(".file-tree-name")).not.toBeNull();

  const item = directory.closest('[role="treeitem"]');
  fireEvent.click(directory);
  await waitFor(() => expect(item).toHaveAttribute("aria-expanded", "true"));
  expect(api.listFileDirectory).toHaveBeenCalledWith(
    "session",
    "workspace",
    `workspace:${longDirectoryName}`,
  );

  fireEvent.click(directory);
  await waitFor(() => expect(item).toHaveAttribute("aria-expanded", "false"));
});
