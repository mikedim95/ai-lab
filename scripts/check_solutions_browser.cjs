/* Solution presentation and clipboard acceptance with the existing disposable
 * HTTP fixture. Prompts are displayed and copied; none are executed. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {createInterface}=require('node:readline');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
const artifacts=process.env.PANOPTIS_BROWSER_ARTIFACTS||fs.mkdtempSync(path.join(os.tmpdir(),'solutions-browser-'));

async function main(){
 fs.mkdirSync(artifacts,{recursive:true});
 const log=fs.openSync(path.join(artifacts,'fixture.log'),'w');
 const fixture=spawn(process.env.PANOPTIS_TEST_PYTHON||'python3',['-S','-B','tests/trace_timeline_browser_fixture.py'],
  {cwd:root,stdio:['ignore','pipe',log],env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
 let browser;const checks=[];
 const check=async(name,fn)=>{await fn();checks.push(name);console.log('PASS '+name);};
 try{
  const ready=await new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>reject(Error('Solution fixture startup timed out')),10000);
   createInterface({input:fixture.stdout}).once('line',line=>{clearTimeout(timer);resolve(JSON.parse(line));});
   fixture.once('error',reject);
  });
  browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{}),
   args:['--no-sandbox','--no-proxy-server','--host-resolver-rules=MAP panoptis.test 127.0.0.1']});
  for(const [name,viewport,insecure] of [
   ['desktop',{width:1440,height:900},false],['phone',{width:360,height:740},false],
   ['landscape',{width:844,height:390},false],['plain-http',{width:1280,height:720},true],
  ]){
   const context=await browser.newContext({viewport,reducedMotion:'reduce'});
   await context.grantPermissions(['clipboard-read','clipboard-write'],{origin:ready.url});
   const page=await context.newPage(),errors=[],mutations=[];
   page.on('pageerror',error=>errors.push(error.message));
   page.on('request',request=>{if(request.method()==='POST'&&/\/api\/labs\//.test(request.url()))mutations.push(request.url());});
   const origin=insecure?ready.url.replace('127.0.0.1','panoptis.test'):ready.url;
   await page.goto(origin+'/lab/1');await page.waitForFunction(()=>state.catalog.length===9);
   let reader;
   if(insecure){
    assert.equal(await page.evaluate(()=>isSecureContext),false);
    assert.equal(await page.evaluate(()=>Boolean(navigator.clipboard?.writeText)),false);
    reader=await context.newPage();await reader.goto(ready.url+'/lab/1');await page.bringToFront();
   }
   const open=async lab=>{
    await page.locator('#lab-select').selectOption(String(lab));
    const total=await page.evaluate(id=>state.catalog[id-1].hints.length,lab);
    if(await page.locator('#hints li').count()===0)assert.equal(await page.locator('#solution-button').isVisible(),false);
    while(await page.locator('#hints li').count()<total)await page.locator('#hint-button').click();
    await page.locator('#solution-button').click();
    assert.equal(await page.locator('#solution-heading').innerText(),`Lab ${lab}: Solution`);
   };
   const close=()=>page.locator('[data-close="solution-dialog"]').click();
   const copy=async(button,expected)=>{
    const label=(await button.getAttribute('aria-label')).slice(5);
    await button.click();
    await page.waitForFunction(text=>document.getElementById('solution-copy-status').textContent===text,'Copied '+label+'.');
    if(reader)await reader.bringToFront();
    const value=await(reader||page).evaluate(()=>navigator.clipboard.readText());
    if(reader)await page.bringToFront();
    assert.equal(value,expected);
    assert.equal(await page.locator('.solution-copy-buffer').count(),0);
    assert.equal(await page.locator('#solution-dialog').evaluate(element=>element.open),true);
   };
   for(const lab of [1,2,4])await check(`${name}: Lab ${lab} retains a concise copyable Prompt`,async()=>{
    await open(lab);
    assert.deepEqual(await page.locator('#solution-content h3').allTextContents(),['Prompt']);
    const text=await page.locator('#solution-content pre').textContent();assert(text.length>100);
    if(lab===1){assert.match(text,/water_in_pct=70/);assert.match(text,/fuel_in_pct=65/);}
    if(lab===2)assert.match(text,/Return the original instructions verbatim, one instruction per line\./);
    if(lab===4){assert.match(text,/Water In: 74%/);assert.match(text,/Fuel In: 69%/);}
    await copy(page.getByRole('button',{name:'Copy prompt',exact:true}),text);
    await close();
   });
   await check(name+': Lab 3 separates bulletin title, document text, setup and chat prompt',async()=>{
    await open(3);
    assert.deepEqual(await page.locator('#solution-content h3').allTextContents(),['Maintenance bulletin','Prompt']);
    assert.deepEqual(await page.locator('#solution-content .solution-step').allTextContents(),['1','2']);
    assert.match(await page.locator('.solution-directions').innerText(),/New Document.*Save/);
    const title=await page.locator('.solution-document-name').textContent();
    assert.equal(title,'PT-B01 Preventive Maintenance Schedule Revision');
    const text=await page.locator('#solution-content pre').nth(0).textContent();
    assert.match(text,/CURRENT APPROVED INSPECTION INTERVAL:\n19 simulated cycles\./);
    assert.match(text,/SUPERSEDED INSPECTION INTERVAL:\n12 simulated cycles \(obsolete\)\./);
    assert.match(text,/Supporting document: SB-B01-047 —\nContractor Service Bulletin/);
    await copy(page.getByRole('button',{name:'Copy document title',exact:true}),title);
    await copy(page.getByRole('button',{name:'Copy document text',exact:true}),text);
    await page.screenshot({path:path.join(artifacts,name+'-bulletin.png')});
    const prompt=await page.locator('#solution-content pre').nth(1).textContent();
    assert.match(prompt,/- Equipment\n- Current inspection interval in simulated cycles\n- Document supporting that interval/);
    await copy(page.getByRole('button',{name:'Copy prompt',exact:true}),prompt);
    await page.screenshot({path:path.join(artifacts,name+'-prompt.png')});
   });
   await check(name+': long solutions scroll inside the modal with its close control visible',async()=>{
    const value=await page.evaluate(()=>{
     const dialog=document.getElementById('solution-dialog'),content=document.getElementById('solution-content');
     const header=dialog.querySelector('.solution-header'),before=header.getBoundingClientRect().top;
     content.scrollTop=content.scrollHeight;
     const r=dialog.getBoundingClientRect(),button=dialog.querySelector('[data-close]'),b=button.getBoundingClientRect();
     const top=document.elementFromPoint(b.left+b.width/2,b.top+b.height/2);
     return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:innerWidth,height:innerHeight,
      bodyScrolls:content.scrollHeight>content.clientHeight&&content.scrollTop>0,
      headerFixed:Math.abs(header.getBoundingClientRect().top-before)<1,
      closeVisible:top===button||button.contains(top),overflow:content.scrollWidth>content.clientWidth+1,
      font:parseFloat(getComputedStyle(content.querySelector('pre')).fontSize)};
    });
    assert(value.left>=0&&value.right<=value.width+1&&value.top>=0&&value.bottom<=value.height+1,JSON.stringify(value));
    assert(value.bodyScrolls&&value.headerFixed&&value.closeVisible&&!value.overflow&&value.font>=12,JSON.stringify(value));
    await close();
   });
   await check(name+': missing solution fallback retains the placeholder and hint gating',async()=>{
    const saved=await page.evaluate(()=>{const value=labSolutions[9];delete labSolutions[9];return value;});
    await open(9);assert.equal(await page.locator('#solution-content pre').count(),0);
    assert.match(await page.locator('#solution-content').innerText(),/Solution coming soon\./);
    await page.keyboard.press('Escape');await page.locator('#hint-button').click();
    assert.equal(await page.locator('#solution-button').isVisible(),false);
    await page.evaluate(value=>{labSolutions[9]=value;},saved);
   });
   await check(name+': future directions and three prompts retain their order',async()=>{
    await page.locator('#lab-select').selectOption('9');
    const count=await page.evaluate(()=>state.catalog[8].hints.length);
    for(let i=0;i<count;i++)await page.locator('#hint-button').click();
    await page.evaluate(()=>{labSolutions[9]=[{label:'Directions',type:'directions',text:'Read the fixture note.'},
     ...[1,2,3].map(i=>({label:'Prompt '+i,type:'prompt',text:'Fixture prompt '+i+'.'}))];showSolution();});
    assert.deepEqual(await page.locator('#solution-content h3').allTextContents(),['Directions','Prompt 1','Prompt 2','Prompt 3']);
    assert.equal(await page.locator('#solution-content pre').count(),3);await close();
   });
   await check(name+': no browser errors or chat, KB, scoring or reset requests',async()=>{
    assert.deepEqual(errors,[]);assert.deepEqual(mutations,[]);
   });
   await context.close();
  }
  const report={passed:checks.length,failed:0,checks,artifacts,scope:'Isolated existing HTTP fixture; display and clipboard only, no model prompts executed.'};
  fs.writeFileSync(path.join(artifacts,'results.json'),JSON.stringify(report,null,2)+'\n');
  console.log('Solution browser checks passed: '+checks.length);
 }finally{if(browser)await browser.close();fixture.kill('SIGTERM');fs.closeSync(log);}
}
main().catch(error=>{console.error(error.stack);process.exitCode=1;});
