import { useEffect, useRef, useState } from "react";
import { EditOutlined, FileTextOutlined } from "@ant-design/icons";
import IconAction from "./IconAction";
import { readEditorFile } from "../api/projects/files";
import { ErrorDisplay } from "./ErrorDisplay";
import MarkdownContent from "./MarkdownContent";
import { openPanelFile, type OpenPanelFile } from "./rightPanel/fileEvents";

export default function PlanFileContent({ sessionId, threadId, path }: { sessionId: string; threadId?: string; path: string }) {
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [updated, setUpdated] = useState(false);
  const highlightTimer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => {
    let disposed = false;
    let generation = 0;
    const refresh = async (highlight = false) => {
      const request = ++generation;
      try {
        const file = await readEditorFile(sessionId, "workspace", "workspace:" + path);
        if (file.kind !== "text") throw new Error("计划文件无法作为文本读取。");
        if (!disposed && request === generation) {
          setContent(file.content); setError(null);
          if (highlight) {
            setUpdated(true);
            clearTimeout(highlightTimer.current);
            highlightTimer.current = setTimeout(() => setUpdated(false), 2500);
          }
        }
      } catch (failure) {
        if (!disposed && request === generation) { setError(failure); setContent(null); }
      }
    };
    const saved = (event: Event) => {
      const target = (event as CustomEvent<OpenPanelFile>).detail;
      if (target.sessionId === sessionId && target.source === "workspace" && target.path.replace(/^workspace:/, "") === path) {
        void refresh(true);
      }
    };
    setContent(null);
    setError(null);
    setUpdated(false);
    void refresh();
    const focused = () => { void refresh(); };
    window.addEventListener("praxis-file-saved", saved);
    window.addEventListener("focus", focused);
    return () => { disposed = true; clearTimeout(highlightTimer.current); window.removeEventListener("praxis-file-saved", saved); window.removeEventListener("focus", focused); };
  }, [sessionId, path]);
  const open = () => openPanelFile({ sessionId, threadId, source: "workspace", path: "workspace:" + path });
  return (
    <section className={"plan-file-result" + (updated ? " is-updated" : "")} data-item-type="tool_result" aria-label="可编辑计划">
      <header className="plan-file-header">
        <FileTextOutlined aria-hidden="true" />
        <strong>实施计划</strong>
        <span className="plan-file-status" role="status">{updated ? "已同步更新" : "可实时编辑"}</span>
        <IconAction label="编辑计划文件" icon={<EditOutlined />} onClick={open} />
        <span className="plan-file-path" title={path}>{path}</span>
      </header>
      <div className="plan-file-body" role="button" tabIndex={0} aria-label="打开计划内容" onClick={(event) => {
        if ((event.target as Element).closest("a, button, input, textarea") || window.getSelection()?.toString()) return;
        open();
      }}
      onKeyDown={(event) => { if (event.target === event.currentTarget && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); open(); } }}>
      {error ? <ErrorDisplay error={error} /> : content === null ? <span role="status">读取计划中…</span> : <MarkdownContent text={content} />}
      </div>
    </section>
  );
}
