const { chromium } = require('../../frontend/node_modules/playwright');
const path = require('node:path');
(async () => {
  const [url, output, sessionId] = process.argv.slice(2);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1365, height: 900 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(url);
    await page.getByText('Flow browser test', { exact: true }).first().waitFor();
    const peer = await browser.newPage();
    await peer.goto(url);
    for (const [index, text] of ['local desktop message', 'local mobile message'].entries()) {
      if (index) await page.setViewportSize({ width: 390, height: 844 });
      const editor = page.locator('[contenteditable="true"]').first();
      await editor.fill(text);
      const acceptance = page.waitForResponse(response => response.url().endsWith('/api/turns') && response.request().method() === 'POST');
      await page.getByRole('button', { name: /^(发送|开始)$/ }).click();
      const response = await acceptance;
      if (response.status() !== 202) throw new Error('Admission failed: ' + await response.text());
      const submitted = response.request().postDataJSON();
      if (submitted.message.role !== 'user' || submitted.message.content[0].text !== text) throw new Error('Wire message format changed');
      const receipt = await response.json();
      let finished = false;
      for (let attempts = 0; attempts < 100; attempts++) {
        const turns = await (await page.request.get(url + '/api/turns?session_id=' + sessionId)).json();
        const turn = turns.find(node => node.id === receipt.turn_id);
        if (turn && turn.status === 'failed') throw new Error(JSON.stringify(turn));
        if (turn && turn.status === 'success') { finished = true; break; }
        await page.waitForTimeout(100);
      }
      if (!finished) throw new Error('Turn did not complete');
      await page.getByText('child answered through local HTTP', { exact: true }).last().waitFor();
      await page.screenshot({ path: path.join(output, index ? 'flow-mobile.png' : 'flow-desktop.png'), fullPage: true });
      await page.reload();
      await page.getByText(text, { exact: true }).last().waitFor();
      await peer.reload();
      await peer.getByText(text, { exact: true }).last().waitFor();
    }
    if (errors.length) throw new Error(errors.join('\n'));
    console.log('Two real model Turns completed; desktop, narrow, refresh and second page passed.');
    await peer.close();
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'flow-failure.png'), fullPage: true });
    console.error(await page.locator('body').innerText());
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
