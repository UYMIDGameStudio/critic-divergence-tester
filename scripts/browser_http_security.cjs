// Exercise real project/task conflicts and pending input in isolated Chrome tabs.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');

async function main() {
  const root = path.resolve(__dirname, '..');
  const output = path.resolve(process.env.HTTP_UI_OUTPUT || path.join(root, 'dist', 'http-protocol-security', 'browser'));
  await fs.mkdir(output, {recursive: true});
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'http-ui-'));
  const child = spawn(process.env.STUDIO_PYTHON || 'python', [path.join(__dirname, 'research_fixture_server.py'), temporary],
    {cwd: root, windowsHide: true, env: {...process.env, PYTHONUTF8: '1'}});
  let browser, stderr = '';
  child.stderr.on('data', value => {stderr += value.toString();});
  try {
    const endpoints = await new Promise((resolve, reject) => {
      let stdout = '';
      const timer = setTimeout(() => reject(Error('Fixture startup timed out: ' + stderr)), 30000);
      child.stdout.on('data', value => {
        stdout += value.toString();
        if (stdout.includes('\n')) {clearTimeout(timer); resolve(JSON.parse(stdout.split('\n')[0]));}
      });
      child.once('exit', code => {clearTimeout(timer); reject(Error('Fixture exited ' + code + ': ' + stderr));});
    });
    browser = await chromium.launch({headless: true, ...(process.env.STUDIO_BROWSER ? {executablePath: process.env.STUDIO_BROWSER} : {})});
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
    const errors = [];
    context.on('page', page => page.on('pageerror', error => errors.push(error.message)));
    const a = await context.newPage(), b = await context.newPage();
    await Promise.all([a.goto(endpoints.product), b.goto(endpoints.product)]);
    const create = async (page, name) => {
      await page.locator('#file').setInputFiles({name: name + '.txt', mimeType: 'text/plain', buffer: Buffer.from('The ' + name + ' claim needs evidence.')});
      await page.locator('#title').fill(name);
      await page.locator('#create').click();
      await page.locator('#report').waitFor();
    };
    await create(a, 'Project A');
    const originalContext = await a.evaluate(() => state.request_context);
    const draft = 'Only Project A. Rātiō 日本語 русский 繁體';
    await a.locator('#report').fill(draft);
    await create(b, 'Project B');
    const conflict = a.waitForResponse(r => r.url().endsWith('/api/action'));
    await a.locator('#importReport').click();
    assert.equal((await conflict).status(), 409);
    await a.waitForFunction(() => !document.body.inert);
    assert.equal(await a.locator('#report').inputValue(), draft);
    assert.equal(await a.evaluate(() => state.selected.title), 'Project A');
    assert.equal(await b.evaluate(() => state.selected.stage), 'review_material');
    assert.equal(await a.evaluate(() => state.request_context), originalContext);
    await a.screenshot({path: path.join(output, 'old-project-draft-conflict.png'), fullPage: true});

    // Pause a real request before it reaches the server, then test actual input.
    const report = 'The Project B claim needs evidence.';
    await b.locator('#report').fill(report);
    let release, started;
    const gate = new Promise(resolve => {release = resolve;});
    const waiting = new Promise(resolve => {started = resolve;});
    await b.route('**/api/action', async route => {started(); await gate; await route.continue();}, {times: 1});
    const saved = b.waitForResponse(r => r.url().endsWith('/api/action'));
    await b.locator('#importReport').click();
    await waiting;
    assert.equal(await b.evaluate(() => document.body.inert), true);
    await b.locator('#report').evaluate(node => node.focus());
    await b.keyboard.type('THIS INPUT MUST NOT BE LOST');
    assert.equal(await b.locator('#report').inputValue(), report);
    await b.screenshot({path: path.join(output, 'pending-report-is-inert.png'), fullPage: true});
    release();
    assert.equal((await saved).status(), 201);
    await b.locator('#response').waitFor();
    assert.equal(await b.evaluate(() => document.body.inert), false);

    // A genuine validated model record containing markup stays plain document data.
    const proposal = await b.evaluate(() => JSON.parse(state.selected.atomization_prompt.match(/```json\s*([\s\S]*?)```/)[1]));
    const markup = '<img data-injected="yes" src="x" onerror="window.injected=true">';
    proposal.findings = [{finding_id: 'F1', claim_id: 'C1', report_quote: report,
      manuscript_quote: report, location_kind: 'exact_quote', assertion: markup,
      criterion: markup, suggested_action: markup, evidence_level: 'unverified', uncertainties: [markup]}];
    await b.locator('#response').fill(JSON.stringify(proposal));
    const collected = b.waitForResponse(r => r.url().endsWith('/api/action'));
    await b.locator('#submitResponse').click();
    assert.equal((await collected).status(), 201);
    await b.locator('#assert-F1').waitFor();
    assert.equal(await b.locator('#assert-F1').inputValue(), markup);
    assert.equal(await b.locator('[data-injected]').count(), 0);
    assert.equal(await b.evaluate(() => window.injected || false), false);

    // Two professional tabs share an immutable view, but only one can adjudicate it.
    const professional = await context.newPage(), other = await context.newPage();
    await Promise.all([professional.goto(endpoints.professional), other.goto(endpoints.professional)]);
    for (const page of [professional, other]) {
      await page.locator('[data-decide]').first().click();
      await page.locator('#decisionValue').selectOption('reject');
    }
    const oldReason = 'Keep this unsaved professional decision draft.';
    await professional.locator('#decisionReason').fill(oldReason);
    await other.locator('#decisionReason').fill('A different explicit human decision.');
    let releaseDecision, decisionStarted;
    const decisionGate = new Promise(resolve => {releaseDecision = resolve;});
    const decisionWaiting = new Promise(resolve => {decisionStarted = resolve;});
    await other.route('**/api/adjudications', async route => {decisionStarted(); await decisionGate; await route.continue();}, {times: 1});
    const first = other.waitForResponse(r => r.url().endsWith('/api/adjudications'));
    await other.locator('#saveDecision').click();
    await decisionWaiting;
    await other.locator('#decisionReason').evaluate(node => node.focus());
    await other.keyboard.type('UNSAVED INPUT');
    const pendingReason = await other.locator('#decisionReason').inputValue();
    releaseDecision();
    assert.equal((await first).status(), 201);
    assert.equal(pendingReason, 'A different explicit human decision.');
    const stale = professional.waitForResponse(r => r.url().endsWith('/api/adjudications'));
    await professional.locator('#saveDecision').click();
    assert.equal((await stale).status(), 409);
    await professional.waitForFunction(() => !document.body.inert);
    assert.equal(await professional.locator('#decisionReason').inputValue(), oldReason);
    assert.equal(await professional.locator('#decisionDialog').evaluate(node => node.open), true);
    await professional.screenshot({path: path.join(output, 'professional-stale-draft-preserved.png'), fullPage: true});
    assert.deepEqual(errors, []);
    const result = {passed: true, browser: browser.version(), checks: [
      'A stale project tab receives 409 while both its original draft and selected Project B remain unchanged',
      'Pending report submission makes the page inert and blocks additional real keyboard input',
      'Successful report save restores interaction and advances to the next task',
      'Validated model markup remains literal text and cannot introduce DOM nodes or event handlers',
      'A professional adjudication changes the task context and rejects a stale second human decision',
      'The modal decision dialog also blocks keyboard input while its save is pending',
      'Rejected professional submission keeps the dialog and unsaved reason unchanged',
    ]};
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
  } finally {
    if (browser) await browser.close();
    child.kill();
    await new Promise(resolve => child.exitCode !== null || child.signalCode !== null ? resolve() : child.once('exit', resolve));
    const cleanup = path.resolve(temporary);
    assert.equal(path.dirname(cleanup), path.resolve(os.tmpdir()));
    assert.ok(path.basename(cleanup).startsWith('http-ui-'));
    await fs.rm(cleanup, {recursive: true, force: true});
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
