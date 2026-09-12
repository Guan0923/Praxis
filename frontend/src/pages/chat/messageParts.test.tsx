import { App as AntApp } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChatMessage, DisplayMode, TurnItem } from "../../types";
import { AssistantMessage, MessageActions, summarizeReasoningTail, ToolLine } from "./messageParts";

describe("runtime thinking summary", () => {
  it("normalizes whitespace into one line", () => {
    expect(summarizeReasoningTail("\n\n  第一段思考  \n\n第二段\t继续 ")).toBe("第一段思考 第二段 继续");
  });

  it("keeps exactly two hundred and fifty Unicode graphemes without an ellipsis", () => {
    const value = "中😀".repeat(125);
    expect(Array.from(summarizeReasoningTail(value))).toHaveLength(250);
    expect(summarizeReasoningTail(value)).toBe(value);
  });

  it("keeps the last two hundred and fifty graphemes and adds one leading ellipsis", () => {
    const tail = "👨‍👩‍👧‍👦".repeat(250);
    const summary = summarizeReasoningTail(`应被截断的前缀${tail}`);
    expect(summary).toBe(`…${tail}`);
  });

  it("returns an empty fallback signal for blank content", () => {
    expect(summarizeReasoningTail(" \n\t ")).toBe("");
  });
});

describe("message actions", () => {
  it("keeps Markdown and formula nodes mounted when a running answer finishes", () => {
    const msg: ChatMessage = {
      id: "turn:message:1", role: "assistant", content: "$x^2$\n\nStable text.", events: [],
      items: [{ type: "text", text: "$x^2$\n\nStable text.", status: "running" }],
      running: true, status: "running", itemVersion: 0,
    };
    const props = { display: "normal" as DisplayMode, busy: false, onDecision: vi.fn() };
    const { container, rerender } = render(<AssistantMessage {...props} msg={msg} />);
    const markdown = container.querySelector(".markdown");
    const formula = container.querySelector(".math-source");
    rerender(<AssistantMessage {...props} msg={{ ...msg, running: false, status: "success", items: [{ ...msg.items![0], status: "success" }] }} />);
    expect(container.querySelector(".markdown")).toBe(markdown);
    expect(container.querySelector(".math-source")).toBe(formula);
  });

  it("removes the user rewind action while retaining copy and edit", () => {
    render(
      <AntApp>
        <MessageActions
          msg={{ id: "user-1", role: "user", content: "hello", events: [] }}
          busy={false}
          onEdit={vi.fn()}
        />
      </AntApp>,
    );

    expect(screen.getByRole("button", { name: "复制" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "编辑" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "回溯" })).not.toBeInTheDocument();
  });

  it("keeps thinking Markdown compact without changing regular paragraph spacing", async () => {
    const fs = await vi.importActual<{ readFileSync(path: string, encoding: "utf8"): string }>("node:fs");
    const runtime = globalThis as typeof globalThis & { process: { cwd(): string } };
    const css = ["chat-runtime.css", "chat-markdown.css"]
      .map((file) => fs.readFileSync(`${runtime.process.cwd()}/src/styles/${file}`, "utf8"))
      .join("\n");
    const rule = css.slice(css.indexOf(".thinking-content {"), css.indexOf(".shimmer-text {"));

    expect(rule).toMatch(/\.thinking-content\s*{[^}]*line-height:\s*1\.5;/s);
    expect(rule).toMatch(/\.thinking-content\s*{[^}]*white-space:\s*normal;/s);
    expect(rule).toMatch(/\.thinking-content \.markdown\s*{[^}]*white-space:\s*normal;/s);
    expect(css).toMatch(/\.thinking-content \.markdown p\s*{[^}]*margin:\s*0;/s);

    const { container } = render(
      <>
        <style>{css}</style>
        <div className="thinking-content"><div className="markdown"><p>第一段</p><p>第二段</p></div></div>
        <div className="markdown regular-markdown"><p>普通第一段</p><p>普通第二段</p></div>
      </>,
    );
    const thinking = container.querySelector<HTMLElement>(".thinking-content")!;
    const thinkingParagraph = container.querySelector<HTMLElement>(".thinking-content .markdown p")!;
    const regularParagraph = container.querySelector<HTMLElement>(".regular-markdown p")!;
    expect(window.getComputedStyle(thinking).lineHeight).toBe("1.5");
    expect(window.getComputedStyle(thinking).whiteSpace).toBe("normal");
    expect(window.getComputedStyle(thinkingParagraph).marginBottom).toBe("0px");
    expect(window.getComputedStyle(regularParagraph).marginBottom).toBe("10px");
  });
});

