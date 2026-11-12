/* Real browser + concurrent HTTP/SSE, with temporary students and mocked Ollama.
 * No live exploitation or timeline UI. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {createInterface}=require('node:readline');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
const artifacts=process.env.PANOPTIS_BROWSER_ARTIFACTS||fs.mkdtempSync(path.join(os.tmpdir(),'trace-stream-browser-'));

async function main(){
  fs.mkdirSync(artifacts,{recursive:true});
  const log=fs.openSync(path.join(artifacts,'fixture.log'),'w');
  const fixture=spawn(process.env.PANOPTIS_TEST_PYTHON||'python3',['-S','-B','tests/trace_stream_browser_fixture.py'],
    {cwd:root,stdio:['ignore','pipe',log],env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
  let browser,passed=0;
  const check=async(name,fn)=>{await fn();passed++;console.log('PASS '+name);};
  try{
    const ready=await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(Error('Fixture startup timed out')),10000);
      createInterface({input:fixture.stdout}).once('line',line=>{clearTimeout(timer);resolve(JSON.parse(line));});
      fixture.once('error',reject);fixture.once('exit',code=>{clearTimeout(timer);reject(Error('Fixture exited: '+code));});
    });
    browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH
      ?{executablePath:process.env.CHROMIUM_PATH,args:['--no-sandbox']}: {})});
    for(const [device,viewport] of [['desktop',{width:1440,height:1000}],['mobile',{width:360,height:740}]]){
      const context=await browser.newContext({viewport,...(device==='mobile'?{isMobile:true,hasTouch:true}:{})});
      const page=await context.newPage(),errors=[];page.on('pageerror',error=>errors.push(error.message));
      await page.goto(ready.url+'/lab/9');await page.waitForFunction(()=>state.catalog.length===9);
      const waitStats=async predicate=>{
        const deadline=Date.now()+10000;
        while(Date.now()<deadline){
          const stats=await(await page.request.get(ready.url+'/test/stats')).json();
          if(predicate(stats))return stats;
          await page.waitForTimeout(50);
        }
        throw Error('Fixture state did not reach the requested condition');
      };
      let traceId;
      await check(device+': prompt immediately receives its reserved trace while inference is pending',async()=>{
        await page.locator('#prompt').fill('Explain public maintenance procedures.');await page.locator('#prompt').press('Enter');
        await page.waitForFunction(()=>Boolean(state.pending.get(9)?.trace_id));
        traceId=await page.evaluate(()=>state.pending.get(9).trace_id);
        assert.match(traceId,/^[a-f0-9]{32}$/);
        await page.waitForFunction(id=>ChatTraceTransport.get(id).events.some(event=>event.stage==='ollama'&&event.status==='running'),traceId);
        assert.equal(await page.evaluate(()=>state.pending.has(9)),true);
        assert.equal(await page.evaluate(()=>state.summary.score),0);
      });
      await check(device+': an SSE reconnect during long inference resumes from its cursor',async()=>{
        const stats=await waitStats(stats=>stats.started&&stats.subscriptions.some(item=>item.trace_id===traceId&&Number(item.cursor)>0));
        const subscriptions=stats.subscriptions.filter(item=>item.trace_id===traceId);
        assert(subscriptions.some(item=>Number(item.cursor)>0));
        const live=await page.evaluate(id=>ChatTraceTransport.get(id),traceId);
        assert.equal(new Set(live.events.map(event=>event.event_id)).size,live.events.length);
        assert.equal(live.terminal,null);assert.equal(await page.evaluate(()=>state.pending.has(9)),true);
      });
      await check(device+': real chat completion delivers one terminal and closes its source',async()=>{
        await page.request.post(ready.url+'/test/release');
        await page.waitForFunction(id=>!state.pending.has(9)&&ChatTraceTransport.get(id).terminal!==null,traceId);
        const result=await page.evaluate(id=>ChatTraceTransport.get(id),traceId);
        assert.equal(result.terminal.status,'passed');assert.equal(result.stream_status,'finished');
        assert.equal(result.events.filter(event=>event.stage==='trace').length,1);
        assert.equal(await page.evaluate(()=>state.labs['9'].messages.at(-1).content),ready.reply);
      });
      await check(device+': persisted replay matches the browser and contains no transcript',async()=>{
        const response=await page.request.get(`${ready.url}/api/traces/${traceId}/events`);
        assert.equal(response.status(),200);
        const text=await response.text(),events=text.split('\n').filter(line=>line.startsWith('data: ')).map(line=>JSON.parse(line.slice(6)));
        const live=await page.evaluate(id=>ChatTraceTransport.get(id),traceId);
        assert.deepEqual(events.map(event=>event.event_id),live.events.map(event=>event.event_id));
        assert(!text.includes(ready.reply));assert(!text.includes('Explain public maintenance procedures.'));
      });
      await check(device+': another student cannot access the stream',async()=>{
        const other=await browser.newContext();
        await other.request.get(ready.url+'/api/state');
        assert.equal((await other.request.get(`${ready.url}/api/traces/${traceId}/events`)).status(),404);
        await other.close();
      });
      await check(device+': a transient SQLite stream read failure replays and restores the live timeline',async()=>{
        const before=(await waitStats(()=>true)).read_errors;
        await page.locator('#prompt').fill('Explain public maintenance scheduling.');await page.locator('#prompt').press('Enter');
        await page.waitForFunction(()=>Boolean(state.pending.get(9)?.trace_id));
        const id=await page.evaluate(()=>state.pending.get(9).trace_id);
        await page.waitForFunction(id=>ChatTraceTransport.get(id).events.some(event=>event.stage==='ollama'&&event.status==='running'),id);
        await page.request.post(ready.url+'/test/read-error');
        await waitStats(stats=>stats.read_errors===before+1&&stats.subscriptions.filter(item=>item.trace_id===id).length>=2);
        await page.waitForFunction(id=>ChatTraceTransport.get(id).stream_status==='open'&&
          document.querySelector('[data-trace-node="ollama"]').dataset.tone==='blue',id);
        const live=await page.evaluate(id=>ChatTraceTransport.get(id),id);
        assert.equal(live.terminal,null);assert.equal(await page.evaluate(()=>state.pending.has(9)),true);
        await page.request.post(ready.url+'/test/release');
        await page.waitForFunction(id=>!state.pending.has(9)&&ChatTraceTransport.get(id).terminal!==null,id);
        const completed=await page.evaluate(id=>ChatTraceTransport.get(id),id);
        assert.equal(completed.terminal.outcome,'NO_EXPLOIT');assert.equal(completed.stream_status,'finished');
        assert.deepEqual(completed.events.map(event=>event.event_id),Array.from({length:completed.events.length},(_,index)=>index+1));
        assert.equal(await page.locator('#trace-outcome-label').innerText(),'NO EXPLOIT');
        assert.equal(await page.evaluate(()=>state.labs['9'].messages.at(-1).content),ready.reply);
        assert.equal(await page.evaluate(()=>state.summary.score),0);
      });
      await check(device+': interrupted streaming leaves the existing chat request successful',async()=>{
        await context.route('**/api/traces/*/events*',route=>route.abort());
        await page.locator('#prompt').fill('Explain the next public maintenance review.');await page.locator('#prompt').press('Enter');
        await page.waitForFunction(()=>state.pending.has(9));await waitStats(stats=>stats.started);
        await page.request.post(ready.url+'/test/release');await page.waitForFunction(()=>!state.pending.has(9));
        assert.equal(await page.evaluate(()=>state.labs['9'].messages.at(-1).content),ready.reply);
        assert.equal(await page.evaluate(()=>state.summary.score),0);
        await context.unroute('**/api/traces/*/events*');
      });
      await check(device+': failed reservation falls back to ordinary chat',async()=>{
        await context.route('**/api/labs/9/traces',route=>route.fulfill({status:503,json:{error:'Isolated trace setup failure'}}));
        await page.locator('#prompt').fill('Explain the public handover checklist.');await page.locator('#prompt').press('Enter');
        await page.waitForFunction(()=>state.pending.has(9));await waitStats(stats=>stats.started);
        assert.equal(await page.evaluate(()=>state.pending.get(9).trace_id),undefined);
        await page.request.post(ready.url+'/test/release');await page.waitForFunction(()=>!state.pending.has(9));
        assert.equal(await page.evaluate(()=>state.labs['9'].messages.at(-1).content),ready.reply);
      });
      await check(device+': transport preserves scoring and the shared trace panel',async()=>{
        assert.equal(await page.locator('#execution-timeline').count(),1);
        assert.equal(await page.evaluate(()=>state.summary.score),0);assert.equal(await page.evaluate(()=>state.labs['9'].completed),false);
        assert.deepEqual(errors,[]);
      });
      await context.close();
    }
    console.log('Trace stream browser checks passed: '+passed);
  }finally{
    if(browser)await browser.close();fixture.kill('SIGTERM');
  }
}
main().catch(error=>{console.error(error.stack);process.exitCode=1;});
