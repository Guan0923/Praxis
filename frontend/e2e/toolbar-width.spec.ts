import { expect, test } from "@playwright/test";

async function expectToolbarAligned(page: import("@playwright/test").Page): Promise<void> {
  await expect.poll(async () => {
    const [toolbar, composer] = await Promise.all([
      page.locator(".trace-toolbar").boundingBox(),
      page.locator(".composer-box").boundingBox(),
    ]);
    if (!toolbar || !composer) return Number.POSITIVE_INFINITY;
    const leftDelta = Math.abs(toolbar.x - composer.x);
    const rightDelta = Math.abs((toolbar.x + toolbar.width) - (composer.x + composer.width));
    return Math.max(leftDelta, rightDelta);
  }).toBeLessThanOrEqual(1);
}

async function expectToolbarControlsContained(page: import("@playwright/test").Page): Promise<void> {
  const toolbar = await page.locator(".trace-toolbar").boundingBox();
  expect(toolbar).not.toBeNull();

  const controls = page.locator('.trace-toolbar button[aria-label="Thread"], .trace-toolbar button[aria-label="Chat"], .trace-toolbar button[aria-label="Trace"]');
  await expect(controls).toHaveCount(3);
  for (const control of await controls.all()) {
    const box = await control.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(toolbar!.x - 1);
    expect(box!.x + box!.width).toBeLessThanOrEqual(toolbar!.x + toolbar!.width + 1);
  }
}

async function expectSidebarWidth(
  page: import("@playwright/test").Page,
  predicate: (width: number) => boolean,
): Promise<void> {
  await expect.poll(async () => {
    const width = await page.locator("#chat-sidebar").evaluate((element) =>
      element.getBoundingClientRect().width,
    );
    return predicate(width);
  }).toBe(true);
}

test("Thread Chat Trace toolbar matches the visible composer width", async ({ page }, testInfo) => {
  test.setTimeout(90_000);
  const sidebarResponse = await page.request.post("/api/sidebar-threads", {
    data: { title: "Toolbar Width Alignment" },
  });
  expect(sidebarResponse.ok(), `${sidebarResponse.status()} ${await sidebarResponse.text()}`).toBeTruthy();
  const sidebar = await sidebarResponse.json() as { thread_id: string };

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/app");
  const conversation = page.getByRole("button", { name: "Toolbar Width Alignment", exact: true });
  await expect(conversation).toBeVisible();
  if (await conversation.isEnabled()) await conversation.click();
  await expect(page.locator(".trace-toolbar-thread-id")).toHaveText(sidebar.thread_id);

  await page.getByLabel("聊天输入").fill("toolbar width alignment");
  const turnResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/turns"),
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  expect((await turnResponse).ok()).toBeTruthy();
  await expect(page.locator(".message.assistant").last().getByRole("button", { name: "Fork" }))
    .toBeVisible({ timeout: 15_000 });

  await expectToolbarAligned(page);
  await expectToolbarControlsContained(page);
  const chatWidth = (await page.locator(".trace-toolbar").boundingBox())!.width;
  await page.getByRole("button", { name: "Trace", exact: true }).click();
  await expect(page.locator(".trace-page")).toBeVisible();
  expect(Math.abs((await page.locator(".trace-toolbar").boundingBox())!.width - chatWidth)).toBeLessThanOrEqual(1);
  await page.getByRole("button", { name: "Chat", exact: true }).click();
  await expectToolbarAligned(page);
  await expect(page.locator(".composer")).toHaveCSS("opacity", "1");
  await page.screenshot({ path: testInfo.outputPath("toolbar-desktop-sidebar-open.png"), fullPage: true });

  await page.getByRole("button", { name: "折叠侧边栏" }).click();
  const reopenSidebar = page.locator(".sidebar-reopen-button");
  await expect(reopenSidebar).toBeVisible();
  await expectSidebarWidth(page, (width) => width <= 1);
  await expectToolbarAligned(page);
  await page.screenshot({ path: testInfo.outputPath("toolbar-desktop-sidebar-collapsed.png"), fullPage: true });

  await reopenSidebar.click();
  await expect(page.getByRole("button", { name: "折叠侧边栏" })).toBeVisible();
  await expectSidebarWidth(page, (width) => width >= 279);
  await page.setViewportSize({ width: 900, height: 720 });
  await expectToolbarAligned(page);
  await expectToolbarControlsContained(page);
  await expect(page.locator(".composer")).toHaveCSS("opacity", "1");
  await page.screenshot({ path: testInfo.outputPath("toolbar-compact.png"), fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  await expectToolbarAligned(page);
  await expectToolbarControlsContained(page);
  await expect(page.locator(".composer")).toHaveCSS("opacity", "1");
  await page.screenshot({ path: testInfo.outputPath("toolbar-mobile.png"), fullPage: true });
});
