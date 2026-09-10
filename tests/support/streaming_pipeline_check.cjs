const { chromium } = require('../../frontend/node_modules/playwright');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const [url, output, expectedStable] = process.argv.slice(2);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1365, height: 900 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    window.pipeline = { received: [], visible: {}, formulaReplacements: 0 };
    const original = window.fetch;
    window.fetch = async (...args) => {
      const response = await original(...args);
      if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) return response;
      const decoder = new TextDecoder();
      let pending = '';
      return new Response(response.body.pipeThrough(new TransformStream({
        transform(chunk, controller) {
          pending += decoder.decode(chunk, { stream: true });
          const events = pending.split('\n\n');
          pending = events.pop();
          for (const event of events) {
            const data = event.split('\n').find(line => line.startsWith('data:'))?.slice(5).trim();
            if (!data?.startsWith('{')) continue;
            const frame = JSON.parse(data);
            for (const op of frame.operations || []) {
              if (op.op === 'append_text') window.pipeline.received.push({ delta: op.delta, at: Date.now() });
            }
          }
          controller.enqueue(chunk);
        },
      })), { status: response.status, headers: response.headers });
    };
    let firstFormula;
    let firstRendered;
    new MutationObserver(() => {
      const text = document.body?.textContent || '';
      for (const match of text.matchAll(/part\d{4}/g)) window.pipeline.visible[match[0]] ??= Date.now();
      const formula = document.querySelector('.math-source');
      if (formula && firstFormula && formula !== firstFormula) window.pipeline.formulaReplacements += 1;
      if (formula) firstFormula = formula;
      const rendered = formula?.querySelector('math, mjx-container');
      if (rendered && firstRendered && rendered !== firstRendered) window.pipeline.renderedReplacements = (window.pipeline.renderedReplacements || 0) + 1;
      if (rendered) firstRendered = rendered;
    }).observe(document, { subtree: true, childList: true, characterData: true });
  });
  try {
    await page.goto(url);
    const editor = page.locator('[contenteditable="true"]').first();
    await editor.fill('stream benchmark');
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await page.getByText('STREAM_COMPLETE', { exact: true }).waitFor({ timeout: 60000 });
    await page.getByRole('button', { name: '暂停', exact: true }).waitFor({ state: 'hidden', timeout: 30000 });
    await page.waitForFunction(() => document.querySelectorAll('.math-source math, .math-source mjx-container').length >= 2);
    if (await page.locator('table').count() < 1 || await page.locator('pre code').count() < 1) throw new Error('Missing rich Markdown');
    if (await page.getByRole('link', { name: 'forward', exact: true }).getAttribute('href') !== 'https://example.com') throw new Error('Forward reference did not resolve');
    const metrics = await page.evaluate(() => window.pipeline);
    if (Object.keys(metrics.visible).length !== 100) throw new Error('Missing text chunks');
    if (expectedStable === 'stable' && (metrics.formulaReplacements || metrics.renderedReplacements)) throw new Error('Unchanged formula was replaced');
    fs.writeFileSync(path.join(output, 'pipeline-browser.json'), JSON.stringify(metrics, null, 2));
    await page.getByText('STREAM_COMPLETE', { exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, 'stream-desktop.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByText('STREAM_COMPLETE', { exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, 'stream-mobile.png') });
    if (await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)) throw new Error('Page overflows narrow viewport');
    if (errors.length) throw new Error(errors.join('\n'));
    console.log(JSON.stringify({ chunks: metrics.received.length, visible: Object.keys(metrics.visible).length, formulaReplacements: metrics.formulaReplacements, renderedReplacements: metrics.renderedReplacements || 0 }));
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'stream-failure.png') }).catch(() => {});
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
