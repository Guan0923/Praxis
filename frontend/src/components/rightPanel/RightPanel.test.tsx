import { App } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useEffect, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RightPanelPayload, RightPanelWindow } from "../../types";
import RightPanel, { RightPanelLauncher, type RightPanelController, useRightPanel } from "./RightPanel";

const api = vi.hoisted(() => ({
  getRightPanel: vi.fn(),
  updateRightPanel: vi.fn(),
  createSideChat: vi.fn(),
  createPanelTerminal: vi.fn(),
  createFilesWindow: vi.fn(),
  renameRightPanelWindow: vi.fn(),
  closeRightPanelWindow: vi.fn(),
}));

vi.mock("../../api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api")>(),
  ...api,
}));

const sideWindow = (id: string, title: string): RightPanelWindow => ({
  id,
  session_id: "session",
  kind: "side_chat",
  title,
  position: Number(id.slice(-1)) || 0,
  created_at: "2026-08-29T00:00:00Z",
  updated_at: "2026-08-29T00:00:00Z",
  thread_id: `thread-${id}`,
  anchor_turn_id: `anchor-${id}`,
  terminal_id: null,
  terminal_type: null,
  cwd: null,
  deleted_at: null,
});

const payload = (windows: RightPanelWindow[], collapsed = false): RightPanelPayload => ({
  state: {
    session_id: "session",
    width: 420,
    collapsed,
    active_window_id: windows[0]?.id ?? null,
  },
  windows,
  capabilities: {
    terminal_available: true,
    terminal_unavailable_reason: null,
  },
});

