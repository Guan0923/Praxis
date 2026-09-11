const { chromium } = require('../../frontend/node_modules/playwright');
const path = require('node:path');
(async () => {
  const [url, output] = process.argv.slice(2);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1365, height: 900 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(url);
    const peer = await browser.newPage();
    peer.on("pageerror", error => errors.push(error.message));
    await peer.goto(url);
    await peer.getByRole("button", { name: "更多操作：Delete desktop" }).waitFor();
    await page.getByRole('button', { name: '更多操作：Delete desktop' }).click();
    await page.getByRole('menuitem', { name: /删除/ }).click();
    const start = Date.now();
    const [response] = await Promise.all([
      page.waitForResponse(r => r.request().method() === 'DELETE'),
      page.locator('.ant-modal-confirm .ant-btn-dangerous').click(),
    ]);
    if (response.status() !== 204) throw new Error('Delete failed: ' + await response.text());
    await page.getByRole('button', { name: '更多操作：Delete desktop' }).waitFor({ state: 'detached' });
    console.log('desktop delete ms:', Date.now() - start);
    await peer.evaluate(() => window.dispatchEvent(new Event('focus')));
    await peer.getByRole('button', { name: '更多操作：Delete desktop' }).waitFor({ state: 'detached' });
    console.log('second page observed deletion');
    await peer.close();
    await page.getByRole('button', { name: /^个人简介/ }).click();
    await page.getByRole('menuitem', { name: /沙箱/ }).click();
    await page.getByText('模型命令总资源限制', { exact: true }).waitFor();
    if (await page.getByRole('button', { name: /^(检\s*查|覆盖修复)$/ }).count()) throw new Error('Removed sandbox buttons are still visible');
    await page.getByLabel('总进程数', { exact: true }).fill('400');
    const saved = page.waitForResponse(r => r.url().endsWith('/api/settings/sandbox') && r.request().method() === 'PUT');
    await page.getByRole('button', { name: '保存', exact: true }).click();
    if (!(await saved).ok()) throw new Error('Settings save failed');
    await page.screenshot({ path: path.join(output, 'resources-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByLabel('总进程数', { exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, 'resources-mobile.png'), fullPage: true });
    const overflow = await page.locator('.user-settings-modal').evaluate(el => el.scrollWidth > el.clientWidth + 2);
    if (overflow) throw new Error('Settings overflow on mobile');
    await page.getByRole('button', { name: 'Close', exact: true }).click();
    await page.reload();
    const toggle = page.getByRole('button', { name: '打开会话列表', exact: true });
    await toggle.click();
    await page.getByRole('button', { name: '更多操作：Delete mobile' }).click();
    await page.getByRole('menuitem', { name: /删除/ }).click();
    await page.locator('.ant-modal-confirm .ant-btn-dangerous').click();
    await page.getByRole('button', { name: '更多操作：Delete mobile' }).waitFor({ state: 'detached' });
    const failureToggle = await page.request.post(url + '/test/start-failure');
    if (!failureToggle.ok()) throw new Error('Startup probe failed: ' + await failureToggle.text());
    await page.setViewportSize({ width: 1365, height: 900 });
    await page.reload();
    await page.getByRole('button', { name: /^个人简介/ }).click();
    await page.getByRole('menuitem', { name: /沙箱/ }).click();
    await page.getByText('自动恢复已暂停', { exact: true }).waitFor({ timeout: 20000 });
    await page.getByRole('dialog', { name: '用户设置' }).getByText(/^FileNotFoundError: \[Errno 2\] test sandbox executable missing$/).waitFor();
    const settingsDialog = page.getByRole('dialog', { name: '用户设置' });
    await settingsDialog.getByText('错误详情', { exact: true }).click();
    await settingsDialog.locator('pre').filter({ hasText: 'test_delete_resource_browser.py' }).waitFor();
    await page.screenshot({ path: path.join(output, 'startup-error-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(output, 'startup-error-mobile.png'), fullPage: true });
    // Wait through one real, read-only 30-second status polling interval.
    await page.waitForTimeout(31000);
    const probe = await (await page.request.get(url + '/test/recovery-count')).json();
    if (probe.calls !== 1) throw new Error('Paused recovery was repeated: ' + probe.calls);
    await page.getByRole('dialog', { name: '用户设置' }).getByText(/^FileNotFoundError: \[Errno 2\] test sandbox executable missing$/).waitFor();
    console.log('startup failure stayed paused through status polling');
    if (errors.length) throw new Error(errors.join('\n'));

  } catch (error) {
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true });
    console.error(await page.locator("body").innerText());
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
