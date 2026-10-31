// Real imported notes, current-source navigation and saved academic requests.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

async function main() {
  const editLinks = process.argv.includes('--edit-links'), label = editLinks ? 'word-hyperlink-edits' : 'docx-notes';
  const root = path.resolve(__dirname, '..'), output = path.join(root, 'dist/browser-' + label);
  await fs.mkdir(output, {recursive: true});
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), label+'-'));
  const child = spawn(process.env.STUDIO_PYTHON || 'python',
    [path.join(__dirname, 'browser_fixture_server.py'), temporary, '--'+label],
    {cwd: root, windowsHide: true, env: {...process.env, PYTHONUTF8:'1'}});
  const report = {passed:false, checks:[], errors:[], externalRequests:[]};
  let browser, stderr='';
  child.stderr.on('data', data => {stderr+=data;});
  try {
    const url = await new Promise((resolve,reject) => {
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
    assert.equal(original.blocks.length,3);
    const body=original.blocks[0], foot=original.blocks.find(b=>b.attrs.docx_story?.kind==='footnote');
    assert.ok(foot.text.includes('Deutsch Straße Français été 日本語 Русский Latīna'));
    for(const [locale,width,label] of [['zh-Hant',1440,'腳註 · 來源標識 7'],['en',320,'Footnote · Source ID 7']]) {
      await page.locator('#ui-language').selectOption(locale);await page.setViewportSize({width,height:844});
      assert.ok((await page.locator('#source-preview').innerText()).includes(label));
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
      await page.screenshot({path:path.join(output,`${locale}-${width}-extraction.png`),fullPage:true});
    }
    report.checks.push('Eight-language note text and source IDs remain distinct from body text in both UI languages');
    await action('[data-action="confirm-extraction"]','confirm_extraction');
    await page.locator('#manuscript-reader > summary').click();
    const passage=page.locator('#source-'+body.block_id), note=page.locator('#source-'+foot.block_id);
    await page.locator('#workspace-search').fill(editLinks ? body.text : 'Bounded claim');
    assert.equal(await note.evaluate(el=>el.hidden),true);
    const noteLink=passage.locator('.source-note-links a').first();
    await noteLink.focus();await page.keyboard.press('Enter');
    await page.waitForFunction(id=>document.activeElement?.id==='source-'+id,foot.block_id);
    assert.equal(await page.locator('#workspace-search').inputValue(),'');
    assert.equal(await note.evaluate(el=>el.hidden),false);
    const geometry=await note.evaluate(el=>({target:el.getBoundingClientRect().top,toolbar:el.closest('.manuscript-pane').querySelector('.source-reader-toolbar').getBoundingClientRect().bottom}));
    assert.ok(geometry.target>=geometry.toolbar,JSON.stringify(geometry));
    await note.locator('.source-note-links a').first().click();
    assert.equal(await page.evaluate(()=>document.activeElement.id),'source-'+body.block_id);
    await page.locator('.source-notes > summary').click();
    const hrefs=await page.locator('.source-notes a').evaluateAll(rows=>rows.map(a=>a.getAttribute('href')));
    assert.equal(new Set(hrefs).size,2);
    await page.locator('.source-notes a').first().click();
    report.checks.push('Keyboard source-to-note and note-to-source links reveal filtered targets below the toolbar; same numeric footnote/endnote IDs remain separate');
    for(const [locale,width] of [['en',320],['zh-Hant',1440]]) {
      await page.locator('#ui-language').selectOption(locale);await page.setViewportSize({width,height:844});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
      await page.locator('.manuscript-pane').screenshot({path:path.join(output,`${locale}-${width}-reader.png`)});
    }
    await page.locator('#review_profile').selectOption(editLinks ? 'document' : 'academic');
    await page.locator('#discipline').selectOption('humanities');
    await page.locator('#research_type').selectOption('theoretical');
    for(const[id,value]of Object.entries({jurisdiction:'unknown',effective_date:'unknown',publisher_type:'author',audience:'researchers'})) await page.locator('#'+id).fill(value);
    await action('#confirm-context','confirm_context');
    await action('#prepare-ai','prepare_ai_audits');
    const requests=await page.evaluate(()=>state.selected.ai_requests);
    for(const request of requests) {
      assert.ok(request.prompt.includes(foot.text));assert.ok(request.prompt.includes('This claim excludes other settings.'));
      const contract=JSON.parse(request.prompt.split('```json\n')[1].split('\n```')[0]);
      assert.equal(contract.source_visibility.note_visibility.footnote.content_extracted,true);
      assert.ok(contract.document_location_contract.note_stories.includes('not rendered note numbers'));
      assert.equal(await fs.readFile(path.join(temporary,original.directory,'ai-requests',request.request_id,'prompt.md'),'utf8'),request.prompt);
    }
    assert.deepEqual(await page.evaluate(()=>state.selected.extraction.blocks),original.blocks);
    assert.deepEqual(await fs.readFile(sourcePath),source);
    report.checks.push('Saved independent protocols receive note context and association rules; source bytes and extracted blocks remain unchanged');
    if(editLinks) {
      await page.locator('.critic').evaluateAll(nodes=>nodes.forEach(node=>{node.checked=node.value==='expression_ambiguity';}));
      await action('#run-precheck','run_local_prechecks');
      const findings=await page.evaluate(()=>state.selected.findings);
      const target=findings.find(f=>f.location.block_id===body.block_id);
      assert.ok(target);
      for(const finding of findings) {
        await page.locator('#reason-'+finding.finding_id).fill('Owned link-edit scope');
        await action(`.decision[data-id="${finding.finding_id}"][data-value="${finding.finding_id===target.finding_id?'accept':'reject'}"]`,'decide_finding');
      }
      await action('#prepare-bridge','prepare_bridge');
      const id=await page.locator('.confirm-operation').first().getAttribute('data-action-id');
      await page.locator('#operation-'+id).selectOption('replace_block');
      await page.locator('#operation-reason-'+id).fill('Explicit fixture operation');
      await action(`.confirm-operation[data-action-id="${id}"]`,'set_revision_action_operation');
      await page.locator('#revision-'+id).fill(body.text.replace('及时','周五前'));
      await page.locator('#revision-reason-'+id).fill('Clarify deadline, keep citation');
      await page.locator('#revision-provenance-'+id).selectOption('human-authored');
      await action(`.propose-hunk[data-action-id="${id}"]`,'propose_revision_hunk');
      const hunk=await page.locator('.decide-hunk[data-decision="approve"]').getAttribute('data-hunk-id');
      await page.locator('#hunk-reason-'+hunk).fill('Explicit fixture approval');
      await action('.decide-hunk[data-decision="approve"]','decide_revision_hunk');
      await action('#finalize-revision','finalize_revision');
      await action('#export-final','export');
      const exportId=await page.evaluate(()=>state.selected.exports.at(-1).export_id);
      const directory=path.join(temporary,original.directory,'exports',exportId);
      const capability=JSON.parse(await fs.readFile(path.join(directory,'track-changes-capability.json'),'utf8'));
      assert.equal(capability.source_layout_preserved,true);assert.equal(capability.native_track_changes,true);
      assert.equal(capability.changed_blocks_with_hyperlinks,1);
      assert.ok((await fs.readFile(path.join(directory,'Word修订标记.docx'))).length>100);
      assert.ok((await fs.readFile(path.join(directory,capability.output_name))).length>100);
      assert.deepEqual(await fs.readFile(sourcePath),source);
      report.checks.push('An explicitly approved linked paragraph edit exports preserved-layout clean and native revision files, keeps citation metadata and leaves the original intact');
    }
    assert.deepEqual(report.errors,[]);assert.deepEqual(report.externalRequests,[]);
    report.browserVersion=browser.version();report.passed=true;
  } finally {
    if(browser) await browser.close();child.kill();
    if(child.exitCode===null&&child.signalCode===null) await new Promise(resolve=>child.once('exit',resolve));
    report.stderr=stderr;await fs.writeFile(path.join(output,'result.json'),JSON.stringify(report,null,2));
    const resolved=path.resolve(temporary);assert.equal(path.dirname(resolved),path.resolve(os.tmpdir()));assert.ok(path.basename(resolved).startsWith(label+'-'));
    await fs.rm(resolved,{recursive:true,force:true});
  }
  console.log(JSON.stringify(report,null,2));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
