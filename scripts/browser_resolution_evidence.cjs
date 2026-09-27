// Real persisted revisions and HTTP imports. Synthetic responses verify UI
// evidence boundaries and human controls, not model semantic accuracy.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const { spawn } = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

async function idle(page) {
  await page.waitForFunction(() => !mutationPending && !root.inert);
}
async function action(page, locator, name) {
  const [response] = await Promise.all([
    page.waitForResponse(response => response.url().endsWith('/api/action')
      && response.request().postDataJSON()?.action === name), locator.click(),
  ]);
  assert.equal(response.status(), 201, await response.text());
  await idle(page);
}
async function snapshot(page) {
  return page.evaluate(() => ({status: state.selected.revision_workspace.external_recheck,
    original: state.selected.extraction.blocks, findings: state.selected.findings}));
}
async function language(page, locale) {
  await page.locator('#ui-language').selectOption(locale);
  await page.waitForFunction(expected => document.documentElement.lang === expected, locale);
  await page.evaluate(() => preferenceWrites);
}
async function reimport(page, payload) {
  const form = page.locator('.external-recheck-reimport');
  if (!await form.evaluate(element => element.open)) await form.locator('summary').click();
  const critic = payload.critic;
  await page.locator(`#external-provider-${critic}`).fill('Browser fixture provider');
  await page.locator(`#external-model-${critic}`).fill('Browser fixture rechecker');
  await page.locator(`#external-response-${critic}`).fill(JSON.stringify(payload));
  await action(page, form.locator('.import-external-recheck'), 'import_external_recheck');
}
async function decide(page, findingId, state) {
  const card = page.locator(`.external-resolution-item[data-finding-id="${findingId}"]`);
  await card.locator('input').fill('Explicit browser-fixture human decision');
  await action(page, card.locator(`button[data-state="${state}"]`), 'decide_external_resolution');
}

