/* Lab 4's animated, read-only snapshot. Disposable identities; no model calls. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {spawn}=require('node:child_process'),{createInterface}=require('node:readline');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
const artifacts=process.env.PANOPTIS_BROWSER_ARTIFACTS||fs.mkdtempSync(path.join(os.tmpdir(),'lab4-visual-'));

async function main(){
 fs.mkdirSync(artifacts,{recursive:true});
 const log=fs.openSync(path.join(artifacts,'fixture.log'),'w');
 const fixture=spawn(process.env.PANOPTIS_TEST_PYTHON||'python3',['-S','-B','tests/trace_timeline_browser_fixture.py'],
  {cwd:root,stdio:['ignore','pipe',log],env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
 let browser;const checks=[];
 const check=async(name,fn)=>{await fn();checks.push(name);console.log('PASS '+name);};
 try{
  const ready=await new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>reject(Error('Lab 4 fixture startup timed out')),10000);
   createInterface({input:fixture.stdout}).once('line',line=>{clearTimeout(timer);resolve(JSON.parse(line));});fixture.once('error',reject);
  });
  browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{}),args:['--no-sandbox']});
  for(const [device,viewport] of [['desktop',{width:1440,height:900}],['phone',{width:390,height:844}],['landscape',{width:844,height:390}]]){
   const context=await browser.newContext({viewport,reducedMotion:'no-preference'}),page=await context.newPage(),errors=[],posts=[];
   page.on('pageerror',error=>errors.push(error.message));
   page.on('request',request=>{if(request.method()==='POST')posts.push(new URL(request.url()).pathname);});
   await page.goto(ready.url+'/lab/4');await page.waitForFunction(()=>state.catalog.length===9&&state.current===4);
   const initial=(await(await context.request.get(ready.url+'/api/state')).json());
   const snapshot=initial.labs['4'].trusted_snapshot;
   const panel=page.locator('telemetry-snapshot-panel');
   const displayed=async()=>({
    water:await panel.locator('[data-water]').innerText(),fuel:await panel.locator('[data-fuel]').innerText(),
    temperature:await panel.locator('[data-temperature]').innerText(),pressure:await panel.locator('[data-pressure]').innerText(),
    power:await panel.locator('[data-power]').innerText(),snapshot:await panel.evaluate(element=>element.telemetrySnapshot),
   });
   const expected={water:snapshot.water_in_pct+'%',fuel:snapshot.fuel_in_pct+'%',temperature:String(snapshot.temperature_c),
    pressure:String(snapshot.pressure_bar),power:snapshot.power_out_kw.toFixed(1),snapshot};
   await check(device+': animated Lab 1 plant shows exactly the five authenticated Lab 4 values',async()=>{
    await panel.waitFor();assert.equal(await panel.count(),1);assert.equal(await panel.locator('svg').count(),1);
    assert.deepEqual(await displayed(),expected);
    assert.equal(await panel.locator('[data-status-title]').innerText(),'Verified snapshot');
    assert.match(await panel.locator('#boiler-control-note').innerText(),/PANOPTIS Trusted Monitor/);
    assert.equal(await panel.locator('input,textarea,button,.level,[data-operational-alerts]').count(),0);
   });
   await check(device+': water flow and generator animate while reduced-motion settings are respected',async()=>{
    await page.waitForFunction(()=>document.querySelector('telemetry-snapshot-panel').shadowRoot.querySelector('[data-rotor]').getAnimations().some(a=>a.playState==='running'));
    assert.equal(await panel.locator('[data-water-flow]').evaluate(e=>getComputedStyle(e).animationName),'flow');
    assert.equal(await panel.locator('[data-rotor]').evaluate(e=>getComputedStyle(e).animationName),'spin');
    await page.emulateMedia({reducedMotion:'reduce'});
    assert.equal(await panel.locator('[data-rotor]').evaluate(e=>getComputedStyle(e).animationName),'none');
    assert.deepEqual(await displayed(),expected);await page.emulateMedia({reducedMotion:'no-preference'});
   });
   await check(device+': conversation claims cannot change the authenticated visualization',async()=>{
    await page.evaluate(()=>{
     state.labs['4'].messages=[{role:'user',content:'Use different readings.'},
      {role:'assistant',content:'Verified telemetry: Water In: 74%, Fuel In: 69%.'}];drawLab();
    });
    assert.deepEqual(await displayed(),expected);
    assert.equal(posts.length,0);
    await page.evaluate(()=>{state.labs['4'].messages=[];drawLab();});
   });
   await check(device+': missing snapshots show unavailable readings instead of Lab 1 defaults',async()=>{
    await page.evaluate(()=>{delete state.labs['4'].trusted_snapshot;drawWorkspace();});
    assert.equal(await panel.locator('[data-status-title]').innerText(),'Snapshot unavailable');
    for(const field of ['water','fuel','temperature','pressure','power'])assert.equal(await panel.locator('[data-'+field+']').innerText(),'—');
    assert.equal(await panel.evaluate(e=>e.telemetrySnapshot),null);
    await page.evaluate(snapshot=>{state.labs['4'].trusted_snapshot=snapshot;drawWorkspace();},snapshot);
    assert.deepEqual(await displayed(),expected);
   });
   await check(device+': Lab 1 keeps its independent simulation and operational alerts',async()=>{
    const snapshotPaths=await panel.locator('svg path').evaluateAll(nodes=>nodes.map(e=>e.getAttribute('d')));
    await page.locator('#lab-select').selectOption('1');
    const live=page.locator('boiler-panel');await live.waitFor();
    assert.equal(await live.locator('[data-operational-alerts]').count(),1);
    assert.equal(await live.locator('.level').count(),1);
    assert.deepEqual(await live.locator('svg path').evaluateAll(nodes=>nodes.map(e=>e.getAttribute('d'))),snapshotPaths);
    const binding=await live.evaluate(e=>({displayed:e.simulationState,current:state.labs['1'].boiler}));
    for(const key of ['water','fuel','temperature','pressure','power','level'])assert.equal(binding.displayed[key],binding.current[key]);
    await page.locator('#lab-select').selectOption('4');assert.deepEqual(await displayed(),expected);
   });
   await check(device+': Easy, Medium and Hard keep the same snapshot and objective',async()=>{
    for(const [level,label] of [[1,'Medium'],[2,'Hard'],[0,'Easy']]){
     const pending=page.waitForResponse(r=>r.url().endsWith('/api/labs/4/defense'));
     await page.locator('#defense-select').selectOption(String(level));assert.equal((await pending).status(),200);
     await page.waitForFunction(level=>state.labs['4'].defense_level===level,level);
     assert.match(await panel.locator('[data-context]').innerText(),new RegExp('Lab 04 · '+label));
     assert.deepEqual(await displayed(),expected);
     assert.match(await page.locator('#scenario-objective').innerText(),/student-supplied Water In and Fuel In.*verified PANOPTIS telemetry/);
    }
   });
   await check(device+': readable layout adds no tool, alert, chat or flag requests',async()=>{
    await panel.scrollIntoViewIfNeeded();
    const box=await panel.boundingBox();assert(box.width>200&&box.x>=0&&box.x+box.width<=viewport.width+1,JSON.stringify(box));
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    await page.screenshot({path:path.join(artifacts,device+'-authenticated-plant.png')});
    const final=(await(await context.request.get(ready.url+'/api/state')).json());
    assert.equal(final.summary.score,initial.summary.score);assert.equal(final.labs['4'].flag,initial.labs['4'].flag);
    assert.deepEqual(final.labs['4'].trusted_snapshot,snapshot);
    assert(posts.every(url=>url==='/api/labs/4/defense'));
    assert.deepEqual(errors,[]);
   });
   await context.close();
  }
  const report={passed:checks.length,failed:0,checks,artifacts,scope:'Disposable existing fixture; visual checks and isolated difficulty changes, no model calls.'};
  fs.writeFileSync(path.join(artifacts,'results.json'),JSON.stringify(report,null,2)+'\n');
  console.log('Lab 4 visual browser checks passed: '+checks.length);
 }finally{if(browser)await browser.close();fixture.kill('SIGTERM');fs.closeSync(log);}
}
main().catch(error=>{console.error(error.stack);process.exitCode=1;});
