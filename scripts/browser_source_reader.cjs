// Source access, navigation and reading state through real persisted projects.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

async function main() {
  const root = path.resolve(__dirname, '..');
  const modeFilter = process.argv[2];
  assert.ok(!modeFilter || ['article', 'pages', 'followup'].includes(modeFilter));
  const output = path.join(root, 'dist', 'browser-source-reader' + (modeFilter ? '-' + modeFilter : ''));
  await fs.mkdir(output, {recursive: true});
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'source-reader-'));
  const browser = await chromium.launch({headless: true,
    ...(process.env.STUDIO_BROWSER ? {executablePath: process.env.STUDIO_BROWSER} : {})});
  const report = {passed: false, checks: [], errors: [], externalRequests: [], actions: []};
  const save = page => page.evaluate(async () => {await saveDraft(); await draftWrites; await preferenceWrites;});
  const language = async (page, locale) => {
    await page.locator('#ui-language').selectOption(locale);
    await page.waitForFunction(expected => document.documentElement.lang === expected, locale);
    await page.evaluate(() => preferenceWrites);
  };
  const follow = async (page, link) => {
    const fragment = await link.getAttribute('href');
    await link.focus();
    await link.press('Enter');
    await page.waitForFunction(id => document.activeElement?.id === id, fragment.slice(1));
    assert.equal(new URL(page.url()).hash, fragment);
    const target = page.locator(`[id="${fragment.slice(1)}"]`);
    assert.equal(await target.isVisible(), true);
    const geometry = await target.evaluate(element => {
      const pane = element.closest('.manuscript-pane');
      if (!pane) return {valid: true};
      const toolbar = pane.querySelector('.source-reader-toolbar').getBoundingClientRect();
      const block = element.getBoundingClientRect();
      return {valid: toolbar.top >= -1 && toolbar.top <= pane.getBoundingClientRect().top + 4
          && block.top >= toolbar.bottom - 1 && block.top < innerHeight,
        toolbar: toolbar.toJSON(), block: block.toJSON(), pane: pane.getBoundingClientRect().toJSON(), scroll: pane.scrollTop};
    });
    assert.equal(geometry.valid, true, 'Source must be visible below the persistent reader controls: ' + JSON.stringify(geometry));
    return target;
  };
  async function records(project) {
    const result = new Map();
    async function walk(directory) {
      for (const entry of await fs.readdir(directory, {withFileTypes: true}).catch(() => [])) {
        const item = path.join(directory, entry.name);
        if (entry.isDirectory()) await walk(item);
        else if (entry.isFile()) result.set(item, await fs.readFile(item));
      }
    }
    for (const name of ['source', 'audits', 'ai-requests', 'finding-decisions', 'revisions', 'review-rounds'])
      await walk(path.join(project, name));
    return result;
  }
  try {
    for (const [mode, flag] of [['article', '--source-reader'], ['pages', '--reader-pages'], ['followup', '--reader-followup']]) {
      if (modeFilter && mode !== modeFilter) continue;
      const data = path.join(temporary, mode);
      await fs.mkdir(data);
      const child = spawn(process.env.STUDIO_PYTHON || 'python',
        [path.join(__dirname, 'browser_fixture_server.py'), data, flag],
        {cwd: root, windowsHide: true, env: {...process.env, PYTHONUTF8: '1'}});
      let stderr = '', page;
      child.stderr.on('data', value => {stderr += value;});
      try {
        const url = await new Promise((resolve, reject) => {
          let stdout = '';
          const timer = setTimeout(() => reject(Error(`${mode}: fixture timeout ${stderr}`)), 30000);
          child.stdout.on('data', value => {
            stdout += value;
            const match = stdout.match(/http:\/\/127\.0\.0\.1:\d+\//);
            if (match) {clearTimeout(timer); resolve(match[0]);}
          });
          child.once('error', error => {clearTimeout(timer); reject(error);});
          child.once('exit', code => {clearTimeout(timer); reject(Error(`Fixture exit ${code}: ${stderr}`));});
        });
        page = await browser.newPage({viewport: {width: 1440, height: 1000}});
        page.on('pageerror', error => report.errors.push(`${mode}: ${error.message}`));
        await page.route('**/*', route => {
          if (new URL(route.request().url()).origin === new URL(url).origin) {
            if (route.request().url().endsWith('/api/action')) report.actions.push(route.request().postDataJSON().action);
            return route.continue();
          }
          report.externalRequests.push(route.request().url());
          return route.abort();
        });
        await page.goto(url);
        await page.waitForFunction(() => state?.selected?.extraction?.blocks?.length > 0);
        const saved = await page.evaluate(() => ({directory: state.selected.directory,
          blocks: state.selected.extraction.blocks, assessment: state.selected.argument_assessments?.[0]?.assessment}));
        const project = path.join(data, saved.directory);
        assert.equal(path.dirname(project), data);
        const originals = await records(project);
        const texts = await page.locator('.source-block .source-text').allTextContents();
        assert.equal(await page.locator('.source-block').count(), saved.blocks.length);
        if (mode === 'article') {
          const reader = page.locator('#manuscript-reader');
          assert.equal(await page.locator('.finding-card').count(), 0);
          assert.equal(await reader.evaluate(element => element.open), false);
          assert.equal(await page.locator('[data-stage="adjudication"]').isDisabled(), false);
          await page.locator('[data-stage="adjudication"]').click();
          assert.equal(await page.evaluate(() => document.activeElement.classList.contains('argument-assessment')), true);
          const evidence = page.locator('.assessment-axis details').first();
          await evidence.locator(':scope > summary').click();
          const quoteLink = evidence.locator('.quote small a').first();
          await follow(page, quoteLink);
          const quoteFragment = await quoteLink.getAttribute('href');
          assert.equal(await reader.evaluate(element => element.open), true);
          await page.locator('#workspace-search').fill('NO-READER-MATCH');
          assert.ok((await page.locator('#source-search-status').innerText()).includes(`0 / ${saved.blocks.length}`));
          await follow(page, quoteLink);
          assert.equal(await page.locator('#workspace-search').inputValue(), '');
          const outline = page.locator('.source-outline');
          await outline.locator(':scope > summary').click();
          const headings = saved.blocks.filter(block => block.kind === 'heading');
          assert.equal(await outline.locator('a').count(), headings.length);
          const repeated = outline.getByRole('link', {name: 'Shared point', exact: true});
          assert.equal(await repeated.count(), 2);
          assert.notEqual(await repeated.nth(0).getAttribute('href'), await repeated.nth(1).getAttribute('href'));
          const sectionFragment = await repeated.nth(1).getAttribute('href');
          const target = await follow(page, repeated.nth(1));
          assert.equal(await target.locator('.source-text').getAttribute('aria-level'), '2');
          await page.goBack();
          await page.waitForFunction(fragment => location.hash === fragment && document.activeElement?.id === fragment.slice(1), quoteFragment);
          await page.goForward();
          await page.waitForFunction(fragment => location.hash === fragment && document.activeElement?.id === fragment.slice(1), sectionFragment);
          assert.equal(await reader.locator('img').count(), 0);
          assert.equal(await page.evaluate(() => window.__readerInjected), undefined);
          await page.evaluate(() => history.replaceState(null, '', location.pathname));
          await page.locator('#workspace-search').fill('Body-position-07.');
          await language(page, 'en');
          assert.equal(await reader.evaluate(element => element.open), true);
          assert.equal(await page.locator('#workspace-search').inputValue(), 'Body-position-07.');
          assert.equal(await page.locator('.manuscript-pane .source-block:not([hidden])').count(), 1);
          assert.match(await page.locator('#source-search-status').innerText(), /^Search results/);
          await save(page);
          await page.reload();
          await page.locator('#manuscript-reader[open]').waitFor();
          assert.equal(await page.locator('#workspace-search').inputValue(), 'Body-position-07.');
          assert.equal(await page.locator('.manuscript-pane .source-block:not([hidden])').count(), 1);
          await page.locator('#clear-source-search').click();
          assert.equal(await page.locator('#clear-source-search').isDisabled(), true);
          const pane = page.locator('.manuscript-pane');
          await pane.evaluate(element => {element.scrollTop = 1600;});
          await page.waitForFunction(() => draftContext.fields['source-reader-scroll'] === '1600');
          await reader.locator(':scope > summary').click();
          await page.waitForFunction(() => draftContext.fields['source-reader-open'] === false);
          await save(page);
          await reader.locator(':scope > summary').click();
          await page.waitForFunction(() => document.querySelector('.manuscript-pane').scrollTop >= 1598);
          await language(page, 'zh-Hant');
          await page.waitForFunction(() => document.querySelector('.manuscript-pane').scrollTop >= 1598);
          await save(page);
          await page.reload();
          await page.locator('#manuscript-reader[open]').waitFor();
          await page.waitForFunction(() => document.querySelector('.manuscript-pane').scrollTop >= 1598);
          report.checks.push('Zero-Finding review links reveal full source; queries, reader expansion and reading position survive language changes, closing and reload');
          await page.locator('.source-outline > summary').click();
          const lastHeading = page.locator('.source-outline a').last();
          const lastFragment = await lastHeading.getAttribute('href');
          await follow(page, lastHeading);
          await save(page);
          await page.reload();
          await page.waitForFunction(id => document.activeElement?.id === id, lastFragment.slice(1));
          assert.equal(await page.locator(`[id="${lastFragment.slice(1)}"]`).isVisible(), true);
          for (const [locale, width] of [['zh-Hant', 1440], ['en', 320]]) {
            await language(page, locale);
            await page.setViewportSize({width, height: 1000});
            assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
            await reader.screenshot({path: path.join(output, `${mode}-${locale}-${width}.png`)});
          }
          await page.locator('[data-stage="extraction"]').click();
          assert.equal(await page.evaluate(() => document.activeElement.id), 'workspace-search');
          const response = page.waitForResponse(value => value.url().endsWith('/api/action') && value.request().postDataJSON()?.action === 'export');
          await page.locator('#export-results').click();
          assert.equal((await response).status(), 201);
          await page.waitForFunction(() => !mutationPending);
          const downloading = page.waitForEvent('download');
          await page.locator('.download[data-path$="/audit.json"]').click();
          const file = path.join(data, 'audit.json');
          await (await downloading).saveAs(file);
          const audit = JSON.parse(await fs.readFile(file, 'utf8'));
          assert.deepEqual(audit.audit_runs[0].argument_assessment, saved.assessment);
          assert.equal(audit.findings.length, 0);
          report.checks.push('Duplicate headings stay distinct, source fragments restore after reload, HTML stays inert, and zero-Finding export preserves original assessment');
        } else if (mode === 'pages') {
          await page.locator('#manuscript-reader > summary').click();
          const pages = new Map();
          for (const block of saved.blocks) if (Number.isInteger(block.location?.page) && block.location.page > 0 && !pages.has(block.location.page)) pages.set(block.location.page, block.block_id);
          await page.locator('.source-pages > summary').click();
          assert.deepEqual(await page.locator('.source-pages a').allTextContents(), [...pages.keys()].map(String));
          assert.ok(pages.size >= 2);
          await page.locator('#workspace-search').fill('NO-MATCH-ON-THIS-PAGE');
          const pageTarget = await follow(page, page.locator('.source-pages a').last());
          assert.equal(await pageTarget.getAttribute('id'), 'source-' + [...pages.values()].at(-1));
          assert.equal(await page.locator('#workspace-search').inputValue(), '');
          await page.locator('#manuscript-reader').screenshot({path: path.join(output, 'source-pages.png')});
          report.checks.push('Actual multi-page source uses original page anchors and clears a conflicting filter on keyboard navigation');
        } else {
          assert.equal(await page.locator('#manuscript-reader').count(), 0);
          assert.equal(await page.locator('#workspace-search').inputValue(), '');
          assert.ok(saved.blocks[0].text.includes('Revised scope'));
          assert.equal(await page.locator('.source-block .source-text').first().textContent(), saved.blocks[0].text);
          assert.equal(await page.locator('.source-outline a').first().textContent(), saved.blocks[0].text);
          assert.equal(await page.locator('.manuscript-pane').evaluate(element => element.scrollTop), 0);
          await follow(page, page.locator('.finding-card > .row a').first());
          assert.equal(await page.locator('#export-results').isDisabled(), true);
          report.checks.push('Followup reader uses current revised IR and does not restore an older document’s query or bypass adjudication');
        }
        assert.deepEqual(await page.locator('.source-block .source-text').allTextContents(), texts);
        for (const [file, bytes] of originals) assert.deepEqual(await fs.readFile(file), bytes);
      } finally {
        if (page) await page.close();
        if (child.exitCode === null) {
          child.kill();
          await new Promise(resolve => {
            const timer = setTimeout(resolve, 5000);
            child.once('exit', () => {clearTimeout(timer); resolve();});
          });
        }
        if (stderr) report.errors.push(`${mode}: ${stderr}`);
      }
    }
    assert.deepEqual(report.errors, []);
    assert.deepEqual(report.externalRequests, []);
    assert.deepEqual(report.actions, !modeFilter || modeFilter === 'article' ? ['export'] : []);
    report.checks.push('Reading actions leave source, requests, model results, decisions and revision records byte-identical');
    report.browserVersion = browser.version();
    report.passed = true;
  } finally {
    await browser.close();
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(report, null, 2));
    const resolved = await fs.realpath(temporary);
    assert.equal(path.dirname(resolved), await fs.realpath(os.tmpdir()));
    assert.ok(path.basename(resolved).startsWith('source-reader-'));
    await fs.rm(resolved, {recursive: true, force: true});
  }
  console.log(JSON.stringify(report, null, 2));
}
main().catch(error => {console.error(error); process.exitCode = 1;});
