// DOCX visibility warnings must stay actionable and reach the saved protocol.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

async function main() {
  const containers = process.argv.includes('--containers');
  const label = containers ? 'docx-containers' : 'docx-visibility';
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'dist', 'browser-' + label);
  await fs.mkdir(output, {recursive: true});
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), label + '-'));
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
    const action = async (selector, name, status=201) => {
      const response = page.waitForResponse(r => r.url().endsWith('/api/action') && r.request().postDataJSON()?.action === name);
      await page.locator(selector).click();
      assert.equal((await response).status(), status);
      await page.waitForFunction(() => !mutationPending);
    };
    await page.goto(url);
    await page.locator('[data-action="confirm-extraction"]').waitFor();
    const originalBlocks = await page.evaluate(() => JSON.stringify(state.selected.extraction.blocks));
    const source = await page.evaluate(() => ({directory: state.selected.directory, source: state.selected.project.source}));
    const sourcePath = path.join(temporary, source.directory, source.source.relative_path);
    const sourceBytes = await fs.readFile(sourcePath);
    if (containers) {
      assert.deepEqual(JSON.parse(originalBlocks).map(block => block.text),
        ['Visible contribution inside a control.', 'Outside paragraph.']);
      report.checks.push('Content-control text is present in extracted blocks in the original order');
    }
    for (const [locale, width, expected] of [
      ['zh-Hant', 1440, containers ? ['尚未完整辨識的特殊內容'] : ['移動或格式修訂', '腳註', '尾註']],
      ['en', 320, containers ? ['special content', 'not been fully extracted'] : ['move or formatting revisions', 'footnote references', 'endnote references']],
    ]) {
      await page.locator('#ui-language').selectOption(locale);
      await page.setViewportSize({width, height: width === 1440 ? 1000 : 844});
      const warnings = await page.locator('.card.next .error, .card.next .warning').allTextContents();
      for (const text of expected) assert.ok(warnings.some(warning => warning.includes(text)));
      assert.equal(await page.evaluate(() => JSON.stringify(state.selected.extraction.blocks)), originalBlocks);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      await page.screenshot({path: path.join(output, `${locale}-${width}.png`), fullPage: true});
      report.checks.push(`${locale}-${width}: ${label} warnings are localized and readable; source remains intact`);
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
        visibility: contract.source_visibility.docx_content_visibility,
        requestId: request.request_id, prompt: request.prompt,
        noteTextPresent: request.prompt.includes('SYNTHETIC-NOTE-CONTENT'),
        embeddedTextPresent: request.prompt.includes('SYNTHETIC-EMBEDDED-NOT-EXTRACTED')};
    });
    assert.deepEqual(result.codes.sort(), containers ? ['docx-special-content'] : ['endnotes-incomplete', 'footnotes-incomplete', 'unaccepted-revisions']);
    assert.equal(result.noteTextPresent, !containers);
    if (containers) {
      assert.equal(result.embeddedTextPresent, false);
      assert.equal(result.visibility.coverage_measurement, 'not-measured');
      assert.equal(result.visibility.issues['word/document.xml']['{http://schemas.openxmlformats.org/wordprocessingml/2006/main}altChunk'], 1);
      assert.ok(result.prompt.includes('Visible contribution inside a control.'));
      assert.ok(result.prompt.includes('equations or compatibility branches must not be treated as absent'));
    }
    assert.equal(await fs.readFile(path.join(temporary, source.directory, 'ai-requests', result.requestId, 'prompt.md'), 'utf8'), result.prompt);
    assert.deepEqual(await fs.readFile(sourcePath), sourceBytes);
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
    assert.ok(path.basename(cleanup).startsWith(label + '-'));
    await fs.rm(cleanup, {recursive: true, force: true});
  }
  process.stdout.write(JSON.stringify(report, null, 2) + '\n');
}
main().catch(error => {process.stderr.write(String(error.stack || error) + '\n'); process.exitCode = 1;});
