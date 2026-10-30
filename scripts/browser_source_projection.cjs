// A real recheck download must preserve policy, historical blocks and current IR.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

const historyMarker = '## Bound original review snapshot\n';
const sourceMarker = '## Internal document blocks\n```json\n';
const revisedMarker = '## Revised document blocks\n```json\n';
const section = (text, marker) => JSON.parse(text.split(marker)[1].split('\n```')[0]);

async function main() {
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'dist', 'browser-source-projection');
  await fs.mkdir(output, {recursive: true});
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'source-projection-'));
  const child = spawn(process.env.STUDIO_PYTHON || 'python',
    [path.join(__dirname, 'browser_fixture_server.py'), temporary, '--source-projection'],
    {cwd: root, windowsHide: true, env: {...process.env, PYTHONUTF8: '1'}});
  let browser, stderr = '';
  const report = {passed: false, checks: [], errors: [], externalRequests: []};
  child.stderr.on('data', data => {stderr += data;});
  try {
    const url = await new Promise((resolve, reject) => {
      let stdout = '';
      const timer = setTimeout(() => reject(Error('Source projection fixture timeout: ' + stderr)), 30000);
      child.stdout.on('data', data => {
        stdout += data;
        const match = stdout.match(/http:\/\/127\.0\.0\.1:\d+\//);
        if (match) {clearTimeout(timer); resolve(match[0]);}
      });
      child.once('error', error => {clearTimeout(timer); reject(error);});
      child.once('exit', code => {clearTimeout(timer); reject(Error(`Fixture failed ${code}: ${stderr}`));});
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
    await page.goto(url);
    await page.locator('.protocol-reuse-notice').waitFor();
    const saved = await page.evaluate(() => ({directory: state.selected.directory,
      request: state.selected.revision_workspace.external_recheck.requests[0]}));
    const request = saved.request;
    const originalPath = path.join(temporary, saved.directory, request.original_prompt_relative_path);
    const originalBytes = await fs.readFile(originalPath);
    const currentPath = path.join(temporary, saved.directory, 'revisions', request.revision_id, 'document.json');
    const actualCurrent = JSON.parse(await fs.readFile(currentPath, 'utf8'));
    for (const [locale, expected, width] of [
      ['zh-Hant', '改動和刪除的舊段落仍完整保留', 1440],
      ['en', 'Every extracted block from this revision is included.', 320],
    ]) {
      await page.locator('#ui-language').selectOption(locale);
      await page.setViewportSize({width, height: 1000});
      assert.ok((await page.locator('.protocol-reuse-notice').innerText()).includes(expected));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      assert.equal(await page.evaluate(() => state.selected.revision_workspace.external_recheck.can_start_followup), false);
      await page.locator('.external-recheck-request').screenshot({path: path.join(output, `${locale}-${width}.png`)});
      report.checks.push(`${locale}-${width}: source reuse is explained without claiming review completion`);
    }
    const downloading = page.waitForEvent('download');
    await page.locator('.external-recheck-request > .row .download').click();
    const download = await downloading;
    const file = path.join(temporary, 'downloaded-recheck.md');
    await download.saveAs(file);
    const raw = await fs.readFile(file);
    assert.deepEqual(raw, Buffer.from(request.prompt));
    const current = section(raw.toString('utf8'), revisedMarker);
    assert.deepEqual(current, actualCurrent.blocks);
    const history = raw.toString('utf8').split(historyMarker)[1];
    const projected = section(history, sourceMarker);
    const lookup = Object.fromEntries(current.map(block => [block.block_id, block]));
    const reconstructed = projected.entries.map(entry => entry.same_as_current ? lookup[entry.same_as_current] : entry.historical_block);
    assert.deepEqual(reconstructed, section(originalBytes.toString('utf8'), sourceMarker));
    assert.deepEqual(await fs.readFile(originalPath), originalBytes);
    const metadata = request.historical_prompt_projection;
    assert.equal(metadata.reused_block_count, 90);
    assert.equal(metadata.literal_block_count, 1);
    assert.ok(metadata.saved_bytes > 20000);
    report.measurement = {historicalPromptBytes: metadata.original_prompt_bytes,
      projectedHistoricalPromptBytes: metadata.projected_prompt_bytes,
      savedBytes: metadata.saved_bytes, currentRecheckBytes: raw.length,
      reusedBlocks: metadata.reused_block_count, literalHistoricalBlocks: metadata.literal_block_count};
    report.checks.push('Actual protocol download reconstructs every historical block and contains the complete current IR; original bytes remain unchanged');
    assert.deepEqual(report.errors, []);
    assert.deepEqual(report.externalRequests, []);
    report.browserVersion = browser.version();
    report.passed = true;
  } finally {
    if (browser) await browser.close();
    if (child.exitCode === null) {
      child.kill();
      await new Promise(resolve => {
        const timer = setTimeout(resolve, 5000);
        child.once('exit', () => {clearTimeout(timer); resolve();});
      });
    }
    report.stderr = stderr;
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(report, null, 2));
    const resolved = await fs.realpath(temporary);
    assert.equal(path.dirname(resolved), await fs.realpath(os.tmpdir()));
    assert.ok(path.basename(resolved).startsWith('source-projection-'));
    await fs.rm(resolved, {recursive: true, force: true});
  }
  console.log(JSON.stringify(report, null, 2));
}
main().catch(error => {console.error(error); process.exitCode = 1;});
