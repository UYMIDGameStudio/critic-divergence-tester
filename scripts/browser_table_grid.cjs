// Complete a real approved cell edit and inspect the downloaded native Word XML.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn, execFileSync} = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

async function main() {
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'dist', 'browser-table-grid');
  await fs.mkdir(output, {recursive: true});
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'table-grid-'));
  const child = spawn(process.env.STUDIO_PYTHON || 'python',
    [path.join(__dirname, 'browser_fixture_server.py'), temporary, '--table-grid'],
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
      const value = await response;
      assert.equal(value.status(), 201, await value.text());
      await page.waitForFunction(() => !mutationPending);
    };
    await page.goto(url);
    await page.locator('[data-action="confirm-extraction"]').waitFor();
    const original = await page.evaluate(() => ({directory: state.selected.directory, source: state.selected.project.source}));
    const originalPath = path.join(temporary, original.directory, original.source.relative_path);
    const originalBytes = await fs.readFile(originalPath);
    for (const [locale, width, text] of [['zh-Hant', 1440, '依邏輯欄展開'], ['en', 320, 'represented by logical columns']]) {
      await page.locator('#ui-language').selectOption(locale);
      await page.setViewportSize({width, height: width === 1440 ? 1000 : 844});
      assert.ok((await page.locator('.card.next .warning').innerText()).includes(text));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      await page.screenshot({path: path.join(output, `${locale}-${width}.png`), fullPage: true});
      report.checks.push(`${locale}-${width}: logical-grid warning is readable`);
    }
    await page.setViewportSize({width: 1440, height: 1000});
    await action(page.locator('[data-action="confirm-extraction"]'), 'confirm_extraction');
    for (const [id, value] of Object.entries({jurisdiction: 'unknown', effective_date: 'unknown', publisher_type: 'author', audience: 'readers'}))
      await page.locator('#' + id).fill(value);
    await action(page.locator('#confirm-context'), 'confirm_context');
    await page.locator('.critic').evaluateAll(nodes => nodes.forEach(node => {node.checked = node.value === 'expression_ambiguity';}));
    await action(page.locator('#run-precheck'), 'run_local_prechecks');
    const selection = await page.evaluate(() => {
      const cell = state.selected.extraction.blocks.find(row => row.kind === 'table_cell' && row.location.column === 3);
      const finding = state.selected.findings.find(row => row.location.block_id === cell.block_id);
      return {finding: finding.finding_id, all: state.selected.findings.map(row => row.finding_id)};
    });
    for (const id of selection.all) {
      await page.locator('#reason-' + id).fill('Synthetic explicit scope: change the selected cell only.');
      await action(page.locator(`.decision[data-id="${id}"][data-value="${id === selection.finding ? 'accept' : 'reject'}"]`), 'decide_finding');
    }
    await action(page.locator('#prepare-bridge'), 'prepare_bridge');
    const operation = page.locator('.confirm-operation').first();
    const identity = await operation.getAttribute('data-action-id');
    await page.locator('#operation-' + identity).selectOption('replace_table_cell');
    await page.locator('#operation-reason-' + identity).fill('Preserve the neighboring cell and grid.');
    await action(operation, 'set_revision_action_operation');
    const replacement = '负责人周五前完成报名。';
    await page.locator('#revision-' + identity).fill(replacement);
    await page.locator('#revision-reason-' + identity).fill('Specify the actor and deadline.');
    await action(page.locator('.propose-hunk').first(), 'propose_revision_hunk');
    const approve = page.locator('.decide-hunk[data-decision="approve"]').first();
    await page.locator('#hunk-reason-' + await approve.getAttribute('data-hunk-id')).fill('Synthetic explicit approval.');
    await action(approve, 'decide_revision_hunk');
    await action(page.locator('#finalize-revision'), 'finalize_revision');
    await action(page.locator('#export-final'), 'export');
    const destination = path.join(temporary, 'result.docx');
    const download = page.waitForEvent('download');
    await page.locator('.download[data-path$="修改稿.docx"]').click();
    await (await download).saveAs(destination);
    const code = `import json,sys,zipfile,xml.etree.ElementTree as E
with zipfile.ZipFile(sys.argv[1]) as z: root=E.fromstring(z.read('word/document.xml'))
n={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
t=root.find('w:body/w:tbl',n); r=t.find('w:tr',n)
print(json.dumps({'cells':[''.join(x.itertext()) for x in r.findall('w:tc',n)],'before':r.find('w:trPr/w:gridBefore',n).get('{'+n['w']+'}val'),'columns':len(t.findall('w:tblGrid/w:gridCol',n))},ensure_ascii=False))`;
    const word = JSON.parse(execFileSync(process.env.STUDIO_PYTHON || 'python', ['-c', code, destination],
      {cwd: root, windowsHide: true, encoding: 'utf8', env: {...process.env, PYTHONUTF8: '1'}}));
    assert.deepEqual(word, {cells: ['Unchanged', replacement], before: '1', columns: 5});
    const markdownDownload = page.waitForEvent('download');
    await page.locator('.download[data-path$="修改稿.md"]').last().click();
    const markdownPath = path.join(temporary, 'result.md');
    await (await markdownDownload).saveAs(markdownPath);
    const markdown = await fs.readFile(markdownPath, 'utf8');
    assert.ok(markdown.includes(`|  | Unchanged |  | ${replacement} |  |`));
    assert.equal(markdown.includes('相关人员及时完成报名。'), false);
    assert.deepEqual(await fs.readFile(originalPath), originalBytes);
    report.checks.push('Explicit finding, operation, hunk approval and export update logical column 3 only');
    report.checks.push('Downloaded Word retains two physical cells, its grid and omitted leading column; normalized Markdown has five aligned columns');
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
    assert.ok(path.basename(cleanup).startsWith('table-grid-'));
    await fs.rm(cleanup, {recursive: true, force: true});
  }
  process.stdout.write(JSON.stringify(report, null, 2) + '\n');
}
main().catch(error => {process.stderr.write(String(error.stack || error) + '\n'); process.exitCode = 1;});
