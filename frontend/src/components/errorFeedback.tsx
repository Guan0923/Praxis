import { CloseOutlined } from "@ant-design/icons";
import { Alert, Button } from "antd";
import type { MessageInstance } from "antd/es/message/interface";
import { errorSummary, reportFromError } from "../api/errorReport";
import { ErrorDisplay } from "./ErrorDisplay";

export function showErrorMessage(message: MessageInstance, error: unknown, key = "praxis-action-error") {
  return message.error({
    key,
    duration: 8,
    content: <div style={{ display: "flex", alignItems: "flex-start", gap: 8, minWidth: 0 }}>
      <ErrorDisplay error={error} />
      <Button type="text" size="small" aria-label="关闭错误提示" title="关闭错误提示"
        style={{ flexShrink: 0 }} icon={<CloseOutlined />} onClick={() => message.destroy(key)} />
    </div>,
  });
}

export function ErrorAlerts({ errors }: { errors: readonly unknown[] }) {
  const unique = new Map<string, unknown>();
  for (const error of errors) {
    if (!error) continue;
    const identity = JSON.stringify([errorSummary(error), reportFromError(error)?.traceback ?? ""]);
    unique.set(identity, error);
  }
  if (unique.size === 0) return null;
  return <Alert key={JSON.stringify([...unique.keys()])} type="error" showIcon
    closable={{ closeIcon: <CloseOutlined />, "aria-label": "关闭错误提示" }}
    title={<>{[...unique].map(([key, error]) => <ErrorDisplay key={key} error={error} />)}</>} />;
}
