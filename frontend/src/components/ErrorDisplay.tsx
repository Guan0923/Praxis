import { CopyOutlined } from "@ant-design/icons";
import { Button, Collapse, Tooltip } from "antd";
import { useState } from "react";
import { errorSummary, readErrorReport, reportFromError } from "../api/errorReport";

export function ErrorDisplay({ error, report }: { error?: unknown; report?: unknown }) {
  const diagnostic = readErrorReport(report) ?? reportFromError(error);
  const [copyFailed, setCopyFailed] = useState(false);
  const summary = diagnostic ? diagnostic.type + ": " + diagnostic.message : errorSummary(error);
  return <div style={{ minWidth: 0, maxWidth: "100%", overflowWrap: "anywhere", whiteSpace: "pre-wrap" }}>
    <span>{summary}</span>
    {diagnostic?.traceback ? <Collapse ghost size="small" items={[{
      key: "traceback", label: "错误详情", children: <>
        <Tooltip title="复制错误详情"><Button type="text" aria-label="复制错误详情" icon={<CopyOutlined />} onClick={async () => {
          try {
            await navigator.clipboard.writeText(diagnostic.traceback);
            setCopyFailed(false);
          } catch {
            setCopyFailed(true);
          }
        }} /></Tooltip>
        {copyFailed ? <span role="status">复制失败</span> : null}
        <pre style={{ maxHeight: 320, maxWidth: "100%", overflow: "auto", whiteSpace: "pre-wrap", overflowWrap: "anywhere", margin: 0, fontSize: 12 }}>{diagnostic.traceback}</pre>
      </>,
    }]} /> : null}
  </div>;
}
