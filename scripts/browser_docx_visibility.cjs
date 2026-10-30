// DOCX visibility warnings must stay actionable and reach the saved protocol.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

async function main() {
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'dist', 'browser-docx-visibility');
  await fs.mkdir(output, {recursive: true});
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'docx-visibility-'));
  const child = spawn(process.env.STUDIO_PYTHON || 'python',
    [path.join(__dirname, 'browser_fixture_server.py'), temporary, '--docx-visibility'],
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
    const action = async (selector, name, status=201) => {
      const response = page.waitForResponse(r => r.url().endsWith('/api/action') && r.request().postDataJSON()?.action === name);
      await page.locator(selector).click();
      assert.equal((await response).status(), status);
      await page.waitForFunction(() => !mutationPending);
    };
    await page.goto(url);
    await page.locator('[data-action="confirm-extraction"]').waitFor();
    const originalBlocks = await page.evaluate(() => JSON.stringify(state.selected.extraction.blocks));
    for (const [locale, width, expected] of [
      ['zh-Hant', 1440, ['移動或格式修訂', '腳註', '尾註']],
      ['en', 320, ['move or formatting revisions', 'footnotes', 'endnotes']],
    ]) {
      await page.locator('#ui-language').selectOption(locale);
      await page.setViewportSize({width, height: width === 1440 ? 1000 : 844});
      const warnings = await page.locator('.card.next .error, .card.next .warning').allTextContents();
      for (const text of expected) assert.ok(warnings.some(warning => warning.includes(text)));
      assert.equal(await page.evaluate(() => JSON.stringify(state.selected.extraction.blocks)), originalBlocks);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      await page.screenshot({path: path.join(output, `${locale}-${width}.png`), fullPage: true});
      report.checks.push(`${locale}-${width}: revisions, footnotes and endnotes have readable localized warnings; source remains intact`);
    }
    await action('[data-action="confirm-extraction"]', 'confirm_extraction', 400);
    assert.equal(await page.evaluate(() => state.selected.state.extraction_state), 'unconfirmed');
    report.checks.push('Ordinary confirmation cannot bypass high-risk extraction warnings');
    await action('[data-action="continue-extraction"]', 'confirm_extraction');
    assert.equal(await page.evaluate(() => state.selected.state.extraction_state), 'confirmed_with_warning');
    await page.locator('#review_profile').selectOption('academic');
    await page.locator('#discipline').selectOption('humanities');
    await page.locator('#research_type').selectOption('theoretical');
    for (const [id, value] of Object.entries({jurisdiction: 'unknown', effective_date: 'unknown', publisher_type: 'author', audience: 'researchers'}))
      await page.locator('#' + id).fill(value);
    await action('#confirm-context', 'confirm_context');
    await action('#prepare-ai', 'prepare_ai_audits');
    const result = await page.evaluate(() => {
      const request = state.selected.ai_requests.find(row => row.critic === 'academic_citations');
      const contract = JSON.parse(request.prompt.split('```json\n')[1].split('\n```')[0]);
      return {codes: contract.source_visibility.extraction_warnings.map(row => row.code),
        noteTextPresent: request.prompt.includes('SYNTHETIC-NOTE-NOT-EXTRACTED')};
    });
    assert.deepEqual(result.codes.sort(), ['endnotes-present', 'footnotes-present', 'unaccepted-revisions']);
    assert.equal(result.noteTextPresent, false);
    report.checks.push('Explicit warning acceptance preserves the limits in the actual saved academic protocol');
    assert.deepEqual(report.errors, []);
    assert.deepEqual(report.externalRequests, []);
    report.passed = true;
    report.browserVersion = browser.version();
  } finally {
    if (browser) await browser.close();
    child.kill();
    if (child.exitCode === null && child.signalCode === null) await new Promise(resolve => child.once('exit', resolve));
    report.stderr = stderr;
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(report, null, 2));
    const cleanup = path.resolve(temporary);
    assert.equal(path.dirname(cleanup), path.resolve(os.tmpdir()));
    assert.ok(path.basename(cleanup).startsWith('docx-visibility-'));
    await fs.rm(cleanup, {recursive: true, force: true});
  }
  process.stdout.write(JSON.stringify(report, null, 2) + '\n');
}
main().catch(error => {process.stderr.write(String(error.stack || error) + '\n'); process.exitCode = 1;});
