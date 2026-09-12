import { PlayCircleOutlined, ReloadOutlined, SaveOutlined } from "@ant-design/icons";
import { Alert, App, Button, Input, Space, Spin, Switch, Tag, Typography } from "antd";
import { useEffect, useState } from "react";
import {
  getMcpSettings, saveMcpSettings, setMcpEnabled, testMcpServer,
  type McpConfigDocument, type McpSettingsResponse,
} from "../../api";
import { ErrorDisplay } from "../ErrorDisplay";

function documentText(data: McpConfigDocument): string {
  return JSON.stringify({ mcpServers: data.mcpServers }, null, 2);
}

export function McpSettingsSection() {
  const { message } = App.useApp();
  const [data, setData] = useState<McpSettingsResponse | null>(null);
  const [text, setText] = useState('{\n  "mcpServers": {}\n}');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toggling, setToggling] = useState(false);
  const [testing, setTesting] = useState<string | null>(null);
  const [error, setError] = useState<Error | string>("");
  const [results, setResults] = useState<Record<string, Error | string>>({});
  const dirty = data !== null && text !== documentText(data);
  const busy = saving || toggling || testing !== null;

  useEffect(() => {
    let active = true;
    getMcpSettings().then((next) => {
      if (!active) return;
      setData(next);
      setText(documentText(next));
    }).catch((cause: unknown) => {
      if (active) setError(cause instanceof Error ? cause : "MCP 配置加载失败");
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  async function save() {
    let document: McpConfigDocument;
    try {
      document = JSON.parse(text) as McpConfigDocument;
    } catch {
      setError("JSON 格式错误，请检查引号、逗号和括号。");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const next = await saveMcpSettings(document);
      setData(next);
      setText(documentText(next));
      setResults({});
      message.success("MCP 配置已保存");
    } catch (cause) {
      setError(cause instanceof Error ? cause : "MCP 配置保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function toggle(enabled: boolean) {
    setToggling(true);
    setError("");
    try {
      const next = await setMcpEnabled(enabled);
      setData((current) => current ? { ...current, enabled: next.enabled } : next);
    } catch (cause) {
      setError(cause instanceof Error ? cause : "MCP 开关保存失败");
    } finally {
      setToggling(false);
    }
  }

  async function test(name: string) {
    setTesting(name);
    setResults((current) => ({ ...current, [name]: "" }));
    try {
      const result = await testMcpServer(name);
      setResults((current) => ({ ...current, [name]:
        `连接成功：${result.protocol_version}；工具 ${result.counts.tools}，资源 ${result.counts.resources}，资源模板 ${result.counts.resource_templates}，提示词 ${result.counts.prompts}` }));
    } catch (cause) {
      setResults((current) => ({ ...current, [name]: cause instanceof Error ? cause : new Error("MCP 连接测试失败") }));
    } finally {
      setTesting(null);
    }
  }

  if (loading) return <div className="user-settings-loading"><Spin /></div>;

  return (
    <div style={{ minWidth: 0, paddingInline: 4 }}>
      <Space wrap style={{ marginBottom: 16 }}>
        <Switch aria-label="启用 MCP" checked={data?.enabled ?? false} loading={toggling}
          disabled={!data || busy} onChange={(enabled) => void toggle(enabled)} />
        <Typography.Text>MCP</Typography.Text>
        {dirty ? <Tag color="warning">未保存</Tag> : null}
      </Space>
      {error ? <Alert type="error" showIcon title={<ErrorDisplay error={error} />} style={{ marginBottom: 12 }} /> : null}
      <Input.TextArea aria-label="MCP JSON 配置" value={text} spellCheck={false} autoComplete="off"
        disabled={!data || busy} onChange={(event) => setText(event.target.value)}
        style={{ height: 420, minHeight: 240, fontFamily: "monospace", resize: "vertical" }} />
      <Space wrap style={{ marginTop: 12, marginBottom: 20 }}>
        <Button type="primary" icon={<SaveOutlined />} loading={saving} disabled={!dirty || busy}
          onClick={() => void save()}>保存配置</Button>
        <Button icon={<ReloadOutlined />} disabled={!dirty || busy} onClick={() => {
          if (data) setText(documentText(data));
          setError("");
        }}>撤销修改</Button>
      </Space>
      {Object.entries(data?.mcpServers ?? {}).map(([name, server]) => (
        <div key={name} style={{ borderTop: "1px solid var(--border-color, #ddd)", padding: "12px 0", minWidth: 0 }}>
          <Space wrap style={{ width: "100%", justifyContent: "space-between" }}>
            <Space wrap>
              <Typography.Text style={{ overflowWrap: "anywhere" }}>{name}</Typography.Text>
              <Tag>{server.type}</Tag>
              {server.disabled ? <Tag>已停用</Tag> : null}
            </Space>
            <Button icon={<PlayCircleOutlined />} aria-label={`测试连接 ${name}`}
              loading={testing === name} disabled={dirty || busy}
              onClick={() => void test(name)}>测试连接</Button>
          </Space>
          {results[name] ? <Alert style={{ marginTop: 8 }} showIcon
            type={results[name] instanceof Error ? "error" : "success"}
            title={<ErrorDisplay error={results[name]} />} /> : null}
        </div>
      ))}
    </div>
  );
}
