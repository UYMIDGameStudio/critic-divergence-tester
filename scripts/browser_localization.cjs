// Real-browser language, original-content and shutdown regressions. All projects
// live in an isolated temporary library; no provider or external service is used.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const { spawn } = require('node:child_process');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

async function within(promise, label, timeout = 30000) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(Error(label + ' timed out')), timeout);
    })]);
  } finally {
    clearTimeout(timer);
  }
}

async function idle(page) {
  await page.waitForFunction(() => !mutationPending && !root.inert);
}

async function action(page, locator, name) {
  const [response] = await Promise.all([
    page.waitForResponse(response => response.url().endsWith('/api/action')
      && response.request().postDataJSON()?.action === name),
    locator.click(),
  ]);
  assert.equal(response.status(), 201, await response.text());
  await idle(page);
}

async function language(page, locale) {
  await page.locator('#ui-language').selectOption(locale);
  await page.waitForFunction(expected => document.documentElement.lang === expected, locale);
  await page.evaluate(() => preferenceWrites);
}

async function saveDraft(page) {
  await page.locator('#retry-draft').click();
  await page.waitForFunction(() => draftContext && draftContext.pending === 0
    && draftContext.sequence === draftContext.savedSequence && !draftContext.error);
}

async function sourceSnapshot(page) {
  return page.evaluate(() => ({
    title: state.selected.project.title,
    source: state.selected.project.source,
    blocks: state.selected.extraction.blocks,
    encoding: state.selected.extraction.metadata.encoding,
  }));
}