function assistant(items: TurnItem[], running = false): ChatMessage {
  return {
    id: "assistant-runtime",
    role: "assistant",
    content: items.filter((item) => item.type === "text").map((item) => String(item.text ?? "")).join(""),
    events: [],
    items,
    itemVersion: 0,
    running,
    status: running ? "running" : "success",
  };
}

function renderAssistant(message: ChatMessage, display: DisplayMode = "developer") {
  return (
    <AntApp>
      <AssistantMessage
        msg={message}
        display={display}
        busy={false}
        onDecision={vi.fn().mockResolvedValue(undefined)}
      />
    </AntApp>
  );
}

describe("subagent Assistant report", () => {
  it("renders multiple reports and final text inside one Assistant reply frame", () => {
    const { container } = render(renderAssistant(assistant([
      {
        type: "subagent",
        event: "agent_report",
        status: "success",
        text: "thread_path: /root/one\nthread_status: success\ntask_result: one",
        delivery_id: "agent_report_one",
      },
      {
        type: "subagent",
        event: "agent_report",
        status: "success",
        report_status: "failed",
        text: "thread_path: /root/two\nthread_status: failed\ntask_result: two",
        delivery_id: "agent_report_two",
      },
      { type: "text", text: "all done", status: "success" },
    ])));

    expect(container.querySelectorAll(".message.assistant")).toHaveLength(1);
    expect(container.querySelectorAll(".assistant-icon")).toHaveLength(1);
    expect(container.querySelectorAll(".runtime-agent-report")).toHaveLength(2);
    expect(container.querySelectorAll(".runtime-agent-report.failed")).toHaveLength(1);
    expect(screen.getByText("all done")).toBeVisible();
  });

  it("renders the exact plain text with preserved line breaks", async () => {
    const report = "thread_path: /root/worker\nthread_status: success\ntask_result: 第一行\n第二行";
    const { container } = render(renderAssistant(assistant([{
      type: "subagent",
      event: "agent_report",
      status: "success",
      text: report,
      delivery_id: "agent_report_1",
    }])));
    const element = container.querySelector<HTMLElement>(".runtime-agent-report");
    expect(element).not.toBeNull();
    expect(element?.textContent).toBe(report);

    const fs = await vi.importActual<{ readFileSync(path: string, encoding: "utf8"): string }>("node:fs");
    const runtime = globalThis as typeof globalThis & { process: { cwd(): string } };
    const css = fs.readFileSync(`${runtime.process.cwd()}/src/styles/chat-runtime.css`, "utf8");
    expect(css).toMatch(/\.runtime-agent-report\s*{[^}]*white-space:\s*pre-wrap;/s);
  });

  it("uses delivery metadata for failed styling without parsing report text", () => {
    const report = "opaque content without status fields";
    const { container } = render(renderAssistant(assistant([{
      type: "subagent",
      event: "agent_report",
      status: "success",
      report_status: "failed",
      text: report,
      delivery_id: "agent_report_failed",
    }])));
    const element = container.querySelector<HTMLElement>(".runtime-agent-report.failed");
    expect(element?.dataset.reportStatus).toBe("failed");
    expect(element?.textContent).toBe(report);
  });
});

describe("parallel tool groups", () => {
  it("groups calls by call ID and keeps approval status outside the nested Collapse", async () => {
    const group = "parallel:call-1";
    const items: TurnItem[] = [
      { type: "tool_call", call_id: "call-1", name: "read_file", arguments: { path: "one" }, status: "success", parallel_group_id: group, parallel_index: 0, parallel_size: 2, execution_stage: "succeeded" },
      { type: "tool_call", call_id: "call-2", name: "read_file", arguments: { path: "two" }, status: "success", parallel_group_id: group, parallel_index: 1, parallel_size: 2, execution_stage: "succeeded" },
      { type: "approval", event: "approval_resolved", approval_status: "allowed", call_id: "call-2", tool: "read_file", status: "success" },
      { type: "tool_result", call_id: "call-2", tool: "read_file", content: "two", status: "success", parallel_group_id: group, parallel_index: 1, parallel_size: 2, execution_stage: "succeeded" },
      { type: "tool_result", call_id: "call-1", tool: "read_file", content: "one", status: "success", parallel_group_id: group, parallel_index: 0, parallel_size: 2, execution_stage: "succeeded" },
    ];
    const view = render(renderAssistant(assistant(items)));
    const outer = view.container.querySelector(".runtime-parallel-collapse")!;

    expect(view.container.querySelectorAll(".runtime-parallel-collapse")).toHaveLength(1);
    expect(outer.querySelector(":scope > .ant-collapse-item")).not.toHaveClass("ant-collapse-item-active");
    expect(screen.getByText("已允许 read_file").closest(".runtime-parallel-collapse")).toBeNull();

    fireEvent.click(outer.querySelector(":scope > .ant-collapse-item > .ant-collapse-header")!);
    await waitFor(() => expect(outer.querySelector(":scope > .ant-collapse-item")).toHaveClass("ant-collapse-item-active"));
    expect(screen.getAllByText("read_file")).toHaveLength(2);
    expect(screen.getByText("call-1")).toBeInTheDocument();
    expect(screen.getByText("call-2")).toBeInTheDocument();
    const firstChild = outer.querySelector(".runtime-parallel-inner-collapse > .ant-collapse-item")!;
    fireEvent.click(firstChild.querySelector(":scope > .ant-collapse-header")!);
    await waitFor(() => expect(firstChild).toHaveClass("ant-collapse-item-active"));

    const updated = assistant(items.map((item) => ({ ...item })));
    updated.itemVersion = 1;
    view.rerender(renderAssistant(updated));
    expect(outer.querySelector(":scope > .ant-collapse-item")).toHaveClass("ant-collapse-item-active");
    expect(firstChild).toHaveClass("ant-collapse-item-active");
  });
});

