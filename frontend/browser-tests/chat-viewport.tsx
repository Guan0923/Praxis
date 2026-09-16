import { useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { flushSync } from "react-dom";
import { App } from "antd";
import type { TextAreaRef } from "antd/es/input/TextArea";
import { ChatMessageList } from "../src/pages/chat/ChatMessageList";
import { useChatScroll } from "../src/pages/chat/useChatScroll";
import { receiveView, ViewStateContext } from "../src/app/viewState";
import type { ChatMessage } from "../src/types";
import "../src/styles/index.css";

let saved = { session_id: "virtual-fixture", thread_id: "virtual-fixture", revision: 1,
  draft: "", references: [], uploads: [], reading: null, expanded: {} };
receiveView(saved);
// No backend or model calls are allowed from this isolated verification page.
window.fetch = async (_input, init) => {
  if (init?.body) saved = { ...saved, ...JSON.parse(String(init.body)), revision: saved.revision + 1 };
  return Response.json(saved);
};
const noop = () => {};
const asyncNoop = async () => {};
const initial: ChatMessage[] = Array.from({ length: 60 }, (_, index) => ({
  id: "message-" + index, role: index % 2 ? "assistant" : "user", events: [],
  content: index % 2 ? "" : "Question " + index,
  items: index % 2 ? [
    { type: "tool_call", name: "read_file", call_id: "tool-" + index, arguments: { path: "fixture.txt" }, status: "success" },
    { type: "text", text: Array.from({ length: 18 }, (_, paragraph) =>
      "## Section " + index + "." + paragraph + "\n\n" + "A paragraph with enough text to wrap across several lines. ".repeat(8)).join("\n\n"), status: "success" },
  ] : undefined,
}));
const frame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
async function settle() { for (let index = 0; index < 18; index++) await frame(); }

function Harness() {
  const [messages, setMessages] = useState(initial);
  const [width, setWidth] = useState(900);
  const [active, setActive] = useState(true);
  const [report, setReport] = useState("Ready");
  const editRef = useRef<TextAreaRef>(null);
  const scroll = useChatScroll("virtual-fixture", messages, active);
  async function run() {
    try {
      const root = scroll.chatScrollRef.current!;
      const stats = () => ({ blocks: root.querySelectorAll("[data-virtual-block]").length,
        mounted: root.querySelectorAll('[data-mounted="true"]').length,
        nodes: root.querySelectorAll("*").length });
      const check = (value: boolean, message: string) => { if (!value) throw new Error(message); };
      const probe = () => {
        const top = root.getBoundingClientRect().top;
        return [...root.querySelectorAll<HTMLElement>('[data-mounted="true"]')]
          .find((element) => element.getBoundingClientRect().top >= top && element.getBoundingClientRect().top < top + root.clientHeight);
      };
      await settle();
      const initialStats = stats();
      check(initialStats.mounted < initialStats.blocks / 3, "Too many initial blocks mounted");
      check(Math.abs(root.scrollHeight - root.clientHeight - root.scrollTop) <= 2, "Initial bottom anchor failed");
      scroll.scrollToPosition(root.scrollHeight * 0.4);
      await settle();
      const anchor = probe();
      check(Boolean(anchor), "No visible body after scrolling");
      const id = anchor!.dataset.virtualBlock!;
      const before = anchor!.getBoundingClientRect().top;
      const update = () => setMessages((current) => [...current.slice(0, -1), {
        ...current[current.length - 1], running: true,
        items: [...(current[current.length - 1].items ?? []), { type: "text", status: "running", text: "Streaming update\n\n" + "Additional text. ".repeat(100) }],
      }]);
      flushSync(update);
      await settle();
      const after = root.querySelector<HTMLElement>('[data-virtual-block="' + CSS.escape(id) + '"]')!.getBoundingClientRect().top;
      check(Math.abs(before - after) <= 2, "Reading anchor moved during stream: " + (after - before));
      flushSync(() => setWidth(660));
      await settle();
      const resized = root.querySelector<HTMLElement>('[data-virtual-block="' + CSS.escape(id) + '"]')!.getBoundingClientRect().top;
      check(Math.abs(before - resized) <= 2, "Reading anchor moved during resize: " + (resized - before));
      flushSync(() => setActive(false));
      await settle();
      flushSync(() => setActive(true));
      await settle();
      const revealed = root.querySelector<HTMLElement>('[data-virtual-block="' + CSS.escape(id) + '"]')!.getBoundingClientRect().top;
      check(Math.abs(resized - revealed) <= 2, "Reading anchor moved after returning: " + (revealed - resized));
      flushSync(() => setMessages((current) => [{ id: "earlier", role: "user", content: "Earlier history", events: [] }, ...current]));
      await settle();
      const prepended = root.querySelector<HTMLElement>('[data-virtual-block="' + CSS.escape(id) + '"]')!.getBoundingClientRect().top;
      check(Math.abs(revealed - prepended) <= 2, "Reading anchor moved after history prepend: " + (prepended - revealed));
      scroll.scrollToPosition(root.scrollHeight);
      await settle();
      check(Math.abs(root.scrollHeight - root.clientHeight - root.scrollTop) <= 2, "Bottom follow failed");
      scroll.scrollToPosition(0);
      await settle();
      check(root.innerText.includes("Question 0"), "First message did not remount");
      const tool = root.querySelector<HTMLElement>('.runtime-collapse [role="button"]');
      check(Boolean(tool), "Tool did not remount");
      tool!.click();
      await settle();
      const toolBlock = tool!.closest<HTMLElement>("[data-virtual-block]")!;
      const toolId = toolBlock.dataset.virtualBlock!;
      scroll.scrollToPosition(root.scrollHeight);
      await settle();
      check(!toolBlock.querySelector('.ant-collapse'), "Offscreen tool was not unloaded");
      scroll.scrollToPosition(0);
      await settle();
      const restored = root.querySelector('[data-virtual-block="' + CSS.escape(toolId) + '"] [aria-expanded="true"]');
      check(Boolean(restored), "Expansion state was lost after remount");
      setReport(JSON.stringify({ status: "passed", initial: initialStats, final: stats(), streamDrift: after - before, resizeDrift: resized - before }));
    } catch (error) { setReport(JSON.stringify({ status: "failed", error: String(error) })); }
  }
  return <App><button onClick={() => void run()}>Run viewport checks</button><output data-testid="report">{report}</output>
    <ViewStateContext.Provider value="virtual-fixture/virtual-fixture"><div style={{ width, maxWidth: "100vw", height: 600, display: active ? "flex" : "none" }}>
      <ChatMessageList messages={messages} sessionId="virtual-fixture" threadId="virtual-fixture" display="medium"
        interactionBusy={false} compactionPending={false} chatScrollRef={scroll.chatScrollRef} onScroll={scroll.handleScroll}
        editingMessageId={null} editingDraft="" editRef={editRef} rewindPending={false} editingSubmitting={false} canEdit={false}
        setEditingDraft={noop} cancelEdit={noop} saveEdit={asyncNoop} beginEdit={noop} handleUserBubbleClick={noop}
        messageVersion={() => undefined} changeMessageVersion={asyncNoop} onDecision={asyncNoop} />
    </div></ViewStateContext.Provider></App>;
}
createRoot(document.getElementById("root")!).render(<Harness />);
