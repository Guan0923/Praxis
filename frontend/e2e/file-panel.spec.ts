import { expect, test, type Page } from "@playwright/test";

async function createConversation(page: Page): Promise<string> {
  const response = await page.request.post("/api/sidebar-threads", { data: { title: "文件面板测试" } });
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBeTruthy();
  return (await response.json() as { session_id: string }).session_id;
}

async function selectConversation(page: Page, title: string): Promise<void> {
  const thread = page.getByRole("button", { name: title, exact: true });
  await expect(thread).toBeVisible();
  if (await thread.getAttribute("aria-current") !== "page") {
    await expect(thread).toBeEnabled();
    await thread.click();
  }
  await expect(thread).toHaveAttribute("aria-current", "page");
  await expect(page.getByLabel("聊天输入")).toBeVisible();
}

async function openRightPanel(page: Page): Promise<void> {
  const responsePromise = page.waitForResponse((response) =>
    response.request().method() === "PATCH" && /\/api\/right-panel\/[^/]+$/.test(new URL(response.url()).pathname),
  );
  await page.getByRole("button", { name: "打开右侧边栏" }).click();
  const response = await responsePromise;
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBeTruthy();
  await expect(page.locator(".right-panel-shell")).toBeVisible();
}

test("creates, edits, renames, and recycles a workspace file from the right panel", async ({ page }) => {
  const sessionId = await createConversation(page);
  await page.goto("/app");
  await selectConversation(page, "文件面板测试");
  await openRightPanel(page);
  await page.getByRole("button", { name: "打开文件", exact: true }).click();
  await expect(page.getByRole("tree", { name: "文件树" })).toBeVisible();

  await page.getByText("workspace", { exact: true }).click({ button: "right" });
  await page.getByText("新建文件", { exact: true }).click();
  await page.getByRole("textbox", { name: "名称" }).fill("panel-note.txt");
  await page.getByRole("dialog", { name: "新建文件" }).getByRole("button", { name: "确 定" }).click();

  const workspaceRow = page.getByText("workspace", { exact: true }).locator("xpath=ancestor::*[contains(@class,'ant-tree-treenode')]");
  await workspaceRow.locator(".ant-tree-switcher").click();
  await page.getByText("panel-note.txt", { exact: true }).click();
  const editor = page.getByRole("textbox", { name: "文件编辑器" });
  await editor.fill("hello from file panel\n");
  await expect(page.getByText("已保存", { exact: true })).toBeVisible({ timeout: 10_000 });

  const saved = await page.request.get(`/api/sessions/${sessionId}/files/editor`, {
    params: { source: "workspace", path: "workspace:panel-note.txt" },
  });
  expect(saved.ok(), `${saved.status()} ${await saved.text()}`).toBeTruthy();
  expect((await saved.json() as { content: string }).content).toBe("hello from file panel\n");

  await page.getByText("panel-note.txt", { exact: true }).click({ button: "right" });
  await page.getByText("重命名", { exact: true }).click();
  await page.getByRole("textbox", { name: "名称" }).fill("renamed-note.txt");
  await page.getByRole("dialog", { name: "重命名" }).getByRole("button", { name: "确 定" }).click();
  await expect(page.getByText("renamed-note.txt", { exact: true })).toBeVisible();

  await page.getByText("renamed-note.txt", { exact: true }).click({ button: "right" });
  await page.getByText("删除", { exact: true }).click();
  await expect(page.getByText("renamed-note.txt", { exact: true })).toHaveCount(0);
});

test("keeps the file tree collapsed by default on a narrow screen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await createConversation(page);
  await page.goto("/app");
  await expect(page.getByLabel("聊天输入")).toBeVisible();
  await openRightPanel(page);
  await page.getByRole("button", { name: "打开文件", exact: true }).click();

  await expect(page.getByRole("tree", { name: "文件树" })).toHaveCount(0);
  await page.getByRole("button", { name: "展开文件树" }).click();
  await expect(page.getByRole("tree", { name: "文件树" })).toBeVisible();
  const body = await page.locator(".right-panel-files").evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
  }));
  expect(body.scrollWidth).toBeLessThanOrEqual(body.clientWidth + 1);
});
