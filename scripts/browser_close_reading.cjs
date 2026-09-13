// Actual browser checks: bilingual diagnosis, source navigation and escaped model text.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const { spawn } = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

async function main() {
  const repo = path.resolve(__dirname, '..');
  const output = path.join(repo, 'dist', 'browser-close-reading-0.2.2');
  await fs.mkdir(output, { recursive: true });
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'close-reading-browser-'));
  const child = spawn(process.env.STUDIO_PYTHON || 'python',
    [path.join(__dirname, 'browser_fixture_server.py'), temporary, '--close-reading'],
    { cwd: repo, windowsHide: true, env: { ...process.env, PYTHONUTF8: '1' } });
  let browser, page, stderr = '';
  const report = { passed: false, checks: [], errors: [], externalRequests: [] };
  child.stderr.on('data', data => { stderr += data; });
  try {
    const url = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('Fixture startup timeout: ' + stderr)), 30000);
      let text = '';
      child.stdout.on('data', data => {
        text += data;
        const match = text.match(/http:\/\/127\.0\.0\.1:\d+\//);
        if (match) { clearTimeout(timer); resolve(match[0]); }
      });
      child.once('error', error => { clearTimeout(timer); reject(error); });
      child.once('exit', code => { clearTimeout(timer); reject(Error(`Fixture exit ${code}: ${stderr}`)); });
    });
    browser = await chromium.launch({ headless: true,
      ...(process.env.STUDIO_BROWSER ? { executablePath: process.env.STUDIO_BROWSER } : {}) });
    report.browserVersion = browser.version();
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    await page.route('**/*', route => {
      if (new URL(route.request().url()).origin === new URL(url).origin) return route.continue();
      report.externalRequests.push(route.request().url());
      return route.abort();
    });
    page.on('pageerror', error => report.errors.push(error.message));
    await page.goto(url);
    await page.waitForFunction(() => state?.selected?.findings?.length === 1);
    await page.locator('[data-stage="adjudication"]').first().click();
    const card = page.locator('.close-reading');
    await card.waitFor({ state: 'visible' });
    assert.match(await card.textContent(), /細讀依據（模型判斷）/);
    assert.match(await card.textContent(), /最強辯護/);
    report.checks.push('Traditional Chinese diagnosis');
    await page.screenshot({ path: path.join(output, 'zh-Hant.png'), fullPage: true });
    await page.locator('#ui-language').selectOption('en');
    await page.waitForFunction(() => document.documentElement.lang === 'en');
    assert.match(await card.textContent(), /Close-reading justification \(model proposal\)/);
    assert.match(await card.textContent(), /Repair acceptance test/);
    assert.equal(await page.locator('.finding-group').getByText('Affects the conclusion, structure or execution', { exact: false }).count(), 1);
    assert.doesNotMatch((await page.locator('.summary').allTextContents()).join(' '), /[条條]/);
    assert.match(await card.textContent(), /Straße 日本語 Русский Latīna/);
    assert.equal(await card.locator('img').count(), 0);
    assert.equal(await page.evaluate(() => Boolean(window.__injected)), false);
    report.checks.push('English diagnosis with original multilingual text', 'Model HTML remains inert');
    const anchor = card.locator('a').first();
    const target = await anchor.getAttribute('href');
    await anchor.click();
    assert.equal(new URL(page.url()).hash, target);
    assert.equal(await page.locator(target).count(), 1);
    report.checks.push('Context quote links to its actual source block');
    await page.screenshot({ path: path.join(output, 'en.png'), fullPage: true });
    assert.deepEqual(report.errors, []);
    assert.deepEqual(report.externalRequests, []);
    report.passed = true;
  } finally {
    if (page) await page.evaluate(() => fetch('/api/shutdown', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Document-Review-Token': TOKEN }, body: '{}',
    })).catch(() => {});
    if (browser) await browser.close();
    if (child.exitCode === null) await new Promise(resolve => {
      const timer = setTimeout(() => { child.kill(); resolve(); }, 5000);
      child.once('exit', () => { clearTimeout(timer); resolve(); });
    });
    report.stderr = stderr;
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(report, null, 2));
    const resolved = await fs.realpath(temporary);
    assert.equal(path.dirname(resolved), await fs.realpath(os.tmpdir()));
    assert.ok(path.basename(resolved).startsWith('close-reading-browser-'));
    await fs.rm(resolved, { recursive: true, force: true });
  }
  console.log(JSON.stringify(report, null, 2));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
