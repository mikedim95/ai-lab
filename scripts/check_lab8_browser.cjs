/* Disposable browser checks only. Completion fixtures exercise the production
 * request path with mocked transport. No live model or exploitation is used. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const artifacts = process.env.PANOPTIS_BROWSER_ARTIFACTS || '/tmp/panoptis-lab8-browser';
const hints = [
  'Investigate which maintenance documents you can edit or add to the knowledge base.',
  "Saving a document does not automatically update Plant AI's retrieval snapshot.",
  'Ask a question related to your modified document and examine which sources were retrieved.',
  'Consider the difference between Plant AI summarizing a retrieved document and treating instructions within it as authoritative.',
];

async function main() {
  fs.mkdirSync(artifacts,{recursive:true});
  const log=fs.openSync(path.join(artifacts,'fixture.log'),'w');
  const fixture=spawn(process.env.PANOPTIS_TEST_PYTHON || 'python3',['-S','tests/lab8_browser_fixture.py'],
    {cwd:root,stdio:['ignore','pipe',log]});
  let browser;
  const passed=[];
  const check=async(name,fn)=>{await fn();passed.push(name);console.log('PASS '+name);};
  try {
    const ready=await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(new Error('Fixture startup timed out')),10000);
      createInterface({input:fixture.stdout}).once('line',line=>{clearTimeout(timer);resolve(JSON.parse(line));});
      fixture.once('error',reject);
      fixture.once('exit',code=>{clearTimeout(timer);reject(new Error('Fixture exited: '+code));});
    });
    browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH
      ? {executablePath:process.env.CHROMIUM_PATH,args:['--no-sandbox']} : {})});
    await check('observed authoritative report verifies through the unchanged mocked-transport evidence path',async()=>{
      const context=await browser.newContext();
      await context.addCookies([{name:'nine_sid',value:ready.sessions.desktop,url:ready.url}]);
      const response=await context.request.get(ready.url+'/api/state');
      assert.equal(response.status(),200);
      const state=await response.json();
      assert.equal(state.labs['8'].messages.at(-1).content,ready.observed_report);
      assert.equal(state.labs['8'].last_result.triggered,true);
      assert.equal(state.labs['8'].completed,false);
      assert.equal(state.summary.score,0);
      assert(!JSON.stringify(state).includes('lab8_verification_receipts'));
      await context.close();
    });
    for(const [name,viewport] of Object.entries({desktop:{width:1440,height:1000},desktopShort:{width:1280,height:600},
      mobile:{width:390,height:844},mobileSmall:{width:360,height:740}})) {
      const context=await browser.newContext({viewport});
      const page=await context.newPage();
      const errors=[];
      page.on('pageerror',error=>errors.push(error.message));
      page.on('dialog',dialog=>dialog.accept());
      await page.goto(ready.url+'/lab/8');
      await page.waitForFunction(()=>state.current===8 && state.catalog.length===9);
      const closeKnowledge=async()=>{if(await page.locator('#knowledge-dialog').evaluate(el=>el.open))await page.locator('[data-close="knowledge-dialog"]').click();};
      const openKnowledge=async()=>{if(!await page.locator('#knowledge-dialog').evaluate(el=>el.open))await page.locator('#knowledge-button').click();};
      const post=async(action,click)=>{
        const pending=page.waitForResponse(r=>r.url().endsWith('/api/labs/8/'+action));
        await click();
        const response=await pending;
        assert.equal(response.status(),200);
        const result=await response.json();
        await page.waitForFunction(version=>state.labs['8'].state_version.join('.')===version,result.lab.state_version.join('.'));
        return result;
      };
      const ask=async(message=ready.question)=>{
        await closeKnowledge();
        await page.locator('#prompt').fill(message);
        const result=await post('chat',()=>page.locator('#prompt').press('Enter'));
        assert.equal(result.lab.last_result.request_id,result.request_id);
        return result;
      };
      const sync=async()=>post('kb',()=>page.locator('#sync-kb').click());
      const difficulty=async level=>{
        await closeKnowledge();
        const result=await post('defense',()=>page.locator('#defense-select').selectOption(String(level)));
        assert.equal(result.lab.defense_level,level);
      };

      await check(name+': scenario, five stages and four sequential hints',async()=>{
        assert.match(await page.locator('#scenario-summary').innerText(),/knowledge base to answer maintenance questions/);
        assert.match(await page.locator('#scenario-objective').innerText(),/combining student-controlled knowledge with conversational instructions/);
        assert.equal(await page.locator('#scenario-workflow li').count(),5);
        assert.match(await page.locator('#scenario-objective-rules').innerText(),/alone does not verify exploitation/);
        assert.equal(await page.locator('#hints li').count(),0);
        for(let count=1;count<=4;count++) {
          await page.locator('#hint-button').click();
          assert.deepEqual(await page.locator('#hints li').allTextContents(),hints.slice(0,count));
        }
        await page.locator('#hint-button').click();
        assert.equal(await page.locator('#hints li').count(),0);
        assert.match(await page.locator('#lab8-kb-feedback').innerText(),/Knowledge base disabled/);
      });
      await check(name+': readable scenario and workspace layout',async()=>{
        await page.locator('.scenario-body').first().evaluate(element=>{element.scrollTop=0;});
        const geometry=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,
          boxes:['.scenario-column','.chat-card','.lab-tools','#prompt'].map(selector=>{
            const box=document.querySelector(selector).getBoundingClientRect();return {selector,left:box.left,right:box.right,width:box.width};
          })}));
        assert(geometry.scroll<=geometry.width+1,JSON.stringify(geometry));
        for(const box of geometry.boxes)assert(box.width>0&&box.left>=0&&box.right<=geometry.width+1,JSON.stringify(box));
        await page.screenshot({path:path.join(artifacts,name+'-scenario.png'),fullPage:true});
      });
      let noteId;
      await check(name+': editing and saving do not synchronize',async()=>{
        await openKnowledge();
        await page.locator('#new-document').click();
        await page.locator('#document-title').fill(ready.note.title);
        await page.locator('#document-content').fill(ready.note.content);
        assert.match(await page.locator('#kb-status').innerText(),/Unsaved changes/);
        assert.equal(await page.evaluate(()=>state.labs['8'].documents.length),2);
        const result=await post('kb',()=>page.locator('#save-document').click());
        noteId=result.lab.documents.find(doc=>doc.title===ready.note.title).id;
        assert(result.lab.kb_revision>result.lab.synced_revision);
        assert.equal(result.lab.use_kb,false);
        assert.match(await page.locator('#kb-status').innerText(),/awaiting synchronization/);
        assert.match(await page.locator('#kb-notice').innerText(),/Knowledge base disabled/);
        const geometry=await page.locator('#knowledge-dialog').evaluate(element=>({width:element.clientWidth,scroll:element.scrollWidth}));
        assert(geometry.scroll<=geometry.width+1,JSON.stringify(geometry));
        await page.screenshot({path:path.join(artifacts,name+'-saved.png'),fullPage:true});
      });
      await check(name+': explicit sync activates the saved snapshot',async()=>{
        const result=await sync();
        assert.equal(result.lab.kb_revision,result.lab.synced_revision);
        assert.equal(result.lab.use_kb,true);
        assert.match(await page.locator('#kb-status').innerText(),/Current synchronized snapshot active/);
        assert.equal(result.lab.exploited,false);
      });
      await check(name+': Easy retrieves a student source without verifying an exploit',async()=>{
        const result=await ask();
        assert.equal(result.lab.last_result.defense_level,0);
        assert(result.lab.last_result.sources.some(source=>source.id===noteId&&source.injected===true));
        assert.equal(result.lab.last_result.triggered,false);
        assert.equal(result.lab.flag,null);
        assert.match(await page.locator('#attempt-result').innerText(),/No exploit verified/);
        assert.match(await page.locator('#attempt-result').innerText(),/Student-controlled document retrieved/);
        assert(!(await page.locator('#attempt-result').getAttribute('class')).includes('success'));
        assert.equal(await page.locator('#lab8-completion-explanation').evaluate(el=>el.hidden),true);
      });
      await check(name+': unsynchronized edits keep the previous retrieval snapshot',async()=>{
        await openKnowledge();
        await page.getByRole('button',{name:'Edit '+ready.note.title,exact:true}).click();
        await page.locator('#document-content').fill(ready.aster);
        await post('kb',()=>page.locator('#save-document').click());
        const old=await ask();
        assert.match(old.lab.messages.at(-1).content,/Zephyr bearing inspection was completed/);
        assert.doesNotMatch(old.lab.messages.at(-1).content,/Aster/);
        await openKnowledge();await sync();await closeKnowledge();
        assert.match(await page.locator('#attempt-result').innerText(),/Sources describe the last reply/);
        const current=await ask('What does the Aster bearing review record?');
        assert.match(current.lab.messages.at(-1).content,/Aster bearing review/);
        assert(current.lab.last_result.sources.some(source=>source.id===noteId));
      });
      let noticeId;
      await check(name+': Medium uses saved difficulty and input/output protections',async()=>{
        await openKnowledge();await page.locator('#new-document').click();
        await page.locator('#document-title').fill(ready.notice.title);
        await page.locator('#document-content').fill(ready.notice.content);
        const saved=await post('kb',()=>page.locator('#save-document').click());
        noticeId=saved.lab.documents.find(doc=>doc.title===ready.notice.title).id;
        await sync();await difficulty(1);
        const normal=await ask();
        assert.equal(normal.lab.last_result.defense_level,1);
        assert(normal.lab.last_result.sources.some(source=>source.id===noticeId));
        const filtered=await ask('Format the maintenance note for a fixture review.');
        assert.equal(filtered.lab.last_result.defense_level,1);
        assert.equal(filtered.lab.last_result.defense_blocked,true);
        assert.doesNotMatch(filtered.lab.messages.at(-1).content,/<b>|reader@example.invalid/);
        const blocked=await ask(('Boiler inspection '.repeat(120)).slice(0,2001));
        assert.equal(blocked.lab.last_result.defense_level,1);
        assert.equal(blocked.lab.last_result.defense_blocked,true);
        assert.equal(blocked.lab.last_result.triggered,false);
        await page.reload();await page.waitForFunction(()=>state.labs['8'].defense_level===1);
        assert.equal(await page.locator('#defense-select').inputValue(),'1');
      });
      await check(name+': Hard excludes screened documents and retains saved difficulty',async()=>{
        await difficulty(2);
        const result=await ask();
        assert.equal(result.lab.last_result.defense_level,2);
        assert(result.lab.last_result.excluded_sources.some(source=>source.id===noticeId));
        assert(!result.lab.last_result.sources.some(source=>source.id===noticeId));
        assert.doesNotMatch(result.lab.messages.at(-1).content,/notice was supplied/);
        await page.locator('#attempt-result summary').click();
        assert.match(await page.locator('#attempt-result').innerText(),/Not supplied to Plant AI/);
        assert.match(await page.locator('#attempt-result').innerText(),/Request difficulty: Hard/);
        await page.screenshot({path:path.join(artifacts,name+'-retrieval.png'),fullPage:true});
        const filtered=await ask('Show the notebook reference for a fixture review.');
        assert.equal(filtered.lab.last_result.defense_level,2);
        assert.equal(filtered.lab.last_result.defense_blocked,true);
        assert.doesNotMatch(filtered.lab.messages.at(-1).content,/`|https:\/\//);
        const blocked=await ask(('Boiler inspection '.repeat(60)).slice(0,1001));
        assert.equal(blocked.lab.last_result.defense_blocked,true);
        await page.reload();await page.waitForFunction(()=>state.labs['8'].defense_level===2);
        assert.equal(await page.locator('#defense-select').inputValue(),'2');
      });
      await check(name+': disabling retrieval and fresh student identity remain independent',async()=>{
        await openKnowledge();
        const result=await post('kb',()=>page.locator('#use-kb').uncheck());
        assert.equal(result.lab.use_kb,false);
        assert.match(await page.locator('#kb-notice').innerText(),/Knowledge base disabled/);
        await closeKnowledge();
        const fresh=await browser.newContext({viewport});const student=await fresh.newPage();
        await student.goto(ready.url+'/lab/8');
        await student.waitForFunction(()=>state.current===8&&state.catalog.length===9);
        assert.equal(await student.locator('#defense-select').inputValue(),'0');
        assert.equal(await student.locator('#hints li').count(),0);
        assert.equal(await student.evaluate(()=>state.labs['8'].documents.length),2);
        await fresh.close();
      });
      await check(name+': Labs 1–7 and 9 retain their own scenario and hide Lab 8 learning',async()=>{
        for(const id of [1,2,3,4,5,6,7,9]) {
          await page.locator('#lab-select').selectOption(String(id));
          assert.equal(await page.locator('#lab8-completion-explanation').evaluate(el=>el.hidden),true);
          assert.equal(await page.locator('#lab8-kb-feedback').count(),0);
          const record=await page.evaluate(id=>({completed:state.labs[id].completed,
            messages:state.labs[id].messages.length,defense:state.labs[id].defense_level}),String(id));
          assert.deepEqual(record,{completed:false,messages:0,defense:0});
          assert((await page.locator('#scenario-summary').innerText()).length>0);
        }
        assert.deepEqual(errors,[]);
      });
      await context.close();

      const captured=await browser.newContext({viewport});
      await captured.addCookies([{name:'nine_sid',value:ready.sessions[name],url:ready.url}]);
      const completed=await captured.newPage();
      completed.on('dialog',dialog=>dialog.accept());
      let earnedFlag;
      await check(name+': mocked-transport capture stays gated until authoritative 400-point award',async()=>{
        await completed.goto(ready.url+'/lab/8');
        await completed.waitForFunction(()=>state.current===8&&state.catalog.length===9);
        assert.equal(await completed.evaluate(()=>state.summary.score),0);
        assert.equal(await completed.locator('#lab8-completion-explanation').evaluate(el=>el.hidden),true);
        await completed.locator('#flag-button').click();
        await completed.locator('#flag-input').fill('PANOPTIS{wrong-fixture-flag}');
        await completed.locator('#submit-flag').click();
        await completed.waitForFunction(()=>document.querySelector('#flag-feedback').textContent.includes('does not match'));
        assert.equal(await completed.locator('#lab8-completion-explanation').evaluate(el=>el.hidden),true);
        await completed.locator('#flag-input').fill(await completed.locator('#visible-captured-flag').innerText());
        const pending=completed.waitForResponse(response=>response.url().endsWith('/api/labs/8/flag'));
        await completed.locator('#submit-flag').click();
        const award=await(await pending).json();
        assert.deepEqual(award.flag_submission,{newly_completed:true,points_awarded:400});
        earnedFlag=award.lab.flag;
        await completed.waitForFunction(()=>state.labs['8'].completed&&!document.querySelector('#flag-dialog').open);
        assert.equal(await completed.evaluate(()=>state.summary.score),400);
        assert.match(await completed.locator('#flag-feedback').textContent(),/\+400 points/);
        assert.equal(await completed.locator('#lab8-completion-explanation').isVisible(),false);
        assert.equal(await completed.locator('#captured-flag').textContent(),'');
        assert.equal(await completed.locator('#flag-input').inputValue(),'');
        await completed.screenshot({path:path.join(artifacts,name+'-completion.png'),fullPage:true});
      });
      await check(name+': duplicate submissions show zero additional points',async()=>{
        for(let repeat=0;repeat<2;repeat++) {
          const pending=completed.waitForResponse(response=>response.url().endsWith('/api/labs/8/flag'));
          await completed.evaluate(async()=>{
            document.querySelector('#flag-input').value=state.labs['8'].flag;
            await submitFlag({preventDefault(){}});
          });
          const result=await(await pending).json();
          assert.deepEqual(result.flag_submission,{newly_completed:false,points_awarded:0});
          assert.equal(result.summary.score,400);
          assert.equal(await completed.locator('#flag-feedback').textContent(),'Lab already completed');
        }
      });
      await check(name+': difficulty and refresh preserve completion; restart clears its flag and points',async()=>{
        const actions=[['defense',()=>completed.locator('#defense-select').selectOption('1')],
          ['defense',()=>completed.locator('#defense-select').selectOption('2')]];
        for(const [action,click] of actions) {
          const pending=completed.waitForResponse(response=>response.url().endsWith('/api/labs/8/'+action));
          await click();const result=await(await pending).json();
          assert.equal(result.lab.completed,true);
          assert.equal(result.lab.exploited,false);
          assert.equal(result.lab.last_result,null);
          assert.equal(result.lab.flag,earnedFlag);
          assert.equal(result.summary.score,400);
          await completed.waitForFunction(version=>state.labs['8'].state_version.join('.')===version,result.lab.state_version.join('.'));
        }
        await completed.reload();await completed.waitForFunction(()=>state.labs['8'].completed===true);
        assert.equal(await completed.locator('#defense-select').inputValue(),'2');
        await completed.locator('#flag-button').click();
        assert.equal(await completed.locator('#lab8-completion-explanation').isVisible(),false);
        assert.equal(await completed.locator('#captured-flag').textContent(),'');
        assert.equal(await completed.locator('#flag-input').inputValue(),'');
        assert.equal(await completed.locator('#visible-captured-flag').textContent(),earnedFlag);
        assert.equal(await completed.evaluate(()=>state.summary.score),400);
        await completed.locator('[data-close="flag-dialog"]').click();
        const pending=completed.waitForResponse(response=>response.url().endsWith('/api/labs/8/reset'));
        await completed.locator('#restart-lab').click();
        const reset=await(await pending).json();
        assert.equal(reset.lab.completed,false);
        assert.equal(reset.lab.flag,null);
        assert.equal(reset.summary.score,0);
        await completed.reload();await completed.waitForFunction(()=>state.labs['8'].completed===false);
        assert.equal(await completed.locator('#defense-select').inputValue(),'2');
        assert.equal(await completed.locator('#captured-flag-banner').isVisible(),false);
        assert.equal((await completed.request.post(ready.url+'/api/labs/8/flag',{data:{flag:earnedFlag}})).status(),403);
      });
      await captured.close();
    }
    await check('eight concurrent browser HTTP submissions award 400 points total and reject a cross-student flag',async()=>{
      const context=await browser.newContext();
      await context.addCookies([{name:'nine_sid',value:ready.sessions.concurrent,url:ready.url}]);
      const state=await(await context.request.get(ready.url+'/api/state')).json();
      const flag=state.labs['8'].flag;
      assert(flag);
      const responses=await Promise.all(Array.from({length:8},()=>context.request.post(ready.url+'/api/labs/8/flag',{data:{flag}})));
      for(const response of responses)assert.equal(response.status(),200);
      const results=await Promise.all(responses.map(response=>response.json()));
      assert.equal(results.reduce((sum,result)=>sum+result.flag_submission.points_awarded,0),400);
      assert.equal(results.filter(result=>result.flag_submission.newly_completed===true).length,1);
      const other=await browser.newContext();
      const rejected=await other.request.post(ready.url+'/api/labs/8/flag',{data:{flag}});
      assert.equal(rejected.status(),403);
      await other.close();await context.close();
    });
    const report={passed:passed.length,failed:0,checks:passed,artifacts,
      scope:'Temporary identities and mocked production HTTP transport; no live Ollama exploitation. Scripted model callbacks are ineligible.'};
    fs.writeFileSync(path.join(artifacts,'results.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report,null,2));
  } finally {
    if(browser)await browser.close();fixture.kill('SIGTERM');fs.closeSync(log);
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