describe("assistant Item presentation", () => {
  it("keeps Turn execution errors inside the assistant message bubble", () => {
    const message = assistant([]);
    message.error = "Turn execution failed";
    const { container } = render(renderAssistant(message));

    expect(screen.getByText("Turn execution failed")).toBeInTheDocument();
    expect(container.querySelector(".message.assistant .bubble .ant-alert-error")).toBeInTheDocument();
  });

  it.each<DisplayMode>(["minimal", "medium", "verbose", "developer"])(
    "shows a live network retry and retains the raw backend message after recovery in %s mode",
    (display) => {
    const retry: TurnItem = {
      type: "retry",
      event: "model_retry",
      category: "network",
      message: "connection reset by peer",
      attempt: 1,
      max_retries: 3,
      delay_seconds: 0.5,
      status: "running",
    };
      const view = render(renderAssistant(assistant([retry], true), display));

      expect(screen.getByRole("status", { name: /网络异常，正在重试（1\/3）/ })).toBeInTheDocument();
      expect(screen.getByText("connection reset by peer")).toBeVisible();
      expect(view.container.querySelector('[data-item-type="retry"]')).toHaveClass("is-active");

      view.rerender(renderAssistant(assistant([{ ...retry, status: "success" }], true), display));
      expect(screen.queryByRole("status", { name: /网络异常，正在重试/ })).not.toBeInTheDocument();
      expect(screen.getByText("网络请求已重试（1/3）")).toBeVisible();

      view.rerender(renderAssistant(assistant([
        { ...retry, status: "success" },
        { type: "text", text: "recovered", status: "success" },
      ]), display));
      expect(screen.getByText("网络请求已重试（1/3）")).toBeVisible();
      expect(screen.getByText("connection reset by peer")).toBeVisible();
      expect(screen.getByText("recovered")).toBeVisible();
    },
  );

  it("keeps multiple network retries in canonical order", () => {
    const retries: TurnItem[] = [
      {
        type: "retry",
        event: "model_retry",
        category: "network",
        message: "first failure",
        attempt: 1,
        max_retries: 5,
        delay_seconds: 0.5,
        status: "success",
      },
      {
        type: "retry",
        event: "model_retry",
        category: "network",
        message: "second failure",
        attempt: 2,
        max_retries: 5,
        delay_seconds: 1,
        status: "running",
      },
    ];
    const { container } = render(renderAssistant(assistant(retries, true), "developer"));

    const rendered = [...container.querySelectorAll<HTMLElement>('[data-item-type="retry"]')];
    expect(rendered.map((item) => item.textContent)).toEqual([
      "网络请求已重试（1/5）first failure",
      "网络异常，正在重试（2/5）second failure",
    ]);
    expect(screen.getByRole("status", { name: "网络异常，正在重试（2/5）" })).toBe(rendered[1]);
  });

  it("hides Skill metadata and shows the running indicator instead of none", () => {
    render(renderAssistant(assistant([
      { type: "skill_snapshot", event: "skills_selected", text: "none", skills: [], status: "success" },
    ], true)));

    expect(screen.queryByText("none")).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "思考中" })).toBeInTheDocument();
  });

  it("renders one pending tool approval card", () => {
    const message = assistant([
      { type: "approval", event: "decision_requested", decision_id: "dec-search", kind: "tool", call_id: "call-search", tool: "web_search", arguments: { query: "local" }, text: "Call tool web_search?", status: "success" },
    ], true);
    message.decision = {
      decision_id: "dec-search",
      kind: "tool",
      tool: "web_search",
      arguments: { query: "local" },
      message: "Call tool web_search?",
    };
    const { container } = render(renderAssistant(message));

    expect(screen.getAllByText("Call tool web_search?")).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "本次允许" })).toHaveLength(1);
    expect(container.querySelectorAll('[data-item-type="approval"]')).toHaveLength(1);
  });

  it("renders resolved approval once in canonical Item order", () => {
    const { container } = render(renderAssistant(assistant([
      { type: "tool_call", call_id: "call-search", name: "web_search", arguments: { query: "local" }, status: "success" },
      { type: "approval", event: "approval_resolved", approval_status: "allowed", call_id: "call-search", tool: "web_search", status: "success" },
      { type: "tool_result", call_id: "call-search", tool: "web_search", content: "local result", status: "success" },
      { type: "text", text: "done", status: "success" },
    ])));

    expect(screen.getAllByText("已允许 web_search")).toHaveLength(1);
    const runtimeItems = container.querySelector(".runtime-items");
    expect(Array.from(runtimeItems!.children).map((element) => (element as HTMLElement).dataset.itemType)).toEqual([
      "tool_call",
      "approval",
      "tool_result",
      "text",
    ]);
  });

  it("renders a denied approval as one static status", () => {
    render(renderAssistant(assistant([
      { type: "approval", event: "approval_resolved", approval_status: "denied", call_id: "call-search", tool: "web_search", status: "success" },
    ])));

    expect(screen.getAllByText("已拒绝 web_search")).toHaveLength(1);
    expect(screen.queryByText("Call tool web_search?")).not.toBeInTheDocument();
  });

  it("renders every Item in canonical order and keeps answers outside Collapse", async () => {
    const items: TurnItem[] = [
      { type: "reasoning", text: "第一次思考", status: "success" },
      { type: "tool_call", call_id: "call-1", name: "read_file", arguments: { path: "README.md" }, status: "success" },
      { type: "tool_result", call_id: "call-1", tool: "read_file", content: "工具结果", status: "success" },
      { type: "text", text: "中间回答", status: "success" },
      { type: "reasoning", text: "第二次思考", status: "success" },
      { type: "tool_call", call_id: "call-2", name: "glob", arguments: { pattern: "*.ts" }, status: "success" },
      { type: "text", text: "最终回答", status: "success" },
    ];
    const { container } = render(renderAssistant(assistant(items)));

    const runtimeItems = container.querySelector(".runtime-items");
    expect(runtimeItems).not.toBeNull();
    expect(Array.from(runtimeItems!.children).map((element) => (element as HTMLElement).dataset.itemType)).toEqual([
      "reasoning",
      "tool_call",
      "tool_result",
      "text",
      "reasoning",
      "tool_call",
      "text",
    ]);
    expect(container.querySelectorAll(".runtime-item-collapse")).toHaveLength(5);
    expect(screen.getByText("中间回答").closest(".runtime-collapse")).toBeNull();
    expect(screen.getByText("最终回答").closest(".runtime-collapse")).toBeNull();

    const resultCollapse = container.querySelector('[data-item-type="tool_result"]');
    fireEvent.click(resultCollapse!.querySelector(".ant-collapse-header")!);
    await waitFor(() => expect(resultCollapse!.querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active"));
    expect(resultCollapse).toHaveTextContent("工具结果");
  });

  it.each<DisplayMode>(["medium", "verbose", "developer"])(
    "starts every runtime Collapse folded in %s mode",
    (display) => {
      const activeItems: TurnItem[] = [
        { type: "reasoning", text: "实时思考", status: "running" },
        { type: "tool_call", call_id: "call-folded", name: "read_file", arguments: {}, status: "running" },
        { type: "tool_result", call_id: "call-folded", tool: "read_file", content: "实时结果", status: "running" },
      ];

      for (const item of activeItems) {
        const view = render(renderAssistant(assistant([item], true), display));
        expect(view.container.querySelector(".runtime-item-collapse .ant-collapse-item")).not.toHaveClass("ant-collapse-item-active");
        view.unmount();
      }
    },
  );

  it("mounts reasoning only while expanded and restores the latest content", async () => {
    const item: TurnItem = { type: "reasoning", text: "initial detail", status: "running" };
    const view = render(renderAssistant(assistant([item], true)));
    const header = () => view.container.querySelector(".runtime-item-collapse .ant-collapse-header")!;
    expect(view.container.querySelector(".thinking-content")).toBeNull();
    fireEvent.click(header());
    await waitFor(() => expect(view.container.querySelector(".thinking-content")).toHaveTextContent("initial detail"));
    fireEvent.click(header());
    await waitFor(() => expect(view.container.querySelector(".thinking-content")).toBeNull());
    view.rerender(renderAssistant(assistant([{ ...item, text: "latest detail" }], true)));
    expect(view.container.querySelector(".thinking-content")).toBeNull();
    fireEvent.click(header());
    await waitFor(() => expect(view.container.querySelector(".thinking-content")).toHaveTextContent("latest detail"));
  });

  it("keeps manual expansion across active changes while new Items stay folded", async () => {
    const first: TurnItem = { type: "reasoning", text: "流式思考", status: "running" };
    const tool: TurnItem = { type: "tool_call", call_id: "call-1", name: "read_file", arguments: {}, status: "running" };
    const result: TurnItem = { type: "tool_result", call_id: "call-1", tool: "read_file", content: "读取完成", status: "running" };
    const view = render(renderAssistant(assistant([first], true)));

    let collapses = view.container.querySelectorAll(".runtime-item-collapse");
    expect(collapses[0].querySelector(".ant-collapse-item")).not.toHaveClass("ant-collapse-item-active");
    expect(collapses[0].querySelector(".ant-collapse-header")).toHaveTextContent("流式思考");
    expect(collapses[0].querySelectorAll(".runtime-status-dot")).toHaveLength(0);
    expect(collapses[0].querySelector(".shimmer-text.is-active")).toBeNull();

    fireEvent.click(collapses[0].querySelector(".ant-collapse-header")!);
    await waitFor(() => expect(collapses[0].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active"));
    expect(collapses[0].querySelector(".ant-collapse-header")).toHaveTextContent("正在思考中");
    expect(collapses[0].querySelectorAll(".runtime-status-dot")).toHaveLength(3);
    expect(collapses[0].querySelector(".shimmer-text.is-active")).toBeNull();

    const updatedFirst: TurnItem = { type: "reasoning", text: "流式思考继续", status: "running" };
    view.rerender(renderAssistant(assistant([updatedFirst], true)));
    collapses = view.container.querySelectorAll(".runtime-item-collapse");
    expect(collapses[0].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active");
    expect(collapses[0].querySelector(".ant-collapse-header")).toHaveTextContent("正在思考中");
    expect(collapses[0].querySelector(".shimmer-text.is-active")).toBeNull();

    view.rerender(renderAssistant(assistant([updatedFirst, tool], true)));
    collapses = view.container.querySelectorAll(".runtime-item-collapse");
    expect(collapses[0].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active");
    expect(collapses[1].querySelector(".ant-collapse-item")).not.toHaveClass("ant-collapse-item-active");
    expect(collapses[0].querySelector(".shimmer-text.is-active")).toBeNull();
    expect(collapses[0].querySelector(".ant-collapse-header")).toHaveTextContent("思考详情");
    expect(collapses[1].querySelector(".ant-collapse-header")).toHaveTextContent("正在调用 read_file");
    expect(collapses[1].querySelectorAll(".runtime-status-dot")).toHaveLength(3);
    expect(collapses[1].querySelector(".shimmer-text.is-active")).toHaveTextContent("正在调用 read_file");

    fireEvent.click(collapses[1].querySelector(".ant-collapse-header")!);
    await waitFor(() => expect(collapses[1].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active"));
    expect(collapses[1].querySelector(".shimmer-text.is-active")).toBeNull();
    expect(collapses[1].querySelectorAll(".runtime-status-dot")).toHaveLength(3);

    view.rerender(renderAssistant(assistant([updatedFirst, tool, result], true)));
    collapses = view.container.querySelectorAll(".runtime-item-collapse");
    expect(collapses[0].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active");
    expect(collapses[1].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active");
    expect(collapses[2].querySelector(".ant-collapse-item")).not.toHaveClass("ant-collapse-item-active");
    expect(collapses[1].querySelector(".ant-collapse-header")).toHaveTextContent("调用 read_file");
    expect(collapses[2].querySelector(".ant-collapse-header")).toHaveTextContent("正在处理 read_file 结果");
    expect(collapses[2].querySelector(".shimmer-text.is-active")).toHaveTextContent("正在处理 read_file 结果");

    fireEvent.click(collapses[2].querySelector(".ant-collapse-header")!);
    await waitFor(() => expect(collapses[2].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active"));
  });

  it("keeps folded reasoning summaries pinned to the right edge", async () => {
    let resizeCallback: ResizeObserverCallback | undefined;
    class MockResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        resizeCallback = callback;
      }

      observe = vi.fn();
      unobserve = vi.fn();
      disconnect = vi.fn();
    }
    const originalResizeObserver = window.ResizeObserver;
    window.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

    const view = render(renderAssistant(assistant([{ type: "reasoning", text: "初始思考", status: "running" }], true)));
    const collapse = view.container.querySelector(".runtime-item-collapse")!;
    expect(collapse.querySelector(".ant-collapse-item")).not.toHaveClass("ant-collapse-item-active");

    const viewport = collapse.querySelector<HTMLElement>(".runtime-summary-viewport")!;
    const summaryText = viewport.querySelector(".runtime-summary-text");
    expect(viewport.querySelector(".shimmer-text")).toBeNull();
    let clientWidth = 120;
    let scrollWidth = 80;
    Object.defineProperties(viewport, {
      clientWidth: { configurable: true, get: () => clientWidth },
      scrollWidth: { configurable: true, get: () => scrollWidth },
    });

    viewport.scrollLeft = 42;
    view.rerender(renderAssistant(assistant([{ type: "reasoning", text: "短摘要更新", status: "running" }], true)));
    expect(viewport.scrollLeft).toBe(0);
    expect(collapse.querySelector(".runtime-summary-viewport")).toBe(viewport);
    expect(viewport.querySelector(".runtime-summary-text")).toBe(summaryText);
    expect(viewport.querySelector(".shimmer-text")).toBeNull();

    clientWidth = 100;
    scrollWidth = 260;
    view.rerender(renderAssistant(assistant([{ type: "reasoning", text: "足够长的摘要更新并贴住最新字符", status: "running" }], true)));
    expect(viewport.scrollLeft).toBe(160);

    clientWidth = 150;
    resizeCallback?.([], {} as ResizeObserver);
    expect(viewport.scrollLeft).toBe(110);

    clientWidth = 90;
    scrollWidth = 240;
    view.rerender(renderAssistant(assistant([{ type: "reasoning", text: "已完成且仍然跟随尾部", status: "success" }], false)));
    const completedViewport = view.container.querySelector<HTMLElement>(".runtime-summary-viewport")!;
    Object.defineProperties(completedViewport, {
      clientWidth: { configurable: true, get: () => clientWidth },
      scrollWidth: { configurable: true, get: () => scrollWidth },
    });
    resizeCallback?.([], {} as ResizeObserver);
    expect(completedViewport.scrollLeft).toBe(150);
    expect(completedViewport.querySelector(".shimmer-text.is-active")).toBeNull();
    expect(completedViewport.querySelector(".runtime-summary-text")).toHaveTextContent("已完成且仍然跟随尾部");

    window.ResizeObserver = originalResizeObserver;
  });

  it("uses static completed labels and distinguishes failed tool results", async () => {
    const items: TurnItem[] = [
      { type: "reasoning", text: "完成后的思考摘要", status: "success" },
      { type: "tool_call", name: "read_file", arguments: {}, status: "success" },
      { type: "tool_result", tool: "read_file", content: "成功结果", status: "success" },
      { type: "tool_result", tool: "write_file", content: "失败结果", status: "failed" },
    ];
    const { container } = render(renderAssistant(assistant(items)));
    const collapses = container.querySelectorAll(".runtime-item-collapse");

    expect(collapses[0].querySelector(".ant-collapse-header")).toHaveTextContent("完成后的思考摘要");
    expect(collapses[1].querySelector(".ant-collapse-header")).toHaveTextContent("调用 read_file");
    expect(collapses[2].querySelector(".ant-collapse-header")).toHaveTextContent("read_file 结果");
    expect(collapses[3].querySelector(".ant-collapse-header")).toHaveTextContent("write_file 失败");

    fireEvent.click(collapses[3].querySelector(".ant-collapse-header")!);
    await waitFor(() => expect(collapses[3].querySelector(".tool-result > pre")?.textContent).toBe("失败结果"));
    expect(collapses[3].querySelector(".error-text, .tool-line.failed, .tool-status.failed")).toBeNull();
    expect(collapses[3].querySelector(".ant-collapse-header")).toHaveTextContent("write_file 失败");

    fireEvent.click(collapses[0].querySelector(".ant-collapse-header")!);
    fireEvent.click(collapses[1].querySelector(".ant-collapse-header")!);
    await waitFor(() => expect(collapses[0].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active"));
    expect(collapses[0].querySelector(".ant-collapse-header")).toHaveTextContent("思考详情");
    expect(collapses[1].querySelector(".ant-collapse-header")).toHaveTextContent("调用 read_file");
    expect(container.querySelector(".shimmer-text.is-active")).toBeNull();
  });

  it("falls back to the active reasoning status when folded content is empty", () => {
    const { container } = render(renderAssistant(assistant([{ type: "reasoning", text: "", status: "running" }], true)));
    const collapse = container.querySelector(".runtime-item-collapse")!;
    expect(collapse.querySelector(".ant-collapse-item")).not.toHaveClass("ant-collapse-item-active");
    expect(collapse.querySelector(".shimmer-text.is-active")).toHaveTextContent("正在思考中");
    expect(collapse.querySelectorAll(".runtime-status-dot")).toHaveLength(3);
  });

  it("renders only the current non-collapsible status in minimal mode", () => {
    const view = render(renderAssistant(assistant([
      { type: "reasoning", text: "历史思考", status: "success" },
      { type: "tool_call", name: "read_file", arguments: { path: "README.md" }, status: "success" },
      { type: "tool_result", tool: "read_file", content: "隐藏结果", status: "running" },
    ], true), "minimal"));

    expect(view.container.querySelector(".runtime-item-collapse")).toBeNull();
    expect(view.container.querySelectorAll(".runtime-minimal-status")).toHaveLength(1);
    expect(screen.getByRole("status", { name: "正在处理 read_file 结果" })).toBeInTheDocument();
    expect(view.container.querySelectorAll(".runtime-status-dot")).toHaveLength(3);
    expect(view.container).not.toHaveTextContent("历史思考");
    expect(view.container).not.toHaveTextContent("隐藏结果");

    view.rerender(renderAssistant(assistant([{ type: "reasoning", text: "实时思考", status: "running" }], true), "minimal"));
    expect(screen.getByRole("status", { name: "思考中" })).toBeInTheDocument();
    expect(view.container).not.toHaveTextContent("实时思考");

    view.rerender(renderAssistant(assistant([{ type: "reasoning", text: "完成思考", status: "success" }]), "minimal"));
    expect(view.container.querySelector(".runtime-minimal-status")).toBeNull();
    expect(view.container.querySelector(".runtime-item-collapse")).toBeNull();
  });

  it("uses one non-repeating shimmer band and honors reduced motion for both animations", async () => {
    const fs = await vi.importActual<{ readFileSync(path: string, encoding: "utf8"): string }>("node:fs");
    const runtime = globalThis as typeof globalThis & { process: { cwd(): string } };
    const css = fs.readFileSync(`${runtime.process.cwd()}/src/styles/chat-runtime.css`, "utf8");
    const activeRule = css.slice(css.indexOf(".shimmer-text.is-active"), css.indexOf("@keyframes runtime-summary-shimmer"));
    const reducedMotion = css.slice(css.indexOf("@media (prefers-reduced-motion: reduce)"));

    expect(activeRule).toContain("var(--shimmer-highlight)");
    expect(activeRule).toMatch(/animation:\s*runtime-summary-shimmer/);
    expect(activeRule).toMatch(/background-repeat:\s*no-repeat/);
    expect(css).toMatch(/\.runtime-status-dot\s*{[^}]*animation:\s*runtime-status-dot 900ms ease-in-out infinite;/s);
    expect(css).toMatch(/\.runtime-status-dot:nth-child\(2\)\s*{[^}]*animation-delay:\s*120ms;/s);
    expect(css).toMatch(/\.runtime-status-dot:nth-child\(3\)\s*{[^}]*animation-delay:\s*240ms;/s);
    expect(css).toMatch(/@keyframes runtime-status-dot[\s\S]*transform:\s*translateY\(-3px\);[\s\S]*background-color:\s*var\(--shimmer-highlight\);/);
    expect(reducedMotion).toMatch(/\.shimmer-text\.is-active\s*{[^}]*animation:\s*none;/s);
    expect(reducedMotion).toMatch(/-webkit-text-fill-color:\s*currentColor/);
    expect(reducedMotion).toMatch(/\.runtime-status-dot\s*{[^}]*animation:\s*none;/s);
  });

  it("uses one unclipped summary track and matching header and body padding", async () => {
    const fs = await vi.importActual<{ readFileSync(path: string, encoding: "utf8"): string }>("node:fs");
    const runtime = globalThis as typeof globalThis & { process: { cwd(): string } };
    const css = fs.readFileSync(`${runtime.process.cwd()}/src/styles/chat-runtime.css`, "utf8");

    expect(css).toMatch(/\.runtime-collapse\s*{[^}]*--runtime-collapse-inline-padding:\s*12px;/s);
    expect(css).toMatch(/\.runtime-collapse \.ant-collapse-header\s*{[^}]*padding-inline:\s*var\(--runtime-collapse-inline-padding\)/s);
    expect(css).toMatch(/\.runtime-collapse \.ant-collapse-body\s*{[^}]*padding-inline:\s*var\(--runtime-collapse-inline-padding\)/s);
    expect(css).toMatch(/\.runtime-collapse \.ant-collapse-expand-icon\s*{[^}]*position:\s*absolute;[^}]*inset-inline-start:\s*0;/s);
    expect(css).toMatch(/\.runtime-summary-viewport\s*{[^}]*width:\s*100%;[^}]*overflow:\s*hidden;/s);
    expect(css).toMatch(/\.runtime-summary-track\s*{[^}]*width:\s*max-content;[^}]*white-space:\s*nowrap;/s);
    expect(css).toMatch(/\.runtime-summary-viewport \.runtime-summary-text\s*{[^}]*overflow:\s*visible;[^}]*text-overflow:\s*clip;/s);
  });

  it("keeps tool results inside a pre block in developer mode", () => {
    const result = "第一行\n第二行\n第三行\n第四行\n第五行\n第六行";
    const { container } = render(
      <ToolLine
        ev={{ kind: "tool_result", message: result, data: { tool: "读取文件", result } }}
        display="developer"
      />,
    );

    const resultBlock = container.querySelector(".tool-result > pre");
    expect(resultBlock).not.toBeNull();
    expect(resultBlock?.textContent).toBe(result);
    const payloadBlock = container.querySelector("pre.tool-payload");
    expect(payloadBlock).not.toBeNull();
    expect(payloadBlock).not.toHaveClass("tool-result");
  });

  it("keeps answer Markdown code blocks separate from tool results", () => {
    const message = assistant([{ type: "text", text: "```text\n最终答案代码\n```", status: "success" }]);
    const { container } = render(renderAssistant(message));

    const answerCode = container.querySelector(".markdown pre");
    expect(answerCode).not.toBeNull();
    expect(answerCode).not.toHaveClass("tool-result");
    expect(answerCode).toHaveTextContent("最终答案代码");
  });

  it.each(["minimal", "verbose", "developer"] as const)("keeps a denied tool visible without exposing model feedback in %s mode", (display) => {
    const { container } = render(
      <ToolLine
        ev={{
          kind: "tool_failed",
          message: "The user denied this write_file tool call.",
          data: { tool: "write_file", call_id: "call-denied", failure_code: "user_denied" },
        }}
        display={display}
      />,
    );

    expect(container).toHaveTextContent("write_file");
    expect(container).toHaveTextContent("已拒绝");
    expect(container).not.toHaveTextContent("The user denied this write_file tool call.");
  });

  it.each(["minimal", "verbose", "developer"] as const)("renders tool failures like ordinary results in %s mode", (display) => {
    const result = "The selected lines no longer match expected_lines; file was not changed.\n  Original indentation preserved.\n";
    const data = { tool: "edit_file", call_id: "call-failed", result };
    const { container } = render(
      <>
        <section data-testid="failed-result"><ToolLine ev={{ kind: "tool_failed", message: "fallback", data }} display={display} /></section>
        <section data-testid="successful-result"><ToolLine ev={{ kind: "tool_result", message: "fallback", data }} display={display} /></section>
      </>,
    );

    const failed = screen.getByTestId("failed-result");
    expect(failed.innerHTML).toBe(screen.getByTestId("successful-result").innerHTML);
    expect(failed.querySelector(".tool-result-label")).toHaveTextContent("edit_file 结果");
    expect(container.querySelector(".error-text, .tool-line.failed, .tool-status.failed")).toBeNull();
    expect(failed.querySelector(".tool-result > pre")?.textContent ?? null).toBe(display === "minimal" ? null : result);
    expect(failed.querySelector(".tool-call-id")?.textContent ?? null).toBe(display === "developer" ? "call ID: call-failed" : null);
    expect(failed.querySelector(".tool-payload")?.textContent ?? null).toBe(display === "developer" ? JSON.stringify(data, null, 2) : null);
  });

  it("uses the failure message when no result field is provided", () => {
    const message = "first line\n  second line\n";
    const { container } = render(<ToolLine ev={{ kind: "tool_failed", message, data: { tool: "edit_file" } }} display="verbose" />);
    expect(container.querySelector(".tool-result > pre")?.textContent).toBe(message);
  });

  it("keeps genuine task errors in their error alert", () => {
    const { container } = render(renderAssistant(assistant([{ type: "error", message: "Task execution failed", status: "failed" }])));
    expect(container.querySelector(".ant-alert.error-text")).toHaveTextContent("Task execution failed");
    expect(container.querySelector(".tool-result")).toBeNull();
  });
});
