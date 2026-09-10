import { useState } from "react";
import { createRoot } from "react-dom/client";
import { flushSync } from "react-dom";
import { App } from "antd";
import AgentShell, { type AgentShellProps } from "../src/app/AgentShell";
import type { ChatMessage, Conversation, Page } from "../src/types";
import "../src/styles/index.css";

// This test page never talks to the user's backend or model provider.
const requests: string[] = [];
const nativeFetch = window.fetch.bind(window);
window.fetch = async (input, init) => {
  const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, location.href);
  if (!url.pathname.startsWith("/api/") && !url.pathname.startsWith("/benchmark/")) return nativeFetch(input, init);
  requests.push(url.pathname);
  let value: unknown;
  if (url.pathname.startsWith("/api/right-panel/")) value = {
    state: { session_id: "fixture", width: 420, collapsed: !new URLSearchParams(location.search).has("panel"), active_window_id: null },
    windows: [], capabilities: { terminal_available: false, terminal_unavailable_reason: "Fixture" },
  };
  else if (url.pathname === "/benchmark/runs") value = { instance_id: "scroll-fixture", runs: [] };
  else if (["/benchmark/tasks", "/benchmark/resources", "/api/skills"].includes(url.pathname)) value = [];
  else return Response.json({ detail: `Unexpected fixture request: ${url.pathname}` }, { status: 404 });
  return Response.json(value);
};
const NativeSocket = window.WebSocket;
window.WebSocket = class {
  static OPEN = 1;
  readyState = 1;
  onmessage?: (event: { data: string }) => void;
  constructor(url: string | URL, protocols?: string | string[]) {
    if (!String(url).includes("/api/window-control/ws")) return new NativeSocket(url, protocols);
    queueMicrotask(() => this.reply({ type: "ready", token: "fixture", generation: 1 }));
  }
  reply(data: unknown) { this.onmessage?.({ data: JSON.stringify(data) }); }
  send(data: string) {
    const request = JSON.parse(data);
    queueMicrotask(() => this.reply({ ...request, writable: true, seq: 0, ack: 0 }));
  }
  close() { this.readyState = 3; }
} as unknown as typeof WebSocket;

const noOp = async () => undefined;
const initialMessages: ChatMessage[] = Array.from({ length: 36 }, (_, index) => ({
  id: `message-${index}`, role: index % 2 ? "assistant" : "user", events: [],
  content: `## Message ${index}\n\n${"Long paragraph for stable scrolling and line wrapping. ".repeat(18)}\n\n\`\`\`text\nlocal fixture only\n\`\`\``,
}));
initialMessages[1].items = [{ type: "tool_call", tool: "read_file", name: "read_file", call_id: "fixture-tool", arguments: { path: "fixture.txt" }, status: "success" },
  { type: "tool_result", call_id: "fixture-tool", tool: "read_file", status: "success", content: "Local tool result" }];
initialMessages[35].running = true;
initialMessages[35].items = [{ type: "text", text: initialMessages[35].content, status: "running" }];

const frame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
const settle = async () => { await frame(); await frame(); await frame(); };