function StatefulPanel({ initial }: { initial: RightPanelPayload }) {
  const [current, setCurrent] = useState(initial);
  const controller: RightPanelController = {
    payload: current,
    loading: false,
    createWindow: vi.fn(),
    closeWindow: async (target) => setCurrent((value) => ({
      ...value,
      state: {
        ...value.state,
        active_window_id: value.windows.find((item) => item.id !== target.id)?.id ?? null,
      },
      windows: value.windows.filter((item) => item.id !== target.id),
    })),
    renameWindow: vi.fn(),
    setActive: (id) => setCurrent((value) => ({ ...value, state: { ...value.state, active_window_id: id } })),
    setLayout: (patch) => setCurrent((value) => ({ ...value, state: { ...value.state, ...patch } })),
  };
  return (
    <App>
      <RightPanel
        controller={controller}
        sourceAvailable
        terminalAvailable
        terminalReason=""
        renderSideChat={(item) => <div data-testid={`pane-${item.id}`}>{item.title}</div>}
      />
      <output data-testid="collapsed">{String(current.state.collapsed)}</output>
    </App>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("RightPanel tabs", () => {
  it("removes the top file shortcut while keeping the plus-menu file entry and tab hint", async () => {
    const controller: RightPanelController = {
      payload: payload([sideWindow("window-1", "Side")]), loading: false,
      createWindow: vi.fn().mockResolvedValue(undefined), closeWindow: vi.fn(),
      renameWindow: vi.fn(), setActive: vi.fn(), setLayout: vi.fn(),
    };
    render(<App><RightPanel controller={controller} sourceAvailable terminalAvailable terminalReason="" renderSideChat={() => null} /></App>);
    expect(screen.queryByRole("button", { name: "打开文件" })).not.toBeInTheDocument();
    fireEvent.mouseEnter(screen.getByText("Side"));
    expect(await screen.findByRole("tooltip")).toHaveTextContent("双击重命名");
    fireEvent.mouseLeave(screen.getByText("Side"));
    fireEvent.click(screen.getByRole("button", { name: "新增右栏窗口" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: /文件$/ }));
    expect(controller.createWindow).toHaveBeenCalledWith("files");
  });
  it("opens the panel directly from the main launcher without creating a window", () => {
    const controller: RightPanelController = {
      payload: payload([], true),
      loading: false,
      createWindow: vi.fn(),
      closeWindow: vi.fn(),
      renameWindow: vi.fn(),
      setActive: vi.fn(),
      setLayout: vi.fn(),
    };
    render(<RightPanelLauncher controller={controller} />);

    fireEvent.click(screen.getByRole("button", { name: "打开右侧边栏" }));

    expect(controller.setLayout).toHaveBeenCalledWith({ collapsed: false });
    expect(controller.createWindow).not.toHaveBeenCalled();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("keeps the panel open and shows both choices after closing the last tab", async () => {
    const { container } = render(<StatefulPanel initial={payload([sideWindow("window-1", "侧聊 1")])} />);

    fireEvent.click(container.querySelector(".ant-tabs-tab-remove") as HTMLElement);

    expect(await screen.findByRole("button", { name: /创建侧边聊天/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /打开终端/ })).toBeEnabled();
    expect(screen.getByTestId("collapsed")).toHaveTextContent("false");
  });

  it("applies full-height semantic styles to the Ant Design 6 tab body and content", () => {
    const { container } = render(<StatefulPanel initial={payload([sideWindow("window-1", "侧聊 1")])} />);

    expect(container.querySelector<HTMLElement>(".ant-tabs-body")).toHaveStyle({ height: "100%", minHeight: "0" });
    expect(container.querySelector<HTMLElement>(".ant-tabs-content")).toHaveStyle({ height: "100%", minHeight: "0" });
  });

  it("force-renders every tab and does not unmount inactive content", () => {
    const unmounted: string[] = [];
    function Pane({ id }: { id: string }) {
      useEffect(() => () => { unmounted.push(id); }, [id]);
      return <div data-testid={`mounted-${id}`}>{id}</div>;
    }
    const first = sideWindow("window-1", "侧聊 1");
    const second = sideWindow("window-2", "侧聊 2");
    const current = payload([first, second]);
    const controller: RightPanelController = {
      payload: current,
      loading: false,
      createWindow: vi.fn(),
      closeWindow: vi.fn(),
      renameWindow: vi.fn(),
      setActive: (id) => { current.state.active_window_id = id; },
      setLayout: vi.fn(),
    };
    render(
      <App>
        <RightPanel
          controller={controller}
          sourceAvailable
          terminalAvailable
          terminalReason=""
          renderSideChat={(item) => <Pane id={item.id} />}
        />
      </App>,
    );

    expect(screen.getByTestId("mounted-window-1")).toBeInTheDocument();
    expect(screen.getByTestId("mounted-window-2")).toBeInTheDocument();
    fireEvent.click(screen.getByText("侧聊 2"));
    expect(unmounted).toEqual([]);
  });

  it("explains why creation is disabled without a current Turn", () => {
    const controller: RightPanelController = {
      payload: payload([]),
      loading: false,
      createWindow: vi.fn(),
      closeWindow: vi.fn(),
      renameWindow: vi.fn(),
      setActive: vi.fn(),
      setLayout: vi.fn(),
    };
    render(
      <App>
        <RightPanel
          controller={controller}
          sourceAvailable={false}
          terminalAvailable={false}
          terminalReason="当前没有可用 Turn"
          renderSideChat={() => null}
        />
      </App>,
    );

    expect(screen.getByRole("button", { name: /创建侧边聊天/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /打开终端/ })).toBeDisabled();
    expect(screen.getByText(/当前主聊天没有可用 Turn/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "打开文件" })).toBeEnabled();
  });

  it("opens the file window without requiring a current Turn", () => {
    const controller: RightPanelController = {
      payload: payload([]),
      loading: false,
      createWindow: vi.fn().mockResolvedValue(undefined),
      closeWindow: vi.fn(),
      renameWindow: vi.fn(),
      setActive: vi.fn(),
      setLayout: vi.fn(),
    };
    render(
      <App>
        <RightPanel
          controller={controller}
          sourceAvailable={false}
          terminalAvailable={false}
          terminalReason="当前没有可用 Turn"
          renderSideChat={() => null}
        />
      </App>,
    );

    fireEvent.click(screen.getByRole("button", { name: "打开文件" }));
    expect(controller.createWindow).toHaveBeenCalledWith("files");
  });
});

function HookHarness({ sessionId, threadId }: { sessionId: string; threadId?: string }) {
  const controller = useRightPanel(sessionId, "turn-main", vi.fn(), vi.fn(), threadId);
  return <output>{controller.payload?.state.session_id ?? "loading"}</output>;
}

it("hides the previous panel immediately when switching between conversations in one Session", async () => {
  api.getRightPanel.mockResolvedValueOnce({ ...payload([]), state: { ...payload([]).state, session_id: "main-panel" } });
  const { rerender } = render(<HookHarness sessionId="session" threadId="main" />);
  await screen.findByText("main-panel");
  api.getRightPanel.mockReturnValueOnce(new Promise(() => {}));
  rerender(<HookHarness sessionId="session" threadId="fork" />);
  expect(screen.queryByText("main-panel")).not.toBeInTheDocument();
  expect(api.getRightPanel).toHaveBeenLastCalledWith("session", "fork");
});

function LauncherHookHarness({ sessionId }: { sessionId: string }) {
  const controller = useRightPanel(sessionId, undefined, vi.fn(), vi.fn());
  return (
    <>
      <RightPanelLauncher controller={controller} />
      <output data-testid="panel-state">{controller.payload ? String(controller.payload.state.collapsed) : "loading"}</output>
    </>
  );
}

it("reloads the canonical layout when the main Session changes", async () => {
  api.getRightPanel.mockImplementation(async (sessionId: string) => ({
    ...payload([]),
    state: { ...payload([]).state, session_id: sessionId, width: sessionId === "session-a" ? 420 : 700 },
  }));
  const { rerender } = render(<HookHarness sessionId="session-a" />);
  expect(await screen.findByText("session-a")).toBeInTheDocument();

  rerender(<HookHarness sessionId="session-b" />);
  expect(await screen.findByText("session-b")).toBeInTheDocument();
  await waitFor(() => expect(api.getRightPanel).toHaveBeenCalledWith("session-b", undefined));
});

it("keeps a launcher click made while the initial layout is still loading", async () => {
  let resolveInitial!: (value: RightPanelPayload) => void;
  api.getRightPanel.mockReturnValueOnce(new Promise((resolve) => { resolveInitial = resolve; }));
  api.updateRightPanel.mockResolvedValue(payload([], false));
  render(<LauncherHookHarness sessionId="session" />);

  fireEvent.click(screen.getByRole("button", { name: "打开右侧边栏" }));
  resolveInitial(payload([], true));

  expect(await screen.findByTestId("panel-state")).toHaveTextContent("false");
  expect(api.updateRightPanel).toHaveBeenCalledWith("session", { collapsed: false }, undefined);
});
