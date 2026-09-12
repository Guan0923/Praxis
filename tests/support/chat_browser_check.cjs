const { chromium } = require('../../frontend/node_modules/playwright');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1365, height: 900 } });
  const errors = [];
  page.on('response', async response => {
    if (response.status() >= 400) console.error(response.status(), new URL(response.url()).pathname, await response.text().catch(() => ''));
  });
  page.on('pageerror', error => errors.push(error.message));
  const [url, output] = process.argv.slice(2);
  const timings = {};
  let lastRequest;
  await page.addInitScript(() => {
    window.chatTiming = {};
    document.addEventListener('click', event => {
      if (event.target.closest('button')?.getAttribute('aria-label') === '发送' && !window.chatTiming.click) window.chatTiming.click = Date.now();
    }, true);
    new MutationObserver(() => {
      if (!window.chatTiming.click) return;
      if (!document.querySelector('[contenteditable="true"]')?.textContent) window.chatTiming.clear ??= Date.now();
      if (document.body?.textContent.includes('LOCAL')) window.chatTiming.visible ??= Date.now();
    }).observe(document, { childList: true, subtree: true, characterData: true });
  });
  const captureAccepted = async (response) => {
    const submitted = response.request().postDataJSON();
    const turn = await response.json();
    if (!turn.id) throw new Error('Turn creation response did not contain an id');
    lastRequest = { ...submitted, id: turn.id };
    return lastRequest;
  };
  const settled = async (status, request = lastRequest) => {
    const deadline = Date.now() + 15000;
    while (true) {
      const nodes = await page.request.get(`${url}/api/turns?session_id=${request.session_id}`).then(response => response.json());
      if (nodes.find(node => node.id === request.id)?.status === status) break;
      if (Date.now() > deadline) throw new Error(`Turn ${request.id} did not reach ${status}`);
      await page.waitForTimeout(30);
    }
    await page.getByRole('button', { name: '暂停', exact: true }).waitFor({ state: 'hidden' });
  };
  const persistedTurnForPrompt = async (sessionId, prompt) => {
    const deadline = Date.now() + 15000;
    while (true) {
      const nodes = await page.request.get(`${url}/api/turns?session_id=${sessionId}`).then(response => response.json());
      const turn = nodes.find(node => node.data?.some(version => version.some(message =>
        message.role === 'user' && message.content?.some(item => item.text === prompt),
      )));
      if (turn?.status === 'failed') throw new Error(JSON.stringify(turn));
      if (turn?.status === 'success') return turn;
      if (Date.now() > deadline) throw new Error(`Turn for ${prompt} did not reach success`);
      await page.waitForTimeout(30);
    }
  };
  const send = async (label = '发送') => {
    const accepted = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/turns');
    await page.getByRole('button', { name: label, exact: true }).click();
    const response = await accepted;
    if (response.status() !== 202) throw new Error('Turn creation failed: ' + await response.text());
    return captureAccepted(response);
  };
  try {
    await page.goto(url);
    await page.waitForSelector('[contenteditable="true"]');
    const editor = page.locator('[contenteditable="true"]').first();
    await editor.fill('hold first request');
    const start = performance.now();
    await send();
    await page.waitForFunction(() => document.querySelector('[contenteditable="true"]')?.textContent === '');
    timings.composer_clear_ms = performance.now() - start;
    await editor.fill('later draft');
    await page.getByText('LOCAL small stream response', { exact: false }).first().waitFor();
    timings.visible_output_ms = performance.now() - start;
    if (await editor.textContent() !== 'later draft') throw new Error('receipt erased the newer draft');
    timings.browser_timestamps = await page.evaluate(() => window.chatTiming);
    await editor.fill('');
    await page.reload();
    await page.getByText('LOCAL small stream response', { exact: false }).first().waitFor();
    await page.getByRole('button', { name: '暂停', exact: true }).waitFor();
    if (await page.getByText('hold first request', { exact: true }).count() !== 1) throw new Error('reconnect duplicated the user message');
    await editor.fill('finish steering');
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await page.getByRole('button', { name: '追加指令', exact: true }).click();
    await settled('success');
    await editor.fill('hold second request');
    await send();
    await page.getByRole('button', { name: '暂停', exact: true }).waitFor();
    await page.waitForTimeout(600);
    await page.route('**/api/turns/*/pause', route => route.abort());
    await page.getByRole('button', { name: '暂停', exact: true }).click();
    await page.getByText(/暂停失败，请重试/).first().waitFor();
    const stillRunning = await page.request.get(`${url}/api/turns?session_id=${lastRequest.session_id}`).then(response => response.json());
    if (stillRunning.find(node => node.id === lastRequest.id)?.status !== 'running') throw new Error('a failed pause was reported as stopped');
    await page.unroute('**/api/turns/*/pause');
    const stop = performance.now();
    await page.getByRole('button', { name: '暂停', exact: true }).click();
    await settled('paused');
    timings.pause_settle_ms = performance.now() - stop;
    await editor.fill('finish after pause');
    await send();
    await settled('success');
    let drop = true;
    await page.route('**/api/turns', async route => {
      if (drop && route.request().method() === 'POST') {
        drop = false;
        await route.fetch();
        await route.abort();
      } else await route.continue();
    });
    await editor.fill('retry lost receipt');
    const lostRequestPromise = page.waitForRequest(request => request.method() === 'POST' && new URL(request.url()).pathname === '/api/turns');
    await page.getByRole('button', { name: '发送', exact: true }).click();
    const lostRequest = (await lostRequestPromise).postDataJSON();
    await page.getByRole('button', { name: '发送', exact: true }).waitFor();
    if (await page.locator('.message.user').filter({ hasText: 'retry lost receipt' }).count()) throw new Error('lost receipt left a temporary message');
    await persistedTurnForPrompt(lostRequest.session_id, 'retry lost receipt');
    await page.unroute('**/api/turns');
    await page.reload();
    await page.getByText('retry lost receipt', { exact: true }).waitFor();
    await page.screenshot({ path: path.join(output, 'chat-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(output, 'chat-mobile.png'), fullPage: true });
    await page.setViewportSize({ width: 1365, height: 900 });
    let releaseOld;
    await page.route('**/api/turns', async route => {
      if (route.request().method() !== 'POST') return route.continue();
      const response = await route.fetch();
      await new Promise(resolve => { releaseOld = resolve; });
      await route.fulfill({ response });
    });
    await editor.fill('delayed old session');
    const delayedResponse = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/turns');
    const delayedRequest = page.waitForRequest(request => request.method() === 'POST' && new URL(request.url()).pathname === '/api/turns');
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await delayedRequest;
    while (!releaseOld) await page.waitForTimeout(20);
    await page.getByRole('button', { name: '新建对话', exact: true }).click();
    await page.locator('.welcome').waitFor();
    await page.waitForFunction(() => document.querySelector('[contenteditable="true"]')?.textContent === '');
    await editor.fill('other session draft');
    releaseOld();
    const oldRequest = await captureAccepted(await delayedResponse);
    await settled('success', oldRequest);
    if (await editor.textContent() !== 'other session draft') throw new Error('old session receipt changed the current draft');
    await page.unroute('**/api/turns');
    const preserved = await page.request.get(`${url}/api/turns?session_id=${oldRequest.session_id}`).then(response => response.json());
    if (!preserved.some(node => node.id === oldRequest.id && node.status === 'success')) throw new Error('switched-away request did not finish in its original session: ' + JSON.stringify({ expected: oldRequest.id, returned: preserved.map(node => ({ id: node.id, status: node.status })) }));
    if (errors.length) throw new Error(errors.join('\n'));
    const real = timings.browser_timestamps;
    timings.click_to_clear_ms = real.clear - real.click;
    timings.click_to_first_visible_ms = real.visible - real.click;
    timings.browser_timestamps = real;
    console.log(JSON.stringify(timings));
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'chat-failure.png'), fullPage: true });
    console.error((await page.locator('body').innerText()).slice(-7000));
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
