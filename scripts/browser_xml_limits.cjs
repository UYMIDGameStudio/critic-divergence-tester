// Parser limits must preserve the original and offer a usable recovery path.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');

async function main() {
  const irBudget = process.argv.includes('--ir-budget');
  const label = irBudget ? 'ir-budget' : 'xml-limits';
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'dist', 'browser-' + label);
  await fs.mkdir(output, {recursive: true});
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), label + '-'));
  // Owned fixtures exercise rejection without allocating oversized JSON.
  // The XML case uses a small limit; the IR case exercises the default budget.
  const child = spawn(process.env.STUDIO_PYTHON || 'python',
    [path.join(__dirname, 'browser_fixture_server.py'), temporary, '--' + label],
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
    await page.locator('#retry-extraction').waitFor();
    const original = await page.evaluate(() => ({directory: state.selected.directory, source: state.selected.project.source}));
    const sourcePath = path.join(temporary, original.directory, original.source.relative_path);
    const sourceBytes = await fs.readFile(sourcePath);
    assert.equal(crypto.createHash('sha256').update(sourceBytes).digest('hex'), original.source.sha256);
    for (const [locale, width, expected] of [
      ['zh-Hant', 1440, irBudget ? '解析結果展開後超過安全大小上限' : '請拆分或簡化檔案後重新匯入'],
      ['en', 320, irBudget ? 'Parsed document exceeds the safe size limit after expansion' : 'Split or simplify the file, then import it again'],
    ]) {
      await page.locator('#ui-language').selectOption(locale);
      await page.setViewportSize({width, height: width === 1440 ? 1000 : 844});
      assert.ok((await page.locator('.card.next p.error').first().innerText()).includes(expected));
      const stateCheck = await page.evaluate(() => ({blocked: state.selected.state.extraction_state,
        blocks: state.selected.extraction.blocks.length, review: state.selected.can_review}));
      assert.deepEqual(stateCheck, {blocked: 'blocked', blocks: 0, review: false});
      assert.equal(await page.locator('[data-action="confirm-extraction"], [data-action="continue-extraction"], #prepare-ai').count(), 0);
      await page.locator('#continue-workflow').click();
      assert.equal(await page.evaluate(() => document.activeElement?.id), 'extraction-recovery');
      assert.equal(await page.locator('#correction-editor, #correction, #submit-correction').count(), 0);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      await page.screenshot({path: path.join(output, `${locale}-${width}.png`), fullPage: true});
      report.checks.push(`${locale}-${width}: actionable parser-limit message; no partial source or review controls`);
    }
    report.checks.push('Continue focuses named recovery controls; rejected extraction exposes no correction editor');
    if (irBudget) {
      const retry = page.waitForResponse(r => r.url().endsWith('/api/action') && r.request().postDataJSON()?.action === 'retry_extraction');
      await page.locator('#retry-extraction').click();
      assert.equal((await retry).status(), 400);
      await page.waitForFunction(() => !mutationPending);
      assert.equal(await page.evaluate(() => state.selected.state.extraction_state), 'blocked');
      assert.equal(await page.evaluate(() => state.selected.extraction.blocks.length), 0);
      assert.deepEqual(await fs.readFile(sourcePath), sourceBytes);
      report.checks.push('A real retry remains bounded, keeps the blocked state and preserves the original');
      await page.locator('#source-search').fill('Owned saved query');
      await page.evaluate(async () => {window.scrollTo(0, 0); await saveDraft();});
      await page.waitForFunction(() => draftContext.pending === 0 && draftContext.sequence === draftContext.savedSequence);
      await page.locator('#ui-language').selectOption('zh-Hant');
      assert.equal(await page.locator('#source-search').inputValue(), 'Owned saved query');
      assert.equal(await page.evaluate(() => draftContext.revision === state.selected.ui_draft.revision), true);
      report.checks.push('An acknowledged clean draft survives a language rerender after failed extraction retry');
    }
    const diagnostic = '确认识别 <img data-injected="yes" src="x" onerror="window.__xmlInjected=true"> 日本語 Русский';
    await page.evaluate(value => {state.selected.state.diagnostics.push(value); render();}, diagnostic);
    for (const locale of ['zh-Hant', 'en']) {
      await page.locator('#ui-language').selectOption(locale);
      assert.ok((await page.locator('.card.next').innerText()).includes(diagnostic));
      assert.equal(await page.locator('[data-injected]').count(), 0);
    }
    assert.equal(await page.evaluate(() => window.__xmlInjected || false), false);
    report.checks.push('Unknown diagnostics remain original literal text in both languages');
    await page.locator('#back').click();
    await page.waitForFunction(() => !mutationPending);
    assert.equal(await page.locator('#file').count(), 1, await page.evaluate(() => operationBox.textContent));
    await page.locator('#file').setInputFiles({name: 'recovered.md', mimeType: 'text/markdown', buffer: Buffer.from('# New document\n\nA normal manuscript.')});
    await page.locator('#upload').click();
    await page.locator('[data-action="confirm-extraction"]').waitFor();
    assert.equal(await page.evaluate(() => state.selected.extraction.available), true);
    await page.locator('#continue-workflow').click();
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'source-preview');
    await page.locator('#show-correction').click();
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'correction');
    await page.locator('#continue-workflow').click();
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'correction');
    await page.locator('#cancel-correction').click();
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'show-correction');
    await page.locator('#continue-workflow').click();
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'source-preview');
    assert.equal(await page.evaluate(() => state.selected.state.extraction_state), 'unconfirmed');
    report.checks.push('Normal preview navigation follows the opened correction editor and returns after closing it without confirming extraction');
    assert.notEqual(await page.evaluate(() => state.selected.directory), original.directory);
    assert.deepEqual(await fs.readFile(sourcePath), sourceBytes);
    report.checks.push('The project library remains reachable, a new import succeeds, and the rejected original stays byte-for-byte intact');
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
    assert.ok(path.basename(cleanup).startsWith(label + '-'));
    await fs.rm(cleanup, {recursive: true, force: true});
  }
  process.stdout.write(JSON.stringify(report, null, 2) + '\n');
}
main().catch(error => {process.stderr.write(String(error.stack || error) + '\n'); process.exitCode = 1;});
