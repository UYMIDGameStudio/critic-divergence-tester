// Browser layout evidence uses synthetic, isolated projects only. No user library
// or external service is opened. Run with the same browser/runtime env as e2e.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');

async function main() {
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'dist', 'ui-style-0.2.26');
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'studio-layout-'));
  await fs.mkdir(output, {recursive: true});
  const report = {passed: false, fixture: 'Synthetic close-reading and research projects',
    checks: [], screenshots: [], errors: [], externalRequests: [], failures: []};
  const servers = [];
  let browser, page, stage = 'startup';
  function fixture(script, directory, args = []) {
    const child = spawn(process.env.STUDIO_PYTHON || 'python',
      [path.join(__dirname, script), path.join(temporary, directory), ...args],
      {cwd: root, windowsHide: true, env: {...process.env, PYTHONUTF8: '1'}});
    const record = {child, stderr: ''};
    servers.push(record);
    child.stderr.on('data', chunk => {record.stderr += chunk.toString();});
    return new Promise((resolve, reject) => {
      let stdout = '';
      const timer = setTimeout(() => reject(Error('Fixture startup timeout: ' + record.stderr)), 30000);
      child.stdout.on('data', chunk => {
        stdout += chunk.toString();
        if (!stdout.includes('\n')) return;
        clearTimeout(timer);
        const first = stdout.split('\n')[0].trim();
        try {resolve(first.startsWith('{') ? JSON.parse(first) : first);}
        catch (error) {reject(error);}
      });
      child.once('error', error => {clearTimeout(timer); reject(error);});
      child.once('exit', code => {clearTimeout(timer); reject(Error(`Fixture exit ${code}: ${record.stderr}`));});
    });
  }
  const check = (name, passed, detail) => {
    report.checks.push({name, passed, ...(detail === undefined ? {} : {detail})});
    if (!passed) report.failures.push(name);
  };
  try {
    const [homeUrl, reviewUrl, research] = await Promise.all([
      fixture('browser_fixture_server.py', 'home'),
      fixture('browser_fixture_server.py', 'review', ['--close-reading']),
      fixture('research_fixture_server.py', 'research'),
    ]);
    const allowedOrigins = new Set([homeUrl, reviewUrl, research.product, research.resume,
      research.professional, ...research.samples.map(sample => sample.url)].map(url => new URL(url).origin));
    browser = await chromium.launch({headless: true,
      ...(process.env.STUDIO_BROWSER ? {executablePath: process.env.STUDIO_BROWSER} : {})});
    report.browserVersion = browser.version();
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}, reducedMotion: 'reduce'});
    await context.route('**/*', route => {
      const url = route.request().url();
      if (allowedOrigins.has(new URL(url).origin)) return route.continue();
      report.externalRequests.push(url);
      return route.abort();
    });
    // Observe options passed by actual navigation handlers, while preserving the
    // native scrolling implementation (including focus/fragment behavior).
    await context.addInitScript(() => {
      window.__layoutScrollCalls = [];
      const nativeScroll = Element.prototype.scrollIntoView;
      Element.prototype.scrollIntoView = function(options) {
        window.__layoutScrollCalls.push({id: this.id, classes: this.className,
          behavior: options && typeof options === 'object' ? options.behavior : undefined});
        return nativeScroll.call(this, options);
      };
    });
    page = await context.newPage();
    page.on('pageerror', error => report.errors.push({stage, message: error.message}));
    page.setDefaultTimeout(15000);
    const screenshot = async (name, {fullPage = true, top = true} = {}) => {
      await page.evaluate(top => {document.activeElement?.blur(); if (top) window.scrollTo(0, 0);}, top);
      await page.screenshot({path: path.join(output, name + '.png'), fullPage});
      report.screenshots.push(name + '.png');
    };
    const language = async (locale, selector) => {
      await page.locator(selector).selectOption(locale);
      await page.waitForFunction(value => document.documentElement.lang === value, locale);
    };
    const layout = async name => {
      const metrics = await page.evaluate(() => {
        const visible = node => {const r = node.getBoundingClientRect();
          return r.width > 0 && r.height > 0 && getComputedStyle(node).visibility !== 'hidden';};
        const label = node => node.id ? '#' + node.id : node.tagName.toLowerCase() +
          (node.className && typeof node.className === 'string' ? '.' + node.className.trim().replace(/\s+/g, '.') : '');
        const controls = [...document.querySelectorAll('button, input, select, textarea, a.button-link, summary')]
          .filter(node => visible(node) && !node.matches('[type="checkbox"], [type="radio"], [type="hidden"]'));
        return {
          viewport: innerWidth, documentWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
          undersizedControls: controls.map(node => {const r = node.getBoundingClientRect();
            return {selector: label(node), width: +r.width.toFixed(2), height: +r.height.toFixed(2)};})
            .filter(r => r.width < 43.5 || r.height < 43.5),
          overflowingRegions: [...document.querySelectorAll('dialog[open], .pane, .pane-body, form')]
            .filter(node => visible(node) && node.scrollWidth > node.clientWidth + 2)
            .map(node => ({selector: label(node), scrollWidth: node.scrollWidth, clientWidth: node.clientWidth})),
          outlyingElements: [...document.body.querySelectorAll('*')].filter(node => visible(node) &&
            !node.closest('.skip-link') && node.getBoundingClientRect().right > innerWidth + 2)
            .slice(0, 8).map(node => ({selector: label(node), right: Math.round(node.getBoundingClientRect().right)})),
        };
      });
      check(name + ': no horizontal page overflow', metrics.documentWidth <= metrics.viewport + 2, metrics);
      check(name + ': controls at least 44px', metrics.undersizedControls.length === 0, metrics.undersizedControls);
      check(name + ': reading panes and dialogs fit', metrics.overflowingRegions.length === 0, metrics.overflowingRegions);
    };
    const keyboard = async name => {
      await page.reload();
      await page.locator('body:not([inert])').waitFor();
      await page.keyboard.press('Tab');
      const focus = await page.evaluate(() => {
        const node = document.activeElement, rect = node.getBoundingClientRect(), css = getComputedStyle(node);
        return {tag: node.tagName, href: node.getAttribute('href'), text: node.textContent.trim(),
          skipLink: node.classList.contains('skip-link'), visible: rect.top >= 0 && rect.bottom <= innerHeight,
          outline: css.outlineStyle, outlineWidth: parseFloat(css.outlineWidth)};
      });
      check(name + ': first Tab exposes skip link with visible focus', focus.skipLink && focus.visible &&
        focus.outline !== 'none' && focus.outlineWidth >= 2, focus);
      if (focus.skipLink) {
        await page.keyboard.press('Enter');
        const target = await page.evaluate(() => ({active: document.activeElement.id, hash: location.hash}));
        check(name + ': skip link moves keyboard focus to content', '#' + target.active === focus.href, target);
      }
    };
    const contrast = async (name, selectors) => {
      const results = await page.evaluate(selectors => {
        const rgba = value => (value.match(/[\d.]+/g) || []).map(Number);
        const over = (fg, bg) => {const a = fg[3] ?? 1; return fg.slice(0, 3).map((v, i) => a * v + (1 - a) * bg[i]);};
        const lum = rgb => rgb.map(v => {v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;})
          .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
        return selectors.map(selector => {
          const node = document.querySelector(selector);
          if (!node) return {selector, missing: true};
          const css = getComputedStyle(node), ancestors = [];
          for (let current = node; current; current = current.parentElement) ancestors.push(current);
          let bg = [255, 255, 255];
          for (const ancestor of ancestors.reverse()) bg = over(rgba(getComputedStyle(ancestor).backgroundColor), bg);
          const color = over(rgba(css.color), bg), a = lum(color), b = lum(bg);
          const minimum = parseFloat(css.fontSize) >= 24 ||
            (parseFloat(css.fontSize) >= 18.66 && parseInt(css.fontWeight, 10) >= 700) ? 3 : 4.5;
          return {selector, foreground: css.color, background: bg, ratio: +(Math.max(a, b) + .05).toFixed(8) /
            (Math.min(a, b) + .05), minimum};
        });
      }, selectors);
      check(name + ': sampled text/action contrast', results.every(item => !item.missing && item.ratio >= item.minimum), results);
    };
    const fontResize = async name => {
      // Text-only 200% resize, including px-based text. Snapshot first so inherited
      // sizes are doubled once, rather than compounded at every ancestor.
      await page.evaluate(() => {
        const sizes = [...document.querySelectorAll('body, body *')].map(node => [node, parseFloat(getComputedStyle(node).fontSize)]);
        for (const [node, size] of sizes) node.style.setProperty('font-size', size * 2 + 'px', 'important');
      });
      await layout(name + '-text-200-percent');
      await screenshot(name + '-text-200-percent');
      await page.reload();
    };
    const matrix = async ({name, url, ready, localeControl, bodyText, action}) => {
      stage = name;
      await page.goto(url);
      await page.locator(ready).first().waitFor();
      await keyboard(name);
      await page.locator(ready).first().waitFor();
      for (const locale of ['zh-Hant', 'en']) {
        await language(locale, localeControl);
        for (const width of [1440, 390, 320]) {
          await page.setViewportSize({width, height: width === 1440 ? 1000 : 844});
          await layout(`${name}-${locale}-${width}`);
          if (width === 1440 || width === 390 && locale === 'zh-Hant' || width === 320 && locale === 'en') {
            await screenshot(`${name}-${locale}-${width}`);
          }
        }
      }
      await page.setViewportSize({width: 1440, height: 1000});
      await contrast(name, [bodyText, action]);
      await fontResize(name);
      await page.locator(ready).first().waitFor();
    };
    await matrix({name: 'studio-home', url: homeUrl, ready: '#upload', localeControl: '#ui-language',
      bodyText: '#studio-description', action: '#upload'});
    await matrix({name: 'studio-review', url: reviewUrl, ready: '.close-reading', localeControl: '#ui-language',
      bodyText: '.source-block', action: '.decision'});
    stage = 'studio reduced-motion navigation';
    await page.evaluate(() => {window.__layoutScrollCalls.length = 0;});
    await page.locator('[data-stage="adjudication"]').first().click();
    const studioMotion = await page.evaluate(() => ({calls: window.__layoutScrollCalls,
      focused: document.activeElement?.matches('.finding-card'), reduced: matchMedia('(prefers-reduced-motion: reduce)').matches}));
    check('studio navigation respects reduced motion and focuses finding', studioMotion.reduced && studioMotion.focused &&
      studioMotion.calls.some(call => call.behavior === 'auto' || call.behavior === 'instant') &&
      studioMotion.calls.every(call => call.behavior !== 'smooth'), studioMotion);
    const sourceBefore = await page.locator('.source-block').allTextContents();
    const sourceLink = page.locator('.close-reading a').first(), fragment = await sourceLink.getAttribute('href');
    await page.locator('#workspace-search').fill('no-synthetic-source-matches');
    await sourceLink.focus();
    await sourceLink.press('Enter');
    check('keyboard evidence navigation restores filtered source and focuses it',
      await page.locator('#workspace-search').inputValue() === '' &&
      await page.locator(fragment).evaluate(node => document.activeElement === node), {fragment});
    assert.deepEqual(await page.locator('.source-block').allTextContents(), sourceBefore);

    await matrix({name: 'research-home', url: research.product, ready: '#create', localeControl: '#research-language',
      bodyText: '.research-description', action: '#create'});
    await matrix({name: 'research-revision', url: research.resume, ready: '#prepareAtomization', localeControl: '#research-language',
      bodyText: '.research-description', action: '#prepareAtomization'});
    await matrix({name: 'research-professional', url: research.professional, ready: '[data-decide]', localeControl: '#research-language',
      bodyText: '#claimDetail p', action: '[data-decide]'});
    stage = 'research reduced-motion navigation';
    await page.evaluate(() => {window.__layoutScrollCalls.length = 0;});
    await page.locator('[data-select="C1"]').click();
    const researchMotion = await page.evaluate(() => ({calls: window.__layoutScrollCalls,
      selected: selectedClaim, reduced: matchMedia('(prefers-reduced-motion: reduce)').matches}));
    check('research claim navigation respects reduced motion', researchMotion.reduced && researchMotion.selected === 'C1' &&
      researchMotion.calls.some(call => call.behavior === 'auto' || call.behavior === 'instant') &&
      researchMotion.calls.every(call => call.behavior !== 'smooth'), researchMotion);
    for (const locale of ['zh-Hant', 'en']) {
      await language(locale, '#research-language');
      await page.setViewportSize({width: 320, height: 844});
      await page.locator('[data-decide]').first().click();
      await page.locator('#decisionReason').fill('Synthetic author decision draft · 原文保留');
      await layout(`research-decision-${locale}-320`);
      await screenshot(`research-decision-${locale}-320`, {fullPage: false});
      await page.locator('#decisionDialog button[value="cancel"]').click();
      await page.locator('#historyButton').click();
      await layout(`research-history-${locale}-320`);
      await screenshot(`research-history-${locale}-320`, {fullPage: false});
      await page.locator('#historyDialog button').click();
    }
    check('no browser JavaScript errors or external requests', !report.errors.length && !report.externalRequests.length,
      {errors: report.errors, externalRequests: report.externalRequests});
    report.passed = report.failures.length === 0;
  } catch (error) {
    report.failure = {stage, message: error.message, stack: error.stack};
    if (page && !page.isClosed()) await page.screenshot({path: path.join(output, 'failure.png'), fullPage: true}).catch(() => {});
    throw error;
  } finally {
    await browser?.close();
    for (const record of servers) {
      const child = record.child;
      if (child.exitCode === null && child.signalCode === null) {
        const exited = new Promise(resolve => child.once('exit', resolve));
        child.kill();
        await Promise.race([exited, new Promise(resolve => setTimeout(resolve, 5000))]);
      }
    }
    report.fixtureStderr = servers.map(record => record.stderr).filter(Boolean);
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(report, null, 2));
    const resolved = await fs.realpath(temporary);
    assert.equal(path.dirname(resolved), await fs.realpath(os.tmpdir()));
    assert.ok(path.basename(resolved).startsWith('studio-layout-'));
    await fs.rm(resolved, {recursive: true, force: true});
  }
  console.log(JSON.stringify({passed: report.passed, checks: report.checks.length,
    screenshots: report.screenshots.length, failures: report.failures, output}, null, 2));
  assert.equal(report.passed, true, 'Layout checks failed; see result.json');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