async function main() {
  const repo = path.resolve(__dirname, '..');
  const output = path.join(repo, 'dist', 'browser-resolution-evidence');
  await fs.mkdir(output, {recursive: true});
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'resolution-evidence-browser-'));
  const child = spawn(process.env.STUDIO_PYTHON || 'python',
    [path.join(__dirname, 'browser_resolution_fixture.py'), temporary],
    {cwd: repo, windowsHide: true, env: {...process.env, PYTHONUTF8: '1'}});
  const report = {passed: false, checks: [], errors: [], externalRequests: []};
  let browser, page, stderr = '';
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
    await page.waitForFunction(() => state?.selected?.revision_workspace?.external_recheck?.requests?.[0]?.result);
    const workspace = page.locator('.external-recheck-workspace');
    for (const [locale, warning, importLabel] of [
      ['zh-Hant', /舊版結果：修訂稿引文未核驗/, /匯入另一份複審結果/],
      ['en', /Legacy result: revised-document quotations were not checked/, /Import another recheck result/],
    ]) {
      await language(page, locale);
      assert.match(await workspace.textContent(), warning);
      assert.match(await workspace.locator('.external-recheck-reimport summary').textContent(), importLabel);
      assert.equal(await workspace.locator('.external-recheck-reimport').evaluate(element => element.open), false);
      assert.equal(await workspace.locator('.external-human-decision').count(), 0);
      report.checks.push(`${locale}: persisted legacy result is explicitly unchecked, and reimport is available`);
    }
    let current = await snapshot(page);
    const request = current.status.requests[0];
    const binding = Object.fromEntries(['request_id', 'prompt_sha256', 'revision_id', 'revised_sha256', 'critic']
      .map(key => [key, request[key]]));
    // Resolve the revised text only from the actual protocol supplied to the model.
    const promptBlocks = JSON.parse(request.prompt.split('## Revised document blocks\n```json\n')[1].split('\n```')[0]);
    const revised = promptBlocks.find(block => block.text.includes('Revised owner: Ada'));
    assert.ok(revised);
    const original = current.original.find(block => block.block_id === revised.block_id);
    assert.ok(original);
    assert.notEqual(original.text, revised.text);
    const checkedId = current.findings.find(finding => finding.location.block_id === revised.block_id).finding_id;
    const unableId = request.original_finding_ids.find(id => id !== checkedId);
    const explanation = '<img src=x onerror="window.__resolutionInjected=true"> Explanation: 简体中文 Straße 日本語';
    const payload = {...binding, resolutions: [
      {finding_id: checkedId, state: 'still-present', reason: 'The model proposes checking acceptance.',
        evidence: explanation, source_evidence: [{block_id: revised.block_id, quote: revised.text}]},
      {finding_id: unableId, state: 'unable-to-assess', reason: 'No reliable conclusion from the available passages.',
        evidence: 'The removed passage cannot supply a current source anchor.', source_evidence: []},
    ], new_findings: []};
    await reimport(page, payload);
    current = await snapshot(page);
    assert.equal(current.status.complete, false);
    assert.ok(current.status.requests[0].items.every(item => item.human_decision === null));
    assert.equal(current.status.requests[0].items.find(item => item.finding_id === unableId).state, 'unable-to-assess');
    report.checks.push('HTTP reimport stores unable-to-assess without creating a human decision');

    for (const [locale, stateLabel, caveat, noEvidence] of [
      ['zh-Hant', /無法評估/, /問題是否解決仍需人工判斷/, /未提供可核對的修訂稿引文/],
      ['en', /Unable to assess/, /Whether the issue is resolved still requires human judgment/, /No checkable revised-document quotations/],
    ]) {
      await language(page, locale);
      const checkedCard = page.locator(`.external-resolution-item[data-finding-id="${checkedId}"]`);
      const unableCard = page.locator(`.external-resolution-item[data-finding-id="${unableId}"]`);
      assert.match(await unableCard.textContent(), stateLabel);
      assert.match(await unableCard.textContent(), noEvidence);
      assert.match(await checkedCard.textContent(), caveat);
      assert.equal(await checkedCard.locator('.external-evidence-explanation').textContent(), explanation);
      assert.equal(await checkedCard.locator('.external-source-evidence p').textContent(), revised.text);
      assert.equal(await workspace.locator('img').count(), 0);
      assert.equal(await page.evaluate(() => Boolean(window.__resolutionInjected)), false);
      assert.equal(await workspace.locator('.external-human-decision').count(), 0);
      assert.equal(await unableCard.locator('.external-resolution').count(), 3);
      const sourcePanel = workspace.locator('.external-revised-sources');
      assert.equal(await sourcePanel.evaluate(element => element.open), false);
      await page.locator('#workspace-search').fill('no-original-text-matches');
      const originalTarget = page.locator(`[id="source-${revised.block_id}"]`);
      assert.equal(await originalTarget.isVisible(), false);
      const link = checkedCard.locator('.external-source-evidence a');
      const fragment = await link.getAttribute('href');
      assert.ok(fragment.startsWith('#revised-source-'));
      await link.focus();
      await link.press('Enter');
      const target = page.locator(`[id="${fragment.slice(1)}"]`);
      assert.equal(new URL(page.url()).hash, fragment);
      assert.equal(await sourcePanel.evaluate(element => element.open), true);
      assert.equal(await target.isVisible(), true);
      assert.equal(await target.evaluate(element => document.activeElement === element), true);
      assert.ok((await target.textContent()).includes(revised.text));
      assert.ok((await originalTarget.textContent()).includes(original.text));
      assert.equal(await originalTarget.isVisible(), false);
      assert.equal(await page.locator('#workspace-search').inputValue(), 'no-original-text-matches');
      await page.screenshot({path: path.join(output, locale + '.png'), fullPage: true});
      report.checks.push(`${locale}: escaped untranslated quotations navigate to the revised text with the same block ID; original filtering stays separate`);
    }
    await decide(page, checkedId, 'resolved');
    await decide(page, unableId, 'unresolved');
    current = await snapshot(page);
    assert.ok(current.status.followup_blockers.includes(unableId));
    assert.equal(current.status.can_start_followup, false);
    assert.equal(await page.locator('#start-followup-round').count(), 0);
    assert.match(await workspace.locator('.external-followup-blocked').textContent(), /reliable source location/);
    report.checks.push('An explicitly unresolved item with no current anchor explains the blocked next round');
    payload.resolutions[1] = {...payload.resolutions[1], state: 'still-present',
      evidence: 'The revised owner passage still does not name an acceptance reviewer.',
      source_evidence: [{block_id: revised.block_id, quote: revised.text}]};
    await reimport(page, payload);
    current = await snapshot(page);
    assert.ok(current.status.requests[0].items.every(item => item.human_decision === null));
    assert.equal(current.status.can_start_followup, false);
    assert.equal(await workspace.locator('.external-human-decision').count(), 0);
    await decide(page, checkedId, 'resolved');
    await decide(page, unableId, 'unresolved');
    current = await snapshot(page);
    assert.deepEqual(current.status.followup_blockers, []);
    assert.equal(current.status.can_start_followup, true);
    assert.equal(await page.locator('#start-followup-round').isVisible(), true);
    report.checks.push('Replacing the incomplete recheck requires fresh human decisions before follow-up becomes available');
    assert.deepEqual(report.errors, []);
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
    assert.ok(path.basename(resolved).startsWith('resolution-evidence-browser-'));
    await fs.rm(resolved, {recursive: true, force: true});
  }
  console.log(JSON.stringify(report, null, 2));
}
main().catch(error => {console.error(error); process.exitCode = 1;});
