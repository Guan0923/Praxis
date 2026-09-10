import { useEffect, useState } from "react";
import { Alert, App, Button, Card, Col, Collapse, Modal, Row, Select, Spin, Statistic, Tag, Typography } from "antd";
import {
  ApiOutlined,
  BarChartOutlined,
  ClockCircleOutlined,
  PlayCircleOutlined,
  StopOutlined,
  TeamOutlined,
  ToolOutlined,
  DownloadOutlined,
  DeleteOutlined,
} from "@ant-design/icons";
import { benchmarkTraceDownloadUrl, listTasks } from "../api";
import type { BenchmarkResult, BenchmarkTaskRun, TaskInfo } from "../types";
import { isActive, useBenchmarkRuns } from "./benchmark/useBenchmarkRuns";

const STATUS_LABEL: Record<string, string> = {
  queued: "排队中", running: "运行中", stopping: "正在停止", completed: "已完成", failed: "执行失败", cancelled: "已停止",
};
const RESOURCE_STATUS: Record<string, string> = {
  not_prepared: "未准备", preparing: "准备中", ready: "已就绪", deleting: "删除中", error: "失败",
};
const RESOURCE_PHASE: Record<string, string> = {
  queued: "等待处理", sources: "下载源码", data: "准备数据及独立依赖", image: "下载镜像", environment: "准备容器", cleanup: "清理任务资源",
  validation: "检查资源", interrupted: "操作中断", prepare: "准备资源", delete: "删除资源",
};
const PHASE_LABEL: Record<string, string> = {
  environment: "准备容器", timeout: "执行超时",
  queued: "等待执行", workspace: "准备目录", application: "初始化", agent: "执行任务", grading: "评分", cleanup: "收尾",
};
const ACTIVITY_LABEL: Record<string, string> = {
  model_request: "请求模型", model_response: "模型已响应", model_retry: "模型请求重试",
  model_delta: "接收模型输出", tool_call: "调用工具", tool_result: "工具已返回", run_finished: "执行结束",
};

const CAPABILITY_LABEL: Record<string, string> = {
  terminal: "终端任务",
  software_engineering: "软件修复",
  tool_workflow: "工具工作流",
  data_processing: "数据处理",
};

function scoreColor(score: number | null | undefined): string {
  if (score == null) return "var(--muted)";
  if (score >= 0.9) return "var(--success)";
  if (score >= 0.5) return "var(--warning)";
  return "var(--error)";
}

