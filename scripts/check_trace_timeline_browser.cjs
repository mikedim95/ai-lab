/* Real HTTP/SSE timeline acceptance with temporary students and mocked Ollama.
 * Existing verification fixtures remain authoritative; no live exploitation. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const artifacts = process.env.PANOPTIS_BROWSER_ARTIFACTS || fs.mkdtempSync(path.join(os.tmpdir(), 'timeline-browser-'));
const pipeline = ['input','session','policy','context','ollama','tools','output','verification'];

async function main() {
  fs.mkdirSync(artifacts, {recursive:true});
  const log = fs.openSync(path.join(artifacts, 'fixture.log'), 'w');
  const fixture = spawn(process.env.PANOPTIS_TEST_PYTHON || 'python3', ['-S','-B','tests/trace_timeline_browser_fixture.py'],
    {cwd:root,stdio:['ignore','pipe',log],env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
  let browser, passed = 0;
  const check = async(name, fn) => { await fn(); passed++; console.log('PASS ' + name); };
  try {
    const ready = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('Timeline fixture startup timed out')), 10000);
      createInterface({input:fixture.stdout}).once('line', line => { clearTimeout(timer); resolve(JSON.parse(line)); });
      fixture.once('error', reject);
      fixture.once('exit', code => { clearTimeout(timer); reject(Error('Timeline fixture exited: ' + code)); });
    });
    browser = await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH
      ? {executablePath:process.env.CHROMIUM_PATH,args:['--no-sandbox']} : {})});
    const layouts = [
      ['desktop',{width:1440,height:1000},false],
      ['desktopShort',{width:1280,height:600},false],
      ['mobile',{width:360,height:740},true],
    ];
    for (const [device, viewport, reducedMotion] of layouts) {
      const context = await browser.newContext({viewport,reducedMotion:reducedMotion?'reduce':'no-preference',
        ...(device === 'mobile' ? {isMobile:true,hasTouch:true} : {})});
      const page = await context.newPage(), errors = [];
      page.on('pageerror', error => errors.push(error.message));
      page.on('dialog', dialog => dialog.accept());
      await page.goto(ready.url + '/lab/9');
      await page.waitForFunction(() => state.catalog.length === 9 && Boolean(globalThis.TraceTimeline));
      const timeline = page.locator('#execution-timeline');
      const stage = key => timeline.locator(`button[data-trace-node="${key}"]`);
      const snapshot = () => page.evaluate(() => TraceTimeline.snapshot());
      const stats = async() => (await page.request.get(ready.url + '/test/stats')).json();
      const waitStats = async predicate => {
        const deadline = Date.now() + 10000;
        while (Date.now() < deadline) {
          const value = await stats();
          if (predicate(value)) return value;
          await page.waitForTimeout(40);
        }
        throw Error('Fixture did not reach requested state');
      };
      const mode = async(name, hold=false) => {
        assert.equal((await page.request.post(ready.url + '/test/mode', {data:{mode:name,hold}})).status(), 200);
      };
      const openPipeline = async() => {
        if(await page.locator('#trace-toggle').getAttribute('aria-expanded')!=='true')await page.locator('#trace-toggle').click();
      };
      const send = async(message='Explain public maintenance procedures.') => {
        if(viewport.width<=760&&await page.locator('#trace-toggle').getAttribute('aria-expanded')==='true')await page.locator('#trace-toggle').click();
        await page.locator('#prompt').fill(message);
        await page.locator('#prompt').press('Enter');
      };
      const waitOutcome = async label => {
        await page.waitForFunction(expected => document.getElementById('trace-outcome-label').textContent.trim() === expected, label);
        await page.waitForFunction(() => state.pending.size === 0);
        await dismissFlag();
        await openPipeline();
      };
      const dismissFlag = async() => {
        if (await page.locator('#flag-capture-dialog').evaluate(element => element.open)) {
          await page.keyboard.press('Escape');
        }
      };
      const selectLab = async id => {
        await dismissFlag();
        await page.locator('#lab-select').selectOption(String(id));
        await page.waitForFunction(id => state.current === id, id);
      };
      const difficulty = async(level, lab) => {
        if (await page.locator('#defense-select').inputValue() !== String(level)) {
          await page.locator('#defense-select').selectOption(String(level));
          await page.waitForFunction(({level,lab}) => state.labs[String(lab)].defense_level === level, {level,lab});
        }
      };
      const safeTimeline = async() => {
        const response = await page.request.post(ready.url + '/test/check-trace', {data:{text:await timeline.innerText()}});
        assert.equal((await response.json()).safe, true);
      };
      let normalId, activeId, winId;

      await check(device + ': idle timeline is a rightmost vertical sidebar', async() => {
        assert.equal(await timeline.getAttribute('data-state'), 'idle');
        assert.equal(await page.locator('#trace-inspector').isVisible(), false);
        assert.deepEqual(await timeline.locator('button[data-trace-node]').evaluateAll(nodes => nodes.map(node => node.dataset.traceNode)), pipeline);
        assert.equal(await timeline.locator('button[data-trace-node][data-tone="gray"]').count(), 8);
        const geometry = await page.evaluate(() => {
          const trace = document.getElementById('execution-timeline').getBoundingClientRect();
          const workspace = document.querySelector('.lab-workspace').getBoundingClientRect();
          return {right:trace.left >= workspace.right-1,aligned:Math.abs(trace.top-workspace.top)<1,overflow:document.documentElement.scrollWidth>innerWidth+1};
        });
        assert(geometry.right&&geometry.aligned&&!geometry.overflow);
        await openPipeline();
      });

      await check(device + ': submission activates before reservation without invented stage progress', async() => {
        await mode('normal', true);
        await page.request.post(ready.url + '/test/reserve-delay');
        const before = (await stats()).calls;
        await send(); await waitStats(value => value.reservation_pending);
        try {
          assert.equal(await timeline.getAttribute('data-state'), 'preparing');
          assert.equal(await timeline.locator('button[data-trace-node][data-tone="gray"]').count(), 8);
          assert.equal((await stats()).calls, before);
        } finally { await page.request.post(ready.url + '/test/reserve-release'); }
      });

      await check(device + ': real Ollama SSE activates blue processing with elapsed time', async() => {
        await page.waitForFunction(() => document.querySelector('[data-trace-node="ollama"]').dataset.tone === 'blue');
        await openPipeline();
        activeId = (await snapshot()).trace_id;
        assert.match(activeId,/^[a-f0-9]{32}$/);
        assert.equal(await page.evaluate(() => state.pending.has(9)), true);
        assert.equal((await snapshot()).metadata.difficulty, 0);
        await page.evaluate(id => {
          const live=ChatTraceTransport.get(id);
          window.dispatchEvent(new CustomEvent('panoptis:trace-event',{detail:{trace_id:id,
            event_id:live.last_event_id+1,span_id:999,stage:'policy',status:'blocked',
            timestamp:Date.now()/1000,duration_ms:1,reason_code:'input_policy_rejected',
            metadata:live.metadata,persisted:true}}));
        },activeId);
        assert.equal(await timeline.locator('button[data-tone="red"]').count(),0);
        assert.equal(await stage('ollama').getAttribute('data-tone'),'blue');
        const before = (await snapshot()).nodes.find(item => item.id === 'ollama').duration_ms;
        await page.waitForTimeout(350);
        const after = (await snapshot()).nodes.find(item => item.id === 'ollama').duration_ms;
        assert(after > before);
        const animated = await stage('ollama').evaluate(element => element.getAnimations({subtree:true}).some(animation => animation.playState === 'running'));
        assert.equal(animated, !reducedMotion);
        await timeline.screenshot({path:path.join(artifacts,device+'-running.png')});
      });

      await check(device + ': reconnect replay preserves the active trace without duplicate stages', async() => {
        await waitStats(value => value.started && value.subscriptions.some(item => item.trace_id===activeId && Number(item.cursor)>0));
        await page.waitForFunction(() => document.querySelector('[data-trace-node="ollama"]').dataset.tone==='blue');
        const current=await snapshot();
        const events=await page.evaluate(id => ChatTraceTransport.get(id).events,activeId);
        assert.equal(current.trace_id,activeId);assert.equal(current.outcome.key,'RUNNING');
        assert.equal(new Set(events.map(item=>item.event_id)).size,events.length);
      });

      await check(device + ': normal processing is NO EXPLOIT with successful and skipped stages', async() => {
        await page.request.post(ready.url + '/test/release'); await waitOutcome('NO EXPLOIT');
        normalId = (await snapshot()).trace_id;
        assert.equal(await stage('input').getAttribute('data-tone'),'green');
        assert.equal(await stage('ollama').getAttribute('data-tone'),'green');
        assert.equal(await stage('verification').getAttribute('data-tone'),'amber');
        assert.equal(await stage('tools').getAttribute('data-tone'),'gray');
        assert.match(await stage('tools').innerText(),/skipped/i);
        await stage('ollama').click();assert.equal(await page.locator('#trace-inspector').isVisible(),true);
        assert.match(await page.locator('#trace-inspector').innerText(),/ms/i);
        assert.match(await page.locator('#trace-total-duration').innerText(),/ms/i);
        await safeTimeline();
        await timeline.screenshot({path:path.join(artifacts,device+'-normal.png')});
        await page.locator('#trace-inspector-close').click();
        assert.equal(await page.evaluate(()=>document.activeElement?.dataset.traceNode),'ollama');
      });

      await check(device + ': duplicated and foreign events cannot replace the completed trace', async() => {
        const before = await snapshot();
        await page.evaluate(id => {
          const first = ChatTraceTransport.get(id).events[0];
          window.dispatchEvent(new CustomEvent('panoptis:trace-event',{detail:{...first,status:'blocked'}}));
          window.dispatchEvent(new CustomEvent('panoptis:trace-event',{detail:{...first,trace_id:'f'.repeat(32),event_id:200,status:'blocked'}}));
        },normalId);
        const after = await snapshot();
        assert.equal(after.trace_id,before.trace_id);assert.deepEqual(after.outcome,before.outcome);assert.deepEqual(after.nodes,before.nodes);
      });

      await check(device + ': late previous-turn events do not replace a genuine model refusal', async() => {
        await mode('refusal_one',true);await send(ready.questions[0]);
        await page.waitForFunction(id => TraceTimeline.snapshot().trace_id && TraceTimeline.snapshot().trace_id !== id,normalId);
        await page.waitForFunction(() => document.querySelector('[data-trace-node="ollama"]').dataset.tone === 'blue');
        const current=(await snapshot()).trace_id;
        await page.evaluate(id => {
          const old = ChatTraceTransport.get(id).terminal;
          window.dispatchEvent(new CustomEvent('panoptis:trace-event',{detail:old}));
        },normalId);
        assert.equal((await snapshot()).trace_id,current);assert.equal(await stage('ollama').getAttribute('data-tone'),'blue');
        await page.request.post(ready.url+'/test/release');await waitOutcome('MODEL REFUSED');
        assert.equal(await stage('ollama').getAttribute('data-tone'),'amber');
        assert.notEqual(await stage('ollama').getAttribute('data-tone'),'red');
      });

      await check(device + ': actual Medium and Hard input rejection use the same evidenced path', async() => {
        await selectLab(6);await mode('normal');
        for (const level of [1,2]) {
          await difficulty(level,6);await send(ready.identity_request);await waitOutcome('BACKEND BLOCKED');
          assert.equal(await stage('policy').getAttribute('data-tone'),'red');
          assert.equal(await stage('ollama').getAttribute('data-tone'),'gray');
          assert.match(await stage('ollama').innerText(),/skipped/i);
          assert.equal(await timeline.locator('button[data-trace-node]').count(),8);
          assert.equal((await snapshot()).metadata.difficulty,level);
          assert.match(await page.locator('#trace-difficulty').innerText(),level===1?/Medium|Hardened/i:/Hard|Guardrailed/i);
        }
        await timeline.screenshot({path:path.join(artifacts,device+'-blocked.png')});
      });

      await check(device + ': real output filtering blocks output while Ollama stays successful', async() => {
        await selectLab(9);await mode('disclosure');
        for (const level of [1,2]) {
          await difficulty(level,9);await send();await waitOutcome('BACKEND BLOCKED');
          assert.equal(await stage('ollama').getAttribute('data-tone'),'green');
          assert.equal(await stage('output').getAttribute('data-tone'),'red');
          assert.equal(await timeline.locator('button[data-trace-node]').count(),8);
          assert.equal((await snapshot()).metadata.difficulty,level);
          await safeTimeline();
        }
      });

      await check(device + ': authentic verification fixtures produce EXPLOIT VERIFIED without points', async() => {
        await difficulty(0,9);
        for (const [modeName,question,label] of [
          ['refusal_one',ready.questions[0],'MODEL REFUSED'],
          ['refusal_two',ready.questions[1],'MODEL REFUSED'],
          ['disclosure',ready.questions[2],'EXPLOIT VERIFIED'],
        ]) {
          await mode(modeName);await send(question);await waitOutcome(label);await dismissFlag();
        }
        const result=await snapshot();winId=result.trace_id;
        assert.equal(await stage('verification').getAttribute('data-tone'),'green');
        assert.equal(await page.evaluate(() => state.labs['9'].last_result.triggered),true);
        assert.equal(await page.evaluate(() => state.summary.score),0);
        await safeTimeline();
      });

      await check(device + ': reset retains historical completion until a new prompt starts', async() => {
        const old=await snapshot();
        await page.locator('#restart-lab').click();
        await page.waitForFunction(attempt => (state.labs['9'].attempt??state.labs['9'].state_version?.[0]) > attempt,old.metadata.attempt);
        assert.equal((await snapshot()).trace_id,winId);assert.equal((await snapshot()).historical,true);
        await difficulty(1,9);
        assert.equal((await snapshot()).metadata.difficulty,0);
        await mode('normal',true);await send();
        await page.waitForFunction(id => TraceTimeline.snapshot().trace_id && TraceTimeline.snapshot().trace_id!==id,winId);
        await page.waitForFunction(() => document.querySelector('[data-trace-node="ollama"]').dataset.tone==='blue');
        const current=await snapshot();assert.equal(current.historical,false);assert.equal(current.metadata.difficulty,1);
        await page.evaluate(id => window.dispatchEvent(new CustomEvent('panoptis:trace-event',{detail:ChatTraceTransport.get(id).terminal})),winId);
        assert.equal((await snapshot()).trace_id,current.trace_id);
        await page.request.post(ready.url+'/test/release');await waitOutcome('NO EXPLOIT');
      });

      await check(device + ': real inference error is ERROR rather than a policy rejection', async() => {
        await mode('error');await send();await waitOutcome('ERROR');
        assert.notEqual(await page.locator('#trace-outcome-label').innerText(),'BACKEND BLOCKED');
        await safeTimeline();
      });

      await check(device + ': student diagnostics remain safe and forged instructor access is denied', async() => {
        const id=(await snapshot()).trace_id;
        const denied=await page.request.get(`${ready.url}/api/traces/${id}/events?view=instructor`,
          {headers:{'X-Role':'instructor','X-Instructor':'true','X-Panoptis-Role':'instructor'}});
        assert.equal(denied.status(),403);
        await stage('verification').click();
        const detail=(await page.locator('#trace-inspector').innerText()).toLowerCase();
        for (const privateName of ['lab9_verification_receipts','insufficient_verified_refusals','_input_validator','matched_protected_records']) {
          assert(!detail.includes(privateName));
        }
        await safeTimeline();
      });

      if (device === 'desktop') await check(device + ': a trace completed away restores after transport cache eviction without another prompt', async() => {
        await mode('normal',true);await send();
        await page.waitForFunction(() => document.querySelector('[data-trace-node="ollama"]').dataset.tone==='blue');
        const original=await snapshot(),oldId=original.trace_id;
        await selectLab(6);
        await page.request.post(ready.url+'/test/release');
        await page.waitForFunction(() => state.pending.size===0);
        await difficulty(0,6);await mode('normal');
        for(let index=0;index<8;index++) {
          await send(`Explain public maintenance checklist item ${index+1}.`);
          await waitOutcome('NO EXPLOIT');
        }
        assert.equal(await page.evaluate(id=>ChatTraceTransport.get(id),oldId),null);
        const before=await stats();
        await selectLab(9);await waitOutcome('NO EXPLOIT');
        const restored=await snapshot(),after=await stats();
        assert.equal(restored.trace_id,oldId);assert.deepEqual(restored.metadata,original.metadata);
        assert.equal(restored.outcome.key,'NO_EXPLOIT');
        assert.equal(after.calls,before.calls);
        assert.equal(after.reservations['9'],before.reservations['9']);
      });

      await check(device + ': layout stays readable and reduced motion is respected', async() => {
        const layout=await timeline.evaluate(element => {
          const scroll=[element,...element.querySelectorAll('*')].find(item =>
            item.scrollWidth>item.clientWidth+1&&['auto','scroll'].includes(getComputedStyle(item).overflowX));
          if(scroll)scroll.scrollLeft=scroll.scrollWidth;
          const workspace=document.querySelector('.lab-workspace').getBoundingClientRect();
          const trace=element.getBoundingClientRect();
          return {overflow:document.documentElement.scrollWidth>innerWidth+1,right:trace.left>=workspace.right-1||innerWidth<=760,aligned:Math.abs(trace.top-workspace.top)<1,
            scrollable:Boolean(scroll),scrolled:scroll?.scrollLeft>0,
            animations:element.getAnimations({subtree:true}).filter(animation=>animation.playState==='running').length};
        });
        assert(!layout.overflow&&layout.right&&layout.aligned);
        assert(!layout.scrollable,'the vertical pipeline must not need horizontal scrolling');
        if(reducedMotion)assert.equal(layout.animations,0);
        assert.deepEqual(errors,[]);
      });
      await context.close();
    }
    console.log('Trace timeline browser checks passed: '+passed);
  } finally {
    if(browser)await browser.close();fixture.kill('SIGTERM');fs.closeSync(log);
  }
}
main().catch(error => { console.error(error.stack); process.exitCode=1; });
