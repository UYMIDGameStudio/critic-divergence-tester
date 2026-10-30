// Recover actual body text and a destructive approved edit through the real UI.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

async function main() {
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'dist', 'browser-reviewable-text');
  await fs.mkdir(output, {recursive: true});
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'reviewable-text-'));
  const child = spawn(process.env.STUDIO_PYTHON || 'python',
    [path.join(__dirname, 'browser_fixture_server.py'), temporary, '--empty-body'],
    {cwd: root, windowsHide: true, env: {...process.env, PYTHONUTF8: '1'}});
  let browser, stderr = '';
  const report = {passed: false, checks: [], errors: [], externalRequests: []};
  child.stderr.on('data', data => {stderr += data;});
  try {
    const url = await new Promise((resolve, reject) => {
      let value = '';
      const timeout = setTimeout(() => reject(Error('Fixture startup timeout: ' + stderr)), 30000);
      child.stdout.on('data', data => {
        value += data;
        const match = value.match(/http:\/\/127\.0\.0\.1:\d+\//);
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
    const action = async (locator, name, status = 201) => {
      const response = page.waitForResponse(r => r.url().endsWith('/api/action') && r.request().postDataJSON()?.action === name);
      await locator.click();
      const result = await response;
      assert.equal(result.status(), status, await result.text());
      await page.waitForFunction(() => !mutationPending);
    };
    await page.goto(url);
    await page.locator('.empty-body-warning').waitFor();
    const directory = await page.evaluate(() => state.selected.directory);
    const original = await fs.readFile(path.join(temporary, directory, 'source/empty.csv'));
    for (const [locale, width, expected] of [['zh-Hant', 1440, '沒有可審查正文'], ['en', 320, 'no body text to review']]) {
      await page.locator('#ui-language').selectOption(locale);
      await page.setViewportSize({width, height: width === 1440 ? 1000 : 844});
      assert.ok((await page.locator('.empty-body-warning').innerText()).includes(expected));
      assert.equal(await page.locator('[data-action="confirm-extraction"], [data-action="continue-extraction"]').count(), 0);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      await page.screenshot({path: path.join(output, `${locale}-${width}.png`), fullPage: true});
      report.checks.push(`${locale}-${width}: no empty-body confirmation; correction and replacement remain available`);
    }
    await page.setViewportSize({width: 1440, height: 1000});
    await page.locator('#show-correction').click();
    await page.locator('#correction').fill('\u200b\u2060');
    await action(page.locator('#show-correction'), 'confirm_extraction', 400);
    assert.equal(await page.locator('#correction').inputValue(), '\u200b\u2060');
    await page.locator('#correction').fill('相关人员及时完成报名。');
    await action(page.locator('#show-correction'), 'confirm_extraction');
    for (const [id, value] of Object.entries({jurisdiction: 'unknown', effective_date: 'unknown', publisher_type: 'author', audience: 'readers'}))
      await page.locator('#' + id).fill(value);
    await action(page.locator('#confirm-context'), 'confirm_context');
    await page.locator('.critic').evaluateAll(nodes => nodes.forEach(node => {node.checked = node.value === 'expression_ambiguity';}));
    await action(page.locator('#run-precheck'), 'run_local_prechecks');
    const findings = await page.evaluate(() => state.selected.findings.map(f => f.finding_id));
    for (const [index, id] of findings.entries()) {
      await page.locator('#reason-' + id).fill('Synthetic explicit scope.');
      await action(page.locator(`.decision[data-id="${id}"][data-value="${index === 0 ? 'accept' : 'reject'}"]`), 'decide_finding');
    }
    await action(page.locator('#prepare-bridge'), 'prepare_bridge');
    const identity = await page.locator('.confirm-operation').first().getAttribute('data-action-id');
    const confirmOperation = async value => {
      await page.locator('#operation-' + identity).selectOption(value);
      await page.locator('#operation-reason-' + identity).fill('Synthetic explicit operation.');
      await action(page.locator(`.confirm-operation[data-action-id="${identity}"]`), 'set_revision_action_operation');
    };
    const propose = async text => {
      await page.locator('#revision-' + identity).fill(text);
      await page.locator('#revision-reason-' + identity).fill('Synthetic explicit proposal.');
      await page.locator('#revision-provenance-' + identity).selectOption('ai-assisted-manual-import');
      await action(page.locator(`.propose-hunk[data-action-id="${identity}"]`), 'propose_revision_hunk');
      assert.equal(await page.locator('#finalize-revision').isDisabled(), true);
    };
    const approve = async () => {
      const button = page.locator('.decide-hunk[data-decision="approve"]');
      const id = await button.getAttribute('data-hunk-id');
      await page.locator('#hunk-reason-' + id).fill('Synthetic explicit approval.');
      await action(button, 'decide_revision_hunk');
      return id;
    };
    await confirmOperation('delete_block');
    await propose('');
    const first = await approve();
    const oldHunk = await fs.readFile(path.join(temporary, directory, 'revision-hunks', first + '.json'));
    await action(page.locator('#finalize-revision'), 'finalize_revision', 400);
    assert.ok((await page.locator('body').innerText()).includes('leave no body text'));
    await page.locator('.operation-settings summary').click();
    await confirmOperation('replace_block');
    assert.equal(await page.evaluate(() => state.selected.revision_workspace.actions[0].hunk_stale), true);
    await propose('负责人周五前完成报名。');
    await approve();
    await page.locator('.hunk-redraft > summary').click();
    assert.equal(await page.locator('#revision-' + identity).inputValue(), '负责人周五前完成报名。');
    assert.equal(await page.locator('#revision-provenance-' + identity).inputValue(), 'ai-assisted-manual-import');
    await page.locator('.revision-action-card').screenshot({path: path.join(output, 'approved-proposal-edit.png')});
    await propose('项目负责人周五前完成报名并确认名单。');
    await approve();
    await action(page.locator('#finalize-revision'), 'finalize_revision');
    await action(page.locator('#export-final'), 'export');
    assert.deepEqual(await fs.readFile(path.join(temporary, directory, 'revision-hunks', first + '.json')), oldHunk);
    assert.deepEqual(await fs.readFile(path.join(temporary, directory, 'source/empty.csv')), original);
    report.checks.push('Format-only correction is rejected without losing the draft; real text recovers the review flow');
    report.checks.push('An approved deletion cannot generate an empty revision; changing operation requires a fresh proposal and approval');
    report.checks.push('An approved proposal can be edited with provenance retained, reapproved and exported; previous hunk and source bytes remain intact');
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
    assert.ok(path.basename(cleanup).startsWith('reviewable-text-'));
    await fs.rm(cleanup, {recursive: true, force: true});
  }
  process.stdout.write(JSON.stringify(report, null, 2) + '\n');
}
main().catch(error => {process.stderr.write(String(error.stack || error) + '\n'); process.exitCode = 1;});