function ResultCard({ result, runId, taskRun }: { result: BenchmarkResult; runId: string; taskRun: BenchmarkTaskRun }) {
  const metrics = (result.metrics ?? {}) as Record<string, unknown>;
  const score = (result.score as number | null | undefined) ?? null;
  const verdicts = (result.verdicts ?? []) as Array<Record<string, unknown>>;
  const passed = result.passed;
  const statusLabel = score == null ? "未评分" : passed === true ? "通过" : "未通过";
  const statusColor = score == null ? "default" : passed === true ? "success" : "error";
  const failurePhase = typeof result.failure_phase === "string" ? result.failure_phase : "";

  return (
    <section className="result-card">
      <div className="result-top">
        <Statistic
          className="score"
          title="得分"
          value={score != null ? score * 100 : "未评分"}
          precision={score != null ? 0 : undefined}
          suffix={score != null ? "分" : undefined}
          styles={{ content: { color: scoreColor(score) } }}
        />
        <Tag color={statusColor}>评分：{statusLabel}</Tag>
        {result.error && taskRun.status !== "cancelled" ? <Alert className="error-text" title={String(result.error)} type="error" showIcon /> : null}
        {failurePhase && taskRun.status !== "cancelled" ? <Typography.Text type="secondary">失败阶段：{PHASE_LABEL[failurePhase] ?? failurePhase}</Typography.Text> : null}
      </div>
      <Row className="result-metrics" gutter={[12, 12]}>
        <Col xs={12} sm={8}><Statistic prefix={<ClockCircleOutlined />} title="耗时" value={Number(metrics.duration_ms ?? 0) / 1000} precision={1} suffix="s" /></Col>
        <Col xs={12} sm={8}><Statistic prefix={<ApiOutlined />} title="模型调用" value={Number(metrics.model_calls ?? 0)} suffix="次" /></Col>
        <Col xs={12} sm={8}><Statistic prefix={<ToolOutlined />} title="工具调用" value={Number(metrics.tool_calls ?? 0)} suffix="次" /></Col>
        <Col xs={12} sm={8}><Statistic prefix={<TeamOutlined />} title="子代理完成" value={Number(metrics.subagent_completed ?? 0)} suffix="个" /></Col>
        <Col xs={12} sm={8}><Statistic prefix={<BarChartOutlined />} title="Tokens" value={Number(metrics.total_tokens ?? 0)} /></Col>
      </Row>
      {verdicts.length > 0 ? (
        <ul className="verdicts">
          {verdicts.map((verdict, index) => {
            const ok = Number(verdict.score) >= 1;
            return (
              <li key={index} className={ok ? "ok" : "no"}>
                <Tag color={ok ? "success" : "error"}>{ok ? "✓" : "✗"}</Tag> {String(verdict.detail ?? "")}
              </li>
            );
          })}
        </ul>
      ) : null}
      {result.final_answer ? (
        <Collapse
          className="final-answer"
          size="small"
          items={[{ key: "answer", label: "最终答复", children: <pre>{String(result.final_answer)}</pre> }]}
        />
      ) : null}
      {!isActive(taskRun.status) ? (
        <Button
          className="benchmark-trace-download"
          icon={<DownloadOutlined />}
          href={benchmarkTraceDownloadUrl(runId, taskRun.id)}
          target="_blank"
          rel="noopener noreferrer"
          download
        >
          下载 Trace
        </Button>
      ) : null}
    </section>
  );
}

