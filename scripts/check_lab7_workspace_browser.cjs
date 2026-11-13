/* Lab 7's existing KB form in the side workspace, using a disposable backend.
 * Only isolated KB save/sync operations are performed; no model requests. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {spawn}=require('node:child_process');
const {createInterface}=require('node:readline');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
const artifacts=process.env.PANOPTIS_BROWSER_ARTIFACTS||fs.mkdtempSync(path.join(os.tmpdir(),'lab7-workspace-'));

async function main(){
 fs.mkdirSync(artifacts,{recursive:true});
 const log=fs.openSync(path.join(artifacts,'fixture.log'),'w');
 const fixture=spawn(process.env.PANOPTIS_TEST_PYTHON||'python3',['-S','-B','tests/trace_timeline_browser_fixture.py'],
  {cwd:root,stdio:['ignore','pipe',log],env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
 let browser;const checks=[];const check=async(name,fn)=>{await fn();checks.push(name);console.log('PASS '+name);};
 try{
  const ready=await new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>reject(Error('Lab 7 fixture startup timed out')),10000);
   createInterface({input:fixture.stdout}).once('line',line=>{clearTimeout(timer);resolve(JSON.parse(line));});
   fixture.once('error',reject);
  });
  browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH,args:['--no-sandbox']}: {})});
  for(const [device,viewport] of [['desktop',{width:1440,height:900}],['phone',{width:390,height:844}],['landscape',{width:844,height:390}]]){
   const context=await browser.newContext({viewport,reducedMotion:'reduce'}),page=await context.newPage(),errors=[],posts=[];
   page.on('pageerror',error=>errors.push(error.message));
   page.on('request',request=>{if(request.method()==='POST')posts.push({path:new URL(request.url()).pathname,body:request.postDataJSON()});});
   await page.goto(ready.url+'/lab/7');await page.waitForFunction(()=>state.catalog.length===9&&state.current===7);
   const initial=(await(await context.request.get(ready.url+'/api/state')).json()).labs;
   const initialCount=initial['7'].documents.length;
   const navigate=async lab=>{await page.locator('#lab-select').selectOption(String(lab));await page.waitForFunction(id=>state.current===id,lab);};
   const post=async action=>{
    const pending=page.waitForResponse(r=>r.url().endsWith('/api/labs/7/kb'));
    await action();const response=await pending;assert.equal(response.status(),200);const body=await response.json();
    await page.waitForFunction(()=>!state.kbBusy.has(7));return body;
   };
   const inline=async()=>{
    assert.equal(await page.locator('#knowledge-panel').count(),1);
    assert.equal(await page.locator('#knowledge-panel').evaluate(e=>Boolean(e.closest('#workspace-content'))),true);
    assert.equal(await page.locator('#knowledge-dialog').evaluate(e=>e.open),false);
   };
   await check(device+': Lab 7 shows its existing KB controls in the side workspace',async()=>{
    await inline();assert.equal(await page.locator('#knowledge-panel').isVisible(),true);
    assert.equal(await page.locator('#document-list .document-row').count(),initialCount);
    assert.equal(await page.locator('#new-document').isVisible(),true);
    assert.equal(await page.locator('#sync-kb').isVisible(),true);
    assert.equal(posts.length,0);
   });
   await check(device+': inline edits remain unsaved and survive navigation through Labs 3 and 8',async()=>{
    await page.locator('#document-list .document-actions button').first().click();
    await page.locator('#document-title').fill('Lab 7 workspace draft');
    await page.locator('#document-content').fill('Public maintenance note for isolated workspace QA.');
    assert.equal(posts.length,0);
    await navigate(3);assert.equal(await page.locator('#knowledge-panel').evaluate(e=>Boolean(e.closest('#knowledge-dialog'))),true);
    await page.locator('#review-bulletin').click();assert.equal(await page.locator('#knowledge-dialog').evaluate(e=>e.open),true);
    await page.locator('[data-close="knowledge-dialog"]').click();
    await navigate(8);await page.locator('#knowledge-button').click();
    assert.equal(await page.locator('#knowledge-dialog').evaluate(e=>e.open),true);
    assert.equal(await page.locator('#knowledge-panel').count(),1);
    await page.locator('[data-close="knowledge-dialog"]').click();await navigate(7);await inline();
    assert.equal(await page.locator('#document-title').inputValue(),'Lab 7 workspace draft');
    assert.equal(await page.locator('#document-content').inputValue(),'Public maintenance note for isolated workspace QA.');
    const saved=(await(await context.request.get(ready.url+'/api/state')).json()).labs['7'];
    assert.deepEqual(saved.documents,initial['7'].documents);assert.equal(posts.length,0);
   });
   await check(device+': Save persists the document without synchronizing or enabling retrieval',async()=>{
    const result=await post(()=>page.locator('#save-document').click());
    assert.equal(result.lab.documents[0].title,'Lab 7 workspace draft');
    assert.equal(result.lab.documents[0].content,'Public maintenance note for isolated workspace QA.');
    assert(result.lab.kb_revision>result.lab.synced_revision);assert.equal(result.lab.use_kb,false);
    assert.equal(posts.at(-1).body.sync,false);assert.equal(posts.at(-1).body.use_kb,false);
    await inline();assert.equal(await page.locator('#document-editor').isVisible(),false);
    assert.match(await page.locator('#kb-notice').innerText(),/Document saved/);
   });
   let synced;
   await check(device+': Sync and enable activates the saved snapshot in place',async()=>{
    const result=await post(()=>page.locator('#sync-kb').click());synced=result.lab.synced_revision;
    assert.equal(result.lab.kb_revision,synced);assert.equal(result.lab.use_kb,true);
    assert.equal(posts.at(-1).body.sync,true);assert.equal(posts.at(-1).body.use_kb,true);
    assert.equal(await page.locator('#use-kb').isChecked(),true);await inline();
   });
   await check(device+': adding a document and refreshing preserves the saved and synced versions separately',async()=>{
    await page.locator('#new-document').click();
    await page.locator('#document-title').fill('Lab 7 extra workspace note');
    await page.locator('#document-content').fill('An additional public maintenance note.');
    const result=await post(()=>page.locator('#save-document').click());
    assert.equal(result.lab.documents.length,initialCount+1);assert.equal(result.lab.synced_revision,synced);
    assert(result.lab.kb_revision>synced);assert.equal(result.lab.use_kb,true);
    await page.reload();await page.waitForFunction(()=>state.current===7&&state.catalog.length===9);await inline();
    assert.equal(await page.locator('#document-list .document-row').count(),initialCount+1);
    assert.equal(await page.locator('#use-kb').isChecked(),true);
    assert.match(await page.locator('#kb-status').innerText(),/waiting to sync/i);
   });
   await check(device+': the chat shortcut focuses the side panel without page overflow',async()=>{
    await page.locator('#knowledge-button').click();await inline();
    assert.equal(await page.evaluate(()=>document.activeElement===document.querySelector('#knowledge-panel>summary')),true);
    const geometry=await page.evaluate(()=>({width:innerWidth,height:innerHeight,scrollWidth:document.scrollingElement.scrollWidth,
     scrollHeight:document.scrollingElement.scrollHeight,x:scrollX,y:scrollY}));
    assert(geometry.scrollWidth<=geometry.width+2&&geometry.scrollHeight<=geometry.height+2&&Math.abs(geometry.x)<1&&Math.abs(geometry.y)<1,JSON.stringify(geometry));
    await page.screenshot({path:path.join(artifacts,device+'-workspace.png')});
   });
   await check(device+': another student starts with independent Lab 7 documents',async()=>{
    const other=await browser.newContext({viewport});const fresh=await other.newPage();await fresh.goto(ready.url+'/lab/7');
    await fresh.waitForFunction(()=>state.current===7&&state.catalog.length===9);
    assert.equal(await fresh.locator('#document-list .document-row').count(),initialCount);
    assert.equal(await fresh.locator('#use-kb').isChecked(),false);
    assert(!(await fresh.locator('#document-list').innerText()).includes('Lab 7 workspace draft'));
    await other.close();
   });
   await check(device+': other labs and scoring remain unchanged with no chat or flag requests',async()=>{
    const after=(await(await context.request.get(ready.url+'/api/state')).json());
    for(const lab of ['2','3','4','5','6','8','9'])assert.deepEqual(after.labs[lab],initial[lab]);
    assert.equal(after.summary.score,0);assert(posts.every(r=>r.path==='/api/labs/7/kb'));
    assert.deepEqual(errors,[]);
   });
   await context.close();
  }
  const report={passed:checks.length,failed:0,checks,artifacts,scope:'Existing disposable HTTP fixture, isolated KB writes, no model calls.'};
  fs.writeFileSync(path.join(artifacts,'results.json'),JSON.stringify(report,null,2)+'\n');
  console.log('Lab 7 workspace browser checks passed: '+checks.length);
 }finally{if(browser)await browser.close();fixture.kill('SIGTERM');fs.closeSync(log);}
}
main().catch(error=>{console.error(error.stack);process.exitCode=1;});
