// Isolated browser regressions for draft durability and uncertain HTTP results.
// No model/provider calls are made; every application request stays on loopback.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const { spawn } = require('node:child_process');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function within(promise, label, timeout = 30000) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(Error(label + ' timed out')), timeout);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

async function waitIdle(page) {
  await page.waitForFunction(() => !mutationPending);
}

async function action(page, locator, name) {
  const [response] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/action') && r.request().postDataJSON()?.action === name),
    locator.click(),
  ]);
  assert.equal(response.status(), 201, await response.text());
  await waitIdle(page);
}

async function save(page, status = 200) {
  const [response] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/draft') && r.request().method() === 'POST'),
    page.locator('#retry-draft').click(),
  ]);
  assert.equal(response.status(), status, await response.text());
  await page.waitForFunction(() => draftContext && draftContext.pending === 0);
  if (status === 200) {
    await page.waitForFunction(() => draftContext.sequence === draftContext.savedSequence && !draftContext.error);
  }
  return response.json();
}

async function assertLeaveProtection(page, expected) {
  assert.equal(await page.evaluate(() => {
    const event = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  }), expected, 'beforeunload must protect every pending/unsaved write');
}

async function jsonFiles(directory) {
  try {
    return (await fs.readdir(directory)).filter((name) => name.endsWith('.json'));
  } catch (error) {
    if (error.code === 'ENOENT') return [];
    throw error;
  }
}

