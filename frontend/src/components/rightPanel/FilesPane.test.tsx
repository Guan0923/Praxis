import { App, Grid } from "antd";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

it("expands directories from the row or switcher and keeps file switcher spacing", async () => {
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
      }, {
        source: "workspace",
        path: "workspace:note.txt",
        name: "note.txt",
        kind: "file",
        size: 4,
        mtime: "2026-09-06T00:00:00Z",
        mime: "text/plain",
        is_image: false,
        version: "1",
      }]
    : []);
  renderPane();

  const workspace = await screen.findByText("workspace", { exact: true });
  const workspaceItem = workspace.closest('[role="treeitem"]');
  const workspaceSwitcher = workspaceItem?.querySelector(".ant-tree-switcher");
  expect(workspaceSwitcher).toHaveClass("ant-tree-switcher_close");
  fireEvent.click(workspaceSwitcher!);

  const directory = await screen.findByText(longDirectoryName, { exact: true });
  expect(api.listFileDirectory).toHaveBeenCalledWith("session", "workspace", "workspace:");
  expect(workspaceSwitcher).toHaveClass("ant-tree-switcher_open");
  const content = directory.closest(".ant-tree-node-content-wrapper");
  expect(content).not.toBeNull();
  expect(content?.querySelector(".ant-tree-iconEle")).not.toBeNull();
  expect(content?.querySelector(".file-tree-name")).not.toBeNull();
  const file = screen.getByText("note.txt", { exact: true });
  const fileSwitcher = file.closest('[role="treeitem"]')?.querySelector(".ant-tree-switcher");
  expect(fileSwitcher).toHaveClass("ant-tree-switcher-noop");
  expect(fileSwitcher?.querySelector("svg")).toBeNull();

  const item = directory.closest('[role="treeitem"]');
  fireEvent.click(directory);
  await waitFor(() => expect(item).toHaveAttribute("aria-expanded", "true"));
  expect(api.listFileDirectory).toHaveBeenCalledWith(
    "session",
    "workspace",
    `workspace:${longDirectoryName}`,
  );

  const directorySwitcher = item?.querySelector(".ant-tree-switcher");
  expect(directorySwitcher).toHaveClass("ant-tree-switcher_open");
  fireEvent.click(directorySwitcher!);
  await waitFor(() => expect(item).toHaveAttribute("aria-expanded", "false"));
  fireEvent.click(directorySwitcher!);
  await waitFor(() => expect(item).toHaveAttribute("aria-expanded", "true"));
  expect(api.listFileDirectory.mock.calls.filter((call) => call[2] === `workspace:${longDirectoryName}`)).toHaveLength(1);
});

it("selects a move destination when its folder switcher is clicked", async () => {
  api.listFileDirectory.mockImplementation(async (_sessionId, _source, path) => path === "workspace:"
    ? [{
        source: "workspace",
        path: "workspace:note.txt",
        name: "note.txt",
        kind: "file",
        size: 4,
        mtime: "2026-09-06T00:00:00Z",
        mime: "text/plain",
        is_image: false,
        version: "1",
      }, {
        source: "workspace",
        path: "workspace:target-dir",
        name: "target-dir",
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
  const file = await screen.findByText("note.txt", { exact: true });
  fireEvent.contextMenu(file);
  fireEvent.click(await screen.findByText("移动到", { exact: true }));

  const dialog = await screen.findByRole("dialog", { name: "移动到" });
  const picker = within(dialog).getByRole("tree", { name: "移动目标目录" });
  const pickerWorkspace = within(picker).getByText("workspace", { exact: true });
  fireEvent.click(pickerWorkspace.closest('[role="treeitem"]')!.querySelector(".ant-tree-switcher")!);

  const target = await within(picker).findByText("target-dir", { exact: true });
  const targetItem = target.closest('[role="treeitem"]')!;
  fireEvent.click(targetItem.querySelector(".ant-tree-switcher")!);

  await waitFor(() => expect(target.closest(".ant-tree-node-content-wrapper")).toHaveClass("ant-tree-node-selected"));
  expect(within(dialog).getByRole("button", { name: "OK" })).toBeEnabled();
});
