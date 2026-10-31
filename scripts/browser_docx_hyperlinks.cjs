// Real import -> source reader -> saved academic protocol, with inert targets.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

async function main() {
  const html = process.argv.includes('--html'), label = html ? 'html-hyperlinks' : 'docx-hyperlinks';
  const root = path.resolve(__dirname, '..'), output = path.join(root, 'dist/browser-' + label);
  await fs.mkdir(output, {recursive:true});
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), label + '-'));
  const child = spawn(process.env.STUDIO_PYTHON || 'python',
    [path.join(__dirname, 'browser_fixture_server.py'), temporary, '--' + label],
    {cwd:root, windowsHide:true, env:{...process.env, PYTHONUTF8:'1'}});
  const report = {passed:false, checks:[], errors:[], externalRequests:[]};
  let browser, stderr='';
  child.stderr.on('data', data=>{stderr+=data;});
  try {
    const url = await new Promise((resolve,reject)=>{
      let text='';
      const timer=setTimeout(()=>reject(Error('Fixture startup timeout: '+stderr)),30000);
      child.stdout.on('data',data=>{text+=data;const match=text.match(/http:\/\/127\.0\.0\.1:\d+\//);if(match){clearTimeout(timer);resolve(match[0]);}});
      child.once('error',error=>{clearTimeout(timer);reject(error);});
      child.once('exit',code=>{clearTimeout(timer);reject(Error('Fixture exit '+code+': '+stderr));});
    });
    browser=await chromium.launch({headless:true,...(process.env.STUDIO_BROWSER?{executablePath:process.env.STUDIO_BROWSER}:{})});
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('pageerror',error=>report.errors.push(error.message));
    await page.route('**/*',route=>{
      if(new URL(route.request().url()).origin===new URL(url).origin) return route.continue();
      report.externalRequests.push(route.request().url());return route.abort();
    });
    const action=async(selector,name)=>{
      const response=page.waitForResponse(r=>r.url().endsWith('/api/action')&&r.request().postDataJSON()?.action===name);
      await page.locator(selector).click();assert.equal((await response).status(),201);
      await page.waitForFunction(()=>!mutationPending);
    };
    await page.goto(url);
    await page.locator('[data-action="confirm-extraction"]').waitFor();
    const original=await page.evaluate(()=>({blocks:state.selected.extraction.blocks,directory:state.selected.directory,source:state.selected.project.source}));
    const sourcePath=path.join(temporary,original.directory,original.source.relative_path), source=await fs.readFile(sourcePath);
    await action('[data-action="confirm-extraction"]','confirm_extraction');
    await page.locator('#manuscript-reader > summary').click();
    const note=original.blocks.find(b=>html ? b.attrs.hyperlink_references?.some(r=>r.target.startsWith('javascript:')) : b.attrs.docx_story?.kind==='footnote');
    const unsafeTarget=note.attrs.hyperlink_references[0].target;
    for(const [locale,width,title] of [['zh-Hant',1440,'原件連結聲明'],['en',320,'Original link declarations']]) {
      await page.locator('#ui-language').selectOption(locale);
      await page.setViewportSize({width,height:844});
      const declarations=page.locator('#source-'+note.block_id+' .source-hyperlinks');
      assert.ok((await declarations.locator('summary').innerText()).includes(title));
      if(!await declarations.evaluate(el=>el.open)) await declarations.locator('summary').click();
      assert.ok((await declarations.innerText()).includes(unsafeTarget));
      assert.equal(await declarations.locator('a,img,script,iframe').count(),0);
      assert.equal(await page.locator('.source-text img').count(),0);
      await page.locator('#workspace-search').fill('javascript:alert');
      assert.equal(await page.locator('.source-block:not([hidden])').count(),1);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
      await page.locator('.manuscript-pane').screenshot({path:path.join(output,`${locale}-${width}-targets.png`)});
      await page.locator('#clear-source-search').click();
    }
    report.checks.push('Part-scoped targets remain searchable, literal and inert in Hant desktop and English 320px views');
    if (html) {
      assert.equal(await page.locator('.source-html-bases').count(),1);
      await page.locator('.source-html-bases > summary').click();
      assert.ok((await page.locator('.source-html-bases').innerText()).includes('https://example.org/articles/'));
      assert.equal(await page.locator('.source-html-bases a, .source-html-unassociated a').count(),0);
      await page.locator('.source-html-unassociated > summary').click();
      assert.ok((await page.locator('.source-html-unassociated').innerText()).includes('unexpanded'));
      assert.ok((await page.locator('.source-hyperlinks').allTextContents()).some(text=>text.includes('Empty target declaration (unresolved)')));
      assert.ok((await page.locator('.source-hyperlinks').allTextContents()).some(text=>text.includes('other-data')));
      report.checks.push('HTML bases are displayed once, unassociated and duplicate declarations remain visible, empty targets stay explicit and no final address is guessed');
    } else assert.ok((await page.locator('.source-hyperlinks').allTextContents()).some(text=>text.includes('Cached text does not establish')));
    await page.locator('#review_profile').selectOption('academic');
    await page.locator('#discipline').selectOption('humanities');
    await page.locator('#research_type').selectOption('theoretical');
    for(const[id,value]of Object.entries({jurisdiction:'unknown',effective_date:'unknown',publisher_type:'author',audience:'researchers'})) await page.locator('#'+id).fill(value);
    await action('#confirm-context','confirm_context');
    await action('#prepare-ai','prepare_ai_audits');
    for(const request of await page.evaluate(()=>state.selected.ai_requests)) {
      const contract=JSON.parse(request.prompt.split('```json\n')[1].split('\n```')[0]);
      const visibility = html ? contract.document_location_contract.html_hyperlink_visibility : contract.source_visibility.docx_hyperlink_visibility;
      assert.equal(visibility.reference_count,html ? 5 : 2);
      assert.equal(visibility.fetched,false);
      if (html) assert.equal(visibility.url_resolution,'not-performed');
      assert.ok(contract.document_location_contract.hyperlinks.includes('historical'));
      assert.ok(request.prompt.includes(unsafeTarget));
      assert.equal(await fs.readFile(path.join(temporary,original.directory,'ai-requests',request.request_id,'prompt.md'),'utf8'),request.prompt);
    }
    assert.deepEqual(await page.evaluate(()=>state.selected.extraction.blocks),original.blocks);
    assert.deepEqual(await fs.readFile(sourcePath),source);
    report.checks.push('Saved academic requests include declared targets and field/historical-source limits without changing source text or bytes');
    assert.deepEqual(report.errors,[]);assert.deepEqual(report.externalRequests,[]);
    report.browserVersion=browser.version();report.passed=true;
  } finally {
    if(browser) await browser.close();child.kill();
    if(child.exitCode===null&&child.signalCode===null) await new Promise(resolve=>child.once('exit',resolve));
    report.stderr=stderr;await fs.writeFile(path.join(output,'result.json'),JSON.stringify(report,null,2));
    const resolved=path.resolve(temporary), parent=path.resolve(os.tmpdir());
    assert.equal(path.dirname(resolved),parent);assert.ok(path.basename(resolved).startsWith(label+'-'));
    await fs.rm(resolved,{recursive:true,force:true});
  }
  console.log(JSON.stringify(report,null,2));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
