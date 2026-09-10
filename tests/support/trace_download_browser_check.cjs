const { chromium } = require('../../frontend/node_modules/playwright');
const path = require('node:path');

(async () => {
  const [url, output, sessionId] = process.argv.slice(2);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1365, height: 900 }, acceptDownloads: true });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const saveDownload = async (link, expectedName) => {
    const downloaded = page.waitForEvent('download');
    await link.click();
    const download = await downloaded;
    if (download.suggestedFilename() !== expectedName) throw new Error('Unexpected download filename');
    await download.saveAs(path.join(output, expectedName));
    if (await download.failure()) throw new Error('Download failed');
  };
  const capture = async (name, selector) => {
    const button = page.locator(selector);
    await button.scrollIntoViewIfNeeded();
    const box = await button.boundingBox();
    const viewport = page.viewportSize();
    if (!box || box.x < 0 || box.x + box.width > viewport.width + 1) throw new Error('Download button is outside viewport');
    await page.screenshot({ path: path.join(output, `${name}.png`) });
  };
  try {
    await page.goto(url);
    // The single-row drag container has aria-disabled; its navigation button still accepts keyboard input.
    await page.getByRole('button', { name: 'Trace export acceptance', exact: true }).press('Enter');
    await page.getByRole('button', { name: 'Trace', exact: true }).click();
    const threadDownload = page.locator('.trace-download-actions a');
    await threadDownload.waitFor();
    await capture('trace-desktop', '.trace-download-actions a');
    await saveDownload(threadDownload, `thread-${sessionId}-trace.jsonl`);
    await page.setViewportSize({ width: 390, height: 844 });
    // The app remounts its chat surface when switching between desktop and mobile layouts.
    await page.getByRole('button', { name: 'Trace', exact: true }).click();
    await capture('trace-mobile', '.trace-download-actions a');
    await page.setViewportSize({ width: 1365, height: 900 });
    await page.getByRole('button', { name: 'Benchmark', exact: true }).click();
    const benchmarkDownload = page.locator('.benchmark-trace-download');
    await benchmarkDownload.waitFor();
    if (await page.locator('.benchmark-trace-list, .benchmark-trace-event').count()) throw new Error('Benchmark still renders trace details');
    await capture('benchmark-desktop', '.benchmark-trace-download');
    await saveDownload(benchmarkDownload, 'benchmark-run-export-test-export-trace.jsonl');
    await page.setViewportSize({ width: 390, height: 844 });
    await capture('benchmark-mobile', '.benchmark-trace-download');
    if (errors.length) throw new Error(errors.join('\n'));
    console.log(JSON.stringify({ downloads: 2, screenshots: 4, pageErrors: 0 }));
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true });
    console.error((await page.locator('body').innerText()).slice(-5000));
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
