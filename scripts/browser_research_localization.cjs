// Real local APIs and browser File inputs. No model or remote service is used.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');

async function main() {
  const root = path.resolve(__dirname, '..');
  const output = path.resolve(process.env.RESEARCH_UI_OUTPUT || path.join(root, 'dist', 'browser-research-localization'));
  await fs.mkdir(output, {recursive: true});
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'research-ui-'));
  const server = spawn(process.env.STUDIO_PYTHON || 'python', [
    path.join(__dirname, 'research_fixture_server.py'), temporary,
  ], {cwd: root, windowsHide: true, env: {...process.env, PYTHONUTF8: '1'}});
  const unifiedServer = spawn(process.env.STUDIO_PYTHON || 'python', [
    path.join(__dirname, 'browser_fixture_server.py'), path.join(temporary, 'unified-library'),
  ], {cwd: root, windowsHide: true, env: {...process.env, PYTHONUTF8: '1'}});
  let unifiedOutput = '', unifiedError = '';
  unifiedServer.stdout.on('data', data => {unifiedOutput += data.toString();});
  unifiedServer.stderr.on('data', data => {unifiedError += data.toString();});
  let browser, stderr = '';
  server.stderr.on('data', data => {stderr += data.toString();});
  try {
    const endpoints = await new Promise((resolve, reject) => {
      let stdout = '';
      const timer = setTimeout(() => reject(Error('Fixture startup timed out: ' + stderr)), 30000);
      server.stdout.on('data', data => {
        stdout += data.toString();
        if (stdout.includes('\n')) {clearTimeout(timer); resolve(JSON.parse(stdout.split('\n')[0]));}
      });
      server.once('exit', code => {clearTimeout(timer); reject(Error('Fixture exited ' + code + ': ' + stderr));});
    });
    browser = await chromium.launch({headless: true,
      ...(process.env.STUDIO_BROWSER ? {executablePath: process.env.STUDIO_BROWSER} : {})});
    const page = await browser.newPage({viewport: {width: 1440, height: 1050}});
    const screenshot = async name => {
      await page.screenshot({path: path.join(output, name + '.png'), fullPage: true});
    };
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const language = async (locale, selector = '#research-language') => {
      await page.locator(selector).selectOption(locale);
      await page.waitForFunction(locale => document.documentElement.lang === locale, locale);
    };
    const unifiedUrl = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('Unified fixture startup timed out: ' + unifiedError)), 30000);
      const inspect = () => {
        const match = unifiedOutput.match(/http:\/\/127\.0\.0\.1:\d+\//);
        if (match) {clearTimeout(timer); resolve(match[0]);}
      };
      unifiedServer.stdout.on('data', inspect);
      unifiedServer.once('exit', code => {clearTimeout(timer); reject(Error('Unified fixture exited ' + code + ': ' + unifiedError));});
      inspect();
    });
    await page.goto(unifiedUrl);
    await page.locator('#start-report-revision').waitFor();
    assert.equal(await page.locator('#start-new-review').innerText(), '新稿審查');
    assert.equal(await page.locator('#start-report-revision').innerText(), '已有審查報告，直接修稿');
    assert.equal(await page.locator('#start-report-revision').getAttribute('href'), '/research/');
    await language('en', '#ui-language');
    assert.equal(await page.locator('#start-new-review').innerText(), 'Review a new manuscript');
    assert.equal(await page.locator('#start-report-revision').innerText(), 'Revise from an existing review report');
    assert.ok((await page.locator('#app').innerText()).includes('Argument maps and existing research projects'));
    await screenshot('default-task-entry-en');
    await language('zh-Hant', '#ui-language');
    await page.locator('#start-report-revision').click();
    await page.waitForURL(new URL('research/', unifiedUrl).href);
    await page.locator('#file').waitFor();
    assert.equal(await page.locator('html').getAttribute('lang'), 'zh-Hant');
    const russian = endpoints.samples.find(sample => sample.encoding === 'cp1251');
    const fileBytes = Buffer.from(russian.base64, 'base64');
    const title = '新建项目 · Straße · Français · Rātiō';
    await page.locator('#title').fill(title);
    await page.locator('#encoding').selectOption('cp1251');
    await page.locator('#file').setInputFiles({name: 'русский.txt', mimeType: 'text/plain', buffer: fileBytes});
    await language('en');
    assert.equal(await page.locator('#title').inputValue(), title);
    assert.equal(await page.locator('#encoding').inputValue(), 'cp1251');
    assert.equal(await page.locator('#file').evaluate(node => node.files[0].name), 'русский.txt');
    assert.equal(await page.locator('#create').innerText(), 'Import and preserve as V1');
    await screenshot('research-upload-en');
    await language('zh-Hant');
    const submitted = page.waitForResponse(response => response.url().endsWith('/api/projects'));
    await page.locator('#create').click();
    const response = await submitted;
    assert.equal(response.status(), 201, await response.text());
    const payload = response.request().postDataJSON();
    assert.equal(payload.content_base64, russian.base64);
    assert.equal(payload.encoding, 'cp1251');
    assert.equal(payload.title, title);
    assert.equal(Object.hasOwn(payload, 'content'), false);
    await page.locator('#report').waitFor();
    const report = '未裁决 新建项目 · evidence Straße é 日本語 русский Rātiō';
    await page.locator('#report').fill(report);
    await language('en');
    assert.equal(await page.locator('#report').inputValue(), report);
    assert.ok((await page.locator('#app').innerText()).includes(title));
    await language('zh-Hant');
    assert.equal(await page.locator('#report').inputValue(), report);

    // Reach the actual confirmation stage through the real report/revision APIs.
    // Responses below are explicit synthetic fixtures, not model-quality evidence.
    const action = async (selector, expectedStage) => {
      const request = page.waitForResponse(response => response.url().endsWith('/api/action'));
      await page.locator(selector).click();
      const response = await request;
      assert.equal(response.status(), 201, await response.text());
      if (expectedStage) await page.waitForFunction(stage => state.selected.stage === stage, expectedStage);
    };
    const promptObjects = async field => {
      const prompt = await page.evaluate(field => state.selected[field], field);
      return [...prompt.matchAll(/```json\s*([\s\S]*?)```/g)].map(match => JSON.parse(match[1]));
    };
    await action('#importReport', 'atomization_result');
    await page.locator('#response').fill('{"invalid_fixture":true}');
    await action('#submitResponse', 'atomization_result');
    await page.locator('#copyRepair').waitFor();
    assert.equal(await page.evaluate(() => state.selected.atomization_attempt.valid), false);
    const [atomization] = await promptObjects('atomization_prompt');
    atomization.findings = ['F1', 'F2', 'F3', 'F4'].map((finding_id, index) => ({
      finding_id, claim_id: 'C1', report_quote: report, manuscript_quote: russian.text,
      location_kind: 'exact_quote', assertion: 'Synthetic review issue ' + (index + 1),
      criterion: 'Make the claim scope explicit', suggested_action: 'State the claim limitation.',
      evidence_level: 'unverified', uncertainties: ['Independent evidence is still missing.'],
    }));
    await page.locator('#response').fill(JSON.stringify(atomization));
    await action('#submitResponse', 'findings_confirm');
    for (const [id, decision] of [['F1', 'accept'], ['F2', 'accept'], ['F3', 'reject'], ['F4', 'defer']]) {
      await page.locator('#reason-' + id).fill('Synthetic participant chooses ' + decision);
      await action(`.finding[data-id="${id}"][data-decision="${decision}"]`);
      await page.waitForFunction(({id, decision}) => state.selected.findings.find(row => row.finding_id === id).decision === decision,
        {id, decision});
    }
    await action('#prepareRevision', 'revision_result');
    const [proposal, allowed] = await promptObjects('revision_prompt');
    assert.deepEqual(allowed.map(row => row.finding_id), ['F1', 'F2']);
    const revisedText = russian.text + ' Это ограниченное утверждение.';
    proposal.changes = [{...proposal.changes[0], original_quote: russian.text,
      replacement_text: revisedText, finding_ids: allowed.map(row => row.finding_id),
      action_ids: allowed.map(row => row.action_id), reason: 'Apply the accepted scope clarification.',
      uncertainties: ['The statement still needs independent support.']}];
    await page.locator('#response').fill(JSON.stringify(proposal));
    await action('#submitResponse', 'hunk_review');
    await page.locator('#hreason-CH1').fill('Synthetic author checks the wording without claiming factual verification.');
    await action('.hunkDecision[data-id="CH1"][data-decision="edit"]', 'apply_revision');
    await action('#applyRevision', 'resolution_prepare');
    await action('#prepareResolution', 'resolution_result');
    const [resolution] = await promptObjects('resolution_prompt');
    resolution.results = [{finding_id: 'F1', proposed_status: 'unresolved',
      reason: 'Changed wording does not establish the missing evidence.', evidence_quotes: [revisedText],
      uncertainties: ['No independent source has been checked.']},
    {finding_id: 'F2', proposed_status: 'partially_resolved', reason: 'Scope improved, evidence remains open.',
      evidence_quotes: [revisedText], uncertainties: ['The factual basis remains unverified.']}];
    await page.locator('#response').fill(JSON.stringify(resolution));
    await action('#submitResponse', 'resolution_confirm');
    const saveResolution = id => page.locator(`.resolution[data-id="${id}"]`);
    const resolutionRequests = [];
    page.on('request', request => {
      if (request.method() === 'POST' && request.url().endsWith('/api/action') &&
          request.postDataJSON()?.action === 'decide_resolution') resolutionRequests.push(request.postDataJSON());
    });
    for (const id of ['F1', 'F2']) {
      assert.equal(await page.locator('#status-' + id).inputValue(), '');
      assert.equal(await saveResolution(id).isDisabled(), true);
    }
    await page.locator('#rreason-F1').fill('I checked the change; the evidence issue remains unresolved.');
    assert.equal(await saveResolution('F1').isDisabled(), true);
    await page.evaluate(() => document.querySelector('.resolution[data-id="F1"]').onclick());
    assert.equal(resolutionRequests.length, 0, 'Even a direct handler call must reject an unselected status');
    await language('en');
    assert.ok((await page.locator('#app').innerText()).includes('Choose a final status'));
    assert.ok((await page.locator('#app').innerText()).includes('No independent source has been checked.'));
    assert.equal(await page.locator('#status-F1').inputValue(), '');
    assert.equal(await saveResolution('F1').isDisabled(), true);
    await screenshot('resolution-explicit-choice-en');
    await page.locator('#status-F1').selectOption('unresolved');
    assert.equal(await saveResolution('F1').isEnabled(), true);
    await page.locator('#status-F1').selectOption('');
    assert.equal(await saveResolution('F1').isDisabled(), true);
    await page.locator('#status-F1').selectOption('unresolved');
    await action('.resolution[data-id="F1"]', 'resolution_confirm');
    await page.waitForFunction(() => state.selected.resolution_results[0].human_decision?.final_status === 'unresolved');
    const firstDecision = await page.evaluate(() => state.selected.resolution_results[0].human_decision);
    await page.reload();
    await page.locator('#status-F1').waitFor();
    assert.equal(await page.locator('#status-F1').inputValue(), 'unresolved');
    assert.equal(await page.locator('#rreason-F1').inputValue(), firstDecision.reason);
    assert.equal(await page.locator('#status-F2').inputValue(), '');
    assert.equal(await saveResolution('F2').isDisabled(), true);
    await page.locator('#status-F2').selectOption('partially_resolved');
    await page.locator('#rreason-F2').fill('Only the scope issue is resolved; the evidence remains unverified.');
    for (const locale of ['zh-Hant', 'en']) {
      await language(locale);
      assert.equal(await page.locator('#status-F1').inputValue(), 'unresolved');
      assert.equal(await page.locator('#rreason-F1').inputValue(), firstDecision.reason);
      assert.equal(await page.locator('#status-F2').inputValue(), 'partially_resolved');
      assert.equal(await saveResolution('F2').isEnabled(), true);
    }
    await screenshot('resolution-existing-decision-en');
    await action('.resolution[data-id="F2"]', 'export');
    await action('#export', 'complete');
    const exportPath = await page.evaluate(() => state.selected.export_path);
    const audit = JSON.parse(await fs.readFile(path.join(exportPath, 'audit.json'), 'utf8'));
    assert.deepEqual(audit.resolution_decisions.map(row => row.final_status), ['unresolved', 'partially_resolved']);
    assert.deepEqual(resolutionRequests.map(row => row.data.status), ['unresolved', 'partially_resolved']);
    const resolutionEvidence = {modelProposals: resolution.results.map(row => row.proposed_status),
      humanDecisions: audit.resolution_decisions.map(row => row.final_status),
      submittedDecisions: resolutionRequests.length, defaultStatus: '', exportComplete: true,
      v1Sha256: audit.v1_sha256, v2Sha256: audit.v2_sha256};

    await page.goto(endpoints.resume);
    await page.locator('#prepareAtomization').waitFor();
    await language('en');
    const resumed = page.waitForResponse(response => response.url().endsWith('/api/action'));
    await page.locator('#prepareAtomization').click();
    const resumedResponse = await resumed;
    assert.equal(resumedResponse.status(), 201, await resumedResponse.text());
    assert.equal(resumedResponse.request().postDataJSON().action, 'prepare_atomization');
    await page.locator('#response').waitFor();
    assert.equal(await page.evaluate(() => state.selected.stage), 'atomization_result');

    for (const sample of endpoints.samples) {
      await page.goto(sample.url);
      await page.locator('[data-select="C1"]').waitFor();
      const before = await page.evaluate(() => JSON.stringify(state));
      for (const locale of ['en', 'zh-Hant']) {
        await language(locale);
        assert.ok((await page.locator('#manuscript').innerText()).includes(sample.text));
        assert.ok((await page.locator('#claimDetail').innerText()).includes(sample.text));
        assert.equal(await page.evaluate(() => JSON.stringify(state)), before);
        assert.equal(await page.evaluate(() => selectedClaim), 'C1');
      }
    }
    await page.goto(endpoints.professional);
    await page.locator('[data-decide]').first().waitFor();
    assert.equal(await page.locator('.verdict-fail').count() > 0, true);
    await page.locator('[data-decide]').first().click();
    await page.locator('#decisionReason').fill(report);
    await page.locator('#actionText').fill(title);
    await page.locator('#actionType').selectOption('narrow_claim');
    await language('en', '#decisionDialog [data-research-language]');
    assert.equal(await page.locator('#decisionReason').inputValue(), report);
    assert.equal(await page.locator('#actionText').inputValue(), title);
    assert.equal(await page.locator('#actionType').inputValue(), 'narrow_claim');
    assert.equal(await page.locator('#saveDecision').innerText(), 'Save formal decision');
    assert.ok((await page.locator('#claimDetail').innerText()).includes('Descriptive empirical'));
    await screenshot('research-decision-en');
    await language('zh-Hant', '#decisionDialog [data-research-language]');
    assert.equal(await page.locator('#decisionReason').inputValue(), report);
    assert.equal(await page.locator('#actionText').inputValue(), title);
    await page.locator('#decisionDialog button[value="cancel"]').click();
    await page.locator('#historyButton').click();
    await language('en', '#historyDialog [data-research-language]');
    assert.ok((await page.locator('#historyDialog').innerText()).includes('Argument history'));
    assert.equal(await page.locator('#historyDialog').evaluate(node => node.open), true);
    await screenshot('research-history-en');
    assert.deepEqual(errors, []);
    const result = {passed: true, browser: browser.version(), resolutionEvidence, checks: ['raw File bytes and encoding submitted intact',
      'selected File and title survive both language changes', 'report draft remains unchanged',
      'eight languages keep original text and state in both locales',
      'professional status classes and labels remain valid', 'decision drafts and history dialog remain open',
      'interrupted report preparation resumes through its explicit API action',
      'default home exposes both writing tasks and its visible report entry reaches the complete revision flow',
      'real report flow archives invalid model output, repairs and applies only accepted findings',
      'unresolved and partially resolved model proposals require an explicit final human status',
      'saved human status and reason survive refresh and both interface languages',
      'export preserves unresolved and partial human decisions without silently resolving either issue']};
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
  } finally {
    if (browser) await browser.close();
    for (const child of [server, unifiedServer]) {
      child.kill();
      await new Promise(resolve => child.exitCode !== null || child.signalCode !== null ? resolve() : child.once('exit', resolve));
    }
    const cleanup = path.resolve(temporary);
    assert.equal(path.dirname(cleanup), path.resolve(os.tmpdir()));
    assert.ok(path.basename(cleanup).startsWith('research-ui-'));
    await fs.rm(cleanup, {recursive: true, force: true});
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
