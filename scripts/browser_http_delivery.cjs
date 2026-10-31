// Large shells must arrive whole, including when several tabs remain open.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('node:child_process');
const {createHash} = require('node:crypto');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');

const digest = value => createHash('sha256').update(value).digest('hex');

async function main() {
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'dist', 'browser-http-delivery');
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'studio-http-delivery-'));
  const child = spawn(process.env.STUDIO_PYTHON || 'python', [
    path.join(__dirname, 'http_delivery_fixture_server.py'), temporary,
  ], {cwd: root, windowsHide: true, env: {...process.env, PYTHONUTF8: '1'}});
  let browser, stderr = '';
  const report = {passed: false, completed: [], errors: [], externalRequests: []};
  child.stderr.on('data', value => {stderr += value.toString();});
  try {
    const endpoints = await new Promise((resolve, reject) => {
      let stdout = '';
      const timer = setTimeout(() => reject(Error('Fixture startup timed out: ' + stderr)), 30000);
      child.stdout.on('data', value => {
        stdout += value.toString();
        if (stdout.includes('\n')) {clearTimeout(timer); resolve(JSON.parse(stdout.split('\n')[0]));}
      });
      child.once('error', error => {clearTimeout(timer); reject(error);});
      child.once('exit', code => {clearTimeout(timer); reject(Error('Fixture exited ' + code + ': ' + stderr));});
    });
    browser = await chromium.launch({headless: true,
      ...(process.env.STUDIO_BROWSER ? {executablePath: process.env.STUDIO_BROWSER} : {})});
    report.browser = browser.version();
    const context = await browser.newContext();
    const origins = new Set(Object.values(endpoints).map(url => new URL(url).origin));
    await context.route('**/*', route => {
      if (origins.has(new URL(route.request().url()).origin)) return route.continue();
      report.externalRequests.push(route.request().url());
      return route.abort('blockedbyclient');
    });
    context.on('page', page => page.on('pageerror', error => report.errors.push(error.message)));
    const expected = {};
    for (const [name, url] of Object.entries(endpoints)) {
      const response = await context.request.get(url);
      assert.equal(response.status(), 200);
      const body = await response.body();
      assert.equal(body.length, Number(response.headers()['content-length']));
      expected[name] = {bytes: body.length, sha256: digest(body)};
      await response.dispose();
    }
    const pages = [];
    for (let round = 0; round < 24; round++) {
      for (const [name, url] of Object.entries(endpoints)) {
        const page = await context.newPage();
        pages.push(page);
        const response = await page.goto(url, {timeout: 30000});
        assert.equal(response.status(), 200);
        const body = await response.body();
        assert.equal(body.length, expected[name].bytes, name + ' body length');
        assert.equal(body.length, Number(response.headers()['content-length']), name + ' declared length');
        assert.equal(digest(body), expected[name].sha256, name + ' body content');
        assert.equal(response.headers().connection, undefined, 'HTTP/1.0 closes implicitly');
        await page.waitForFunction(() => typeof state !== 'undefined' && !!state);
        assert.equal(await page.evaluate(() => document.readyState), 'complete');
        report.completed.push({name, round, ...expected[name]});
        // Retain two tabs while opening the next, matching the original failure.
        if (pages.length > 2) await pages.shift().close();
      }
    }
    assert.equal(report.completed.length, 96);
    assert.deepEqual(report.errors, []);
    assert.deepEqual(report.externalRequests, []);
    report.passed = true;
    console.log(JSON.stringify({passed: true, pageLoads: report.completed.length,
      interfaces: Object.keys(endpoints), browser: report.browser}));
  } catch (error) {
    report.error = String(error);
    throw error;
  } finally {
    await fs.mkdir(output, {recursive: true});
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify({...report, stderr}, null, 2));
    if (browser) await browser.close();
    child.kill();
    if (child.exitCode === null && child.signalCode === null) await new Promise(resolve => child.once('exit', resolve));
    assert.equal(path.dirname(path.resolve(temporary)), path.resolve(os.tmpdir()));
    assert.ok(path.basename(temporary).startsWith('studio-http-delivery-'));
    await fs.rm(temporary, {recursive: true, force: true});
  }
}

main().catch(error => {console.error(error); process.exitCode = 1;});
