// Inspect source fragments and attribution without treating them as verdicts.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

async function main() {
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'dist', 'browser-quote-context');
  await fs.mkdir(output, {recursive: true});
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'quote-context-'));
  const child = spawn(process.env.STUDIO_PYTHON || 'python',
    [path.join(__dirname, 'browser_fixture_server.py'), temporary, '--quote-context'],
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
    const action = async (locator, name) => {
      const response = page.waitForResponse(r => r.url().endsWith('/api/action') && r.request().postDataJSON()?.action === name);
      await locator.click();
      const result = await response;
      assert.equal(result.status(), 201, await result.text());
      await page.waitForFunction(() => !mutationPending);
    };
    await page.goto(url);
    await page.locator('.source-quote-review').first().waitFor();
    const saved = await page.evaluate(() => ({directory: state.selected.directory,
      ids: Object.fromEntries(state.selected.findings.map(f => [f.source_finding_id, f.finding_id])),
      text: state.selected.extraction.blocks[0].text}));
    const project = path.join(temporary, saved.directory);
    const source = await fs.readFile(path.join(project, 'source/excerpt.md'));
    const auditDirectory = path.join(project, 'audits/execution_feasibility');
    const originals = new Map();
    for (const item of await fs.readdir(auditDirectory, {withFileTypes: true}))
      if (item.isFile()) originals.set(item.name, await fs.readFile(path.join(auditDirectory, item.name)));
    const card = name => page.locator(`.finding-card:has(.decision[data-id="${saved.ids[name]}"])`);
    for (const [locale, width, edge, repeat] of [
      ['zh-Hant', 1440, '數字內部截取', '出現多次'], ['en', 320, 'cut into a word', 'more than once']]) {
      await page.locator('#ui-language').selectOption(locale);
      await page.setViewportSize({width, height: width === 1440 ? 1000 : 844});
      assert.ok((await card('FWORD').locator('.excerpt-edge-warning').innerText()).includes(edge));
      assert.ok((await card('FWORD').locator('.excerpt-context').innerText()).includes('cannot'));
      assert.ok((await card('FNUMBER').locator('.excerpt-context').innerText()).includes('15%'));
      assert.equal(await card('FWORD').locator('mark').innerText(), 'can');
      assert.ok((await card('FREPEAT').locator('.excerpt-repeat-warning').innerText()).includes(repeat));
      assert.equal(await card('FREPEAT').locator('.excerpt-context').count(), 2);
      assert.ok((await card('FREPEAT').locator('.source-quote-review').innerText()).includes('not approved'));
      await card('FESC').locator('.source-quote-review > summary').click();
      assert.equal(await card('FESC').locator('.excerpt-context img').count(), 0);
      assert.ok((await card('FESC').locator('.excerpt-context').innerText()).includes('<img src=x'));
      assert.equal(await page.evaluate(() => window.__quoteInjected), undefined);
      assert.equal(await page.evaluate(() => state.selected.extraction.blocks[0].text), saved.text);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      await card('FWORD').screenshot({path: path.join(output, `${locale}-${width}.png`)});
      report.checks.push(`${locale}-${width}: partial words and quantities, repeated attribution and inert source HTML are visible`);
    }
    await page.setViewportSize({width: 1440, height: 1000});
    await action(page.locator('#export-ai-reviews'), 'export_ai_reviews');
    const download = page.waitForEvent('download');
    await page.locator('.download[data-path$="AI审查结果.json"]').click();
    const destination = path.join(temporary, 'snapshot.json');
    await (await download).saveAs(destination);
    const value = JSON.parse(await fs.readFile(destination, 'utf8'));
    assert.equal(value.verification_context[saved.ids.FNUMBER].source_quote_review.contexts[0].excerpt, '5%');
    assert.equal(value.verification_context[saved.ids.FNUMBER].source_quote_review.semantic_accuracy, 'not-established');
    assert.equal(value.human_decisions_included, false);
    for (const id of Object.values(saved.ids)) {
      await page.locator('#reason-' + id).fill('A fragment alone does not establish the synthetic criticism.');
      await action(page.locator(`.decision[data-id="${id}"][data-value="reject"]`), 'decide_finding');
    }
    await action(page.locator('#export-results'), 'export');
    for (const [name, data] of originals) assert.deepEqual(await fs.readFile(path.join(auditDirectory, name)), data);
    assert.deepEqual(await fs.readFile(path.join(project, 'source/excerpt.md')), source);
    report.checks.push('Downloaded AI snapshot retains source contexts and the advisory boundary, without human decisions');
    report.checks.push('Human adjudication and formal export preserve all original model audit and source bytes');
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
    assert.ok(path.basename(cleanup).startsWith('quote-context-'));
    await fs.rm(cleanup, {recursive: true, force: true});
  }
  process.stdout.write(JSON.stringify(report, null, 2) + '\n');
}
main().catch(error => {process.stderr.write(String(error.stack || error) + '\n'); process.exitCode = 1;});
