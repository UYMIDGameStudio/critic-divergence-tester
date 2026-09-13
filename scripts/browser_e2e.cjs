// Real browser regression: upload -> review -> approval -> export -> restore.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const { spawn } = require('node:child_process');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
async function main(){
  const root=path.resolve(__dirname,'..'), temp=await fs.mkdtemp(path.join(os.tmpdir(),'studio-browser-'));
  const server=spawn(process.env.STUDIO_PYTHON||'python',[path.join(__dirname,'browser_fixture_server.py'),path.join(temp,'library')],{cwd:root,windowsHide:true,env:{...process.env,PYTHONUTF8:'1'}});
  let browser, stderr='';server.stderr.on('data',chunk=>stderr+=chunk.toString());
  try{
    const url=await new Promise((resolve,reject)=>{let output='';const timer=setTimeout(()=>reject(Error('Server startup timeout: '+stderr)),30000);server.stdout.on('data',chunk=>{output+=chunk;const match=output.match(/http:\/\/127\.0\.0\.1:\d+\//);if(match){clearTimeout(timer);resolve(match[0]);}});server.on('exit',code=>{clearTimeout(timer);reject(Error('Server exited '+code+': '+stderr));});});
    browser=await chromium.launch({headless:true,...(process.env.STUDIO_BROWSER?{executablePath:process.env.STUDIO_BROWSER}:{})});
    const page=await browser.newPage({viewport:{width:1440,height:1000},acceptDownloads:true});
    page.setDefaultTimeout(30000);const errors=[];page.on('pageerror',error=>errors.push(error.message));page.on('dialog',dialog=>dialog.accept());
    await page.goto(url);await page.locator('#file').waitFor();
    await page.locator('#file').setInputFiles({name:'浏览器验收.md',mimeType:'text/markdown',buffer:Buffer.from('# 活动方案\n\n相关人员应当及时完成报名。\n\n活动目的：说明报名的安排。\n')});
    await page.locator('#upload').click();await page.locator('[data-action="confirm-extraction"]').waitFor();
    async function action(locator,name){const [response]=await Promise.all([page.waitForResponse(r=>r.url().endsWith('/api/action')&&r.request().postDataJSON()?.action===name),locator.click()]);assert.equal(response.status(),201,await response.text());await page.waitForFunction(()=>!document.querySelector('main')?.classList.contains('busy')&&!document.getElementById('app')?.classList.contains('busy')&&!mutationPending);}
    await action(page.locator('[data-action="confirm-extraction"]'),'confirm_extraction');
    const draftSaved=page.waitForResponse(r=>r.url().endsWith('/api/draft'));
    await page.locator('#jurisdiction').fill('unknown');await draftSaved;await page.reload();await page.locator('#jurisdiction').waitFor();assert.equal(await page.locator('#jurisdiction').inputValue(),'unknown');
    for(const [id,value] of Object.entries({effective_date:'unknown',publisher_type:'作者',audience:'参与者'}))await page.locator('#'+id).fill(value);
    await action(page.locator('#confirm-context'),'confirm_context');
    const checks=page.locator('.critic');await checks.evaluateAll(nodes=>nodes.forEach(n=>n.checked=n.value==='expression_ambiguity'));
    await action(page.locator('#run-precheck'),'run_local_prechecks');
    const ids=await page.locator('.decision[data-value="accept"]').evaluateAll(nodes=>nodes.map(n=>n.dataset.id));assert.ok(ids.length>0);
    for(let i=0;i<ids.length;i++){const id=ids[i];await page.locator('#reason-'+id).fill('已阅读原文，按本轮修订范围处理');await action(page.locator(`.decision[data-id="${id}"][data-value="${i===0?'accept':'reject'}"]`),'decide_finding');}
    await action(page.locator('#prepare-bridge'),'prepare_bridge');
    const op=page.locator('.confirm-operation').first(), id=await op.getAttribute('data-action-id');
    await page.locator('#operation-'+id).selectOption('replace_block');await page.locator('#operation-reason-'+id).fill('明确负责人和期限');await action(op,'set_revision_action_operation');
    await page.locator('#revision-'+id).fill('项目负责人于2026年9月30日前完成报名。');await page.locator('#revision-reason-'+id).fill('明确主体、事项和截止日期');await action(page.locator('.propose-hunk').first(),'propose_revision_hunk');
    const approve=page.locator('.decide-hunk[data-decision="approve"]').first(), hunk=await approve.getAttribute('data-hunk-id');await page.locator('#hunk-reason-'+hunk).fill('逐字检查后批准');await action(approve,'decide_revision_hunk');
    await action(page.locator('#finalize-revision'),'finalize_revision');await action(page.locator('#export-final'),'export');
    const word=page.locator('.download');assert.ok(await word.count()>0);
    const downloadPromise=page.waitForEvent('download');await page.locator('.download[data-path$="修改稿.docx"]').click();const download=await downloadPromise;await download.saveAs(path.join(temp,'result.docx'));assert.ok((await fs.stat(path.join(temp,'result.docx'))).size>100);
    await action(page.locator('#create-backup'),'create_backup');
    const backups=await fs.readdir(path.join(temp,'library','backups'));assert.equal(backups.length,1);
    await action(page.locator('#back'),'close_project');await page.locator('#backup-file').setInputFiles(path.join(temp,'library','backups',backups[0]));await action(page.locator('#restore-backup'),'restore_backup');
    assert.match(await page.locator('body').textContent(),/恢复为独立项目/);assert.deepEqual(errors,[]);
    const out=process.env.STUDIO_E2E_OUTPUT||path.join(root,'dist','browser-verification');await fs.mkdir(out,{recursive:true});await page.screenshot({path:path.join(out,'completed.png'),fullPage:true});await fs.writeFile(path.join(out,'result.json'),JSON.stringify({passed:true,flow:['upload','extraction','draft-persist-reload','context','precheck','decisions','operation','hunk','approval','revision','export-download','backup','restore'],browserErrors:errors},null,2));
    console.log('Browser end-to-end passed: upload, draft recovery, revision, Word download, backup and restore.');
  }catch(error){if(stderr)console.error(stderr);throw error;}finally{if(browser)await browser.close();server.kill();await new Promise(resolve=>server.exitCode!==null?resolve():server.once('exit',resolve));await fs.rm(temp,{recursive:true,force:true});}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
