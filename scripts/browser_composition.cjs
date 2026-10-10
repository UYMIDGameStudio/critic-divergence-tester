// Four-axis review with no defects, original model text and local-only navigation.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

async function main() {
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'dist', 'browser-composition-0.2.29');
  await fs.mkdir(output, {recursive: true});
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'composition-browser-'));
  const child = spawn(process.env.STUDIO_PYTHON || 'python',
    [path.join(__dirname, 'browser_fixture_server.py'), temporary, '--argument-composition'],
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
    await page.waitForFunction(() => state?.selected?.argument_assessments?.length === 1);
    const panel = page.locator('.argument-assessment');
    await panel.waitFor();
    const content = await panel.locator('.assessment-description').allTextContents();
    for (const [locale, heading, labels] of [
      ['zh-Hant', '論證與文章綜合審查', ['論證結構', '論證方向', '論證方法', '特徵特點', 'Problem framing']],
      ['en', 'Article and argument review', ['Argument structure', 'Argument direction', 'Argument methods', 'Distinctive features', 'Problem framing']],
    ]) {
      await page.locator('#ui-language').selectOption(locale);
      await panel.locator('h2').getByText(heading, {exact: true}).waitFor();
      assert.deepEqual(await panel.locator('h3').allTextContents(), labels);
      assert.deepEqual(await panel.locator('.assessment-description').allTextContents(), content);
      assert.equal(await page.evaluate(() => state.selected.findings.length), 0);
      report.checks.push(`${locale}: selected design judgements and an additional angle survive with zero Findings and original model text`);
      const evidence = panel.locator('.assessment-axis details').first();
      await evidence.locator('summary').click();
      assert.ok((await evidence.locator('.quote').innerText()).includes('Bounded comparison'));
      report.checks.push(`${locale}: exact source evidence is readable`);
      for (const width of [1440, 390, 320]) {
        await page.setViewportSize({width, height: width === 1440 ? 1000 : 844});
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
        await panel.screenshot({path: path.join(output, `${locale}-${width}.png`)});
        report.checks.push(`${locale}-${width}: readable layout without horizontal overflow`);
      }
    }
    assert.equal(await panel.locator('img').count(), 0);
    assert.equal(await page.evaluate(() => window.__compositionInjected || false), false);
    assert.equal(report.errors.length, 0);
    assert.equal(report.externalRequests.length, 0);
    report.checks.push('Model HTML stays inert; no script errors or external requests');
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
