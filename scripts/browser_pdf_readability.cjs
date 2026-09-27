// Synthetic legacy extraction only; never opens a user project or manuscript.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawnSync} = require('node:child_process');
const path = require('node:path');
const assert = require('node:assert/strict');

async function main() {
  const repo = path.resolve(__dirname, '..');
  const shell = spawnSync(process.env.STUDIO_PYTHON || 'python',
    ['-c', 'from studio_web import shell_template; print(shell_template())'],
    {cwd: repo, encoding: 'utf8', windowsHide: true, env: {...process.env, PYTHONUTF8: '1'}, maxBuffer: 2 * 1024 * 1024});
  assert.equal(shell.status, 0, shell.stderr);
  const config = {critics: {scope: 'Scope'}, extensions: ['.pdf'], disciplines: {}, research_types: {}};
  const html = shell.stdout.replace('__TOKEN__', JSON.stringify('synthetic-browser-test'))
    .replace('__REVIEW_CONFIG__', JSON.stringify(config));
  const fragmented = 'Alpha Beta Gamma Delta Epsilon Zeta Eta Theta Iota Kappa Lambda Mu Nu Xi'.split(' ').join('\n \n');
  const verse = 'Dawn\nLight\nCloud\nRain\nWind\nLeaf\nBird\nSong\nPath\nField\nStar\nNight';
  const poem = 'The morning rises\nAcross the quiet water\nEach line stays here';
  const blocks = [fragmented, verse, poem].map((text, index) => ({
    block_id: `b${index + 1}`, text, kind: 'paragraph', attrs: {},
    location: {block_id: `b${index + 1}`, block_kind: 'pdf_text_block', page: index + 1},
  }));
  const finding = {finding_id: 'f1', critic: 'scope', severity: 'low', status: 'open',
    verification_state: 'checked', location: blocks[0].location, evidence: fragmented,
    issue: 'Synthetic reader check', consequence: '', suggested_action: '', uncertainties: [],
    check_data: {close_reading: {author_position: '', strongest_defense: '', why_defense_fails: '',
      repair_test: '', context_evidence: [{block_id: 'b1', role: 'context', quote: fragmented}]}}};
  const selected = {directory: 'synthetic', project: {title: 'Synthetic legacy PDF', source: {name: 'synthetic.pdf', sha256: 'synthetic'}},
    state: {extraction_state: 'unconfirmed', context_state: 'missing', review_state: 'complete'},
    extraction: {available: true, metadata: {pdf_kind: 'text', coordinates_available: false},
      warnings: [{code: 'pdf-coordinates-unavailable', severity: 'medium', message: 'Synthetic pypdf metadata'}], blocks},
    findings: [finding], finding_summary: {total: 1, open: 1}, ai_requests: [], workflow: [], exports: [],
    attention_queue: {groups: [{block_id: 'b1', finding_count: 1, critic_count: 1, priority_reasons: [], findings: [finding]}]},
    ui_draft: {scope: 'synthetic', revision: 0, fields: {}}, revision_workspace: {}};
  const browser = await chromium.launch({headless: true,
    ...(process.env.STUDIO_BROWSER ? {executablePath: process.env.STUDIO_BROWSER} : {})});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const requests = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    requests.push({path: url.pathname, method: route.request().method()});
    if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: html});
    if (url.pathname === '/api/state') return route.fulfill({json: {selected, projects: [], dependencies: []}});
    return route.abort();
  });
  try {
    await page.goto('http://127.0.0.1:43191/');
    await page.locator('#pdf-line-breaks-toggle').waitFor();
    const originals = await page.locator('.source-block, .finding-card > .quote, .close-reading .quote p, #source-preview').allTextContents();
    const originalState = await page.evaluate(() => JSON.stringify(state.selected));
    assert.equal(await page.locator('#source-b1 .pdf-fragmented-text').textContent(), fragmented);
    assert.equal(await page.locator('.finding-card > .quote .pdf-fragmented-text').textContent(), fragmented);
    assert.equal(await page.locator('.close-reading .quote p .pdf-fragmented-text').textContent(), fragmented);
    assert.equal(await page.locator('#source-b2 .pdf-fragmented-text, #source-b3 .pdf-fragmented-text').count(), 0);
    async function wordPositions(selector) {
      return page.locator(selector).evaluate(element => {
        const node = element.firstChild;
        return ['Alpha', 'Beta'].map(word => {
          const range = document.createRange();
          const start = node.textContent.indexOf(word);
          range.setStart(node, start); range.setEnd(node, start + word.length);
          const box = range.getBoundingClientRect();
          return {x: box.x, y: box.y};
        });
      });
    }
    for (const selector of ['#source-b1 .pdf-fragmented-text', '.finding-card > .quote .pdf-fragmented-text', '.close-reading .quote p .pdf-fragmented-text']) {
      const [first, second] = await wordPositions(selector);
      assert.ok(Math.abs(first.y - second.y) < 1 && second.x > first.x, selector);
    }
    assert.match(await page.locator('.pdf-reader-control').textContent(), /原文與引文保持不變/);
    await page.locator('#pdf-line-breaks-toggle').click();
    assert.equal(await page.locator('#pdf-line-breaks-toggle').getAttribute('aria-pressed'), 'true');
    const [first, second] = await wordPositions('#source-b1 .pdf-fragmented-text');
    assert.ok(second.y > first.y, 'Original extracted line breaks are restored');
    assert.deepEqual(await page.locator('.source-block, .finding-card > .quote, .close-reading .quote p, #source-preview').allTextContents(), originals);
    assert.equal(await page.evaluate(() => JSON.stringify(state.selected)), originalState);
    assert.equal(await page.locator('.finding-card .pill').first().getAttribute('href'), '#source-b1');
    await page.locator('#pdf-line-breaks-toggle').click();
    await page.locator('#source-search').fill('Alpha Beta');
    assert.equal(await page.locator('#source-preview .pdf-fragmented-text').textContent(), fragmented);
    await page.locator('#workspace-search').fill('Alpha Beta');
    assert.equal(await page.locator('#source-b1').isVisible(), true);
    assert.equal(await page.locator('#source-b2').isVisible(), false);
    assert.equal(await page.locator('#source-b3').isVisible(), false);
    assert.equal(await page.locator('#source-b1 .pdf-fragmented-text').textContent(), fragmented);
    assert.equal(await page.evaluate(() => JSON.stringify(state.selected)), originalState);
    const revisedQuote = await page.evaluate(text => externalResolutionEvidence({
      kind: 'existing', evidence_validation: 'checked-revised-excerpts', evidence: '',
      source_evidence: [{block_id: 'revised-b1', quote: text}],
    }, {revision_id: 'r1', revised_blocks: [{block_id: 'revised-b1', text}]}), fragmented);
    assert.ok(!revisedQuote.includes('pdf-fragmented-text'), 'Revised quotes do not inherit original parser metadata');
    await page.evaluate(() => {uiLocale = 'en'; render();});
    assert.match(await page.locator('.pdf-reader-control').textContent(), /source text and quotations stay unchanged/);
    assert.equal(await page.locator('#pdf-line-breaks-toggle').textContent(), 'Show original extracted breaks');
    const styleChecks = await page.evaluate(() => [
      '#source-b2', '#source-b3', '#source-b1 .pdf-fragmented-text',
    ].map(selector => getComputedStyle(document.querySelector(selector)).whiteSpace));
    assert.deepEqual(styleChecks, ['pre-wrap', 'pre-wrap', 'normal']);
    // Same fragmented-looking words from a non-PDF source must remain untouched.
    await page.evaluate(() => {state.selected.project.source.name = 'poem.txt'; render();});
    assert.equal(await page.locator('.pdf-fragmented-text, #pdf-line-breaks-toggle').count(), 0);
    // A PDF with another parser, or single-word verse without separators, is not eligible.
    await page.evaluate(() => {state.selected.project.source.name = 'poem.pdf'; state.selected.extraction.warnings = []; render();});
    assert.equal(await page.locator('.pdf-fragmented-text, #pdf-line-breaks-toggle').count(), 0);
    await page.evaluate(() => {
      state.selected.extraction.warnings = [{code: 'pdf-coordinates-unavailable', message: 'Builtin PDF backend has no character coordinates'}];
      render();
    });
    assert.equal(await page.locator('.pdf-fragmented-text, #pdf-line-breaks-toggle').count(), 0);
    const thresholds = await page.evaluate(() => ({
      short: hasFragmentedPdfLines(Array(11).fill('word').join('\n \n')),
      noSeparators: hasFragmentedPdfLines(Array(12).fill('word').join('\n')),
      ordinary: hasFragmentedPdfLines(Array(12).fill('two words').join('\n \n')),
      cjk: hasFragmentedPdfLines(Array(12).fill('這是一段普通中文行文').join('\n \n')),
      japanese: hasFragmentedPdfLines(Array(12).fill('これは普通の日本語の行です').join('\n \n')),
    }));
    assert.deepEqual(thresholds, {short: false, noSeparators: false, ordinary: false, cjk: false, japanese: false});
    assert.deepEqual(errors, []);
    assert.ok(requests.every(request => request.method === 'GET'), 'Reader changes make no service writes');
    console.log('PASS: PDF source/quotes display horizontally; original text, anchors and state unchanged; original-break toggle, bilingual notice, multiword search in both panes, revised-quote and poem/parser guards verified.');
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
