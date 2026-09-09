import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  List,
  Modal,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Typography,
} from "antd";
import { DeleteOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  cancelMemoryJob,
  clearMemories,
  consolidateMemory,
  deleteMemory,
  discoverProviderModels,
  dryRunMemory,
  extractMemory,
  getSettings,
  listMemoryEvidence,
  listMemoryInjectionHistory,
  listMemoryItems,
  listMemoryJobs,
  listSidebarThreads,
  restoreMemory,
  setMemoryEnabled,
  updateMemoryConfig,
  type MemoryConfig,
  type MemoryDryRunResult,
  type MemoryEvidence,
  type MemoryInjectionRecord,
  type MemoryItem,
  type MemoryJob,
  type ProviderConfig,
} from "../api";
import type { SidebarThread } from "../types";

const KIND_LABEL = { episodic: "Episodic", semantic: "Semantic", procedural: "Procedural" };
const STATUS_COLOR: Record<string, string> = {
  active: "success",
  disabled: "default",
  deleted: "error",
  superseded: "warning",
  pending: "processing",
  running: "processing",
  succeeded: "success",
  failed: "error",
  cancelled: "default",
};

export default function MemorySettingsSection() {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [config, setConfig] = useState<MemoryConfig | null>(null);
  const [provider, setProvider] = useState<ProviderConfig | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [jobs, setJobs] = useState<MemoryJob[]>([]);
  const [threads, setThreads] = useState<SidebarThread[]>([]);
  const [injections, setInjections] = useState<MemoryInjectionRecord[]>([]);
  const [threadId, setThreadId] = useState<string>();
  const [projectFilter, setProjectFilter] = useState("all");
  const [diagnosticQuery, setDiagnosticQuery] = useState("");
  const [diagnostic, setDiagnostic] = useState<MemoryDryRunResult | null>(null);
  const [evidence, setEvidence] = useState<MemoryEvidence[]>([]);
  const [evidenceItem, setEvidenceItem] = useState<MemoryItem | null>(null);
  const [clearOpen, setClearOpen] = useState(false);
  const [clearText, setClearText] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [settings, memoryItems, memoryJobs, sessionItems, records] = await Promise.all([
        getSettings(),
        listMemoryItems(),
        listMemoryJobs(),
        listSidebarThreads("active"),
        listMemoryInjectionHistory(),
      ]);
      setConfig(settings.memory_config);
      setProvider(settings.provider_config);
      setItems(memoryItems);
      setJobs(memoryJobs.slice().reverse());
      setThreads(sessionItems.filter((value) => !value.archived_at && !value.deleted_at));
      setInjections(records);
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  useEffect(() => {
    if (!provider) return;
    let cancelled = false;
    setModels([]);
    setModelsLoading(true);
    setModelsError(null);
    void discoverProviderModels({
      config_id: provider.id,
      provider_name: provider.provider_name,
      protocol: provider.protocol,
      base_url: provider.base_url,
    }).then((result) => {
      if (!cancelled) setModels(result.models);
    }).catch((value: unknown) => {
      if (!cancelled) setModelsError(value instanceof Error ? value.message : String(value));
    }).finally(() => {
      if (!cancelled) setModelsLoading(false);
    });
    return () => { cancelled = true; };
  }, [provider]);

  const modelOptions = [
    { value: "", label: provider?.model ? `使用当前模型（${provider.model}）` : "使用当前模型" },
    ...Array.from(new Set([
      ...models,
      provider?.model,
      config?.extraction_model,
      config?.consolidation_model,
    ].filter((value): value is string => Boolean(value))))
      .map((value) => ({ value, label: value })),
  ];

  const projectIds = useMemo(
    () => Array.from(new Set(items.map((item) => item.project_id).filter((value): value is string => Boolean(value)))),
    [items],
  );
  const visibleItems = useMemo(
    () => projectFilter === "all"
      ? items
      : items.filter((item) => projectFilter === "global" ? item.project_id === null : item.project_id === projectFilter),
    [items, projectFilter],
  );

  async function saveConfig(next: MemoryConfig) {
    setSaving(true);
    setError(null);
    try {
      setConfig(await updateMemoryConfig(next));
      setNotice("Memory 设置已保存。");
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value));
    } finally {
      setSaving(false);
    }
  }

  async function runAction(action: () => Promise<unknown>, success: string) {
    setSaving(true);
    setError(null);
    try {
      await action();
      setNotice(success);
      await refresh();
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value));
    } finally {
      setSaving(false);
    }
  }

  async function showEvidence(item: MemoryItem) {
    setError(null);
    try {
      setEvidence(await listMemoryEvidence(item.memory_id));
      setEvidenceItem(item);
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value));
    }
  }

  if (loading && config === null) return <div style={{ padding: 32 }}><Spin description="正在加载 Memory…" /></div>;

  return (
    <div className="memory-page">
      <Space orientation="vertical" size="large" style={{ width: "100%" }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 16, alignItems: "center" }}>
          <div>
            <Typography.Title level={4} style={{ margin: 0 }}>记忆</Typography.Title>
          </div>
          <Button icon={<ReloadOutlined />} onClick={() => void refresh()} loading={loading}>刷新</Button>
        </div>
        {error ? <Alert type="error" showIcon title={error} closable onClose={() => setError(null)} /> : null}
        {notice ? <Alert type="success" showIcon title={notice} closable onClose={() => setNotice(null)} /> : null}

        <Card title="记忆系统">
          {config ? (
            <Space orientation="vertical" style={{ width: "100%" }}>
              <Space>
                <Switch aria-label="启用记忆" checked={config.enabled} loading={saving} onChange={(checked) => void saveConfig({ ...config, enabled: checked })} />
                <Typography.Text strong>启用记忆</Typography.Text>
              </Space>
              {modelsError ? <Alert type="warning" showIcon title={`获取模型列表失败：${modelsError}`} /> : null}
              <label htmlFor="memory-extraction-model">提取模型</label>
              <Select
                id="memory-extraction-model"
                aria-label="提取模型"
                style={{ width: "100%" }}
                showSearch={{ optionFilterProp: "label" }}
                loading={modelsLoading}
                disabled={saving}
                options={modelOptions}
                value={config.extraction_model}
                onChange={(value) => void saveConfig({ ...config, extraction_model: value })}
              />
              <label htmlFor="memory-consolidation-model">整理模型</label>
              <Select
                id="memory-consolidation-model"
                aria-label="整理模型"
                style={{ width: "100%" }}
                showSearch={{ optionFilterProp: "label" }}
                loading={modelsLoading}
                disabled={saving}
                options={modelOptions}
                value={config.consolidation_model}
                onChange={(value) => void saveConfig({ ...config, consolidation_model: value })}
              />
            </Space>
          ) : null}
        </Card>

        <Card title="手动任务">
          <Space wrap>
            <Select
              showSearch
              style={{ minWidth: 300 }}
              placeholder="选择要提取的对话"
              value={threadId}
              onChange={setThreadId}
              optionFilterProp="label"
              options={threads.map((value) => ({ value: value.thread_id, label: value.title || value.thread_id }))}
            />
            <Button type="primary" disabled={!threadId || !config?.enabled} loading={saving} onClick={() => threadId && void runAction(() => extractMemory(threadId), "提取任务已排队。")}>手动提取</Button>
            <Button disabled={!config?.enabled} loading={saving} onClick={() => void runAction(() => consolidateMemory(null), "全局整理任务已排队。")}>整理全局记忆</Button>
            {projectIds.map((projectId) => <Button key={projectId} disabled={!config?.enabled} onClick={() => void runAction(() => consolidateMemory(projectId), `项目 ${projectId} 的整理任务已排队。`)}>整理项目 {projectId}</Button>)}
          </Space>
        </Card>

        <Card title={`Memory 条目（${visibleItems.length}）`} extra={
          <Select
            value={projectFilter}
            onChange={setProjectFilter}
            style={{ minWidth: 180 }}
            options={[
              { value: "all", label: "全部范围" },
              { value: "global", label: "仅全局" },
              ...projectIds.map((projectId) => ({ value: projectId, label: `项目 ${projectId}` })),
            ]}
          />
        }>
          <List
            dataSource={visibleItems}
            locale={{ emptyText: <Empty description="暂无 Memory" /> }}
            renderItem={(item) => (
              <List.Item
                actions={[
                  <Button key="evidence" type="link" onClick={() => void showEvidence(item)}>证据</Button>,
                  item.status === "active" ? <Button key="disable" type="link" onClick={() => void runAction(() => setMemoryEnabled(item.memory_id, false), "Memory 已禁用。")}>禁用</Button> : null,
                  item.status === "disabled" ? <Button key="enable" type="link" onClick={() => void runAction(() => setMemoryEnabled(item.memory_id, true), "Memory 已启用。")}>启用</Button> : null,
                  item.status === "deleted" ? <Button key="restore" type="link" onClick={() => void runAction(() => restoreMemory(item.memory_id), "Memory 已恢复。")}>恢复</Button> : null,
                  item.status !== "deleted" ? <Button key="delete" type="link" danger onClick={() => void runAction(() => deleteMemory(item.memory_id), "Memory 已软删除。")}>删除</Button> : null,
                ].filter(Boolean)}
              >
                <List.Item.Meta
                  title={<Space wrap><Typography.Text strong>{item.title}</Typography.Text><Tag>{KIND_LABEL[item.kind]}</Tag><Tag color={STATUS_COLOR[item.status]}>{item.status}</Tag><Tag>{item.project_id ? `项目 ${item.project_id}` : "全局"}</Tag></Space>}
                  description={<Space orientation="vertical" size={2}><Typography.Paragraph ellipsis={{ rows: 3 }} style={{ margin: 0 }}>{item.content}</Typography.Paragraph><Typography.Text type="secondary">置信度 {item.confidence.toFixed(2)} · 更新于 {new Date(item.updated_at).toLocaleString()}</Typography.Text></Space>}
                />
              </List.Item>
            )}
          />
        </Card>

        <Card title="检索诊断">
          <Space orientation="vertical" style={{ width: "100%" }}>
            <Input.Search
              value={diagnosticQuery}
              onChange={(event) => setDiagnosticQuery(event.target.value)}
              placeholder="输入当前任务文字"
              enterButton="测试检索"
              loading={saving}
              onSearch={(query) => void runAction(async () => {
                setDiagnostic(await dryRunMemory(
                  query,
                  projectFilter === "all" || projectFilter === "global" ? undefined : projectFilter,
                ));
              }, "检索诊断已完成。")}
            />
            {diagnostic ? (
              <Typography.Paragraph code style={{ whiteSpace: "pre-wrap", margin: 0 }}>
                {JSON.stringify(diagnostic.result, null, 2)}
              </Typography.Paragraph>
            ) : null}
          </Space>
        </Card>

        <Card title={`任务（${jobs.length}）`}>
          <List
            size="small"
            dataSource={jobs}
            locale={{ emptyText: "暂无任务" }}
            renderItem={(job) => <List.Item actions={job.status === "pending" || job.status === "running" ? [<Button key="cancel" type="link" danger onClick={() => void runAction(() => cancelMemoryJob(job.job_id), "任务已取消。")}>取消</Button>] : []}><Space wrap><Tag>{job.kind}</Tag><Tag color={STATUS_COLOR[job.status]}>{job.status}</Tag><Typography.Text>{job.source_id || "—"}</Typography.Text><Typography.Text type="secondary">尝试 {job.attempts}/{job.max_attempts}</Typography.Text>{job.last_error ? <Typography.Text type="secondary">{job.last_error}</Typography.Text> : null}</Space></List.Item>}
          />
        </Card>

        <Card title="实际注入记录">
          <List
            size="small"
            dataSource={injections}
            locale={{ emptyText: "本进程尚无 Memory 注入记录" }}
            renderItem={(record) => <List.Item><Space orientation="vertical" size={2}><Space><Tag color={record.injected ? "success" : "default"}>{record.injected ? "已注入" : "未注入"}</Tag><Typography.Text>{String(record.session_id || "未知会话")}</Typography.Text><Typography.Text type="secondary">{String(record.operation || "普通请求")}</Typography.Text></Space><Typography.Text code>{JSON.stringify(record.selected_ids || [])}</Typography.Text></Space></List.Item>}
          />
        </Card>

        <Card title="危险操作" styles={{ header: { color: "#cf1322" } }}>
          <Space orientation="vertical">
            <Typography.Text>清空会取消活动任务，并删除全部记忆、证据、候选和处理进度。数据库结构保留。</Typography.Text>
            <Button danger icon={<DeleteOutlined />} onClick={() => setClearOpen(true)}>清空全部 Memory</Button>
          </Space>
        </Card>
      </Space>

      <Modal title={`Evidence · ${evidenceItem?.title || ""}`} open={Boolean(evidenceItem)} footer={null} onCancel={() => setEvidenceItem(null)} width={760}>
        <List dataSource={evidence} locale={{ emptyText: "暂无证据" }} renderItem={(value) => <List.Item><Space orientation="vertical"><Typography.Text>{value.excerpt}</Typography.Text><Typography.Text type="secondary">会话 {value.session_id} · {value.source_kind}</Typography.Text></Space></List.Item>} />
      </Modal>

      <Modal
        title="确认清空全部 Memory"
        open={clearOpen}
        okText="永久清空"
        okButtonProps={{ danger: true, disabled: clearText !== "CLEAR ALL MEMORIES", loading: saving }}
        onCancel={() => { setClearOpen(false); setClearText(""); }}
        onOk={() => void runAction(() => clearMemories(clearText), "Memory 已清空。").then(() => { setClearOpen(false); setClearText(""); })}
      >
        <Typography.Paragraph>请输入 <Typography.Text code>CLEAR ALL MEMORIES</Typography.Text> 以确认。</Typography.Paragraph>
        <Input aria-label="清空确认文字" value={clearText} onChange={(event) => setClearText(event.target.value)} autoComplete="off" />
      </Modal>
    </div>
  );
}
