import { useEffect, useRef, useState } from "react";
import { Alert, Typography } from "antd";
import type { Terminal as XtermTerminal } from "@xterm/xterm";
import { terminalWebSocketUrl } from "../../api";
import type { RightPanelWindow } from "../../types";
import { useAppearanceMode } from "../../app/AppearanceProvider";
import { terminalTheme } from "../../app/theme";

interface TerminalPaneProps {
  panelWindow: RightPanelWindow;
  readOnly?: boolean;
}

export default function TerminalPane({ panelWindow, readOnly = false }: TerminalPaneProps) {
  const mode = useAppearanceMode();
  const modeRef = useRef(mode);
  modeRef.current = mode;
  const hostRef = useRef<HTMLDivElement | null>(null);
  const terminalRef = useRef<XtermTerminal | null>(null);
  const sequenceRef = useRef(0);
  const exitedRef = useRef(false);
  const [status, setStatus] = useState<"connecting" | "connected" | "exited" | "failed">("connecting");
  const [exitCode, setExitCode] = useState<number | null>(null);

  useEffect(() => {
    const host = hostRef.current;
    const terminalId = panelWindow.terminal_id;
    if (!host || !terminalId) return undefined;
    let disposed = false;
    exitedRef.current = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let resizeObserver: ResizeObserver | undefined;
    let disposeInput: { dispose: () => void } | undefined;

    void Promise.all([
      import("@xterm/xterm"),
      import("@xterm/addon-fit"),
      import("@xterm/xterm/css/xterm.css"),
    ]).then(([{ Terminal }, { FitAddon }]) => {
      if (disposed) return;
      const terminal = new Terminal({
        convertEol: true,
        cursorBlink: true,
        fontFamily: "Cascadia Mono, Consolas, monospace",
        fontSize: 13,
        scrollback: 10_000,
        theme: terminalTheme(modeRef.current),
      });
      const fit = new FitAddon();
      terminal.loadAddon(fit);
      terminal.open(host);
      terminalRef.current = terminal;
      const sendResize = () => {
        if (host.clientWidth === 0 || host.clientHeight === 0) return;
        fit.fit();
        if (!readOnly && socket?.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: "resize", cols: terminal.cols, rows: terminal.rows }));
        }
      };
      resizeObserver = new ResizeObserver(sendResize);
      resizeObserver.observe(host);
      sendResize();

      const connect = () => {
        if (disposed) return;
        setStatus("connecting");
        socket = new WebSocket(terminalWebSocketUrl(terminalId, sequenceRef.current));
        socket.onopen = () => sendResize();
        socket.onmessage = (event) => {
          let payload: { type?: string; data?: string; sequence?: number; code?: number | null };
          try {
            payload = JSON.parse(String(event.data));
          } catch {
            return;
          }
          if (payload.type === "ready") {
            setStatus("connected");
          } else if (payload.type === "output" && typeof payload.data === "string") {
            if (typeof payload.sequence === "number") sequenceRef.current = payload.sequence;
            terminal.write(payload.data);
          } else if (payload.type === "exit") {
            exitedRef.current = true;
            setExitCode(typeof payload.code === "number" ? payload.code : null);
            setStatus("exited");
          }
        };
        socket.onclose = () => {
          if (disposed || exitedRef.current) return;
          setStatus("failed");
          reconnectTimer = window.setTimeout(connect, 1_000);
        };
      };
      disposeInput = terminal.onData((data) => {
        if (!readOnly && socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "input", data }));
      });
      connect();
    }).catch(() => setStatus("failed"));

    return () => {
      disposed = true;
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      socket?.close();
      disposeInput?.dispose();
      resizeObserver?.disconnect();
      terminalRef.current?.dispose();
      terminalRef.current = null;
    };
  }, [panelWindow.terminal_id, readOnly]);

  useEffect(() => {
    if (terminalRef.current) terminalRef.current.options.theme = terminalTheme(mode);
  }, [mode]);

  return (
    <div className="right-panel-terminal">
      <div className="right-panel-terminal-meta">
        <Typography.Text ellipsis title={panelWindow.cwd ?? undefined}>{panelWindow.cwd}</Typography.Text>
        <Typography.Text type="secondary">{panelWindow.terminal_type}</Typography.Text>
      </div>
      {status === "failed" ? <Alert type="warning" showIcon title="终端连接已断开，正在重连" /> : null}
      {status === "exited" ? <Alert type="info" showIcon title={`终端已退出${exitCode === null ? "" : `（${exitCode}）`}`} /> : null}
      <div ref={hostRef} className="right-panel-terminal-host" />
    </div>
  );
}