export default function BenchmarkPage({ active = true }: { active?: boolean }) {
  const { modal } = App.useApp();
  const [tasks, setTasks] = useState<TaskInfo[]>([]);
  const [tasksLoading, setTasksLoading] = useState(true);
  const [sourceFilter, setSourceFilter] = useState("all");
  const [capabilityFilter, setCapabilityFilter] = useState("all");
  const planner = "llm" as const;
  const benchmark = useBenchmarkRuns(active);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    setTasksLoading(true);
    listTasks()
      .then(setTasks)
      .catch((e) => setLoadError(String(e?.message ?? e)))
      .finally(() => setTasksLoading(false));
  }, []);

  const latest = new Map<string, { runId: string; task: BenchmarkTaskRun }>();
  for (const batch of [...benchmark.runs].sort((a, b) => a.created_at.localeCompare(b.created_at))) {
    for (const item of batch.tasks) latest.set(item.task_name, { runId: batch.id, task: item });
  }
  const anyActive = benchmark.runs.some((batch) => isActive(batch.status));
  const batch = [...benchmark.runs].reverse().find((item) => item.total > 1);
  const unavailable = !benchmark.ready || Boolean(benchmark.connectionError);
  const resources = new Map(benchmark.resources.map((item) => [item.task_name, item]));
  const allPrepared = tasks.length > 0 && tasks.every((task) => resources.get(task.name)?.status === "ready");
  const resourceBusy = benchmark.resources.some((item) => item.status === "preparing" || item.status === "deleting");
  const deleteResources = (task: TaskInfo) => {
    const confirm = modal?.confirm ?? Modal.confirm;
    confirm({
      title: `删除“${task.name}”的资源？`,
      content: "历史成绩和运行记录会保留。其他任务仍在使用的共享资源不会删除。",
      okText: "删除资源", cancelText: "取消", okButtonProps: { danger: true },
      onOk: () => benchmark.resourceAction(task.name, "delete"),
    });
  };
  const sources = [...new Set(tasks.map((task) => task.source.benchmark))];
  const visibleTasks = tasks.filter((task) =>
    (sourceFilter === "all" || task.source.benchmark === sourceFilter) &&
    (capabilityFilter === "all" || task.capability === capabilityFilter));

  return (
    <div className="benchmark-page">
      <header className="page-header">
        <h1>Benchmark 成绩单</h1>
        <div className="bench-toolbar">
          <div className="planner-select">
            <label>运行方式</label>
            <span className="muted">真实模型 · 并发上限 3</span>
          </div>
          <Button className="run-all" type="primary" icon={<PlayCircleOutlined />} onClick={() => void benchmark.start()} loading={benchmark.pending.has("all")} disabled={unavailable || anyActive || resourceBusy || !allPrepared || benchmark.pending.size > 0}>
            全部运行
          </Button>
        </div>
      </header>

      <div className="benchmark-filters">
        <Select aria-label="题库来源" value={sourceFilter} onChange={setSourceFilter}
          options={[{ value: "all", label: "全部来源" }, ...sources.map((value) => ({ value, label: value }))]} />
        <Select aria-label="任务类别" value={capabilityFilter} onChange={setCapabilityFilter}
          options={[{ value: "all", label: "全部类别" }, ...Object.entries(CAPABILITY_LABEL).filter(([value]) => tasks.some((task) => task.capability === value)).map(([value, label]) => ({ value, label }))]} />
        <Typography.Text type="secondary">{visibleTasks.length} / {tasks.length} 题 · {tasks[0]?.suite_version}</Typography.Text>
      </div>
      <div className="benchmark-source-scores" aria-live="polite">
        {sources.map((source) => {
          const sourceTasks = tasks.filter((task) => task.source.benchmark === source);
          const results = sourceTasks.map((task) => latest.get(task.name)?.task.result);
          const graded = results.filter((result) => result?.score != null);
          const passed = graded.filter((result) => result?.passed).length;
          return <span key={source}>{source}：{passed} / {sourceTasks.length} 通过 · {graded.length} 已评分</span>;
        })}
      </div>

      {batch ? <div className="benchmark-batch" aria-live="polite">
        <strong>全部运行：{batch.finished} / {batch.total} 项已结束</strong>
        <Tag>{STATUS_LABEL[batch.status]}</Tag>
        {isActive(batch.status) ? <Button icon={<StopOutlined />} danger disabled={batch.status === "stopping" || unavailable} loading={benchmark.pending.has(`stop:${batch.id}`)} onClick={() => void benchmark.stop(batch.id)}>停止整批</Button> : null}
      </div> : null}
      {benchmark.expired ? <Alert type="warning" showIcon title="后端已重启，上次运行记录已失效。" /> : null}
      {benchmark.connectionError ? <Alert type="error" showIcon title={`连接异常：${benchmark.connectionError}`} /> : null}
      {benchmark.actionError ? <Alert type="error" showIcon title={benchmark.actionError} /> : null}

      {loadError ? <Alert className="error-text" title={loadError} type="error" showIcon /> : null}

      {tasksLoading ? (
        <div className="benchmark-loading"><Spin description="正在加载基准任务…" /></div>
      ) : tasks.length === 0 ? (
        <Alert type="info" showIcon title="暂无可运行的基准任务。" />
      ) : (
        <Row className="task-grid" gutter={[16, 16]}>
          {visibleTasks.map((task) => {
            const entry = latest.get(task.name);
            const run = entry?.task;
            const active = run ? isActive(run.status) : false;
            const resource = resources.get(task.name);
            const resourcePending = benchmark.pending.has(`resources:${task.name}`);
            const busy = resourcePending || resource?.status === "preparing" || resource?.status === "deleting";
            return (
              <Col xs={24} lg={12} key={task.name}>
                <Card className="task-card">
                  <div className="task-head">
                    <span className="task-name">{task.name}</span>
                    <span className="task-badges">
                      <Tag className="capability-badge" color="blue">{CAPABILITY_LABEL[task.capability] ?? task.capability}</Tag>
                      <Tag className="source-badge">{task.source.benchmark}</Tag>
                    </span>
                  </div>
                  <p className="task-desc">
                    {task.description} · 难度：{task.difficulty} · 来源：
                    <a href={task.source.url} target="_blank" rel="noreferrer">
                      {task.source.benchmark} / {task.source.task_id}
                    </a>
                  </p>
                  <Typography.Paragraph type="secondary">
                    {task.environment.kind === "docker" ? "Docker" : "本地"}
                  </Typography.Paragraph>
                  <div className="benchmark-resource-status" aria-live="polite">
                    <Tag color={resource?.status === "ready" ? "success" : resource?.status === "error" ? "error" : "default"}>
                      资源：{RESOURCE_STATUS[resource?.status ?? "not_prepared"]}
                    </Tag>
                    {(busy || resource?.status === "error") && resource ? <span>{RESOURCE_PHASE[resource.phase] ?? "处理中"}</span> : null}
                    {resource?.error ? <Alert type="error" showIcon title={resource.error} /> : null}
                  </div>
                  <Collapse
                    className="task-source"
                    size="small"
                    items={[
                      {
                        key: "source",
                        label: "适配说明与许可证",
                        children: <><p>{task.source.adaptation_notes}</p><p>{task.source.license} · {task.source.source_revision}</p></>,
                      },
                    ]}
                  />
                  <Collapse
                    className="task-details"
                    size="small"
                    items={[{
                      key: "details",
                      label: "完整测试内容",
                      children: (
                        <>
                          <Typography.Paragraph>{task.description}</Typography.Paragraph>
                          <Typography.Text strong>测试 Prompt</Typography.Text>
                          <pre className="benchmark-task-prompt">{task.prompt}</pre>
                          <Typography.Text type="secondary">
                            时限：{task.budgets.timeout_seconds / 60} 分钟 · 工具调用：{task.budgets.max_tool_calls ?? "不限次数"}
                          </Typography.Text>
                          {task.tags.length > 0 ? <div className="benchmark-task-tags">{task.tags.map((tag) => <Tag key={tag}>{tag}</Tag>)}</div> : null}
                        </>
                      ),
                    }]}
                  />
                  <div className="task-actions">
                    {task.planner_modes.includes(planner) ? (
                      <Button className="send-btn" type="primary" icon={<PlayCircleOutlined />} onClick={() => void benchmark.start(task.name)} loading={benchmark.pending.has(task.name)} disabled={unavailable || active || busy || resource?.status !== "ready" || benchmark.pending.has("all")}>
                        运行
                      </Button>
                    ) : (
                      <span className="muted">该任务不支持 {planner} 模式</span>
                    )}
                    <Button icon={<DownloadOutlined />} onClick={() => void benchmark.resourceAction(task.name, "prepare")}
                      loading={resource?.status === "preparing"} disabled={unavailable || active || busy || resource?.status === "ready" || benchmark.pending.has(task.name) || benchmark.pending.has("all")}>
                      下载资源
                    </Button>
                    <Button danger icon={<DeleteOutlined />} onClick={() => deleteResources(task)}
                      loading={resource?.status === "deleting"} disabled={unavailable || active || busy || resource?.in_use || !resource?.has_resources || benchmark.pending.has(task.name) || benchmark.pending.has("all")}>
                      删除资源
                    </Button>
                  </div>
                  {run && entry ? <div className="benchmark-run-status" aria-live="polite">
                    <Tag color={run.status === "failed" ? "error" : run.status === "completed" ? "success" : "default"}>状态：{STATUS_LABEL[run.status]}</Tag>
                    {active ? <span>{PHASE_LABEL[run.phase] ?? run.phase}</span> : null}
                    <span>{(run.duration_ms / 1000).toFixed(1)} s</span>
                    {active ? <span className="benchmark-activity">最近活动：{ACTIVITY_LABEL[run.activity] ?? PHASE_LABEL[run.activity] ?? "处理运行事件"} · {new Date(run.updated_at).toLocaleTimeString()}</span> : null}
                    {active ? <Button icon={<StopOutlined />} danger disabled={run.status === "stopping" || unavailable} loading={benchmark.pending.has(`stop:${run.id}`)} onClick={() => void benchmark.stop(entry.runId, run.id)}>停止</Button> : null}
                  </div> : null}
                  {run?.result && entry ? <ResultCard key={run.id} result={run.result} runId={entry.runId} taskRun={run} /> : null}
                </Card>
              </Col>
            );
          })}
        </Row>
      )}
    </div>
  );
}
