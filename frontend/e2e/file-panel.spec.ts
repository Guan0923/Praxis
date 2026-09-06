import { expect, test, type Page } from "@playwright/test";

async function createConversation(page: Page, title: string): Promise<string> {
  const response = await page.request.post("/api/sidebar-threads", { data: { title } });
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
  test.setTimeout(60_000);
  const title = "文件面板操作测试";
  const sessionId = await createConversation(page, title);
  await page.goto("/app");
  await selectConversation(page, title);
  await openRightPanel(page);
  await page.getByRole("button", { name: "打开文件", exact: true }).click();
  await expect(page.getByRole("tree", { name: "文件树" })).toBeVisible();

  await page.getByText("workspace", { exact: true }).click({ button: "right" });
  await page.getByText("新建文件", { exact: true }).click();
  await page.getByRole("textbox", { name: "名称" }).fill("panel-note.txt");
  await page.getByRole("dialog", { name: "新建文件" }).getByRole("button", { name: "确 定" }).click();

  await page.getByText("workspace", { exact: true }).click();
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

  await page.getByText("workspace", { exact: true }).click({ button: "right" });
  await page.getByText("新建目录", { exact: true }).click();
  await page.getByRole("textbox", { name: "名称" }).fill("target-dir");
  await page.getByRole("dialog", { name: "新建目录" }).getByRole("button", { name: "确 定" }).click();
  await expect(page.getByText("target-dir", { exact: true })).toBeVisible();

  await page.getByText("renamed-note.txt", { exact: true }).click({ button: "right" });
  await page.getByText("移动到", { exact: true }).click();
  const moveDialog = page.getByRole("dialog", { name: "移动到" });
  await moveDialog.getByText("workspace", { exact: true }).click();
  await moveDialog.getByText("target-dir", { exact: true }).click();
  await moveDialog.getByRole("button", { name: "确 定" }).click();
  await expect(moveDialog).toBeHidden();

  const fileTree = page.getByRole("tree", { name: "文件树" });
  await fileTree.getByText("target-dir", { exact: true }).click();
  await expect(fileTree.getByText("renamed-note.txt", { exact: true })).toBeVisible();

  await fileTree.getByText("renamed-note.txt", { exact: true }).click({ button: "right" });
  await page.getByText("删除", { exact: true }).click();
  await expect(page.getByText("renamed-note.txt", { exact: true })).toHaveCount(0);
});

test("keeps the file tree collapsed by default on a narrow screen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await createConversation(page, "文件面板窄屏测试");
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

test("uses compact rows and scrolls an overflowing filename on hover", async ({ page }) => {
  const title = "文件面板长名称测试";
  const sessionId = await createConversation(page, title);
  const longName = "a-very-long-file-name-that-must-scroll-inside-the-file-tree.txt";
  const file = await page.request.post("/api/test/session-file", { data: {
    session_id: sessionId,
    source: "workspace",
    display_path: longName,
    content: "long name",
  } });
  expect(file.ok(), `${file.status()} ${await file.text()}`).toBeTruthy();

  await page.goto("/app");
  await selectConversation(page, title);
  await openRightPanel(page);
  await page.getByRole("button", { name: "打开文件", exact: true }).click();
  const tree = page.getByRole("tree", { name: "文件树" });
  await tree.getByText("workspace", { exact: true }).click();

  const name = tree.getByText(longName, { exact: true });
  await expect(name).toBeVisible();
  const row = name.locator("xpath=ancestor::*[contains(@class,'ant-tree-treenode')]");
  const content = row.locator(".ant-tree-node-content-wrapper");
  const icon = content.locator(".ant-tree-iconEle");
  const viewport = content.locator(".file-tree-name");

  const layout = await Promise.all([row.boundingBox(), icon.boundingBox(), viewport.boundingBox()]);
  expect(layout[0]?.height).toBe(24);
  expect(Math.abs((layout[1]?.y ?? 0) + (layout[1]?.height ?? 0) / 2 - ((layout[2]?.y ?? 0) + (layout[2]?.height ?? 0) / 2))).toBeLessThanOrEqual(1);
  await expect(row.locator(".ant-tree-switcher")).toHaveCSS("display", "none");
  await expect(viewport).toHaveClass(/file-tree-name--overflow/);

  await row.hover();
  await expect.poll(() => name.evaluate((element) => getComputedStyle(element).animationName)).toBe("file-tree-name-scroll");
  const movedTransform = await name.evaluate((element) => {
    const animation = element.getAnimations()[0];
    const duration = Number(animation.effect?.getTiming().duration ?? 0);
    animation.currentTime = duration * 0.65;
    return getComputedStyle(element).transform;
  });
  expect(movedTransform).not.toBe("none");
  expect(movedTransform).not.toBe("matrix(1, 0, 0, 1, 0, 0)");

  const collapseTree = page.getByRole("button", { name: "收起文件树" }).last();
  await collapseTree.hover();
  await expect.poll(() => name.evaluate((element) => getComputedStyle(element).transform)).toBe("matrix(1, 0, 0, 1, 0, 0)");

  await name.click();
  await collapseTree.hover();
  await collapseTree.focus();
  await page.keyboard.press("Tab");
  await expect(tree).toBeFocused();
  await expect(row.locator(".file-tree-node")).toHaveClass(/file-tree-node--active/);
  await expect.poll(() => name.evaluate((element) => getComputedStyle(element).animationName)).toBe("file-tree-name-scroll");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect.poll(() => name.evaluate((element) => getComputedStyle(element).animationName)).toBe("none");

  const treeOverflow = await tree.evaluate((element) => ({ clientWidth: element.clientWidth, scrollWidth: element.scrollWidth }));
  expect(treeOverflow.scrollWidth).toBeLessThanOrEqual(treeOverflow.clientWidth + 1);
});
