// Render actual exported reports with raw HTML enabled in the reference parser.
const commonmark = require('commonmark');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

async function main() {
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'dist', 'browser-report-text');
  await fs.mkdir(output, {recursive: true});
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'report-text-'));
  const child = spawn(process.env.STUDIO_PYTHON || 'python',
    [path.join(__dirname, 'report_text_fixture.py'), temporary],
    {cwd: root, windowsHide: true, env: {...process.env, PYTHONUTF8: '1'}});
  let browser, stderr = '';
  child.stderr.on('data', data => {stderr += data;});
  const report = {passed: false, checks: [], errors: [], externalRequests: []};
  try {
    const fixture = await new Promise((resolve, reject) => {
      let stdout = '';
      const timer = setTimeout(() => {child.kill(); reject(Error('Report fixture timeout: ' + stderr));}, 120000);
      child.stdout.on('data', data => {stdout += data;});
      child.once('error', error => {clearTimeout(timer); reject(error);});
      child.once('exit', code => {
        clearTimeout(timer);
        if (code) return reject(Error(`Report fixture failed ${code}: ${stderr}`));
        try {resolve(JSON.parse(stdout));} catch (error) {reject(error);}
      });
    });
    const reader = new commonmark.Parser();
    const writer = new commonmark.HtmlRenderer({safe: false});
    assert.ok(writer.render(reader.parse(fixture.literal)).includes(fixture.literal.split('\n')[0]));
    report.checks.push('Positive control: reference renderer permits raw HTML; escaping is not masked by a safe-renderer option');
    browser = await chromium.launch({headless: true,
      ...(process.env.STUDIO_BROWSER ? {executablePath: process.env.STUDIO_BROWSER} : {})});
    const page = await browser.newPage();
    page.on('pageerror', error => report.errors.push(error.message));
    await page.route('**/*', route => {
      report.externalRequests.push(route.request().url());
      return route.abort();
    });
    for (const [name, file] of Object.entries(fixture.reports)) {
      const markdown = await fs.readFile(file, 'utf8');
      const tree = reader.parse(markdown);
      const walker = tree.walker();
      let event;
      while ((event = walker.next())) {
        if (!event.entering) continue;
        assert.ok(!['html_inline', 'html_block', 'image', 'link'].includes(event.node.type), `${name}: active ${event.node.type}`);
        if (name === 'literal-probe')
          assert.ok(!['heading', 'list', 'emph', 'strong', 'code', 'code_block'].includes(event.node.type), 'literal probe must remain prose');
      }
      const html = writer.render(tree);
      for (const width of [1440, 320]) {
        await page.setViewportSize({width, height: 900});
        await page.setContent('<!doctype html><meta charset="utf-8"><style>body{margin:20px;font:16px/1.6 Arial;overflow-wrap:anywhere}pre{white-space:pre-wrap}code{white-space:pre-wrap}</style><main>' + html + '</main>');
        assert.equal(await page.locator('main img, main script, main iframe, main svg, main a, main style, main object, main form').count(), 0, name);
        assert.equal(await page.evaluate(() => window.__reportInjected), undefined);
        assert.equal(await page.getByRole('heading', {name: 'FORGED APPROVAL', exact: true}).count(), 0);
        if (!['revision-risks', 'word-export-info'].includes(name)) assert.ok((await page.locator('main').textContent()).includes(fixture.languages), name);
        if (['ai-review', 'formal-review'].includes(name)) assert.ok((await page.locator('main').textContent()).includes(fixture.source_name), name);
        if (name === 'literal-probe') {
          const normalize = value => value.replace(/\s+/g, ' ').trim();
          assert.equal(normalize(await page.locator('main').textContent()), normalize(fixture.literal));
        }
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, name);
      }
      if (['academic-formal', 'literal-probe', 'revision-difference'].includes(name))
        await page.screenshot({path: path.join(output, `${name}-320.png`), fullPage: true});
      await fs.writeFile(path.join(output, `${name}.html`), html);
      report.checks.push(`${name}: actual exported Markdown is inert and readable at 1440 and 320 pixels`);
    }
    assert.equal(fixture.source_and_model_records_unchanged, true);
    assert.equal(fixture.historical_reports_preserved, true);
    assert.deepEqual(report.errors, []);
    assert.deepEqual(report.externalRequests, []);
    report.checks.push('Original source, approved manuscript and raw model records are preserved; no browser errors or external requests');
    report.browserVersion = browser.version();
    report.passed = true;
  } finally {
    if (browser) await browser.close();
    if (child.exitCode === null) child.kill();
    report.stderr = stderr;
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(report, null, 2));
    const resolved = await fs.realpath(temporary);
    assert.equal(path.dirname(resolved), await fs.realpath(os.tmpdir()));
    assert.ok(path.basename(resolved).startsWith('report-text-'));
    await fs.rm(resolved, {recursive: true, force: true});
  }
  console.log(JSON.stringify(report, null, 2));
}
main().catch(error => {console.error(error); process.exitCode = 1;});
