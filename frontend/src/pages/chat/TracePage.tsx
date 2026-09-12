import { ErrorDisplay } from "../../components/ErrorDisplay";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Alert, Button, Collapse, Empty, Spin, Tag, type CollapseProps } from "antd";
import { DownloadOutlined, LeftOutlined, RightOutlined } from "@ant-design/icons";
import { getTurnTrace, threadTraceDownloadUrl } from "../../api";
import type {
  RuntimeStateNode,
  TurnItem,
  TurnTraceContext,
  TurnTraceItem,
  TurnTraceResponse,
} from "../../types";

interface TracePageProps {
  turns: RuntimeStateNode[];
}

type SemanticKind = "system" | "developer" | "skill" | "mcp" | "user" | "reasoning" | "assistant" | "retry" | "tool";

const TRACE_TAGS: Record<SemanticKind, { label: string; color?: string }> = {
  system: { label: "System", color: "purple" },
  developer: { label: "Developer", color: "cyan" },
  skill: { label: "Skill", color: "cyan" },
  mcp: { label: "MCP", color: "orange" },
  user: { label: "User Message", color: "green" },
  reasoning: { label: "Assistant Reasoning", color: "gold" },
  assistant: { label: "Assistant Response", color: "blue" },
  retry: { label: "Network Retry", color: "volcano" },
  tool: { label: "Tool" },
};