function Harness() {
  const [page, setPage] = useState<Page>("chat");
  const [messages, setMessages] = useState(initialMessages);
  const [report, setReport] = useState("Ready");
  const conversation: Conversation = { id: "fixture", sessionId: "fixture", threadId: "fixture", title: "Scroll fixture", messagesLoaded: true, messages, runtimeNodes: [] };
  const actions = Object.fromEntries([
    "onNewProject", "onNewProjectConversation", "onRemoveProject", "onRenameProject", "onChangeProjectPath",
    "onRevokeSkillTrust", "onRestoreProject", "onRename", "onArchive", "onDelete", "onReorderSidebar", "onSortSidebar",
    "onRestore", "onProfileUpdate", "onUpdate", "onModeChange", "onPanelModeChange", "onHydratePanelConversation",
    "onForgetPanelConversation", "onFork", "onRewind", "onRewindPanel", "onReload", "onReloadPanel", "onRefresh",
    "onRun", "onStopRun", "onDisplayModeUpdate", "onProviderConfigUpdate", "setSettingsOpen", "onProfileChange",
  ].map((name) => [name, noOp]));
  const props = {
    ...actions, profile: { display_name: "Fixture", agent_preferences: "" }, page, current: conversation,
    activeConversations: [conversation], panelConversations: {}, projects: [], removedProjects: [], archivedConversations: [],
    unreadArchivedCount: 0, modeBySession: {}, draftMode: "agent", displayMode: "developer", providerConfig: null,
    settingsOpen: false, onNew: async () => "fixture", onEnsureSession: async () => "fixture",
    onReload: async () => { requests.push("/fixture/history/nodes"); },
    onReloadPanel: async () => { requests.push("/fixture/history/nodes"); },
    onSelectSession: async () => "fixture", onSelect: () => setPage("chat"), onNavigate: setPage,
    sandboxHealth: { phase: "healthy", installed: true, code: null, detail: null, checking: false,
      autoRecoveryPhase: "idle", nextRetryAt: null, manualRepairing: false, check: noOp,
      notifyUserBackendRequest: () => undefined, repairManually: noOp },
  } as unknown as AgentShellProps;

  async function check() {
    try {
      setReport("Running");
      flushSync(() => setPage("chat"));
      await settle();
      const scroll = document.querySelector<HTMLDivElement>("[data-conversation-scroll]")!;
      const chat = document.querySelector(".chat-page");
      const markdown = document.querySelector(".markdown");
      const editor = document.querySelector<HTMLElement>("[contenteditable=true]");
      const draft = editor?.textContent;
      const tool = document.querySelector<HTMLElement>(".runtime-collapse .ant-collapse-header");
      if (!editor || !draft?.trim() || !tool) throw new Error("Enter a nonempty draft before running checks; real editor and tool are required.");
      if (tool.getAttribute("aria-expanded") !== "true") tool.click();
      await new Promise((resolve) => setTimeout(resolve, 350));
      const toolExpanded = tool?.getAttribute("aria-expanded");
      const historyRequests = requests.filter((url) => /nodes|turns/.test(url)).length;
      const samples: unknown[] = [];
      const require = (ok: boolean, message: string) => { if (!ok) throw new Error(message); };
      const setTop = (top: number) => {
        scroll.dispatchEvent(new WheelEvent("wheel"));
        scroll.scrollTop = top;
        scroll.dispatchEvent(new Event("scroll", { bubbles: true }));
      };
      for (const destination of ["benchmark", "trash", "benchmark", "trash"] as const) {
        for (const atBottom of [false, true]) {
          setTop(atBottom ? scroll.scrollHeight : 1050);
          await settle();
          const before = scroll.scrollTop;
          flushSync(() => setPage(destination));
          await settle();
          const hiddenHeight = scroll.clientHeight;
          // Feed continued output while the chat is hidden, without a model request.
          const appendOutput = () => flushSync(() => setMessages((items) => items.map((item, index) => {
            if (index !== items.length - 1) return item;
            const content = `${item.content}\n\n${"continued output ".repeat(80)}`;
            return { ...item, content, items: [{ type: "text", text: content, status: "running" }] };
          })));
          appendOutput();
          await frame();
          appendOutput();
          flushSync(() => setPage("chat"));
          const positions = [scroll.scrollTop];
          for (let index = 0; index < 5; index += 1) { await frame(); positions.push(scroll.scrollTop); }
          const target = atBottom ? scroll.scrollHeight - scroll.clientHeight : before;
          require(positions.every((top) => Math.abs(top - target) <= 2), `${destination}: position changed ${positions} vs ${target}`);
          require(document.querySelector(".chat-page") === chat && document.querySelector(".markdown") === markdown, "Message nodes were replaced");
          require(document.querySelector("[contenteditable=true]") === editor && editor?.textContent === draft, "Draft was replaced");
          require(tool?.getAttribute("aria-expanded") === toolExpanded, "Tool expansion changed");
          const composer = document.querySelector(".composer")!;
          require(getComputedStyle(composer).animationName === "none" && getComputedStyle(composer).opacity === "1", "Composer entry animation replayed");
          samples.push({ destination, atBottom, before, target, positions, hiddenHeight });
        }
      }
      require(requests.filter((url) => /nodes|turns/.test(url)).length === historyRequests, "History was loaded again");
      setTop(1050);
      setReport(JSON.stringify({ passed: true, viewport: { width: innerWidth, height: innerHeight }, samples, draft, toolExpanded, historyRequests, requests }, null, 2));
    } catch (error) { setReport(JSON.stringify({ passed: false, error: String(error), requests })); }
  }
  return <App>
    <AgentShell {...props} />
    <aside style={{ position: "fixed", right: 8, top: 4, zIndex: 100, background: "white", color: "black", maxWidth: 320 }}>
      <button onClick={() => void check()}>Run navigation checks</button>
      <details><summary>Results</summary><pre data-testid="report" style={{ maxHeight: 200, overflow: "auto", fontSize: 10 }}>{report}</pre></details>
    </aside>
  </App>;
}
const root = createRoot(document.getElementById("root")!);
root.render(<Harness />);
if (import.meta.hot) import.meta.hot.dispose(() => root.unmount());
