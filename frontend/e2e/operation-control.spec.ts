import { expect, test, type BrowserContext, type Page } from "@playwright/test";

interface SidebarThread {
  session_id: string;
  thread_id: string;
  title: string;
  message_count?: number;
}

async function createConversation(page: Page, title: string): Promise<SidebarThread> {
  const response = await page.request.post("/api/sidebar-threads", { data: { title } });
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBeTruthy();
  return response.json() as Promise<SidebarThread>;
}

async function openConversation(page: Page, title: string): Promise<void> {
  await page.getByRole("button", { name: title, exact: true }).click();
}

async function deleteEmptyConversations(page: Page): Promise<void> {
  const response = await page.request.get("/api/sidebar-threads?state=all");
  const threads = await response.json() as SidebarThread[];
  await Promise.all(threads
    .filter((thread) => (thread.message_count ?? 0) === 0)
    .map((thread) => page.request.delete(`/api/sidebar-threads/${encodeURIComponent(thread.thread_id)}`)));
}

async function expectWritable(page: Page): Promise<void> {
  await expect(page.getByLabel("聊天输入")).toHaveAttribute("contenteditable", "true", { timeout: 10_000 });
}

async function expectReadOnly(page: Page): Promise<void> {
  const editor = page.getByLabel("聊天输入");
  await expect(editor).toHaveAttribute("contenteditable", "false", { timeout: 10_000 });
  await expect(editor).toHaveAttribute("placeholder", "当前 session 正在另一个窗口对话");
}

async function setOffline(context: BrowserContext, offline: boolean): Promise<void> {
  await context.setOffline(offline);
}

async function trackWindowControlSockets(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const sockets: WebSocket[] = [];
    Object.defineProperty(window, "__miniAgentWindowControlSockets", { value: sockets });
    window.WebSocket = new Proxy(window.WebSocket, {
      construct(target, args) {
        const socket = Reflect.construct(target, args) as WebSocket;
        if (String(args[0]).includes("/api/window-control/ws")) sockets.push(socket);
        return socket;
      },
    });
  });
}

async function closeWindowControlSocket(page: Page): Promise<void> {
  await page.evaluate(() => {
    const sockets = (window as typeof window & { __miniAgentWindowControlSockets?: WebSocket[] })
      .__miniAgentWindowControlSockets ?? [];
    const socket = [...sockets].reverse().find((candidate) => candidate.readyState === WebSocket.OPEN);
    if (!socket) throw new Error("No open window-control WebSocket was found.");
    socket.close(4000, "e2e disconnect");
  });
}

