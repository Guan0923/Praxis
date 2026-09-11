const { chromium } = require('../../frontend/node_modules/playwright');
const path = require('node:path');

(async () => {
  const [url, output, sessionId] = process.argv.slice(2);
  const browser = await chromium.launch({ headless: true });
  const errors = [];
  let page;
  try {
    page = await browser.newPage({ viewport: { width: 1365, height: 900 } });
    page.on('pageerror', error => errors.push(error.message));
    const pages = [];
    page.on('response', response => {
      if (response.url().includes('/api/turns/history')) pages.push(response.url());
    });
    await page.goto(url);
    await page.locator('.message.user').nth(4).waitFor();
    if (await page.locator('.message.user').count() !== 5) throw new Error('Initial page is not five Turns');
    if (!(await page.locator('.chat-messages').innerText()).includes('HISTORY_11')) throw new Error('Newest Turn missing');
    if ((await page.locator('.chat-messages').innerText()).includes('HISTORY_00')) throw new Error('Full history loaded initially');
    const second = await browser.newPage();
    await second.goto(url);
    await second.locator('.message.user').nth(4).waitFor();
    const scroll = page.locator('[data-conversation-scroll]').first();
    await scroll.evaluate(element => { element.scrollTop = 0; element.dispatchEvent(new Event('scroll')); });
    await page.waitForFunction(() => document.querySelectorAll('.message.user').length === 10);
    if (await scroll.evaluate(element => element.scrollTop) <= 0) throw new Error('Reading position jumped on prepend');
    await scroll.evaluate(element => { element.scrollTop = 0; element.dispatchEvent(new Event('scroll')); });
    await page.waitForFunction(() => document.querySelectorAll('.message.user').length === 12);
    if (await second.locator('.message.user').count() !== 5) throw new Error('Another page lost its independent loaded range');
    await second.close();
    await page.screenshot({ path: path.join(output, 'history-desktop.png'), fullPage: true });
    const afterComplete = pages.length;
    await scroll.evaluate(element => { element.scrollTop = 0; element.dispatchEvent(new Event('scroll')); });
    await page.waitForTimeout(150);
    if (pages.length !== afterComplete) throw new Error('Loading continued beyond oldest Turn');
    await page.reload();
    await page.waitForFunction(() => document.querySelectorAll('.message.user').length === 5);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(output, 'history-mobile.png'), fullPage: true });
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)) throw new Error('Mobile content overflows');
    if (sessionId) {
      await page.setViewportSize({ width: 1365, height: 900 });
      const terminalFrames = [];
      page.on("websocket", socket => socket.on("framereceived", event => terminalFrames.push(String(event.payload))));
      const created = await page.request.post(`${url}/api/right-panel/${sessionId}/terminals`, { data: { source_turn_id: "history-11" } });
      if (created.status() !== 201) throw new Error(`Terminal create failed: ${await created.text()}`);
      await page.reload();
      const input = page.locator(".xterm-helper-textarea").first();
      await input.waitFor({ state: "attached" });
      for (let attempt = 0; attempt < 100 && !terminalFrames.some(frame => frame.includes('"ready"')); attempt++) await page.waitForTimeout(50);
      for (let attempt = 0; attempt < 200 && !terminalFrames.some(frame => { try { return JSON.parse(frame).writable === true; } catch { return false; } }); attempt++) await page.waitForTimeout(50);
      await page.waitForTimeout(300);
      await input.focus();
      await page.keyboard.type("echo BROWSER_TERMI^NAL_OK", { delay: 20 });
      await page.keyboard.press("Enter");
      for (let attempt = 0; attempt < 100 && !terminalFrames.join("").includes("BROWSER_TERMINAL_OK"); attempt++) await page.waitForTimeout(50);
      if (!terminalFrames.join("").includes("BROWSER_TERMINAL_OK")) throw new Error("Real terminal output was not received");
      await page.screenshot({ path: path.join(output, "terminal-desktop.png"), fullPage: true });
      terminalFrames.length = 0;
      await page.reload();
      for (let attempt = 0; attempt < 100 && !terminalFrames.join("").includes("BROWSER_TERMINAL_OK"); attempt++) await page.waitForTimeout(50);
      if (!terminalFrames.join("").includes("BROWSER_TERMINAL_OK")) throw new Error("Terminal refresh did not replay output");
    }
    if (errors.length) throw new Error(errors.join('\n'));
    console.log(JSON.stringify({ initial: 5, appended: [10, 12], refreshed: 5, browserErrors: errors }));
  } catch (error) {
    if (page) {
      await page.screenshot({ path: path.join(output, "history-failure.png"), fullPage: true });
      console.error((await page.locator("body").innerText()).slice(-3000), errors);
    }
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
