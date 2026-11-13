/* Viewport layout acceptance using the existing disposable HTTP/SSE fixture.
 * Model transport is mocked by the fixture; no deployed app or model calls. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const artifacts = process.env.PANOPTIS_BROWSER_ARTIFACTS || fs.mkdtempSync(path.join(os.tmpdir(),'trace-layout-'));
const layouts = [
  ['desktop',{width:1440,height:900}], ['desktopMedium',{width:1280,height:720}],
  ['desktopShort',{width:1024,height:600}], ['mobile',{width:390,height:844}],
  ['mobileSmall',{width:375,height:667}], ['mobileNarrow',{width:360,height:640}],
  ['landscape',{width:844,height:390}],
].filter(([name])=>!process.env.PANOPTIS_LAYOUTS||process.env.PANOPTIS_LAYOUTS.split(',').includes(name));

async function main() {
  fs.mkdirSync(artifacts,{recursive:true});
  const log=fs.openSync(path.join(artifacts,'fixture.log'),'w');
  const fixture=spawn(process.env.PANOPTIS_TEST_PYTHON || 'python3', ['-S','-B','tests/trace_timeline_browser_fixture.py'],
    {cwd:root,stdio:['ignore','pipe',log],env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
  let browser,currentPage,currentDevice,passed=0;
  const checks=[];
  const check=async(name,fn)=>{await fn();passed++;checks.push(name);console.log('PASS '+name);};
  try {
    const ready=await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(Error('Layout fixture startup timed out')),10000);
      createInterface({input:fixture.stdout}).once('line',line=>{clearTimeout(timer);resolve(JSON.parse(line));});
      fixture.once('error',reject);
      fixture.once('exit',code=>{clearTimeout(timer);reject(Error('Layout fixture exited: '+code));});
    });
    browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH
      ? {executablePath:process.env.CHROMIUM_PATH,args:['--no-sandbox']} : {})});
    for(const [device,viewport] of layouts) {
      currentDevice=device;
      const mobile=viewport.width<=1000,phone=viewport.width<=760;
      const extreme=viewport.height<480;
      const context=await browser.newContext({viewport,reducedMotion:'reduce',...(mobile?{hasTouch:true}: {})});
      const page=currentPage=await context.newPage(),errors=[];
      page.on('pageerror',()=>errors.push(true));page.on('dialog',dialog=>dialog.accept());
      await page.goto(ready.url+'/lab/9');
      await page.waitForFunction(()=>state.catalog.length===9&&Boolean(globalThis.TraceTimeline));
      const stage=id=>page.locator(`[data-trace-node="${id}"]`);
      const rects=()=>page.evaluate(()=>{
        const rect=selector=>{const e=document.querySelector(selector),r=e.getBoundingClientRect();return {
          x:r.x,y:r.y,top:r.top,bottom:r.bottom,left:r.left,right:r.right,width:r.width,height:r.height,
          clientHeight:e.clientHeight,scrollHeight:e.scrollHeight,scrollTop:e.scrollTop,
          clientWidth:e.clientWidth,scrollWidth:e.scrollWidth,scrollLeft:e.scrollLeft};};
        return {viewport:{width:innerWidth,height:innerHeight},window:{x:scrollX,y:scrollY},
          document:{height:document.scrollingElement.scrollHeight,width:document.scrollingElement.scrollWidth},
          timeline:rect('#execution-timeline'),workspace:rect('.lab-workspace'),chat:rect('.chat-card'),
          messages:rect('#messages'),composer:rect('#chat-form'),prompt:rect('#prompt'),send:rect('#send'),
          banner:rect('#captured-flag-banner'),result:rect('#attempt-result'),inspector:rect('#trace-inspector'),
          track:rect('.trace-track'),traceContent:rect('#trace-content'),error:rect('#error')};
      });
      const painted=async selector=>page.locator(selector).evaluate(element=>{
        const r=element.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;
        const top=document.elementFromPoint(x,y);
        return {painted:r.width>0&&r.height>0&&x>=0&&y>=0&&x<innerWidth&&y<innerHeight&&Boolean(top&&(element===top||element.contains(top))),
          rect:{x:r.x,y:r.y,width:r.width,height:r.height},tag:top?.tagName,id:top?.id};
      });
      const dock=async label=>{
        const value=await rects();
        assert(value.document.height<=value.viewport.height+2,label+' document scroll: '+JSON.stringify(value));
        assert(value.document.width<=value.viewport.width+2,label+' horizontal page scroll: '+JSON.stringify(value));
        assert(Math.abs(value.window.y)<=1&&Math.abs(value.window.x)<=1,label+' page moved: '+JSON.stringify(value));
        assert(value.timeline.top>=-1&&value.timeline.bottom<=value.viewport.height+1,label+' timeline outside viewport: '+JSON.stringify(value));
        assert(value.timeline.height>40,label+' timeline has no readable height: '+JSON.stringify(value));
        const expanded=await page.locator('#trace-toggle').getAttribute('aria-expanded')==='true';
        if(!phone||!expanded)assert(value.workspace.right<=value.timeline.left+1,label+' trace is not the rightmost column: '+JSON.stringify(value));
        assert(Math.abs(value.workspace.top-value.timeline.top)<=1,label+' sidebar is not aligned with workspace: '+JSON.stringify(value));
        assert(Math.abs(value.workspace.bottom-value.timeline.bottom)<=1,label+' sidebar changed workspace height: '+JSON.stringify(value));
        assert(value.workspace.height>=32,label+' workspace inaccessible: '+JSON.stringify(value));
        assert((await painted('#trace-toggle')).painted,label+' timeline toggle is clipped');
        return value;
      };
      const openPipeline=async()=>{
        if(await page.locator('#trace-toggle').getAttribute('aria-expanded')!=='true') await page.locator('#trace-toggle').click();
      };
      const reach=async(selector,label)=>{
        const region=await page.locator(selector).evaluate(element=>({workspace:Boolean(element.closest('.lab-workspace')),
          pipeline:Boolean(element.closest('#trace-content')),workspaceHeight:document.querySelector('.lab-workspace').clientHeight}));
        if(region.pipeline) await openPipeline();
        if(phone&&region.workspace&&await page.locator('#trace-toggle').getAttribute('aria-expanded')==='true')
          await page.locator('#trace-toggle').click();
        await page.locator(selector).evaluate(element=>{
          for(let parent=element.parentElement;parent&&parent!==document.body;parent=parent.parentElement) {
            const style=getComputedStyle(parent),r=element.getBoundingClientRect(),p=parent.getBoundingClientRect();
            if(['auto','scroll'].includes(style.overflowY)&&parent.scrollHeight>parent.clientHeight+1)
              parent.scrollTop+=(r.top+r.height/2)-(p.top+parent.clientTop+parent.clientHeight/2);
            if(['auto','scroll'].includes(style.overflowX)&&parent.scrollWidth>parent.clientWidth+1)
              parent.scrollLeft+=(r.left+r.width/2)-(p.left+parent.clientLeft+parent.clientWidth/2);
          }
        });
        const point=await painted(selector);
        assert(point.painted,label+' not reachable: '+JSON.stringify({point,geometry:await rects(),ancestors:await page.locator(selector).evaluate(element=>{
          const rows=[];for(let parent=element.parentElement;parent&&parent!==document.body;parent=parent.parentElement){
            const r=parent.getBoundingClientRect(),style=getComputedStyle(parent);rows.push({id:parent.id,className:parent.className,top:r.top,bottom:r.bottom,
              clientHeight:parent.clientHeight,scrollHeight:parent.scrollHeight,scrollTop:parent.scrollTop,overflowY:style.overflowY});}
          return rows;
        })}));
        await dock(label);
      };
      const mode=async(name,hold=false)=>assert.equal((await page.request.post(ready.url+'/test/mode',{data:{mode:name,hold}})).status(),200);
      const send=async message=>{await reach('#prompt','conversation prompt');await page.locator('#prompt').fill(message);await page.locator('#prompt').press('Enter');};
      const outcome=async value=>{
        await page.waitForFunction(expected=>document.querySelector('#trace-outcome-label').textContent.trim()===expected,value);
        await page.waitForFunction(()=>state.pending.size===0);
      };
      const screenshot=async suffix=>page.screenshot({path:path.join(artifacts,device+'-'+suffix+'.png'),
        mask:[page.locator('#messages'),page.locator('#captured-flag-banner'),page.locator('#flag-capture-dialog'),page.locator('#captured-panel')]});
      for(let lab=1;lab<=9;lab++) await check(`${device}: Lab ${lab} idle timeline stays in viewport without page scrolling`,async()=>{
        await page.locator('#lab-select').selectOption(String(lab));
        await page.waitForFunction(id=>state.current===id,lab);
        await dock('Lab '+lab+' idle');
        assert.equal(await page.locator('#execution-timeline').getAttribute('data-state'),'idle');
      });

      await check(device+': prominent restart and solution modal remain accessible',async()=>{
        assert.equal(await page.locator('#restart-lab').evaluate(element=>Boolean(element.closest('.topbar'))),true);
        assert.match(await page.locator('#restart-lab').innerText(),/Restart lab/);
        assert((await painted('#restart-lab')).painted,'restart control is clipped');
        assert.match(await page.locator('#restart-lab').getAttribute('title'),/earned points/);
        assert.equal(await page.locator('#solution-button').isVisible(),false);
        const total=await page.evaluate(()=>state.catalog[8].hints.length);
        for(let index=0;index<total;index++){
          await reach('#hint-button','reveal hint control');await page.locator('#hint-button').click();
          assert.equal(await page.locator('#solution-button').isVisible(),index===total-1);
        }
        await reach('#solution-button','solution control');
        const pair=await page.evaluate(()=>{
          const hint=document.getElementById('hint-button').getBoundingClientRect(),solution=document.getElementById('solution-button').getBoundingClientRect();
          return {hint:{right:hint.right,y:hint.y+hint.height/2},solution:{left:solution.left,y:solution.y+solution.height/2}};
        });
        assert(pair.solution.left>=pair.hint.right-1&&Math.abs(pair.solution.y-pair.hint.y)<=1,'solution must sit beside Close all hints: '+JSON.stringify(pair));
        await page.locator('#solution-button').click();
        assert.equal(await page.locator('#solution-dialog').evaluate(element=>element.open),true);
        assert.match(await page.locator('#solution-heading').innerText(),/Lab 9:/);
        assert.match(await page.locator('#solution-dialog').innerText(),/Solution coming soon\./);
        await screenshot('solution-placeholder');
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('#solution-dialog').evaluate(element=>element.open),false);
        await reach('#hint-button','close all hints control');await page.locator('#hint-button').click();
        assert.equal(await page.locator('#solution-button').isVisible(),false);
        await dock('hint solution closed');
      });

      await check(device+': right sidebar collapses without taking workspace height and stages run vertically',async()=>{
        await openPipeline();const opened=await rects();
        const nodes=await page.locator('[data-trace-node]').evaluateAll(elements=>elements.map(element=>{
          const r=element.getBoundingClientRect();return {left:r.left,top:r.top,bottom:r.bottom};
        }));
        assert(nodes.every((node,index)=>index===0||(node.top>=nodes[index-1].bottom&&Math.abs(node.left-nodes[0].left)<1)));
        await screenshot('right-expanded');
        await page.locator('#trace-toggle').click();const closed=await rects();
        assert.equal(await page.locator('#trace-content').isVisible(),false);
        assert(closed.timeline.width<=46);
        assert(Math.abs(closed.workspace.height-opened.workspace.height)<=1);
        assert(Math.abs(closed.workspace.top-opened.workspace.top)<=1);
        if(!phone)assert(closed.workspace.width>opened.workspace.width+150);
        await page.locator('#lab-select').selectOption('8');await page.waitForFunction(()=>state.current===8);
        await page.locator('#lab-select').selectOption('9');await page.waitForFunction(()=>state.current===9);
        assert.equal(await page.locator('#trace-toggle').getAttribute('aria-expanded'),'false','navigation must retain the collapsed preference');
        await dock('collapsed right rail');await screenshot('right-collapsed');
      });

      await check(device+': held genuine SSE activates the expanded timeline without covering the workspace',async()=>{
        await mode('normal',true);
        const message='Explain this public maintenance checklist:\n'+Array.from({length:36},(_,i)=>`Public checklist item ${i+1}: inspect the maintenance log.`).join('\n');
        await send(message);
        await page.waitForFunction(()=>document.querySelector('[data-trace-node="ollama"]').dataset.tone==='blue');
        assert.equal(await page.evaluate(()=>state.pending.has(9)),true);
        assert.equal(await page.locator('#trace-toggle').getAttribute('aria-expanded'),'false','a prompt must preserve the chosen collapsed state');
        await openPipeline();
        await dock('running');
        assert.equal(await page.locator('#trace-content').isVisible(),true);
        await reach('[data-trace-node="ollama"]','running stage');
        if(!mobile&&viewport.height>=720) {
          const value=await rects();
          assert(value.prompt.top>=value.workspace.top-1&&value.send.bottom<=value.workspace.bottom+1,
            'desktop composer is not fully inside its workspace: '+JSON.stringify(value));
        }
        await screenshot('running');
        await page.locator('#trace-toggle').click();
        await page.request.post(ready.url+'/test/release');await outcome('NO EXPLOIT');
        assert.equal(await page.locator('#trace-toggle').getAttribute('aria-expanded'),'false','completion must retain the collapsed preference');
        await openPipeline();
      });
      await check(device+': terminal timeline and conversation controls remain reachable through internal scrolling',async()=>{
        await dock('terminal');
        await reach('#prompt','terminal prompt');await reach('#send','terminal send');
        await reach('#attempt-result','terminal attempt result');
        const chatScroll=await page.locator('#messages').evaluate(element=>{
          element.scrollTop=0;const before=element.scrollTop;element.scrollTop=element.scrollHeight;
          return {client:element.clientHeight,height:element.scrollHeight,moved:element.scrollTop>before};
        });
        assert(chatScroll.client>=24&&chatScroll.height>chatScroll.client&&chatScroll.moved,
          'long real conversation must scroll inside messages: '+JSON.stringify(chatScroll));
        await dock('conversation scrolled');
      });
      await check(device+': vertical stages remain accessible by Up, Down, Home and End',async()=>{
        await openPipeline();
        await stage('input').scrollIntoViewIfNeeded();await stage('input').focus();
        await stage('input').press('ArrowDown');assert.equal(await page.evaluate(()=>document.activeElement?.dataset.traceNode),'session');
        await stage('session').press('ArrowUp');assert.equal(await page.evaluate(()=>document.activeElement?.dataset.traceNode),'input');
        await stage('input').press('End');
        assert.equal(await page.evaluate(()=>document.activeElement?.dataset.traceNode),'verification');
        assert((await painted('[data-trace-node="verification"]')).painted,'last stage clipped after End navigation');
        const vertical=await page.locator('#trace-content').evaluate(element=>{
          element.scrollTop=0;const before=element.scrollTop;element.scrollTop=element.scrollHeight;
          return {client:element.clientHeight,height:element.scrollHeight,before,after:element.scrollTop,horizontal:element.scrollWidth>element.clientWidth+1};
        });
        assert(!vertical.horizontal,'vertical stages must not need horizontal scrolling');
        if(vertical.height>vertical.client+1)assert(vertical.after>0,'stage body cannot scroll vertically');
        await stage('verification').scrollIntoViewIfNeeded();
        assert((await painted('[data-trace-node="verification"]')).painted,'last vertical stage is clipped');
        await stage('verification').press('Home');assert.equal(await page.evaluate(()=>document.activeElement?.dataset.traceNode),'input');
        await dock('vertical keyboard navigation');
      });
      await check(device+': stage inspector scrolls internally and restores visible keyboard focus',async()=>{
        await reach('[data-trace-node="session"]','session inspection control');await stage('session').click();assert.equal(await page.locator('#trace-inspector').isVisible(),true);
        await reach('#trace-inspector-close','inspector close');
        await reach('#trace-inspector .trace-student-note','inspector final note');
        await reach('#trace-inspector-close','inspector close after internal scroll');
        await page.locator('#trace-inspector-close').click();
        assert.equal(await page.evaluate(()=>document.activeElement?.dataset.traceNode),'session');
        assert((await painted('[data-trace-node="session"]')).painted,'restored stage focus is clipped');
        await dock('inspector closed');
      });
      await check(device+': captured banner, result, and expanded inspector preserve usable chat controls',async()=>{
        for(const [name,question,label] of [['refusal_one',ready.questions[0],'MODEL REFUSED'],
          ['refusal_two',ready.questions[1],'MODEL REFUSED'],['disclosure',ready.questions[2],'EXPLOIT VERIFIED']]) {
          await mode(name);await send(question);await outcome(label);
        }
        if(await page.locator('#flag-capture-dialog').evaluate(element=>element.open)) await page.keyboard.press('Escape');
        assert.equal(await page.locator('#captured-flag-banner').isVisible(),true);
        assert.equal(await page.locator('#attempt-result').isVisible(),true);
        await reach('#attempt-result summary','banner attempt-details control');
        await page.locator('#attempt-result summary').click();
        assert.equal(await page.locator('#attempt-result details').evaluate(element=>element.open),true);
        await reach('[data-trace-node="session"]','banner session inspection control');await stage('session').click();
        await reach('#trace-inspector-close','banner inspector close');
        await reach('#prompt','banner prompt');await reach('#send','banner send');
        await reach('#attempt-result','banner attempt result');
        await reach('#hint-button','scenario hint control');await reach('.workspace-heading','workspace tools pane');
        await dock('banner and inspector');await screenshot('banner-inspector');
      });
      await check(device+': actual inference errors stay visible without obscuring the timeline',async()=>{
        await mode('error');await send('Explain the next public maintenance review.');await outcome('ERROR');
        assert.equal(await page.locator('#error').isVisible(),true);
        if(extreme) await reach('#prompt','extreme error prompt');
        const overlap=await page.evaluate(()=>{
          const error=document.getElementById('error').getBoundingClientRect(),trace=document.getElementById('execution-timeline').getBoundingClientRect();
          const bounds=r=>({top:r.top,bottom:r.bottom,left:r.left,right:r.right,height:r.height});
          return {error:bounds(error),trace:bounds(trace),overlap:error.width>0&&error.height>0&&Math.min(error.right,trace.right)>Math.max(error.left,trace.left)
            &&Math.min(error.bottom,trace.bottom)>Math.max(error.top,trace.top)};
        });
        assert.equal(overlap.overlap,false,'error notification covers the timeline: '+JSON.stringify(overlap));
        await dock('inference error');
        await reach('[data-trace-node="session"]','error session inspection control');await stage('session').click();
        await reach('#trace-inspector-close','error inspector close');await page.locator('#trace-inspector-close').click();
        await reach('#prompt','error prompt');await reach('#send','error send');
        assert((await painted('#error')).painted,'error notification is not reachable');
        assert.equal(errors.length,0,'uncaught browser error');
      });
      await context.close();currentPage=null;
    }
    const report={passed,failed:0,checks,artifacts,scope:'Disposable HTTP/SSE fixture and mocked model transport; no live model requests.'};
    fs.writeFileSync(path.join(artifacts,'results.json'),JSON.stringify(report,null,2)+'\n');
    console.log('Trace layout browser checks passed: '+passed);
  } catch(error) {
    if(currentPage) {
      const geometry=await currentPage.evaluate(()=>({viewport:{width:innerWidth,height:innerHeight},window:{x:scrollX,y:scrollY},
        scrollHeight:document.scrollingElement.scrollHeight,scrollWidth:document.scrollingElement.scrollWidth})).catch(()=>null);
      fs.writeFileSync(path.join(artifacts,'failure-geometry.json'),JSON.stringify({device:currentDevice,geometry,message:error.message},null,2)+'\n');
      await currentPage.screenshot({path:path.join(artifacts,'failure.png'),mask:[currentPage.locator('#messages'),currentPage.locator('#captured-flag-banner'),currentPage.locator('#flag-capture-dialog'),currentPage.locator('#captured-panel')]}).catch(()=>{});
    }
    throw error;
  } finally {
    if(browser)await browser.close();fixture.kill('SIGTERM');fs.closeSync(log);
  }
}
main().catch(error=>{console.error(error.stack);process.exitCode=1;});