test("serializes duplicate send and transfers one session after the reconnect grace", async ({ page, browser }) => {
  test.setTimeout(90_000);
  await trackWindowControlSockets(page);
  const suffix = crypto.randomUUID().slice(0, 8);
  const sessionBTitle = `Window Session B ${suffix}`;
  const sessionATitle = `Window Session A ${suffix}`;
  await createConversation(page, sessionBTitle);
  const sessionA = await createConversation(page, sessionATitle);

  await page.goto("/app");
  await openConversation(page, sessionATitle);
  await expectWritable(page);

  const requests: import("@playwright/test").Request[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith("/api/turns")) requests.push(request);
  });
  const responsePromise = page.waitForResponse((response) => (
    response.request().method() === "POST" && response.url().endsWith("/api/turns")
  ));
  await page.getByLabel("聊天输入").fill("operation control duplicate send");
  await page.getByRole("button", { name: "发送", exact: true }).evaluate((button) => {
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  const response = await responsePromise;
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBeTruthy();
  expect(requests).toHaveLength(1);
  expect(requests[0].headers()["x-mini-agent-seq"]).toBeTruthy();
  expect(requests[0].headers()["x-mini-agent-ack"]).toBeTruthy();
  expect(response.headers()["x-mini-agent-seq"]).toBeTruthy();
  expect(response.headers()["x-mini-agent-ack"]).toBeTruthy();
  await expect(page.locator(".message.user").last()).toContainText("operation control duplicate send");

  await expect.poll(async () => {
    const turnsResponse = await page.request.get(`/api/turns?session_id=${encodeURIComponent(sessionA.session_id)}`);
    expect(turnsResponse.ok()).toBeTruthy();
    const turns = await turnsResponse.json() as Array<{
      data?: Array<Array<{ role: string; content: Array<{ text?: string }> }>>;
    }>;
    return turns.flatMap((turn) => turn.data ?? []).flatMap((version) => version)
      .filter((message) => message.role === "user")
      .filter((message) => message.content.some((item) => item.text === "operation control duplicate send"))
      .length;
  }, { timeout: 15_000 }).toBe(1);

  const secondContext = await browser.newContext();
  const secondPage = await secondContext.newPage();
  try {
    await secondPage.goto("/app");
    await openConversation(secondPage, sessionATitle);
    await expectReadOnly(secondPage);

    await openConversation(secondPage, sessionBTitle);
    await expectWritable(secondPage);
    await openConversation(page, sessionBTitle);
    await expectReadOnly(page);
    await openConversation(page, sessionATitle);
    await expectWritable(page);
    await openConversation(secondPage, sessionATitle);
    await expectReadOnly(secondPage);

    await secondPage.waitForTimeout(3_500);
    await expectReadOnly(secondPage);

    await closeWindowControlSocket(page);
    await page.waitForTimeout(1_000);
    await expectWritable(page);
    await expectReadOnly(secondPage);

    await closeWindowControlSocket(page);
    await page.waitForTimeout(100);
    await setOffline(page.context(), true);
    await expectWritable(secondPage);
    await setOffline(page.context(), false);
    await expectReadOnly(page);
  } finally {
    await setOffline(page.context(), false).catch(() => undefined);
    await secondContext.close();
  }
});

test("accepts one new conversation action from synchronous double click", async ({ page }) => {
  await deleteEmptyConversations(page);
  await page.goto("/app");
  const beforeResponse = await page.request.get("/api/sidebar-threads?state=all");
  const before = await beforeResponse.json() as SidebarThread[];
  const createRequests: import("@playwright/test").Request[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith("/api/sidebar-threads")) createRequests.push(request);
  });
  const createdResponse = page.waitForResponse((response) => (
    response.request().method() === "POST" && response.url().endsWith("/api/sidebar-threads")
  ));

  await page.getByRole("button", { name: "新建对话", exact: true }).evaluate((button) => {
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  const response = await createdResponse;
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBeTruthy();
  expect(createRequests).toHaveLength(1);
  await expect(page.getByLabel("聊天输入")).toBeVisible();

  const afterResponse = await page.request.get("/api/sidebar-threads?state=all");
  const after = await afterResponse.json() as SidebarThread[];
  expect(after).toHaveLength(before.length + 1);
});

test("pauses a group when the write succeeds but its response is lost", async ({ page }) => {
  await deleteEmptyConversations(page);
  await page.goto("/app");
  const beforeResponse = await page.request.get("/api/sidebar-threads?state=all");
  const before = await beforeResponse.json() as SidebarThread[];
  let createRequests = 0;
  await page.route("**/api/sidebar-threads", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    createRequests += 1;
    const response = await route.fetch();
    expect(response.ok(), `${response.status()} ${await response.text()}`).toBeTruthy();
    await route.abort("failed");
  });

  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(page.getByText("操作响应丢失，结果不明确，请刷新页面核对后再继续。", { exact: true })).toBeVisible();
  await expect.poll(async () => {
    const response = await page.request.get("/api/sidebar-threads?state=all");
    return (await response.json() as SidebarThread[]).length;
  }).toBe(before.length + 1);
  expect(createRequests).toBe(1);
  await expect(page.getByRole("button", { name: "新对话", exact: true })).toBeVisible();

  const retryError = await page.evaluate(async () => {
    const { createSidebarThread } = await import("/src/api/conversations/sidebarThreads.ts");
    try {
      await createSidebarThread("must not be created", crypto.randomUUID());
      return null;
    } catch (error) {
      return String((error as Error).message ?? error);
    }
  });
  expect(retryError).toBe("上一次操作结果不明确，请刷新页面核对后再继续。");
  expect(createRequests).toBe(1);
});
