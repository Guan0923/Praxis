const { chromium } = require('../../frontend/node_modules/playwright');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const [url, output] = process.argv.slice(2);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.addInitScript(() => {
    window.todoTiming = {};
    new MutationObserver(() => {
      if (document.body?.textContent.includes('TODO_CANDIDATE')) window.todoTiming.candidateVisible ??= Date.now();
    }).observe(document, { subtree: true, childList: true, characterData: true });
  });
  try {
    await page.goto(url);
    await page.locator('[contenteditable="true"]').first().fill('finish todo streaming test');
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await page.getByText('TODO_CANDIDATE still streaming', { exact: true }).waitFor();
    await page.getByText('TODO_FINAL', { exact: false }).first().waitFor();
    await page.getByRole('button', { name: '暂停', exact: true }).waitFor({ state: 'hidden' });
    const text = await page.locator('.message.assistant').innerText();
    if ((text.match(/TODO_CANDIDATE/g) || []).length !== 1 || (text.match(/TODO_FINAL/g) || []).length !== 1) throw new Error('Todo text duplicated or withdrawn');
    fs.writeFileSync(path.join(output, 'todo-browser.json'), JSON.stringify(await page.evaluate(() => window.todoTiming)));
    console.log('Todo candidate visible before response completion; one extra finalization, no duplicates.');
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'todo-failure.png') });
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
