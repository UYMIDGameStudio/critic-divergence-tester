// Broken request metadata must not hide the safe recovery controls.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

async function main() {
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'dist', 'browser-recovery-0.2.30');
  await fs.mkdir(output, {recursive: true});
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'recovery-browser-'));
  const child = spawn(process.env.STUDIO_PYTHON || 'python',
    [path.join(__dirname, 'browser_fixture_server.py'), temporary, '--readonly-recovery'],
    {cwd: root, windowsHide: true, env: {...process.env, PYTHONUTF8: '1'}});
  let browser, stderr = '';
  const report = {passed: false, checks: [], errors: [], externalRequests: []};
  child.stderr.on('data', data => {stderr += data;});
  try {
    const url = await new Promise((resolve, reject) => {
      let text = '';
      const timeout = setTimeout(() => reject(Error('Fixture startup timeout: ' + stderr)), 30000);
      child.stdout.on('data', data => {
        text += data;
        const match = text.match(/http:\/\/127\.0\.0\.1:\d+\//);
        if (match) {clearTimeout(timeout); resolve(match[0]);}
      });
      child.once('error', error => {clearTimeout(timeout); reject(error);});
      child.once('exit', code => {clearTimeout(timeout); reject(Error(`Fixture exit ${code}: ${stderr}`));});
    });
    browser = await chromium.launch({headless: true,
      ...(process.env.STUDIO_BROWSER ? {executablePath: process.env.STUDIO_BROWSER} : {})});
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    page.on('pageerror', error => report.errors.push(error.message));
    await page.route('**/*', route => {
      if (new URL(route.request().url()).origin === new URL(url).origin) return route.continue();
      report.externalRequests.push(route.request().url());
      return route.abort();
    });
    await page.goto(url);
    await page.locator('.readonly').waitFor();
    const state = await page.evaluate(() => api('/api/state'));
    assert.equal(state.selected.state.read_only, true);
    assert.equal(state.selected.can_review, false);
    assert.equal(state.selected.ai_requests.length, 0);
    assert.equal(JSON.stringify(state).includes('RECOVERY-MUST-NOT-RENDER'), false);
    report.checks.push('Damaged request still serves a sanitized recovery state');
    for (const [locale, width] of [['zh-Hant', 1440], ['en', 320]]) {
      await page.locator('#ui-language').selectOption(locale);
      await page.setViewportSize({width, height: width === 1440 ? 1000 : 844});
      assert.ok(await page.locator('.readonly h2').isVisible());
      assert.ok(await page.locator('#back').isVisible());
      const back = await page.locator('#back').boundingBox();
      assert.ok(back.y >= 0 && back.y + back.height <= page.viewportSize().height,
        'Back to projects should be available in the initial viewport');
      assert.equal(await page.locator('#create-backup, #download-draft, #retry-draft').count(), 0);
      assert.ok(await page.locator('.quit-button').isVisible());
      assert.equal(await page.locator('.integrity-details').evaluate(node => node.open), false);
      assert.equal(await page.locator('.integrity-details summary').innerText(),
        locale === 'en' ? 'View integrity details' : '查看完整性詳情');
      await page.locator('.integrity-details summary').click();
      assert.ok((await page.locator('.integrity-details p').innerText()).trim());
      await page.locator('.integrity-details summary').click();
      assert.equal(await page.locator('#app .source-block, #app .finding-card, #app #prepare-ai, #app #confirm-context').count(), 0);
      assert.equal((await page.locator('#app').innerText()).includes('RECOVERY-MUST-NOT-RENDER'), false);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      await page.screenshot({path: path.join(output, `${locale}-${width}.png`), fullPage: true});
      report.checks.push(`${locale}-${width}: readable recovery controls without review forms or untrusted content`);
    }
    const status = await page.evaluate(async () => {
      const response = await fetch('/api/download?path=project.json', {headers: {'X-Document-Review-Token': TOKEN}});
      return response.status;
    });
    assert.equal(status, 400);
    report.checks.push('Downloads remain blocked by the integrity gate');
    await page.locator('#back').click();
    await page.waitForFunction(() => state?.selected === null);
    assert.ok(await page.locator('.import-panel').isVisible());
    assert.ok(await page.locator('#restore-backup').isVisible());
    report.checks.push('Back control reaches the project library and backup restore entry');
    assert.equal(await page.evaluate(() => window.__recoveryInjected || false), false);
    assert.deepEqual(report.errors, []);
    assert.deepEqual(report.externalRequests, []);
    report.passed = true;
    report.browserVersion = browser.version();
  } finally {
    if (browser) await browser.close();
    child.kill();
    report.stderr = stderr;
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(report, null, 2));
  }
  process.stdout.write(JSON.stringify(report, null, 2) + '\n');
}
main().catch(error => {process.stderr.write(String(error.stack || error) + '\n'); process.exitCode = 1;});