function json(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function text(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  return json(value);
}

function preview(value: unknown): string {
  return text(value).replace(/\s+/g, " ").trim() || "（空）";
}

function TraceLabel({ kind, value, status, timestamp }: {
  kind: SemanticKind;
  value: unknown;
  status?: string;
  timestamp?: string;
}) {
  const tag = TRACE_TAGS[kind];
  const failed = status === "failed";
  return (
    <span className="trace-collapse-label">
      <Tag color={failed ? "red" : tag.color}>{tag.label}</Tag>
      {status ? <Tag color={failed ? "red" : undefined}>{status}</Tag> : null}
      {timestamp ? <time>{timestamp}</time> : null}
      <span className="trace-preview" title={preview(value)}>{preview(value)}</span>
    </span>
  );
}

function panel(
  key: string,
  kind: SemanticKind,
  value: unknown,
  options: { status?: string; timestamp?: string; body?: ReactNode } = {},
): NonNullable<CollapseProps["items"]>[number] {
  return {
    key,
    label: <TraceLabel kind={kind} value={value} status={options.status} timestamp={options.timestamp} />,
    children: options.body ?? <pre className="trace-value">{text(value)}</pre>,
  };
}

function contextPanels(context: TurnTraceContext | null): NonNullable<CollapseProps["items"]> {
  if (!context) return [];
  const result: NonNullable<CollapseProps["items"]> = [
    panel("context:system", "system", context.system_message, { timestamp: context.initialized_at }),
  ];
  context.active_skills.forEach((skill, index) => {
    result.push(panel(`context:skill:${index}`, "skill", skill.instructions ?? skill, {
      body: <pre className="trace-value">{json(skill)}</pre>,
    }));
  });
  context.tools.forEach((tool, index) => {
    const kind = tool.origin?.kind === "mcp" ? "mcp" : "tool";
    result.push(panel(`context:tool:${index}`, kind, tool.name, {
      body: <pre className="trace-value">{json(tool)}</pre>,
    }));
  });
  return result;
}

function itemValue(item: TurnItem): unknown {
  return item.text ?? item.message ?? item.content ?? item.summary ?? item;
}

function itemKind(entry: TurnTraceItem): SemanticKind {
  if (entry.role === "developer") return "developer";
  if (entry.role === "user") return "user";
  if (entry.item.type === "reasoning") return "reasoning";
  if (entry.item.type === "text") return "assistant";
  if (entry.item.type === "skill_snapshot") return "skill";
  if (entry.item.type === "retry") return "retry";
  return "tool";
}

function traceItemPanels(items: TurnTraceItem[]): NonNullable<CollapseProps["items"]> {
  const seenGroups = new Set<string>();
  const result: NonNullable<CollapseProps["items"]> = [];
  items.forEach((entry) => {
    const groupId = typeof entry.item.parallel_group_id === "string" ? entry.item.parallel_group_id : "";
    if (groupId && ["tool_call", "tool_result"].includes(entry.item.type)) {
      if (seenGroups.has(groupId)) return;
      seenGroups.add(groupId);
      const groupEntries = items.filter((candidate) => candidate.item.parallel_group_id === groupId);
      const calls = groupEntries
        .filter((candidate) => candidate.item.type === "tool_call")
        .sort((left, right) => Number(left.item.parallel_index ?? 0) - Number(right.item.parallel_index ?? 0));
      const children = calls.map((call) => {
        const callId = String(call.item.call_id ?? "call_unknown");
        const related = groupEntries.filter((candidate) => candidate.item.call_id === callId);
        const terminal = related.find((candidate) => candidate.item.type === "tool_result") ?? call;
        const tool = String(call.item.name ?? terminal.item.tool ?? "工具");
        return panel(`trace:${groupId}:${callId}`, "tool", `${tool} · ${callId}`, {
          status: terminal.item.status,
          timestamp: terminal.completed_at,
          body: <pre className="trace-value">{json(related.map((candidate) => candidate.item))}</pre>,
        });
      });
      const failed = groupEntries.some((candidate) => candidate.item.status === "failed");
      const completedAt = groupEntries[groupEntries.length - 1]?.completed_at;
      result.push(panel(`trace-group:${groupId}`, "tool", "并行调用工具", {
        status: failed ? "failed" : "success",
        timestamp: completedAt,
        body: <Collapse className="trace-parallel-inner-collapse" items={children} />,
      }));
      return;
    }
    const value = itemValue(entry.item);
    result.push(panel(
      `item:${entry.message_idx}:${entry.item_idx}`,
      itemKind(entry),
      value,
      {
        status: entry.item.status,
        timestamp: entry.completed_at,
        body: (
          <pre className="trace-value">
            {typeof value === "string" && entry.item.type !== "tool_call" ? text(value) : json(entry.item)}
          </pre>
        ),
      },
    ));
  });
  return result;
}

function mergeTrace(current: TurnTraceResponse | null, incoming: TurnTraceResponse): TurnTraceResponse {
  if (!current) return incoming;
  const sequences = new Set(current.items.map((item) => item.sequence));
  return {
    ...incoming,
    context: current.context ?? incoming.context,
    items: [...current.items, ...incoming.items.filter((item) => !sequences.has(item.sequence))]
      .sort((left, right) => left.sequence - right.sequence),
  };
}

function TurnTraceContent({ turn, dataIdx, active }: {
  turn: RuntimeStateNode;
  dataIdx: number;
  active: boolean;
}) {
  const [trace, setTrace] = useState<TurnTraceResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Error | string | null>(null);
  const runningRef = useRef(turn.status === "running");
  const previousStatusRef = useRef({ turnId: turn.id, dataIdx, status: turn.status });
  const finalRefreshRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    if (!active) {
      setTrace(null);
      setLoading(false);
      setError(null);
      return;
    }
    let stopped = false;
    let activeController: AbortController | null = null;
    let timer: number | undefined;
    let cursor = 0;
    let hasContext = false;
    let inFlight = false;
    let queuedRefresh = false;
    setTrace(null);
    setError(null);

    const schedule = () => {
      if (!stopped && runningRef.current) timer = window.setTimeout(() => void load(false), 2_000);
    };
    const load = async (showLoading: boolean) => {
      if (inFlight) {
        queuedRefresh = true;
        return;
      }
      inFlight = true;
      if (showLoading) setLoading(true);
      activeController = new AbortController();
      try {
        const value = await getTurnTrace(
          turn.session_id,
          turn.thread_id,
          turn.id,
          dataIdx,
          activeController.signal,
          hasContext ? cursor : undefined,
        );
        if (stopped) return;
        hasContext ||= value.context !== null;
        cursor = Math.max(cursor, value.last_sequence);
        setTrace((current) => mergeTrace(current, value));
        setError(null);
      } catch (reason) {
        if (!stopped && !activeController.signal.aborted) {
          setError(reason instanceof Error ? reason : String(reason));
        }
      } finally {
        inFlight = false;
        if (!stopped && showLoading) setLoading(false);
        if (!stopped && queuedRefresh) {
          queuedRefresh = false;
          void load(false);
        } else {
          schedule();
        }
      }
    };
    const finalRefresh = () => {
      if (timer !== undefined) window.clearTimeout(timer);
      timer = undefined;
      if (inFlight) queuedRefresh = true;
      else void load(false);
    };
    finalRefreshRef.current = finalRefresh;
    void load(true);
    return () => {
      stopped = true;
      if (finalRefreshRef.current === finalRefresh) finalRefreshRef.current = null;
      activeController?.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [active, turn.session_id, turn.thread_id, turn.id, dataIdx]);

  useEffect(() => {
    const previous = previousStatusRef.current;
    runningRef.current = turn.status === "running";
    previousStatusRef.current = { turnId: turn.id, dataIdx, status: turn.status };
    if (
      active
      && previous.turnId === turn.id
      && previous.dataIdx === dataIdx
      && previous.status === "running"
      && turn.status !== "running"
    ) finalRefreshRef.current?.();
  }, [active, turn.id, dataIdx, turn.status]);

  const innerItems = [
    ...contextPanels(trace?.context ?? null),
    ...traceItemPanels(trace?.items ?? []),
  ];

  return (
    <div className="trace-turn-content">
      {error ? <Alert type="error" showIcon title={<ErrorDisplay error={error} />} /> : null}
      {loading && !trace ? <div className="trace-loading"><Spin /></div> : null}
      {trace && innerItems.length > 0
        ? <Collapse
            className="trace-inner-collapse"
            classNames={{ title: "trace-collapse-title" }}
            items={innerItems}
          />
        : null}
      {!loading && !error && innerItems.length === 0
        ? <Empty description="该版本还没有 Trace 上下文或完成 Item" />
        : null}
    </div>
  );
}

function clampDataIdx(turn: RuntimeStateNode, requestedDataIdx: number): number {
  return Math.min(Math.max(requestedDataIdx, 0), Math.max(turn.data.length - 1, 0));
}

export default function TracePage({ turns }: TracePageProps) {
  const orderedTurns = useMemo(
    () => [...turns].sort((left, right) => left.timestamp.localeCompare(right.timestamp) || left.id.localeCompare(right.id)),
    [turns],
  );
  const [activeTurnIds, setActiveTurnIds] = useState<string[]>(() => {
    const latestTurn = orderedTurns[orderedTurns.length - 1];
    return latestTurn ? [latestTurn.id] : [];
  });
  const activeTurnIdSet = useMemo(() => new Set(activeTurnIds), [activeTurnIds]);
  const [versionByTurn, setVersionByTurn] = useState<Record<string, number>>({});

  if (orderedTurns.length === 0) {
    return <div className="trace-page"><Empty description="当前 Thread 还没有 Turn" /></div>;
  }

  const outerItems: CollapseProps["items"] = orderedTurns.map((turn) => {
    const dataIdx = clampDataIdx(turn, versionByTurn[turn.id] ?? turn.current_data_idx);
    const totalVersions = turn.data.length;
    const versionControls = (
      <span className="trace-version-controls" onClick={(event) => event.stopPropagation()}>
        <Button
          type="text"
          aria-label={`${turn.id} 上一个 data 版本`}
          icon={<LeftOutlined />}
          disabled={dataIdx <= 0}
          onClick={() => setVersionByTurn((current) => ({ ...current, [turn.id]: dataIdx - 1 }))}
        />
        <span className="trace-version-label">
          <span className="trace-version-prefix">data </span>{dataIdx + 1}/{totalVersions}
        </span>
        <Button
          type="text"
          aria-label={`${turn.id} 下一个 data 版本`}
          icon={<RightOutlined />}
          disabled={dataIdx >= totalVersions - 1}
          onClick={() => setVersionByTurn((current) => ({ ...current, [turn.id]: dataIdx + 1 }))}
        />
      </span>
    );

    return {
      key: turn.id,
      label: (
        <span className="trace-turn-label">
          <Tag color={turn.status === "failed" ? "red" : "blue"}>Turn</Tag>
          <span className="trace-turn-id" title={turn.id}>{turn.id}</span>
          <Tag color={turn.status === "failed" ? "red" : undefined}>{turn.status}</Tag>
          <time>{turn.timestamp}</time>
        </span>
      ),
      extra: versionControls,
      children: (
        <TurnTraceContent
          turn={turn}
          dataIdx={dataIdx}
          active={activeTurnIdSet.has(turn.id)}
        />
      ),
    };
  });

  return (
    <div className="trace-page">
      <div className="trace-download-actions">
        <Button
          icon={<DownloadOutlined />}
          href={threadTraceDownloadUrl(orderedTurns[0].session_id, orderedTurns[0].thread_id)}
          target="_blank"
          rel="noopener noreferrer"
          download
        >
          下载 Trace
        </Button>
      </div>
      <Collapse
        className="trace-turn-collapse"
        classNames={{ title: "trace-collapse-title" }}
        activeKey={activeTurnIds}
        onChange={(keys) => setActiveTurnIds(Array.isArray(keys) ? keys.map(String) : [String(keys)])}
        items={outerItems}
      />
    </div>
  );
}