async function main() {
  const root = path.resolve(__dirname, '..');
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'studio-browser-state-'));
  const server = spawn(process.env.STUDIO_PYTHON || 'python', [
    path.join(__dirname, 'browser_fixture_server.py'), path.join(temp, 'library'),
  ], { cwd: root, windowsHide: true, env: { ...process.env, PYTHONUTF8: '1' } });
  let browser;
  let stderr = '';
  let releaseHeldDraft;
  let releaseStartupState;
  const errors = [];
  const externalRequests = [];
  const evidence = {};
  server.stderr.on('data', (chunk) => { stderr += chunk.toString(); });
  try {
    const url = await within(new Promise((resolve, reject) => {
      let output = '';
      server.stdout.on('data', (chunk) => {
        output += chunk;
        const match = output.match(/http:\/\/127\.0\.0\.1:\d+\//);
        if (match) resolve(match[0]);
      });
      server.once('error', reject);
      server.once('exit', (code) => reject(Error('Server exited ' + code + ': ' + stderr)));
    }), 'Fixture startup');
    browser = await chromium.launch({
      headless: true,
      ...(process.env.STUDIO_BROWSER ? { executablePath: process.env.STUDIO_BROWSER } : {}),
    });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.route('**/*', (route) => {
      if (new URL(route.request().url()).origin === new URL(url).origin) return route.continue();
      externalRequests.push(route.request().url());
      return route.abort('blockedbyclient');
    });
    const page = await context.newPage();
    page.setDefaultTimeout(30000);
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto(url);
    await page.locator('#file').waitFor();
    await page.locator('#ui-language').selectOption('zh-Hant');
    await page.evaluate(() => preferenceWrites);
    const startupGate = deferred(), startupReady = deferred();
    releaseStartupState = startupGate.resolve;
    await page.route('**/api/state', async route => {
      const response = await route.fetch();
      startupReady.resolve();
      await startupGate.promise;
      await route.fulfill({response});
    }, {times: 1});
    await page.reload();
    await within(startupReady.promise, 'Initial state response');
    await page.locator('#ui-language').selectOption('en');
    await page.evaluate(() => preferenceWrites);
    assert.equal(await page.evaluate(() => state), null);
    assert.equal(await page.evaluate(() => document.documentElement.lang), 'en');
    startupGate.resolve();
    await page.locator('#file').waitFor();
    assert.equal(await page.evaluate(() => uiLocale), 'en', 'late startup state must not undo the explicit language choice');
    evidence.startupLanguageChoicePreserved = true;
    await page.locator('#ui-language').selectOption('zh-Hant');
    await page.evaluate(() => preferenceWrites);
    await page.locator('#file').setInputFiles({
      name: '草稿故障验收.md', mimeType: 'text/markdown',
      buffer: Buffer.from('# 活动方案\n\n相关人员应当及时完成报名。\n\n活动目的：说明报名的安排。\n'),
    });
    await page.locator('#upload').click();
    await page.locator('[data-action="confirm-extraction"]').waitFor();
    await action(page, page.locator('[data-action="confirm-extraction"]'), 'confirm_extraction');
    for (const [id, value] of Object.entries({ jurisdiction: 'unknown', effective_date: 'unknown', publisher_type: '作者', audience: '参与者' })) {
      await page.locator('#' + id).fill(value);
    }
    await action(page, page.locator('#confirm-context'), 'confirm_context');
    await action(page, page.locator('#prepare-ai'), 'prepare_ai_audits');
    const requests = await page.locator('#ai-request option').evaluateAll((nodes) => nodes.map((node) => node.value));
    assert.ok(requests.length >= 2, 'fixture needs two independent AI tasks');
    const [taskA, taskB] = requests;

    // Responses and binding choices belong to their selected task across both
    // selector changes and the Previous/Next navigation buttons.
    await page.locator('#ai-request').selectOption(taskA);
    await page.locator('#ai-response').fill('Task A: unsent review response');
    await page.locator('#binding-mode').selectOption('strict');
    await save(page);
    await page.locator('#next-request').click();
    assert.equal(await page.locator('#ai-request').inputValue(), taskB);
    assert.equal(await page.locator('#ai-response').inputValue(), '');
    assert.equal(await page.locator('#binding-mode').inputValue(), 'manual_association');
    await page.locator('#ai-response').fill('Task B: different unsent review response');
    await save(page);
    await page.locator('#previous-request').click();
    assert.equal(await page.locator('#ai-response').inputValue(), 'Task A: unsent review response');
    assert.equal(await page.locator('#binding-mode').inputValue(), 'strict');
    await page.locator('#ai-request').selectOption(taskB);
    await save(page);
    await assertLeaveProtection(page, false);
    await page.reload();
    await page.locator('#ai-response').waitFor();
    assert.equal(await page.locator('#ai-request').inputValue(), taskB);
    assert.equal(await page.locator('#ai-response').inputValue(), 'Task B: different unsent review response');
    await page.locator('#ai-request').selectOption(taskA);
    assert.equal(await page.locator('#ai-response').inputValue(), 'Task A: unsent review response');
    await save(page);
    evidence.taskDraftIsolationAndReload = true;

    // The server commits a save, but its response is deliberately held. The
    // debounce timer has ended; an in-flight write must still protect reload.
    const held = deferred();
    const release = deferred();
    releaseHeldDraft = release.resolve;
    let heldOnce = false;
    const delayDraft = async (route) => {
      if (heldOnce) return route.continue();
      heldOnce = true;
      const response = await route.fetch();
      assert.equal(response.status(), 200);
      held.resolve();
      await release.promise;
      await route.fulfill({ response });
    };
    await page.route('**/api/draft', delayDraft);
    await page.locator('#ai-response').fill('Task A: delayed acknowledgement');
    await within(held.promise, 'Held draft response');
    assert.ok(await page.evaluate(() => draftContext.pending > 0));
    await assertLeaveProtection(page, true);
    // Exercise the actual browser navigation prompt and cancel the navigation.
    const dialogPromise = page.waitForEvent('dialog', { predicate: (dialog) => dialog.type() === 'beforeunload', timeout: 10000 });
    const reloadAttempt = page.reload({ timeout: 15000 }).then(() => true, () => false);
    const dialog = await dialogPromise;
    await dialog.dismiss();
    assert.equal(await reloadAttempt, false, 'unsaved reload must be cancellable');
    assert.equal(await page.locator('#ai-response').inputValue(), 'Task A: delayed acknowledgement');
    release.resolve();
    releaseHeldDraft = null;
    await page.waitForFunction(() => draftContext.pending === 0 && draftContext.sequence === draftContext.savedSequence);
    await page.unroute('**/api/draft', delayDraft);
    evidence.inflightSaveProtectsActualReload = true;

    // Failed persistence keeps visible input dirty and leaves the committed
    // version intact. An explicit retry can later save that exact input.
    const failDraft = (route) => route.fulfill({
      status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'Injected draft save failure' }),
    });
    await page.route('**/api/draft', failDraft);
    await page.locator('#ai-response').fill('Task A: must survive save failure');
    await save(page, 503);
    assert.equal(await page.locator('#ai-response').inputValue(), 'Task A: must survive save failure');
    assert.match(await page.locator('#draft-status').innerText(), /Injected draft save failure/);
    await assertLeaveProtection(page, true);
    const savedAfterFailure = await page.evaluate(() => api('/api/state'));
    assert.equal(savedAfterFailure.selected.ui_draft.fields['ai-response::' + taskA], 'Task A: delayed acknowledgement');
    await page.unroute('**/api/draft', failDraft);
    await save(page);
    evidence.saveFailureRetainsInputAndRetry = true;

    // A second tab loads the same revision. Once the first tab saves a newer
    // version, the stale tab receives 409 and cannot overwrite that version.
    const stale = await context.newPage();
    stale.setDefaultTimeout(30000);
    stale.on('pageerror', (error) => errors.push(error.message));
    await stale.goto(url);
    await stale.locator('#ai-response').waitFor();
    const staleRevision = await stale.evaluate(() => draftContext.revision);
    assert.equal(staleRevision, await page.evaluate(() => draftContext.revision));
    await page.locator('#ai-response').fill('Task A: authoritative newer tab');
    const newSave = await save(page);
    assert.ok(newSave.revision > staleRevision);
    await stale.locator('#ai-response').fill('Task A: stale conflicting tab');
    await save(stale, 409);
    assert.equal(await stale.locator('#ai-response').inputValue(), 'Task A: stale conflicting tab');
    for (const [locale, expected] of [['zh-Hant', '另一個頁面已儲存'], ['en', 'Another page saved']]) {
      await stale.locator('#ui-language').selectOption(locale);
      assert.ok((await stale.locator('#draft-status').innerText()).includes(expected));
      assert.equal(await stale.locator('#ai-response').inputValue(), 'Task A: stale conflicting tab');
    }
    await assertLeaveProtection(stale, true);
    const current = await page.evaluate(() => api('/api/state'));
    assert.equal(current.selected.ui_draft.revision, newSave.revision);
    assert.equal(current.selected.ui_draft.fields['ai-response::' + taskA], 'Task A: authoritative newer tab');
    await stale.close();
    evidence.staleTabRejectedWith409 = true;

    // Commit the first mutation, then abort its response. The controller must
    // replay its receipt ID, producing one audit run and one receipt on disk.
    const projectDirectory = current.selected.directory;
    const projectRoot = path.join(temp, 'library', projectDirectory);
    const auditDirectory = path.join(projectRoot, 'audits', 'expression_ambiguity');
    const receiptDirectory = path.join(projectRoot, '.requests');
    const auditsBefore = await jsonFiles(auditDirectory);
    const receiptsBefore = await jsonFiles(receiptDirectory);
    const requestIds = [];
    let firstCommitted = false;
    const loseResponse = async (route) => {
      const payload = route.request().postDataJSON();
      if (payload?.action !== 'run_local_prechecks') return route.continue();
      requestIds.push(payload.request_id);
      if (requestIds.length === 1) {
        const response = await route.fetch();
        assert.equal(response.status(), 201, await response.text());
        firstCommitted = true;
        return route.abort('failed');
      }
      return route.continue();
    };
    await page.route('**/api/action', loseResponse);
    await page.locator('.critic').evaluateAll((nodes) => nodes.forEach((node) => { node.checked = node.value === 'expression_ambiguity'; }));
    await action(page, page.locator('#run-precheck'), 'run_local_prechecks');
    await page.unroute('**/api/action', loseResponse);
    assert.equal(firstCommitted, true);
    assert.equal(requestIds.length, 2, 'controller must retry exactly once');
    assert.equal(requestIds[0], requestIds[1], 'retry must reuse the committed request ID');
    assert.equal((await jsonFiles(auditDirectory)).length, auditsBefore.length + 1);
    assert.equal((await jsonFiles(receiptDirectory)).length, receiptsBefore.length + 1);
    assert.ok(await fs.stat(path.join(receiptDirectory, requestIds[0] + '.json')));
    assert.ok(await page.locator('.finding-card').count() > 0);
    evidence.committedResponseLossHasSingleResult = true;
    evidence.replayedRequestId = requestIds[0];
    const output = process.env.STUDIO_STATE_E2E_OUTPUT || path.join(root, 'dist', 'browser-state-verification');
    await fs.mkdir(output, { recursive: true });

    // Use a shared, already saved note so draft-version conflicts cannot mask
    // an independently stale review decision. DOM activation avoids scrolling
    // and changing the draft's saved scroll position before submission.
    const reason = page.locator('input[id^="reason-"]').first();
    const reasonId = await reason.getAttribute('id');
    const findingId = reasonId.slice('reason-'.length);
    const note = 'Preserve this decision note. 日本語 Русский 繁體';
    await reason.fill(note);
    await save(page);
    for (const [locale, decision] of [['zh-Hant', 'reject'], ['en', 'accept']]) {
      const currentTab = await context.newPage(), oldTab = await context.newPage();
      for (const tab of [currentTab, oldTab]) {
        tab.on('pageerror', error => errors.push(error.message));
        tab.on('dialog', dialog => dialog.dismiss());
        await tab.goto(url);
        await tab.locator('#ui-language').selectOption(locale);
        await tab.locator('#' + reasonId).waitFor();
        await tab.evaluate(() => new Promise(requestAnimationFrame));
      }
      const shown = await oldTab.evaluate(() => ({context: state.review_context,
        revision: draftContext.revision, fields: draftContext.fields}));
      assert.equal(await currentTab.evaluate(() => state.review_context), shown.context);
      assert.equal(await oldTab.locator('#' + reasonId).inputValue(), note);
      let draftPosts = 0;
      for (const tab of [currentTab, oldTab]) tab.on('request', request => {
        if (request.url().endsWith('/api/draft') && request.method() === 'POST') draftPosts++;
      });
      const choose = (tab, value) => tab.locator(`.decision[data-id="${findingId}"][data-value="${value}"]`).evaluate(node => node.click());
      let response = currentTab.waitForResponse(r => r.url().endsWith('/api/action'));
      await choose(currentTab, decision);
      assert.equal((await response).status(), 201);
      await waitIdle(currentTab);
      response = oldTab.waitForResponse(r => r.url().endsWith('/api/action'));
      await choose(oldTab, decision === 'accept' ? 'reject' : 'accept');
      assert.equal((await response).status(), 409);
      await waitIdle(oldTab);
      assert.equal(draftPosts, 0, 'review protection must not depend on a conflicting draft write');
      assert.equal(await oldTab.evaluate(() => state.review_context), shown.context);
      assert.equal(await oldTab.locator('#' + reasonId).inputValue(), note);
      const status = await oldTab.locator('#operation-status').innerText();
      assert.ok(status.includes(locale === 'en' ? 'The review state has changed' : '審查狀態已變更'));
      const currentState = await currentTab.evaluate(() => api('/api/state'));
      assert.equal(currentState.selected.findings.find(row => row.finding_id === findingId).status, decision);
      assert.equal(currentState.selected.ui_draft.revision, shown.revision);
      await oldTab.screenshot({path: path.join(output, `stale-review-${locale}.png`), fullPage: true});
      await oldTab.reload();
      await oldTab.locator('#' + reasonId).waitFor();
      assert.equal(await oldTab.locator('#' + reasonId).inputValue(), note);
      assert.notEqual(await oldTab.evaluate(() => state.review_context), shown.context);
      await currentTab.close();
      await oldTab.close();
    }
    evidence.staleReviewRejectedWithoutDraftConflict = true;
    assert.deepEqual(errors, []);
    assert.deepEqual(externalRequests, []);
    await page.screenshot({ path: path.join(output, 'completed.png'), fullPage: true });
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify({ passed: true, evidence, browserErrors: errors, externalRequests }, null, 2));
    console.log('Browser state regressions passed: task drafts, reload protection, save failure, stale draft/review tabs, and committed-response replay.');
  } catch (error) {
    if (stderr) console.error(stderr);
    throw error;
  } finally {
    releaseStartupState?.();
    releaseHeldDraft?.();
    if (browser) await browser.close();
    server.kill();
    if (server.exitCode === null && server.signalCode === null) await within(new Promise((resolve) => server.once('exit', resolve)), 'Fixture shutdown');
    // Verify the computed recursive-cleanup target remains in the owned temp
    // directory, on Windows as well as POSIX.
    const relative = path.relative(path.resolve(os.tmpdir()), path.resolve(temp));
    assert.ok(relative && !relative.startsWith('..') && !path.isAbsolute(relative) && path.basename(temp).startsWith('studio-browser-state-'));
    await fs.rm(temp, { recursive: true, force: true });
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
