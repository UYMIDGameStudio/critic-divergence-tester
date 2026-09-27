// Real browser regression in an isolated temporary library; no user files or services.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const {createHash} = require('node:crypto');
const assert = require('node:assert/strict');

async function fileHashes(directory, relative = '') {
  const result = {};
  for (const entry of await fs.readdir(path.join(directory, relative), {withFileTypes: true})) {
    const name = path.join(relative, entry.name);
    assert.equal(entry.isSymbolicLink(), false, 'Synthetic library must not contain links');
    if (entry.isDirectory()) Object.assign(result, await fileHashes(directory, name));
    else result[name] = createHash('sha256').update(await fs.readFile(path.join(directory, name))).digest('hex');
  }
  return result;
}

async function main() {
  const repo = path.resolve(__dirname, '..');
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'studio-fresh-import-browser-'));
  const library = path.join(temporary, 'library');
  const output = process.env.STUDIO_E2E_OUTPUT || path.join(repo, 'dist', 'browser-fresh-import-verification');
  await fs.mkdir(output, {recursive: true});
  const child = spawn(process.env.STUDIO_PYTHON || 'python',
    [path.join(__dirname, 'browser_fixture_server.py'), library],
    {cwd: repo, windowsHide: true, env: {...process.env, PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1'}});
  let browser, page, stderr = '';
  const report = {passed: false, checks: [], browserErrors: [], externalRequests: []};
  child.stderr.on('data', chunk => { stderr += chunk.toString(); });
  try {
    const url = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('Fixture startup timeout: ' + stderr)), 30000);
      let text = '';
      child.stdout.on('data', chunk => {
        text += chunk;
        const match = text.match(/http:\/\/127\.0\.0\.1:\d+\//);
        if (match) { clearTimeout(timer); resolve(match[0]); }
      });
      child.once('error', error => { clearTimeout(timer); reject(error); });
      child.once('exit', code => { clearTimeout(timer); reject(Error(`Fixture exited ${code}: ${stderr}`)); });
    });
    browser = await chromium.launch({headless: true,
      ...(process.env.STUDIO_BROWSER ? {executablePath: process.env.STUDIO_BROWSER} : {})});
    report.browserVersion = browser.version();
    page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    page.setDefaultTimeout(30000);
    page.on('pageerror', error => report.browserErrors.push(error.message));
    await page.route('**/*', route => {
      if (new URL(route.request().url()).origin === new URL(url).origin) return route.continue();
      report.externalRequests.push(route.request().url());
      return route.abort('blockedbyclient');
    });
    await page.goto(url);
    await page.locator('#file').waitFor();
    const labelHant = '重新解析為新專案（保留舊專案與審查記錄）';
    const labelEnglish = 'Reparse as a new project (keep the existing project and review history)';
    const verifiedHant = '模型聲明已核實（應用未獨立核驗）';
    const verifiedEnglish = 'Model-declared verified (not independently checked by this application)';
    const file = {name: 'fresh-import-fixture.md', mimeType: 'text/markdown',
      buffer: Buffer.from('# 活动方案\n\n相关人员及时完成报名。\n\nSynthetic import fixture: Straße 日本語 Русский Latīna.\n', 'utf8')};
    async function action(locator, name) {
      const [response] = await Promise.all([
        page.waitForResponse(r => r.url().endsWith('/api/action') && r.request().postDataJSON()?.action === name),
        locator.click(),
      ]);
      assert.equal(response.status(), 201, await response.text());
      await page.waitForFunction(() => !mutationPending);
    }
    async function upload(fresh, label) {
      const control = page.getByLabel(label, {exact: true});
      assert.equal(await control.isChecked(), false, 'New project must remain opt-in on every home render');
      if (fresh) await control.check();
      await page.locator('#file').setInputFiles(file);
      const [response] = await Promise.all([
        page.waitForResponse(r => r.url().endsWith('/api/upload')),
        page.locator('#upload').click(),
      ]);
      assert.equal(response.status(), 201, await response.text());
      assert.equal(response.request().postDataJSON().new_project, fresh);
      await page.waitForFunction(() => !mutationPending && Boolean(state.selected));
      return page.evaluate(() => ({directory: state.selected.directory, project: state.selected.project,
        status: state.selected.state, findings: state.selected.findings.length}));
    }
    async function closeProject() {
      await action(page.locator('#back'), 'close_project');
      await page.locator('#file').waitFor();
      await page.waitForFunction(() => !state.selected && !mutationPending);
    }
    const original = await upload(false, labelHant);
    await action(page.locator('[data-action="confirm-extraction"]'), 'confirm_extraction');
    for (const [id, value] of Object.entries({jurisdiction: 'unknown', effective_date: 'unknown',
      publisher_type: 'Test author', audience: 'Test participants'})) await page.locator('#' + id).fill(value);
    await action(page.locator('#confirm-context'), 'confirm_context');
    await page.locator('.critic').evaluateAll(nodes => nodes.forEach(node => { node.checked = node.value === 'expression_ambiguity'; }));
    await action(page.locator('#run-precheck'), 'run_local_prechecks');
    assert.ok(await page.evaluate(() => state.selected.findings.length > 0));
    assert.ok(await page.evaluate(() => Object.values(state.selected.verification_context).every(item => item.source_kind === 'local')));

    // An imported "verified" enum remains archival data, but its visible authority is explicit.
    await action(page.locator('#prepare-ai'), 'prepare_ai_audits');
    const modelInput = await page.evaluate(() => {
      const request = state.selected.ai_requests.find(item => item.critic === 'expression_ambiguity');
      const finding = {...state.selected.findings.find(item => item.critic === request.critic),
        finding_id: 'MODEL-VERIFIED', verification_state: 'verified', external_basis: {}, check_data: {}};
      return {requestId: request.request_id, response: JSON.stringify({critic: request.critic, findings: [finding]})};
    });
    await page.locator('#ai-request').selectOption(modelInput.requestId);
    await page.locator('#ai-response').fill(modelInput.response);
    await action(page.locator('#import-ai'), 'import_ai_audit');
    await page.locator('[data-stage="adjudication"]').first().click();
    await page.getByText(verifiedHant, {exact: true}).waitFor();
    assert.equal(await page.locator('.finding-card .pill').getByText('verified', {exact: true}).count(), 0);
    await page.locator('#ui-language').selectOption('en');
    await page.waitForFunction(() => document.documentElement.lang === 'en');
    await page.getByText(verifiedEnglish, {exact: true}).waitFor();
    report.checks.push('Real imported verified finding is model-declared in Traditional Chinese and English; local findings have local provenance');
    await page.screenshot({path: path.join(output, 'verified-en.png'), fullPage: true});
    await closeProject();

    const reopened = await upload(false, labelEnglish);
    assert.equal(reopened.directory, original.directory);
    assert.equal(reopened.project.project_id, original.project.project_id);
    assert.equal(reopened.status.extraction_state, 'confirmed');
    assert.equal(reopened.status.context_state, 'confirmed');
    assert.equal(reopened.status.review_state, 'ai_review_imported');
    assert.equal(reopened.findings, 1);
    await closeProject();
    report.checks.push('Ordinary same-file upload reopens the existing reviewed project');
    const originalPath = path.join(library, original.directory);
    const before = await fileHashes(originalPath);

    await page.getByLabel(labelEnglish, {exact: true}).check();
    await page.screenshot({path: path.join(output, 'fresh-import-en.png'), fullPage: true});
    await page.getByLabel(labelEnglish, {exact: true}).uncheck();
    const freshOne = await upload(true, labelEnglish);
    await closeProject();
    await page.locator('#ui-language').selectOption('zh-Hant');
    await page.waitForFunction(() => document.documentElement.lang === 'zh-Hant');
    await page.getByLabel(labelHant, {exact: true}).check();
    await page.screenshot({path: path.join(output, 'fresh-import-zh-Hant.png'), fullPage: true});
    await page.getByLabel(labelHant, {exact: true}).uncheck();
    const freshTwo = await upload(true, labelHant);
    await closeProject();
    for (const fresh of [freshOne, freshTwo]) {
      assert.equal(fresh.status.extraction_state, 'unconfirmed');
      assert.equal(fresh.status.context_state, 'missing');
      assert.equal(fresh.status.review_state, 'not_started');
      assert.equal(fresh.findings, 0);
      assert.deepEqual(fresh.project.source, original.project.source);
      const files = await fileHashes(path.join(library, fresh.directory));
      assert.equal(Object.keys(files).some(name => name === 'context.json' || name.startsWith('audits' + path.sep)), false);
    }
    assert.equal(new Set([original.directory, freshOne.directory, freshTwo.directory]).size, 3);
    assert.equal(new Set([original.project.project_id, freshOne.project.project_id, freshTwo.project.project_id]).size, 3);
    assert.deepEqual(await fileHashes(originalPath), before);
    assert.equal((await fs.readdir(library)).filter(name => name.endsWith('.document-review-studio')).length, 3);
    report.oldProjectFilesChecked = Object.keys(before).length;
    report.checks.push('Both translated opt-in controls create independent unconfirmed projects with identical source binding',
      'Existing source, extraction, review, request, receipt, state and draft files remain byte-identical');
    assert.deepEqual(report.browserErrors, []);
    assert.deepEqual(report.externalRequests, []);
    report.passed = true;
  } finally {
    if (page) await page.evaluate(() => fetch('/api/shutdown', {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Document-Review-Token': TOKEN}, body: '{}'})).catch(() => {});
    if (browser) await browser.close();
    if (child.exitCode === null) await new Promise(resolve => {
      const timer = setTimeout(() => { child.kill(); resolve(); }, 5000);
      child.once('exit', () => { clearTimeout(timer); resolve(); });
    });
    report.stderr = stderr;
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(report, null, 2));
    const resolved = await fs.realpath(temporary);
    assert.equal(path.dirname(resolved), await fs.realpath(os.tmpdir()));
    assert.ok(path.basename(resolved).startsWith('studio-fresh-import-browser-'));
    await fs.rm(resolved, {recursive: true, force: true});
  }
  console.log(JSON.stringify(report, null, 2));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