async function main() {
  const rootPath = path.resolve(__dirname, '..');
  const output = path.resolve(process.env.STUDIO_LOCALIZATION_OUTPUT
    || path.join(rootPath, 'dist', 'browser-localization'));
  await fs.mkdir(output, { recursive: true });
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'studio-browser-locale-'));
  const server = spawn(process.env.STUDIO_PYTHON || 'python', [
    path.join(__dirname, 'browser_fixture_server.py'), path.join(temp, 'library'),
  ], { cwd: rootPath, windowsHide: true, env: { ...process.env, PYTHONUTF8: '1' } });
  const evidence = { passed: false, checks: {}, screenshots: [], errors: [], externalRequests: [] };
  let browser, page, stderr = '', stage = 'startup', releaseHeldDraft;
  const softFailures = [];
  server.stderr.on('data', chunk => { stderr += chunk.toString(); });
  const check = (name, condition, details) => {
    evidence.checks[name] = { passed: Boolean(condition), ...(details === undefined ? {} : { details }) };
    if (!condition) softFailures.push(name);
  };
  async function screenshot(name) {
    await page.screenshot({ path: path.join(output, name + '.png'), fullPage: true });
    evidence.screenshots.push(name + '.png');
  }
  try {
    // Both translation halves must be installed before completeness is assessed.
    const messages = Object.assign({}, ...await Promise.all(['a', 'b'].map(async part =>
      JSON.parse(await fs.readFile(path.join(rootPath, 'studio_web', 'locales-part-' + part + '.json'), 'utf8')))));
    // The original extraction is a local review artifact, not a committed CI
    // dependency. When present, also compare against its full key inventory.
    let phrases = Object.keys(messages), originalInventory = false;
    try {
      phrases = JSON.parse(await fs.readFile(path.join(rootPath, 'dist', 'i18n-phrases.json'), 'utf8'));
      originalInventory = true;
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    const missing = phrases.filter(phrase => typeof messages[phrase]?.en !== 'string'
      || !messages[phrase].en.trim() || typeof messages[phrase]?.['zh-Hant'] !== 'string'
      || !messages[phrase]['zh-Hant'].trim());
    const requiredControls = ['导入文档', '确认识别', '确认上下文', '保存草稿', '退出工作台', '仅导出审查结果'];
    const absentControls = requiredControls.filter(phrase => !messages[phrase]);
    check('translation_catalogue_complete', missing.length === 0 && !absentControls.length
      && Object.keys(messages).length >= 324,
    { phraseCount: phrases.length, originalInventory, missing, absentControls });

    const url = await within(new Promise((resolve, reject) => {
      let stdout = '';
      server.stdout.on('data', chunk => {
        stdout += chunk;
        const match = stdout.match(/http:\/\/127\.0\.0\.1:\d+\//);
        if (match) resolve(match[0]);
      });
      server.once('error', reject);
      server.once('exit', code => reject(Error('Server exited ' + code + ': ' + stderr)));
    }), 'Fixture startup');
    browser = await chromium.launch({ headless: true,
      ...(process.env.STUDIO_BROWSER ? { executablePath: process.env.STUDIO_BROWSER } : {}) });
    evidence.browserVersion = browser.version();
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
    await context.route('**/*', route => {
      if (new URL(route.request().url()).origin === new URL(url).origin) return route.continue();
      evidence.externalRequests.push(route.request().url());
      return route.abort('blockedbyclient');
    });
    page = await context.newPage();
    page.setDefaultTimeout(30000);
    page.on('pageerror', error => evidence.errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    await page.goto(url);
    await page.locator('#file').waitFor();
    stage = 'default language and preference';
    assert.equal(await page.locator('#ui-language').inputValue(), 'zh-Hant');
    assert.equal(await page.locator('html').getAttribute('lang'), 'zh-Hant');
    assert.equal(await page.title(), '文書與論證審查工作台');
    check('default_traditional_chinese', true);
    await screenshot('01-default-traditional');
    await language(page, 'en');
    await page.reload();
    await page.locator('#file').waitFor();
    assert.equal(await page.locator('#ui-language').inputValue(), 'en');
    assert.equal(await page.title(), 'Document & Argument Review Studio');
    assert.equal(await page.evaluate(() => localStorage.getItem('studio-ui-language')), 'en');
    // Removing browser storage also proves the server preference survives a new
    // origin/port on the next app launch, rather than relying on localStorage.
    await page.evaluate(() => localStorage.removeItem('studio-ui-language'));
    await page.reload();
    await page.locator('#file').waitFor();
    assert.equal(await page.locator('#ui-language').inputValue(), 'en');
    check('english_preference_survives_reload_and_empty_browser_storage', true);
    const dependencyText = await page.locator('.dependency').allTextContents();
    check('home_dependency_descriptions_translated_in_english',
      dependencyText.every(text => !/[\u3400-\u9fff]/u.test(text)), dependencyText);
    await screenshot('01b-home-english');

    stage = 'backup recovery errors in both interface languages';
    for (const [locale, expected] of [['en', 'full project backup ZIP'], ['zh-Hant', '完整專案備份 ZIP']]) {
      await language(page, locale);
      const filename = '確認原文 · invalid.zip';
      await page.locator('#backup-file').setInputFiles({name: filename, mimeType: 'application/zip', buffer: Buffer.from('not a ZIP')});
      const rejected = page.waitForResponse(response => response.url().endsWith('/api/action')
        && response.request().postDataJSON()?.action === 'restore_backup');
      await page.locator('#restore-backup').click();
      assert.equal((await rejected).status(), 400);
      await idle(page);
      assert.ok((await page.locator('#err').innerText()).includes(expected));
      assert.equal(await page.locator('#backup-file').evaluate(input => input.files[0].name), filename);
      check('invalid_backup_has_actionable_' + locale + '_message', true);
    }
    const missingMaintenanceTranslations = await page.evaluate(() => [...maintenanceErrorLiterals]
      .filter(key => !UI_MESSAGES[key]?.en || !UI_MESSAGES[key]?.['zh-Hant']));
    assert.deepEqual(missingMaintenanceTranslations, []);
    check('maintenance_error_catalogue_complete', true);
    const originalDiagnostic = '確認原文 / 确认识别.txt <img src=x onerror="window.__maintenanceInjected=true">';
    const injectedFailure = async route => {
      if (route.request().postDataJSON()?.action === 'restore_backup') {
        return route.fulfill({status: 400, contentType: 'application/json', body: JSON.stringify({error: originalDiagnostic})});
      }
      return route.continue();
    };
    await page.route('**/api/action', injectedFailure);
    for (const [locale, guidance] of [['en', 'Restoration could not finish.'], ['zh-Hant', '恢復未能完成。']]) {
      await language(page, locale);
      const rejected = page.waitForResponse(response => response.url().endsWith('/api/action')
        && response.request().postDataJSON()?.action === 'restore_backup');
      await page.locator('#restore-backup').click();
      assert.equal((await rejected).status(), 400);
      await idle(page);
      const message = await page.locator('#err').innerText();
      assert.ok(message.startsWith(guidance));
      assert.ok(message.endsWith(originalDiagnostic));
      assert.equal(await page.locator('#err img').count(), 0);
      assert.equal(await page.evaluate(() => Boolean(window.__maintenanceInjected)), false);
      check('unknown_maintenance_diagnostic_preserved_and_inert_' + locale, true);
    }
    await page.unroute('**/api/action', injectedFailure);
    await language(page, 'en');

    stage = 'unsafe filenames are rejected with bilingual guidance';
    for (const [locale, expected] of [['en', 'Rename the original file'], ['zh-Hant', '請重新命名原檔案']]) {
      await language(page, locale);
      await page.locator('#file').setInputFiles({name: 'NUL.txt', mimeType: 'text/plain', buffer: Buffer.from('Original manuscript.')});
      const rejected = page.waitForResponse(response => response.url().endsWith('/api/upload'));
      await page.locator('#upload').click();
      assert.equal((await rejected).status(), 400);
      await idle(page);
      assert.ok((await page.locator('#err').innerText()).includes(expected));
      assert.equal(await page.locator('#file').evaluate(input => input.files[0].name), 'NUL.txt');
      check('unsafe_filename_rejected_with_' + locale + '_guidance', true);
    }
    await language(page, 'en');

    stage = 'HTML list labels in preview, search and review workspace';
    const listSource = '<ol start="7"><li>原则上 Alpha<ol type="I" start="4"><li>Evidence '
      + '&lt;img src=x onerror="window.__listInjected=true"&gt;</li></ol>Tail</li><li value="12">Conclusion</li></ol>';
    await page.locator('#file').setInputFiles({name: 'list-review.html', mimeType: 'text/html', buffer: Buffer.from(listSource)});
    const listUpload = page.waitForResponse(response => response.url().endsWith('/api/upload'));
    await page.locator('#upload').click();
    assert.equal((await listUpload).status(), 201);
    await idle(page);
    const listSnapshot = await sourceSnapshot(page);
    for (const locale of ['zh-Hant', 'en']) {
      await language(page, locale);
      let preview = await page.locator('#source-preview').textContent();
      assert.ok(preview.includes('7. 原则上 Alpha'));
      assert.ok(preview.includes('  IV. Evidence <img'));
      assert.ok(preview.includes(`↳ 7. (${locale === 'en' ? 'continued' : '續文'}) Tail`));
      const warningText = await page.locator('.next .warning').allTextContents();
      assert.ok(warningText.some(text => text.includes(locale === 'en' ? 'Extracted static HTML text.' : '已提取靜態 HTML 正文')));
      check('html_extraction_warning_translated_' + locale, true);
      await page.locator('#source-search').fill('12.');
      preview = await page.locator('#source-preview').textContent();
      assert.ok(preview.includes('12. Conclusion'));
      assert.ok(!preview.includes('Alpha') && !preview.includes('Evidence'));
      await page.locator('#source-search').fill('IV.');
      assert.ok((await page.locator('#source-preview').textContent()).includes('IV. Evidence'));
      await page.locator('#source-search').fill('');
      assert.equal(await page.locator('#source-preview img').count(), 0);
      assert.deepEqual(await sourceSnapshot(page), listSnapshot);
      check('source_list_labels_search_and_original_preserved_' + locale, true);
    }
    await screenshot('list-preview-english');
    await action(page, page.locator('[data-action="confirm-extraction"]'), 'confirm_extraction');
    for (const [id, value] of Object.entries({document_type: 'Memo', jurisdiction: 'unknown',
      effective_date: 'unknown', publisher_type: 'Author', audience: 'Reviewers'})) {
      await page.locator('#' + id).fill(value);
    }
    await action(page, page.locator('#confirm-context'), 'confirm_context');
    for (const checkbox of await page.locator('.critic').all()) {
      await checkbox.setChecked(await checkbox.inputValue() === 'expression_ambiguity');
    }
    await action(page, page.locator('#run-precheck'), 'run_local_prechecks');
    for (const locale of ['zh-Hant', 'en']) {
      await language(page, locale);
      const sourceText = await page.locator('.source-block').allTextContents();
      assert.ok(sourceText.some(text => text.includes('7. 原则上 Alpha')));
      assert.ok(sourceText.some(text => text.includes(`↳ 7. (${locale === 'en' ? 'continued' : '續文'}) Tail`)));
      const locationOptions = await page.locator('select[id^="location-"] option').allTextContents();
      assert.ok(locationOptions.some(text => text.includes('IV. Evidence')));
      await page.locator('#workspace-search').fill('12.');
      assert.equal(await page.locator('.source-block:visible').count(), 1);
      assert.ok((await page.locator('.source-block:visible').innerText()).includes('12. Conclusion'));
      await page.locator('#workspace-search').fill('');
      assert.equal(await page.locator('.source-block img').count(), 0);
      assert.equal(await page.evaluate(() => Boolean(window.__listInjected)), false);
      assert.deepEqual(await sourceSnapshot(page), listSnapshot);
      check('review_list_labels_search_location_and_original_preserved_' + locale, true);
    }
    await screenshot('list-workspace-english');
    await action(page, page.locator('#back'), 'close_project');

    stage = 'selected file and title across language changes';
    const paragraphs = [
      'English: The garden is quiet. Export and Save draft are original words.',
      '简体中文：天空湛蓝，花园安静。确认识别。',
      '繁體中文：天空湛藍，花園寧靜。匯出與儲存。',
      'Deutsch: Grüße aus Köln; Äpfel, Öl und süße Früchte.',
      'Français : L\u2019été à Noël, le cœur et l\u2019œuvre sont étudiés.',
      '日本語：静かな庭で、ひらがなとカタカナを読みます。',
      'Русский: Тихий сад, зелёные листья и ясное небо.',
      'Latīna: Cælum clārum est; œconomia et rēs pūblica.',
    ];
    const heading = 'Eight-language original · 原文';
    const original = '# ' + heading + '\n\n' + paragraphs.join('\n\n') + '\n\n１２. 日本語の番号付き前提\n';
    const buffer = Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(original, 'utf16le')]);
    const filename = '确认识别 · 原文 Français 日本語 Русский.md';
    const title = '草稿未保存：Original · 繁體 Français Русский';
    await page.locator('#title').fill(title);
    await page.locator('#file').setInputFiles({ name: filename, mimeType: 'text/markdown', buffer });
    await page.locator('#import-encoding').selectOption('auto');
    for (const locale of ['zh-Hant', 'en']) {
      await language(page, locale);
      assert.equal(await page.locator('#title').inputValue(), title);
      const selected = await page.locator('#file').evaluate(async element => ({
        name: element.files[0]?.name,
        bytes: [...new Uint8Array(await element.files[0].arrayBuffer())],
      }));
      assert.equal(selected.name, filename);
      assert.deepEqual(Buffer.from(selected.bytes), buffer);
      assert.equal(await page.locator('#import-encoding').inputValue(), 'auto');
    }
    check('language_switch_preserves_selected_file_bytes_name_title_and_encoding', true,
      { filename, title, bytes: buffer.length, sha256: crypto.createHash('sha256').update(buffer).digest('hex') });

    stage = 'eight-language UTF-16 import';
    const [uploaded] = await Promise.all([
      page.waitForResponse(response => response.url().endsWith('/api/upload')),
      page.locator('#upload').click(),
    ]);
    assert.equal(uploaded.status(), 201, await uploaded.text());
    await idle(page);
    await page.locator('#source-preview').waitFor();
    const imported = await sourceSnapshot(page);
    assert.equal(imported.title, title);
    assert.equal(imported.source.name, filename);
    assert.equal(imported.source.sha256, crypto.createHash('sha256').update(buffer).digest('hex'));
    assert.deepEqual(imported.blocks.map(block => block.text), [heading, ...paragraphs, '日本語の番号付き前提']);
    assert.equal(imported.encoding, 'utf-16-le');
    check('eight_language_utf16_bom_import', true, imported);
    for (const locale of ['zh-Hant', 'en']) {
      await language(page, locale);
      assert.deepEqual(await sourceSnapshot(page), imported);
      const preview = await page.locator('#source-preview').textContent();
      for (const paragraph of paragraphs) assert.ok(preview.includes(paragraph));
      assert.ok(preview.includes('１２. 日本語の番号付き前提'));
      await screenshot(locale === 'en' ? '03-original-english' : '02-original-traditional');
    }
    check('language_switch_preserves_original_source_text_and_filename', true);

    stage = 'context draft and workflow navigation';
    await action(page, page.locator('[data-action="confirm-extraction"]'), 'confirm_extraction');
    const continueButton = page.locator('#continue-workflow');
    assert.equal(await continueButton.getAttribute('data-stage'), 'context');
    await continueButton.click();
    await page.waitForFunction(() => document.activeElement?.id === 'confirm-context');
    check('continue_workflow_targets_next_context_step', true);
    const draftFields = {
      document_type: '原文：Document type · 核驗',
      jurisdiction: 'unknown', effective_date: 'unknown',
      publisher_type: '作者机构 · Änne François',
      audience: '目标受众 · 日本語 Русский Latīna',
      user_provided_materials: '  草稿未保存：\n' + paragraphs.join('\n') + '\n尾端空白  ',
    };
    for (const [id, value] of Object.entries(draftFields)) await page.locator('#' + id).fill(value);
    for (const locale of ['zh-Hant', 'en']) {
      await language(page, locale);
      for (const [id, value] of Object.entries(draftFields)) assert.equal(await page.locator('#' + id).inputValue(), value);
      assert.deepEqual(await sourceSnapshot(page), imported);
    }
    await saveDraft(page);
    await page.reload();
    await page.locator('#confirm-context').waitFor();
    for (const [id, value] of Object.entries(draftFields)) assert.equal(await page.locator('#' + id).inputValue(), value);
    check('language_switch_and_reload_preserve_draft_codepoints_and_whitespace', true, draftFields);
    await screenshot('04-context-draft-english');
    await action(page, page.locator('#confirm-context'), 'confirm_context');

    stage = 'zero-issue precheck and export';
    const untranslatedCritics = await page.locator('label:has(input.critic)').evaluateAll(labels => labels.map(label => {
      const input = label.querySelector('input');
      return { critic: input.value, actual: label.textContent.trim(), expected: tr(critics[input.value]) };
    }).filter(item => item.actual !== item.expected));
    check('critic_labels_translated_in_english', untranslatedCritics.length === 0, untranslatedCritics);
    for (const checkbox of await page.locator('.critic').all()) {
      await checkbox.setChecked(await checkbox.inputValue() === 'expression_ambiguity');
    }
    for (const locale of ['zh-Hant', 'en']) {
      await language(page, locale);
      const selectedCritics = await page.locator('.critic:checked').evaluateAll(nodes => nodes.map(node => node.value));
      check('selected_review_dimensions_survive_language_' + locale,
        JSON.stringify(selectedCritics) === JSON.stringify(['expression_ambiguity']), selectedCritics);
      // Keep the remaining zero-issue scenario independent if this check finds
      // a selection-reset bug, so its evidence is still produced in this run.
      for (const checkbox of await page.locator('.critic').all()) {
        await checkbox.setChecked(await checkbox.inputValue() === 'expression_ambiguity');
      }
    }
    await action(page, page.locator('#run-precheck'), 'run_local_prechecks');
    const afterPrecheck = await page.locator('.critic:checked').evaluateAll(nodes => nodes.map(node => node.value));
    check('selected_review_dimensions_survive_precheck',
      JSON.stringify(afterPrecheck) === JSON.stringify(['expression_ambiguity']), afterPrecheck);
    assert.equal(await page.evaluate(() => state.selected.findings.length), 0);
    assert.ok(await page.locator('#export-results').isVisible());
    assert.ok(await page.locator('#export-results').isEnabled());
    assert.equal(await page.locator('#continue-workflow').getAttribute('data-stage'), 'export');
    await page.locator('#continue-workflow').click();
    await page.waitForFunction(() => document.activeElement?.id === 'export-results');
    await screenshot('05-zero-issues-export');
    await action(page, page.locator('#export-results'), 'export');
    assert.ok(await page.locator('.download').count() > 0);
    const [download] = await Promise.all([page.waitForEvent('download'), page.locator('.download').first().click()]);
    assert.equal(await download.failure(), null);
    await download.saveAs(path.join(output, download.suggestedFilename()));
    check('zero_findings_offer_working_export_and_next_step_navigation', true,
      { filename: download.suggestedFilename(), exportCount: await page.evaluate(() => state.selected.exports.length) });

    stage = 'delayed save during shutdown';
    const quitDraft = 'Unsent original JSON draft\n' + paragraphs.join('\n') + '\n  尾端空白  ';
    const held = deferred(), release = deferred();
    releaseHeldDraft = release.resolve;
    let draftRequest;
    await page.route('**/api/draft', async route => {
      if (route.request().method() !== 'POST') return route.continue();
      draftRequest = route.request().postDataJSON();
      held.resolve();
      await release.promise;
      await route.continue();
    });
    await page.locator('#ai-response').fill(quitDraft);
    const selectedDirectory = await page.evaluate(() => state.selected.directory);
    const quit = page.locator('#app > button').filter({ hasText: await page.evaluate(() => tr('退出工作台')) });
    await quit.click();
    await within(held.promise, 'Delayed draft request');
    await page.waitForFunction(() => mutationPending && root.inert && root.getAttribute('aria-busy') === 'true');
    const beforeAttempt = await page.locator('#ai-response').inputValue();
    let blocked = false;
    // fill() uses a script-assisted editing path that can bypass native inert
    // behavior. Exercise pointer hit testing and real keyboard input instead.
    try { await page.locator('#ai-response').click({ timeout: 500 }); }
    catch (error) { if (error.name === 'TimeoutError') blocked = true; else throw error; }
    assert.ok(blocked, 'Browser must reject editing an inert form during shutdown');
    await page.keyboard.type('SHOULD NOT BE ENTERED');
    assert.equal(await page.locator('#ai-response').inputValue(), beforeAttempt);
    assert.equal(beforeAttempt, quitDraft);
    await page.locator('#ui-language').selectOption('zh-Hant');
    assert.equal(await page.locator('#ui-language').inputValue(), 'en');
    await screenshot('06-shutdown-form-inert');
    const closing = page.waitForResponse(response => response.url().endsWith('/api/shutdown'));
    release.resolve();
    const closed = await closing;
    assert.equal(closed.status(), 200, await closed.text());
    await page.waitForFunction(() => state.closed && !mutationPending && !root.inert);
    const closedText = await page.locator('#app').innerText();
    assert.ok(closedText.includes(await page.evaluate(() => tr('工作台已关闭'))));
    const saved = JSON.parse(await fs.readFile(path.join(temp, 'library', selectedDirectory, '.ui-draft.json'), 'utf8'));
    const responseField = Object.entries(draftRequest.fields).find(([key]) => key.startsWith('ai-response::'));
    assert.ok(responseField, 'Shutdown must persist the active task-scoped response draft');
    assert.equal(responseField[1], quitDraft);
    assert.equal(saved.documents[draftRequest.scope].fields[responseField[0]], quitDraft);
    check('shutdown_blocks_input_until_draft_saved_then_closes', true,
      { inputBlocked: blocked, draftField: responseField[0], savedRevision: saved.revision, closedText });
    await screenshot('07-closed-english');
    check('no_browser_errors_or_external_requests', !evidence.errors.length && !evidence.externalRequests.length);
    assert.equal(softFailures.length, 0, 'Failed checks: ' + softFailures.join(', '));
    evidence.passed = true;
    await fs.rm(path.join(output, 'failure.png'), { force: true });
  } catch (error) {
    evidence.failure = { stage, message: error.message, stack: error.stack };
    if (page && !page.isClosed()) await screenshot('failure').catch(() => {});
    throw error;
  } finally {
    releaseHeldDraft?.();
    evidence.serverStderr = stderr;
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(evidence, null, 2), 'utf8');
    await browser?.close();
    if (server.exitCode === null && server.signalCode === null) {
      const exited = new Promise(resolve => server.once('exit', resolve));
      server.kill();
      await within(exited, 'Fixture cleanup', 10000).catch(() => {});
    }
    const relativeTemp = path.relative(path.resolve(os.tmpdir()), path.resolve(temp));
    assert.ok(relativeTemp && !relativeTemp.startsWith('..') && !path.isAbsolute(relativeTemp)
      && path.basename(temp).startsWith('studio-browser-locale-'));
    await fs.rm(temp, { recursive: true, force: true });
  }
  console.log('PASS browser localization: ' + Object.keys(evidence.checks).length + ' checks; ' + output);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
