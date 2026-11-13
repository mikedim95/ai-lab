/* Shared bulletin workflow on disposable student identities; no model calls. */
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {spawn}=require('node:child_process'),{createInterface}=require('node:readline');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
const artifacts=process.env.PANOPTIS_BROWSER_ARTIFACTS||fs.mkdtempSync(path.join(os.tmpdir(),'bulletin-workspace-'));
async function main(){
 fs.mkdirSync(artifacts,{recursive:true});
 const log=fs.openSync(path.join(artifacts,'fixture.log'),'w');
 const fixture=spawn(process.env.PANOPTIS_TEST_PYTHON||'python3',['-S','-B','tests/trace_timeline_browser_fixture.py'],
  {cwd:root,stdio:['ignore','pipe',log],env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
 let browser;const checks=[];
 const check=async(name,fn)=>{await fn();checks.push(name);console.log('PASS '+name);};
 try{
  const ready=await new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>reject(Error('Bulletin fixture startup timed out')),10000);
   createInterface({input:fixture.stdout}).once('line',line=>{clearTimeout(timer);resolve(JSON.parse(line));});fixture.once('error',reject);
  });
  browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{}),args:['--no-sandbox']});
  for(const [device,viewport] of [['desktop',{width:1440,height:900}],['phone',{width:390,height:844}],['landscape',{width:844,height:390}]]){
   for(const lab of [3,7,8]){
    const context=await browser.newContext({viewport,reducedMotion:'reduce'}),page=await context.newPage(),errors=[],posts=[];
    page.on('pageerror',e=>errors.push(e.message));
    page.on('request',r=>{if(r.method()==='POST')posts.push({path:new URL(r.url()).pathname,body:r.postDataJSON()});});
    await page.goto(ready.url+'/lab/'+lab);await page.waitForFunction(id=>state.catalog.length===9&&state.current===id,lab);
    const initial=(await(await context.request.get(ready.url+'/api/state')).json());
    const label=device+' Lab '+lab;
    const open=()=>page.getByRole('button',{name:'View bulletin',exact:true}).click();
    const close=()=>page.locator('[data-close="knowledge-dialog"]').click();
    await check(label+': one shared workspace card, two existing posts and no knowledge controls',async()=>{
     assert.equal(await page.locator('#workspace-content article').count(),1);
     assert.equal(await page.locator('#workspace-content h3').innerText(),'Maintenance bulletin');
     assert.equal(await page.locator('#knowledge-button,#sync-kb,#use-kb,#kb-status,#kb-notice').count(),0);
     await open();
     assert.equal(await page.locator('#document-list article').count(),2);
     assert.equal(await page.locator('#document-editor').isVisible(),false);
     assert.equal(posts.length,0);
    });
    await check(label+': previous posts are readable without creating or changing documents',async()=>{
     await page.locator('#document-list button').first().click();
     assert.equal(await page.locator('#bulletin-reader-title').innerText(),initial.labs[lab].reference_documents[0].title);
     assert.equal(await page.locator('#bulletin-reader-content').textContent(),initial.labs[lab].reference_documents[0].content);
     assert.equal(await page.locator('#document-editor').isVisible(),false);
     await page.locator('#bulletin-back').click();assert.equal(posts.length,0);
    });
    await check(label+': New Document has empty fields and cancelled drafts do not alter posts',async()=>{
     await page.locator('#new-document').click();
     assert.equal(await page.locator('#document-title').inputValue(),'');assert.equal(await page.locator('#document-content').inputValue(),'');
     await page.locator('#document-title').fill('Discarded local draft');await page.locator('#cancel-document').click();
     await page.locator('#new-document').click();
     assert.equal(await page.locator('#document-title').inputValue(),'');assert.equal(await page.locator('#document-content').inputValue(),'');
     assert.equal(posts.length,0);
    });
    const title='Public bulletin for lab '+lab,body='A public isolated inspection note for this student.';
    await page.locator('#document-title').fill(title);await page.locator('#document-content').fill(body);
    await check(label+': failed Save keeps the modal, fields and original saved state',async()=>{
     await page.route('**/api/labs/'+lab+'/kb',route=>route.fulfill({status:500,contentType:'application/json',body:JSON.stringify({error:'Fixture save failure'})}),{times:1});
     await page.locator('#save-document').click();await page.waitForFunction(()=>!state.kbBusy.has(state.current));
     await page.locator('#document-error').waitFor({state:'visible'});
     assert.equal(await page.locator('#knowledge-dialog').evaluate(e=>e.open),true);
     assert.equal(await page.locator('#document-title').inputValue(),title);assert.equal(await page.locator('#document-content').inputValue(),body);
     const saved=await(await context.request.get(ready.url+'/api/state')).json();
     assert.deepEqual(saved.labs[lab],initial.labs[lab]);
    });
    const beforeSave=posts.length;
    await check(label+': final Save writes, syncs and enables the third post once, then closes',async()=>{
     const pending=page.waitForResponse(r=>r.url().endsWith('/api/labs/'+lab+'/kb'));
     await page.locator('#save-document').click();const response=await pending;
     assert.equal(response.status(),200);const result=await response.json();
     await page.waitForFunction(()=>!document.getElementById('knowledge-dialog').open);
     assert.equal(posts.length,beforeSave+1);assert.equal(posts.at(-1).body.sync,true);assert.equal(posts.at(-1).body.use_kb,true);
     assert.equal(result.lab.documents.length,3);assert.equal(result.lab.use_kb,true);assert.equal(result.lab.kb_revision,result.lab.synced_revision);
     assert(result.lab.documents.some(d=>d.title===title&&d.content===body&&d.is_user_injected===true));
     assert.equal(result.summary.score,0);
    });
    await check(label+': refresh keeps the third post and a fresh student retains only the two references',async()=>{
     await page.reload();await page.waitForFunction(id=>state.current===id&&state.catalog.length===9,lab);await open();
     assert.equal(await page.locator('#document-list article').count(),3);
     assert((await page.locator('#document-list').innerText()).includes(title));await close();
     const other=await browser.newContext({viewport}),fresh=await other.newPage();await fresh.goto(ready.url+'/lab/'+lab);
     await fresh.waitForFunction(()=>state.catalog.length===9);await fresh.getByRole('button',{name:'View bulletin',exact:true}).click();
     assert.equal(await fresh.locator('#document-list article').count(),2);
     assert(!(await fresh.locator('#document-list').innerText()).includes(title));await other.close();
    });
    await check(label+': modal stays within the viewport and its close button remains visible',async()=>{
     await open();await page.locator('#new-document').click();
     await page.locator('#document-content').fill(('Public note. ').repeat(250));
     await page.locator('#save-document').scrollIntoViewIfNeeded();
     const box=await page.locator('#knowledge-dialog').boundingBox();
     assert(box.x>=0&&box.y>=0&&box.x+box.width<=viewport.width+1&&box.y+box.height<=viewport.height+1,JSON.stringify(box));
     assert.equal(await page.locator('[data-close="knowledge-dialog"]').isVisible(),true);
     const overflow=await page.locator('#knowledge-dialog').evaluate(e=>e.scrollWidth>e.clientWidth+1);assert.equal(overflow,false);
     await page.screenshot({path:path.join(artifacts,device+'-lab'+lab+'-editor.png')});await close();
    });
    await check(label+': only the objective and hint controls remain on the left, without model or scoring calls',async()=>{
     assert((await page.locator('#scenario-objective').innerText()).length>20);
     assert.equal(await page.locator('#scenario-summary,#scenario-surfaces,#scenario-workflow,#scenario-objective-rules,#scenario-panel .extraction-progress').count(),0);
     assert.equal(await page.locator('#hint-button').isVisible(),true);
     const saved=await(await context.request.get(ready.url+'/api/state')).json();
     for(const id of Object.keys(initial.labs))if(Number(id)!==lab){
      if(id==='1'){
       // Normal polling advances simulated telemetry; no control write is allowed.
       for(const key of ['messages','completed','exploited','flag','defense_level','boiler_tool_calls'])assert.deepEqual(saved.labs[id][key],initial.labs[id][key]);
       for(const key of ['water','fuel'])assert.equal(saved.labs[id].boiler[key],initial.labs[id].boiler[key]);
      }else assert.deepEqual(saved.labs[id],initial.labs[id]);
     }
     assert.equal(saved.summary.score,0);assert(posts.every(r=>r.path==='/api/labs/'+lab+'/kb'));assert.deepEqual(errors,[]);
    });
    await context.close();
   }
  }
  const report={passed:checks.length,failed:0,checks,artifacts,scope:'Disposable HTTP fixture, isolated bulletin writes, no model calls.'};
  fs.writeFileSync(path.join(artifacts,'results.json'),JSON.stringify(report,null,2)+'\n');
  console.log('Shared bulletin browser checks passed: '+checks.length);
 }finally{if(browser)await browser.close();fixture.kill('SIGTERM');fs.closeSync(log);}
}
main().catch(e=>{console.error(e.stack);process.exitCode=1;});
