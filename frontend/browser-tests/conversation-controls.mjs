import { chromium } from "playwright";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const base = process.argv[2] ?? "http://127.0.0.1:8127";
const output = new URL("../../.test-tmp/interface-browser-20260912/", import.meta.url);
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const timings = [];
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
const editor = page.getByLabel("聊天输入", { exact: true });
async function operation(name, suffix, action, method = "POST") {
  const started = performance.now();
  const response = page.waitForResponse((value) => value.request().method() === method
    && new URL(value.url()).pathname.endsWith(suffix));
  await action();
  const result = await response;
  timings.push({ name, status: result.status(), ms: Math.round(performance.now() - started) });
  if (!result.ok()) throw new Error(name + ": " + await result.text());
  return result.status() === 204 ? null : result.json();
}
try {
  await page.goto(base);
  await page.request.post(base + "/fixture/release?reset=true");
  await editor.fill("first browser message");
  const first = await operation("send", "/api/turns", () => editor.press("Enter"));
  await page.getByRole("button", { name: "Fork", exact: true }).last().waitFor();
  const fork = await operation("fork", "/fork", () => page.getByRole("button", { name: "Fork", exact: true }).last().click());
  await page.waitForURL("**/" + fork.sidebar_thread.thread_id);
  await editor.fill("hold browser output");
  const running = await operation("send after fork", "/api/turns", () => editor.press("Enter"));
  if (running.parent_id !== fork.turn.id) throw new Error("Fork continuation lost its parent");
  await page.getByRole("button", { name: "暂停", exact: true }).waitFor();
  await editor.fill("edit this queued message");
  const queued = await operation("queue create", "/queued-messages", () => editor.press("Enter"));
  await operation("queue edit", "/queued-messages/" + queued.id, () => page.getByRole("button", { name: "编辑第 1 条待发送消息" }).click(), "DELETE");
  await editor.fill("delete this queued message");
  const discarded = await operation("queue create again", "/queued-messages", () => editor.press("Enter"));
  await operation("queue delete", "/queued-messages/" + discarded.id, () => page.getByRole("button", { name: "删除第 1 条待发送消息" }).click(), "DELETE");
  for (const text of ["batch first", "batch second"]) {
    await editor.fill(text);
    await operation("queue batch", "/queued-messages", () => editor.press("Enter"));
  }
  await operation("queue send", "/steer", () => page.getByRole("button", { name: "追加指令", exact: true }).click());
  await page.locator(".queued-message-list").waitFor({ state: "hidden" });
  await page.getByRole("button", { name: "发送", exact: true }).waitFor();
  await editor.fill("hold pause test");
  await operation("send for pause", "/api/turns", () => editor.press("Enter"));
  await operation("pause", "/pause", () => page.getByRole("button", { name: "暂停", exact: true }).click());
  await operation("resume", "/resume", () => page.getByRole("button", { name: "继续", exact: true }).click());
  await page.request.post(base + "/fixture/release");
  await page.getByRole("button", { name: "发送", exact: true }).waitFor();
  await page.getByRole("button", { name: "编辑", exact: true }).last().click();
  await page.getByRole("textbox", { name: "编辑用户消息", exact: true }).fill("rewritten browser message");
  const rewound = await operation("rewind", "/rewind", () => page.getByRole("button", { name: "保存并重新生成", exact: true }).click());
  await page.waitForFunction(async ({ sid, tid, turnId }) => {
    const result = await fetch("/api/turns/history?session_id=" + sid + "&thread_id=" + tid).then((response) => response.json());
    return result.turns.some((turn) => turn.id === turnId && turn.current_data_idx > 0 && turn.status === "success");
  }, { sid: first.session_id, tid: fork.sidebar_thread.thread_id, turnId: rewound.turn_id });
  await page.getByRole("button", { name: "Fork", exact: true }).last().waitFor({ state: "visible" });
  await operation("open branch panel", "/api/right-panel/" + first.session_id, () => page.getByRole("button", { name: "打开右侧边栏", exact: true }).click(), "PATCH");
  await operation("branch files", "/files", () => page.getByRole("button", { name: "打开文件", exact: true }).click());
  await page.getByRole("tab", { name: /文件/ }).waitFor();
  await page.locator(".history-entry-button").nth(1).click();
  await page.waitForURL("**/" + first.session_id + "/" + first.session_id);
  if (await page.getByRole("tab", { name: /文件/ }).isVisible()) throw new Error("Branch files leaked into main conversation");
  await page.locator(".history-entry-button").first().click();
  await page.waitForURL("**/" + fork.sidebar_thread.thread_id);
  await page.getByRole("tab", { name: /文件/ }).waitFor();
  const selectedUrl = page.url();
  let historyReads = 0;
  const countHistory = (request) => { if (new URL(request.url()).pathname === "/api/turns/history") historyReads += 1; };
  page.on("request", countHistory);
  await operation("sidebar reorder", "/order", async () => {
    const rows = page.locator(".history-list-item");
    const from = await rows.nth(0).boundingBox();
    const to = await rows.nth(1).boundingBox();
    await page.mouse.move(from.x + 35, from.y + from.height / 2);
    await page.mouse.down();
    await page.mouse.move(from.x + 35, from.y + from.height / 2 + 10, { steps: 3 });
    await page.locator(".history-list-item.is-dragging").waitFor();
    await page.mouse.move(to.x + 35, to.y + to.height * 0.75, { steps: 10 });
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await page.mouse.up();
  }, "PUT");
  page.off("request", countHistory);
  if (page.url() !== selectedUrl) throw new Error("Reordering changed the selected conversation");
  console.log(JSON.stringify({ reorderHistoryReads: historyReads, panelIsolation: true }));
  if (await page.getByText("该操作正在处理中。", { exact: true }).count()) throw new Error("Unexpected operation pending error");
  console.log(JSON.stringify({ firstTurn: first.id, forkTurn: fork.turn.id, verified: "controls complete" }));
} finally {
  await page.screenshot({ path: fileURLToPath(new URL("controls-desktop.png", output)), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.screenshot({ path: fileURLToPath(new URL("controls-mobile.png", output)), fullPage: true, animations: "disabled" });
  console.log(JSON.stringify({ timings, errors, url: page.url() }));
  await browser.close();
}
