import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import BenchmarkPage from "./BenchmarkPage";
import type { BenchmarkRun, BenchmarkRuns, TaskInfo } from "../types";

const mocks = vi.hoisted(() => ({
  listTasks: vi.fn(),
  runBenchmark: vi.fn(),
  runAllBenchmark: vi.fn(),
  listBenchmarkRuns: vi.fn(),
  cancelBenchmark: vi.fn(),
  listBenchmarkResources: vi.fn(),
  changeBenchmarkResources: vi.fn(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../api")>(),
  ...mocks,
}));

const task = (name: string): TaskInfo => ({
  name,
  capability: "software_engineering",
  description: "修复一个需要较长说明的适配任务",
  difficulty: "中等",
  prompt: "请修复这个适配任务并说明原因。",
  budgets: { max_tool_calls: 32, timeout_seconds: 3600 },
  suite_version: "test-suite",
  environment: { kind: "local", status: "local" },
  tags: ["适配"],
  source: {
    benchmark: "SWE-bench",
    task_id: "owner/repository#123",
    url: "https://example.com/owner/repository/issues/123",
    source_revision: "abc123",
    license: "MIT",
    adaptation_notes: "保留原始任务约束和评测说明。",
  },
  planner_modes: ["llm"],
});

let snapshot: BenchmarkRuns;

function batch(status: "running" | "completed" | "stopping" = "running", all = false): BenchmarkRun {
  const names = all ? ["task-one", "task-two"] : ["task-one"];
  return {
    id: "run-one", instance_id: "backend-one", created_at: "2026-09-09T10:00:00Z", status,
    total: names.length, finished: status === "completed" ? names.length : 0,
    tasks: names.map((name, index) => ({
      id: `execution-${index}`, task_name: name, status, phase: "agent", activity: "model_request",
      updated_at: "2026-09-09T10:00:00Z", duration_ms: 1000, trace_count: status === "completed" ? 1 : 0,
      result: status === "completed" ? { task_name: name, score: 0, passed: false, final_answer: "已完成" } : null,
    })),
  };
}

beforeEach(() => {
  vi.resetAllMocks();
  sessionStorage.clear();
  snapshot = { instance_id: "backend-one", runs: [] };
  mocks.listBenchmarkRuns.mockImplementation(async () => snapshot);
  mocks.listBenchmarkResources.mockImplementation(async () => ["task-one", "task-two", "terminal-case", "data-case"].map((name) => ({ task_name: name, status: "ready", phase: "ready", error: null, has_resources: true, in_use: false })));
  mocks.listTasks.mockResolvedValue([task("task-one"), task("task-two")]);
  mocks.runBenchmark.mockImplementation(async () => {
    const run = batch(); snapshot.runs = [run]; return run;
  });
  mocks.runAllBenchmark.mockImplementation(async () => {
    const run = batch("running", true); snapshot.runs = [run]; return run;
  });
  mocks.cancelBenchmark.mockImplementation(async () => {
    const run = batch("stopping"); snapshot.runs = [run]; return run;
  });
});

describe("BenchmarkPage layout and runs", () => {
  it("pauses hidden polling and refreshes without losing task content", async () => {
    const { rerender } = render(<BenchmarkPage active />);
    await screen.findByText("task-one");
    await waitFor(() => expect(mocks.listBenchmarkResources).toHaveBeenCalled());
    rerender(<BenchmarkPage active={false} />);
    const calls = mocks.listBenchmarkResources.mock.calls.length;
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 1100)); });
    expect(mocks.listBenchmarkResources).toHaveBeenCalledTimes(calls);
    expect(screen.getByText("task-one")).toBeInTheDocument();
    rerender(<BenchmarkPage active />);
    await waitFor(() => expect(mocks.listBenchmarkResources.mock.calls.length).toBeGreaterThan(calls));
  });

  it("requires resources, downloads explicitly, and confirms resource deletion", async () => {
    let ready = false;
    mocks.listBenchmarkResources.mockImplementation(async () => ["task-one", "task-two"].map((name) => ({ task_name: name, status: ready ? "ready" : "not_prepared", phase: "idle", error: null, has_resources: ready, in_use: false })));
    mocks.changeBenchmarkResources.mockImplementation(async (name, action) => {
      ready = action === "prepare";
      return { task_name: name, status: ready ? "ready" : "not_prepared", phase: "idle", has_resources: ready, error: null, in_use: false };
    });
    const user = userEvent.setup();
    render(<BenchmarkPage />);
    await screen.findByText("task-one");
    expect(screen.getByRole("button", { name: /全部运行/ })).toBeDisabled();
    const runButtons = screen.getAllByRole("button").filter((button) => button.textContent?.trim() === "运行");
    expect(runButtons[0]).toBeDisabled();
    await user.click(screen.getAllByRole("button", { name: /下载资源/ })[0]);
    await waitFor(() => expect(runButtons[0]).not.toBeDisabled());
    expect(mocks.runBenchmark).not.toHaveBeenCalled();
    await user.click(screen.getAllByRole("button", { name: /删除资源/ })[0]);
    expect((await screen.findAllByText("删除“task-one”的资源？"))[0]).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: /取\s*消/ }));
    expect(mocks.changeBenchmarkResources).toHaveBeenCalledTimes(1);
  });

  it("filters the visible tasks without narrowing run-all and shows unlimited budgets", async () => {
    const first = task("terminal-case");
    first.capability = "terminal";
    first.budgets.max_tool_calls = null;
    const second = task("data-case");
    second.capability = "data_processing";
    second.source.benchmark = "Terminal-Bench 2.0";
    mocks.listTasks.mockResolvedValueOnce([first, second]);
    const user = userEvent.setup();
    render(<BenchmarkPage />);
    await screen.findByText("terminal-case");
    await user.click(screen.getByRole("combobox", { name: "任务类别" }));
    await user.click(await screen.findByTitle("终端任务"));
    await waitFor(() => expect(screen.queryByText("data-case")).not.toBeInTheDocument());
    await user.click(screen.getByText("完整测试内容"));
    expect(screen.getByText(/工具调用：不限次数/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /全部运行/ }));
    expect(mocks.runAllBenchmark).toHaveBeenCalledWith("llm");
  });

  it("keeps separate source totals and reports an environment failure without a score", async () => {
    const first = task("task-one");
    const second = task("task-two");
    second.source.benchmark = "Terminal-Bench 2.0";
    mocks.listTasks.mockResolvedValueOnce([first, second]);
    const run = batch("completed", true);
    run.tasks[0].result = { task_name: first.name, score: 1, passed: true };
    run.tasks[1].status = "failed";
    run.tasks[1].result = { task_name: second.name, score: null, error: "Docker unavailable", failure_phase: "environment" };
    snapshot.runs = [run];
    render(<BenchmarkPage />);
    expect(await screen.findByText("SWE-bench：1 / 1 通过 · 1 已评分")).toBeInTheDocument();
    expect(screen.getByText("Terminal-Bench 2.0：0 / 1 通过 · 0 已评分")).toBeInTheDocument();
    expect(screen.getByText("失败阶段：准备容器")).toBeInTheDocument();
  });
  afterEach(() => { cleanup(); vi.useRealTimers(); });

  it("renders wide task cards through the shared two-column grid", async () => {
    const { container } = render(<BenchmarkPage />);

    expect(await screen.findByText("task-one")).toBeInTheDocument();
    expect(container.querySelector(".task-grid")).toBeInTheDocument();
    expect(container.querySelectorAll(".task-card")).toHaveLength(2);
    expect(container.querySelectorAll(".ant-col-lg-12")).toHaveLength(2);
  });

  it("starts immediately, polls completion even when the score fails, and only links the trace download", async () => {
    const user = userEvent.setup();
    render(<BenchmarkPage />);
    await screen.findByText("task-one");

    const runButtons = screen.getAllByRole("button").filter((button) => button.textContent?.trim() === "运行");
    await user.click(runButtons[0]);
    await waitFor(() => expect(mocks.runBenchmark).toHaveBeenCalledWith("task-one", "llm"));
    expect(await screen.findByText("状态：运行中")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /下载 Trace/ })).not.toBeInTheDocument();
    expect(runButtons[0]).toBeDisabled();
    expect(runButtons[1]).not.toBeDisabled();
    snapshot = { ...snapshot, runs: [batch("completed")] };
    expect(await screen.findByText("状态：已完成", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.getByText("评分：未通过")).toBeInTheDocument();
    expect(runButtons[0]).not.toBeDisabled();
    expect(screen.getByRole("link", { name: /下载 Trace/ }))
      .toHaveAttribute("href", "/benchmark/runs/run-one/tasks/execution-0/trace/export");
    expect(screen.queryByText(/完整 Trace/)).not.toBeInTheDocument();
    expect(document.querySelector(".benchmark-trace-list")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /全部运行/ }));
    await waitFor(() => expect(mocks.runAllBenchmark).toHaveBeenCalledWith("llm"));
    expect(await screen.findByText("全部运行：0 / 2 项已结束")).toBeInTheDocument();
  });

  it.each(["completed", "failed", "cancelled"] as const)("offers empty trace downloads for %s tests", async (status) => {
    const run = batch("completed");
    run.tasks[0].status = status;
    run.tasks[0].trace_count = 0;
    snapshot.runs = [run];
    render(<BenchmarkPage />);
    expect(await screen.findByRole("link", { name: /下载 Trace/ })).toHaveAttribute("download");
  });

  it("does not offer a download while a result is still being finalized", async () => {
    const run = batch("completed");
    run.tasks[0].status = "stopping";
    snapshot.runs = [run];
    render(<BenchmarkPage />);
    await screen.findByText("评分：未通过");
    expect(screen.queryByRole("link", { name: /下载 Trace/ })).not.toBeInTheDocument();
  });

  it("shows an empty state after loading no tasks", async () => {
    mocks.listTasks.mockResolvedValueOnce([]);
    render(<BenchmarkPage />);
    expect(await screen.findByText("暂无可运行的基准任务。")).toBeInTheDocument();
  });

  it("shows partial batch results before the whole batch completes", async () => {
    const run = batch("running", true);
    run.tasks[0] = batch("completed").tasks[0];
    run.finished = 1;
    snapshot.runs = [run];
    render(<BenchmarkPage />);
    expect(await screen.findByText("全部运行：1 / 2 项已结束")).toBeInTheDocument();
    expect(screen.getByText("评分：未通过")).toBeInTheDocument();
    expect(screen.getByText("状态：运行中")).toBeInTheDocument();
  });

  it("restores results when remounted without starting another run", async () => {
    snapshot.runs = [batch("completed")];
    const view = render(<BenchmarkPage />);
    await screen.findByText("评分：未通过");
    view.unmount();
    render(<BenchmarkPage />);
    expect(await screen.findByText("评分：未通过")).toBeInTheDocument();
    expect(mocks.runBenchmark).not.toHaveBeenCalled();
  });

  it("retains results during connection errors and reports a backend restart", async () => {
    snapshot.runs = [batch("completed")];
    render(<BenchmarkPage />);
    await screen.findByText("评分：未通过");
    mocks.listBenchmarkRuns.mockRejectedValueOnce(new Error("offline"));
    expect(await screen.findByText("连接异常：offline", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.getByText("评分：未通过")).toBeInTheDocument();
    snapshot = { instance_id: "backend-two", runs: [] };
    expect(await screen.findByText("后端已重启，上次运行记录已失效。", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.queryByText("评分：未通过")).not.toBeInTheDocument();
    expect(mocks.runBenchmark).not.toHaveBeenCalled();
  });

  it("shows stopping until the backend confirms termination", async () => {
    snapshot.runs = [batch()];
    const user = userEvent.setup();
    render(<BenchmarkPage />);
    await screen.findByText("状态：运行中");
    await user.click(screen.getByRole("button", { name: /停止/ }));
    expect(mocks.cancelBenchmark).toHaveBeenCalledWith("run-one", "execution-0");
    expect(await screen.findByText("状态：正在停止")).toBeInTheDocument();
    expect(screen.queryByText("状态：已停止")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /停止/ })).toBeDisabled();
  });

  it("does not overlap polls and aborts the pending read on unmount", async () => {
    vi.useFakeTimers();
    mocks.listBenchmarkRuns.mockImplementation(() => new Promise(() => {}));
    const view = render(<BenchmarkPage />);
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    expect(mocks.listBenchmarkRuns).toHaveBeenCalledTimes(1);
    const signal = mocks.listBenchmarkRuns.mock.calls[0][0] as AbortSignal;
    view.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    expect(mocks.listBenchmarkRuns).toHaveBeenCalledTimes(1);
  });

  it("times out a stalled status request and retries without starting a task", async () => {
    vi.useFakeTimers();
    mocks.listBenchmarkRuns.mockImplementation((signal: AbortSignal) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
    }));
    render(<BenchmarkPage />);
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(screen.getByText("连接异常：状态查询超时，正在重新连接。")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(mocks.listBenchmarkRuns).toHaveBeenCalledTimes(2);
    expect(mocks.runBenchmark).not.toHaveBeenCalled();
  });
});
