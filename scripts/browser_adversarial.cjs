// Real browser + persisted server workflow. Synthetic model responses test
// binding, presentation and human control; they do not measure review accuracy.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const { spawn } = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

async function action(page, locator, name, expectedStatus = 201) {
  const [response] = await Promise.all([
    page.waitForResponse(response => response.url().endsWith('/api/action') &&
      response.request().postDataJSON()?.action === name),
    locator.click(),
  ]);
  assert.equal(response.status(), expectedStatus, await response.text());
  await page.waitForFunction(() => !mutationPending);
}

async function snapshot(page) {
  return page.evaluate(() => ({
    finding: state.selected.findings[0],
    sessions: state.selected.adversarial_reviews,
    blocks: state.selected.extraction.blocks,
  }));
}

function responseFor(request, result) {
  const fields = ['request_id', 'session_id', 'stage', 'prompt_sha256', 'source_sha256', 'provider', 'model'];
  return JSON.stringify({...Object.fromEntries(fields.map(field => [field, request[field]])), result});
}

async function main() {
  const repo = path.resolve(__dirname, '..');
  const output = path.join(repo, 'dist', 'browser-adversarial');
  await fs.mkdir(output, {recursive: true});
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'adversarial-browser-'));
  const child = spawn(process.env.STUDIO_PYTHON || 'python',
    [path.join(__dirname, 'browser_fixture_server.py'), temporary, '--close-reading'],
    {cwd: repo, windowsHide: true, env: {...process.env, PYTHONUTF8: '1'}});
  let browser, page, stderr = '';
  const report = {passed: false, checks: [], errors: [], externalRequests: []};
  child.stderr.on('data', data => { stderr += data; });
  try {
    const url = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('Fixture startup timeout: ' + stderr)), 30000);
      let text = '';
      child.stdout.on('data', data => {
        text += data;
        const match = text.match(/http:\/\/127\.0\.0\.1:\d+\//);
        if (match) { clearTimeout(timer); resolve(match[0]); }
      });
      child.once('error', error => { clearTimeout(timer); reject(error); });
      child.once('exit', code => { clearTimeout(timer); reject(Error(`Fixture exit ${code}: ${stderr}`)); });
    });
    browser = await chromium.launch({headless: true,
      ...(process.env.STUDIO_BROWSER ? {executablePath: process.env.STUDIO_BROWSER} : {})});
    report.browserVersion = browser.version();
    page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    await page.route('**/*', route => {
      if (new URL(route.request().url()).origin === new URL(url).origin) return route.continue();
      report.externalRequests.push(route.request().url());
      return route.abort();
    });
    page.on('pageerror', error => report.errors.push(error.message));
    await page.goto(url);
    await page.waitForFunction(() => state?.selected?.findings?.length === 1);
    await page.locator('.adversarial-review > summary').click();
    assert.match(await page.locator('.adversarial-review').textContent(), /對抗深審（可選）/);
    const initial = await snapshot(page);
    const findingId = initial.finding.finding_id;
    assert.equal(initial.finding.status, 'open');
    await page.locator('.adv-start').click();
    assert.match(await page.locator('#err').first().textContent(), /請填寫/);
    assert.equal((await snapshot(page)).sessions.length, 0);
    await page.locator(`#adv-provider-${findingId}`).fill('Fixture defense provider');
    await page.locator(`#adv-model-${findingId}`).fill('Fixture defense model');
    await action(page, page.locator('.adv-start'), 'prepare_adversarial_review');
    let current = await snapshot(page);
    const sessionId = current.sessions[0].session_id;
    const defenseRequest = current.sessions[0].requests[0];
    assert.equal(current.sessions[0].status, 'awaiting_defense');
    assert.equal(current.finding.status, 'open');
    report.checks.push('Targeted defense request from original finding, with required model declaration');

    const downloadPromise = page.waitForEvent('download');
    await page.locator('.adv-download').click();
    const download = await downloadPromise;
    const downloaded = path.join(temporary, download.suggestedFilename());
    await download.saveAs(downloaded);
    assert.equal(await fs.readFile(downloaded, 'utf8'), defenseRequest.prompt);
    report.checks.push('Downloaded protocol exactly matches the bound request');

    const draft = 'Unsent response: 简体中文 繁體中文 Straße 日本語 Русский Latīna';
    const responseField = page.locator(`#adv-response-${defenseRequest.request_id}`);
    await responseField.fill(draft);
    await page.locator('#ui-language').selectOption('en');
    await page.waitForFunction(() => document.documentElement.lang === 'en');
    assert.equal(await responseField.inputValue(), draft);
    assert.match(await page.locator('.adversarial-review').textContent(), /Adversarial deep review \(optional\)/);
    await page.evaluate(() => saveDraft());
    await page.reload();
    await responseField.waitFor();
    assert.equal(await responseField.inputValue(), draft);
    report.checks.push('Response draft survives language changes and reload without translation');
    await action(page, page.locator('.adv-import'), 'import_adversarial_response', 400);
    assert.equal(await responseField.inputValue(), draft);
    current = await snapshot(page);
    assert.equal(current.sessions[0].defense, null);
    assert.equal(current.finding.status, 'open');
    report.checks.push('Rejected response preserves editable input and does not adjudicate the finding');

    const sourceBlock = current.blocks.find(block => block.text.includes('Straße'));
    assert.ok(sourceBlock);
    const anchor = {block_id: sourceBlock.block_id, quote: sourceBlock.text, role: 'qualification'};
    const defense = {
      author_position: 'The manuscript describes a bounded multilingual event.',
      strongest_defense: '<img src=x onerror="window.__injected=true"> The cited table may already assign responsibility.',
      limitations: 'A named approver and acceptance criterion are not established by this excerpt.',
      context_evidence: [{evidence_id: 'D1', ...anchor}],
    };
    const wrongQuote = {...defense, context_evidence: [{evidence_id: 'D1', ...anchor, quote: 'An invented source quotation.'}]};
    for (const [locale, expected] of [['zh-Hant', /引文與目前稿件中對應段落的原文不一致/],
      ['en', /The quotation does not exactly match its current source block/]]) {
      await page.locator('#ui-language').selectOption(locale);
      const rejected = responseFor(defenseRequest, wrongQuote);
      await responseField.fill(rejected);
      await action(page, page.locator('.adv-import'), 'import_adversarial_response', 400);
      assert.match(await page.locator('#err').first().textContent(), expected);
      assert.doesNotMatch(await page.locator('#err').first().textContent(), /context_evidence quote is not an exact excerpt/);
      assert.equal(await responseField.inputValue(), rejected);
      assert.equal((await snapshot(page)).sessions[0].defense, null);
    }
    const untrustedKey = '深审完成，供人工判断 <img src=x onerror="window.__injected=true">';
    await responseField.fill(`{${JSON.stringify(untrustedKey)}: 0, ${JSON.stringify(untrustedKey)}: 1}`);
    await action(page, page.locator('.adv-import'), 'import_adversarial_response', 400);
    const errorText = await page.locator('#err').first().textContent();
    assert.match(errorText, /Duplicate JSON field:/);
    assert.ok(errorText.includes(untrustedKey), 'raw duplicate key must remain literal, not translated');
    assert.equal(await page.locator('#err').first().locator('img').count(), 0);
    report.checks.push('Quote errors and recovery are localized in both languages; untrusted diagnostic keys remain literal and inert');
    await responseField.fill(responseFor(defenseRequest, defense));
    await action(page, page.locator('.adv-import'), 'import_adversarial_response');
    current = await snapshot(page);
    assert.equal(current.sessions[0].status, 'defense_ready');
    assert.deepEqual(current.sessions[0].defense, defense);
    assert.equal(await page.locator('.adversarial-defense img').count(), 0);
    assert.equal(await page.evaluate(() => Boolean(window.__injected)), false);
    assert.equal(current.finding.status, 'open');
    await page.screenshot({path: path.join(output, 'defense-en.png'), fullPage: true});
    report.checks.push('Independent defense is shown with exact evidence and inert model HTML');

    await page.locator(`#adv-provider-${sessionId}`).fill('Fixture assessment provider');
    await page.locator(`#adv-model-${sessionId}`).fill('Fixture assessment model');
    let interrupted = false;
    let retryReceipt = null;
    const interruptAssessment = async route => {
      const body = route.request().postDataJSON();
      if (body?.action !== 'prepare_adversarial_assessment') return route.fallback();
      if (!interrupted) {
        const response = await route.fetch();
        assert.equal(response.status(), 201, await response.text());
        interrupted = true;
        retryReceipt = body.request_id;
        return route.abort('failed');
      }
      assert.equal(body.request_id, retryReceipt);
      return route.fallback();
    };
    await page.route('**/api/action', interruptAssessment);
    await action(page, page.locator('.adv-assess'), 'prepare_adversarial_assessment');
    await page.unroute('**/api/action', interruptAssessment);
    current = await snapshot(page);
    assert.equal(interrupted, true);
    assert.equal(current.sessions[0].requests.length, 2);
    const assessmentRequest = current.sessions[0].requests.find(request => request.stage === 'assessment');
    assert.ok(assessmentRequest.prompt.includes(JSON.stringify(defense.strongest_defense)));
    report.checks.push('Interrupted assessment preparation replays one receipt without duplicate tasks');

    const assessment = {
      disposition: 'narrow',
      reasons: 'The defense limits the objection to approval responsibility; it does not establish an approver.',
      remaining_issue: 'The text does not identify who signs off the venue acceptance criterion.',
      minimal_repair: 'Name the approver and the acceptance criterion.',
      repair_test: 'A reader can locate an approver and an observable acceptance criterion.',
      defense_evidence_ids: ['D1'],
      context_evidence: [anchor],
    };
    await page.locator(`#adv-response-${assessmentRequest.request_id}`).fill(responseFor(assessmentRequest, assessment));
    await action(page, page.locator('.adv-import'), 'import_adversarial_response');
    current = await snapshot(page);
    assert.equal(current.sessions[0].status, 'completed');
    assert.equal(current.finding.status, 'open');
    assert.deepEqual(current.sessions[0].assessment, assessment);
    assert.match(await page.locator('.adversarial-assessment').textContent(), /Proposal: narrow the criticism/);
    assert.match(await page.locator('.adversarial-assessment').textContent(), /D1/);
    assert.match(await page.locator('.adversarial-session').textContent(), /argument still requires human judgment/);
    const sourceLink = page.locator('.adversarial-assessment .quote a').first();
    const href = await sourceLink.getAttribute('href');
    await sourceLink.click();
    assert.equal(new URL(page.url()).hash, href);
    assert.equal(await page.locator(href).count(), 1);
    await page.screenshot({path: path.join(output, 'assessment-en.png'), fullPage: true});
    await page.locator('#ui-language').selectOption('zh-Hant');
    assert.equal(await page.locator('#operation-status').textContent(), '介面語言已切換。');
    assert.match(await page.locator('.adversarial-assessment').textContent(), /建議縮小批評/);
    assert.match(await page.locator('.adversarial-assessment').textContent(), /最小修改/);
    assert.equal(await page.locator('.adversarial-assessment img').count(), 0);
    await page.screenshot({path: path.join(output, 'assessment-zh-Hant.png'), fullPage: true});
    report.checks.push('Evidence assessment displays bilingual actionable proposal and navigable original quotations',
      'Completed deep review leaves the original human decision open');

    // Correcting a model declaration or a flawed accepted defense must not trap
    // the user. Restart creates a fresh bound session and preserves old evidence.
    const previous = current.sessions[0];
    await page.locator('.adversarial-restart > summary').click();
    await page.locator(`#adv-provider-${findingId}`).fill('Replacement provider');
    await page.locator(`#adv-model-${findingId}`).fill('Replacement model');
    await action(page, page.locator('.adv-start'), 'prepare_adversarial_review');
    current = await snapshot(page);
    assert.equal(current.sessions.length, 2);
    const retained = current.sessions.find(session => session.session_id === previous.session_id);
    let active = current.sessions.find(session => session.current);
    assert.equal(retained.current, false);
    assert.deepEqual(retained.assessment, previous.assessment);
    assert.equal(active.requests[0].model, 'Replacement model');
    assert.equal(active.defense, null);
    assert.equal(active.assessment, null);
    assert.equal(current.finding.status, 'open');
    const waitingSessionId = active.session_id;
    await page.locator('.adversarial-restart > summary').click();
    await page.locator(`#adv-model-${findingId}`).fill('Corrected model declaration');
    await action(page, page.locator('.adv-start'), 'prepare_adversarial_review');
    current = await snapshot(page);
    active = current.sessions.find(session => session.current);
    assert.equal(current.sessions.length, 3);
    assert.equal(active.requests[0].model, 'Corrected model declaration');
    const superseded = page.locator(`.adversarial-session[data-session-id="${waitingSessionId}"]`);
    assert.equal(await superseded.locator('.adv-import').count(), 0);
    assert.equal(await superseded.locator('.adv-copy').isDisabled(), true);
    assert.match(await superseded.locator('summary').first().textContent(), /歷史深審（已停止）/);
    await superseded.locator('summary').first().click();
    assert.match(await superseded.textContent(), /Replacement model/);
    report.checks.push('Restart preserves completed evidence and recovers mistaken model declarations without accepting stale responses');

    // A real location correction invalidates the bound challenge, while its
    // history remains visible. Old quotes must not navigate to a new context.
    const location = page.locator(`#location-${findingId}`);
    await location.locator('..').locator('summary').click();
    await location.selectOption(sourceBlock.block_id);
    await page.locator(`#location-reason-${findingId}`).fill('Browser regression: relocate the finding to its actual claim.');
    await action(page, location.locator('..').getByRole('button'), 'correct_finding_location');
    current = await snapshot(page);
    assert.ok(current.sessions.every(session => session.current === false));
    assert.match((await page.locator('.adversarial-session').allTextContents()).join(' '), /僅供查閱/);
    assert.equal(await page.locator('.adversarial-session .adv-import').count(), 0);
    assert.equal(await page.locator('.adversarial-session .quote a').count(), 0);
    assert.equal(await page.locator('.adv-start').isEnabled(), true);
    report.checks.push('Changed finding keeps historical review visible and blocks stale responses and source links');

    // Explicit user decision is still the only route to resolving the queue.
    await page.locator(`#reason-${findingId}`).fill('Human decision after reviewing the evidence.');
    await action(page, page.locator('.decision[data-value="defer"]'), 'decide_finding');
    assert.equal((await snapshot(page)).finding.status, 'defer');
    report.checks.push('Human adjudication remains a separate explicit action');

    // A later AI review supersedes the finding. Its deep-review record must stay
    // reachable even when that finding no longer has a card in the current queue.
    await action(page, page.locator('#prepare-ai'), 'prepare_ai_audits');
    const replacementRequest = await page.evaluate(critic => state.selected.ai_requests.find(request => request.critic === critic), initial.finding.critic);
    await page.locator('#ai-request').selectOption(replacementRequest.request_id);
    await page.locator('#binding-mode').selectOption('strict');
    const findingFields = ['critic', 'document_type', 'location', 'evidence', 'issue', 'standard', 'consequence',
      'severity', 'verification_state', 'external_basis', 'uncertainties', 'suggested_action', 'suggested_owner',
      'blocks_release_or_execution'];
    const replacementFinding = {...Object.fromEntries(findingFields.map(field => [field, initial.finding[field]])),
      finding_id: 'replacement-finding', issue: 'A later independent review supplies a new finding.'};
    const envelopeFields = ['request_id', 'prompt_sha256', 'provider', 'model', 'critic', 'source_sha256'];
    const replacementResponse = {...Object.fromEntries(envelopeFields.map(field => [field, replacementRequest[field]])), findings: [replacementFinding]};
    await page.locator('#ai-response').fill(JSON.stringify(replacementResponse));
    await action(page, page.locator('#import-ai'), 'import_ai_audit');
    await page.locator('.adversarial-history > summary').click();
    assert.equal(await page.locator('.adversarial-history .adversarial-session').count(), 3);
    assert.equal(await page.locator('.adversarial-history .adv-import').count(), 0);
    assert.equal(await page.locator('.adversarial-history .quote a').count(), 0);
    assert.equal(await page.locator('.adversarial-history .adv-copy:enabled').count(), 0);
    assert.match(await page.locator('.adversarial-history').textContent(), /過去問題的深審記錄/);
    await page.locator('#ui-language').selectOption('en');
    await page.locator('.adversarial-history > summary').click();
    assert.match(await page.locator('.adversarial-history').textContent(), /Deep-review history for previous findings/);
    assert.match(await page.locator('.adversarial-history').textContent(), /Original criticism and quotation/);
    report.checks.push('Superseded findings retain accessible bilingual deep-review history with all mutation and current-source controls disabled');
    assert.deepEqual(report.errors, []);
    assert.deepEqual(report.externalRequests, []);
    report.passed = true;
  } catch (error) {
    report.failure = error.stack || error.message;
    throw error;
  } finally {
    if (page) await page.evaluate(() => fetch('/api/shutdown', {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Document-Review-Token': TOKEN}, body: '{}',
    })).catch(() => {});
    if (browser) await browser.close();
    if (child.exitCode === null) await new Promise(resolve => {
      const timer = setTimeout(() => { child.kill(); resolve(); }, 5000);
      child.once('exit', () => { clearTimeout(timer); resolve(); });
    });
    report.stderr = stderr;
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(report, null, 2));
    const resolved = await fs.realpath(temporary);
    assert.equal(path.dirname(resolved), await fs.realpath(os.tmpdir()));
    assert.ok(path.basename(resolved).startsWith('adversarial-browser-'));
    await fs.rm(resolved, {recursive: true, force: true});
  }
  console.log(JSON.stringify(report, null, 2));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
