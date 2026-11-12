/* Isolated UI fixtures: copying never submits a flag; manual pasting does.
 * Uses temporary public state, intercepted APIs and no live exploitation.
 */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const http=require('node:http');
const os=require('node:os');
const path=require('node:path');
const {spawnSync}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
const artifacts=process.env.PANOPTIS_BROWSER_ARTIFACTS||fs.mkdtempSync(path.join(os.tmpdir(),'flag-copy-browser-'));

async function main(){
  const fixture=spawnSync(process.env.PANOPTIS_TEST_PYTHON||'python3',['-S','-B','-c',
    'import json,tempfile\nfrom pathlib import Path\nimport server\nwith tempfile.TemporaryDirectory() as directory:\n app=server.Application(Path(directory))\n sid,data=app.store.get(None)\n print(json.dumps(app.state(sid,data)))'],
    {cwd:root,encoding:'utf8',env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
  assert.equal(fixture.status,0,fixture.stderr);
  const initial=JSON.parse(fixture.stdout);
  fs.mkdirSync(artifacts,{recursive:true});
  const staticRoot=path.join(root,'static');
  const server=http.createServer((request,response)=>{
    const pathname=new URL(request.url,'http://test').pathname;
    const name=/^\/lab\/[1-9]\/?$/.test(pathname)?'index.html':pathname.slice(1);
    const target=path.resolve(staticRoot,name);
    if(!target.startsWith(staticRoot+path.sep)){response.writeHead(404).end();return;}
    fs.readFile(target,(error,data)=>{
      if(error){response.writeHead(404).end();return;}
      response.writeHead(200,{'Content-Type':({'.js':'text/javascript','.css':'text/css','.html':'text/html','.svg':'image/svg+xml'})[path.extname(target)]||'application/octet-stream'});
      response.end(data);
    });
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const port=server.address().port,origin='http://127.0.0.1:'+port;
  let browser,passed=0;
  const check=async(name,fn)=>{await fn();passed++;console.log('PASS '+name);};
  try{
    browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{}),
      args:['--no-sandbox','--no-proxy-server','--host-resolver-rules=MAP panoptis.test 127.0.0.1']});
    const cases=[];
    for(const [device,viewport] of [['desktop',{width:1440,height:1000}],['mobile',{width:360,height:740}]]){
      for(let id=1;id<=9;id++)cases.push({device,viewport,id,insecure:false});
    }
    cases.push({device:'plain-http',viewport:{width:1280,height:720},id:9,insecure:true});
    for(const {device,viewport,id,insecure} of cases){
      const context=await browser.newContext({viewport,...(device==='mobile'?{isMobile:true,hasTouch:true}:{})});
      await context.grantPermissions(['clipboard-read','clipboard-write'],{origin});
      const saved=structuredClone(initial),posts=[],errors=[],flag='PANOPTIS{'+String(id).repeat(32)+'}';
      await context.route('**/api/**',async route=>{
        const request=route.request(),pathname=new URL(request.url()).pathname;
        const headers={'X-Lab2-Progress-Version':saved.labs['2'].completed?'1.1.1':saved.labs['2'].exploited?'1.1.0':'1.0.0'};
        if(pathname==='/api/state')return route.fulfill({json:saved,headers});
        if(pathname==='/api/health')return route.fulfill({json:{ai_ready:true,model:'isolated-ui-fixture'}});
        if(pathname===`/api/labs/${id}/flag`&&request.method()==='POST'){
          const body=request.postDataJSON();posts.push(body);assert.deepEqual(body,{flag});
          saved.labs[String(id)].completed=true;
          saved.summary={...saved.summary,score:saved.catalog[id-1].points,completed:[id]};
          return route.fulfill({json:{lab:saved.labs[String(id)],summary:saved.summary,
            flag_submission:{newly_completed:true,points_awarded:saved.catalog[id-1].points}},
            headers:{'X-Lab2-Progress-Version':'1.1.1'}});
        }
        throw new Error('Unexpected API request: '+request.method()+' '+pathname);
      });
      const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
      await page.goto((insecure?'http://panoptis.test:'+port:origin)+'/lab/'+id,{waitUntil:'networkidle'});
      await page.waitForFunction(()=>state.catalog.length===9);
      const capture={...saved.labs[String(id)],flag,exploited:true,completed:false,
        attempt:1,state_version:[1,1],reply_revision:1,boiler_version:[1,1],
        verified_instruction_count:3,required_instruction_count:3,extraction_completed:true,
        guardrail_status:'disclosed',last_result:null};
      saved.labs[String(id)]=structuredClone(capture);
      await page.evaluate(({id,lab,summary})=>{
        const response={lab,summary};
        acceptResponse(id,id===2?attachLab2Version(response,'1.1.0'):response);
      },{id,lab:capture,summary:saved.summary});
      const label=device+' Lab '+id;
      await check(label+': capture popup contains only flag and copy button',async()=>{
        assert.equal(await page.locator('#flag-capture-dialog').evaluate(dialog=>dialog.open),true);
        assert.equal(await page.locator('#flag-dialog').evaluate(dialog=>dialog.open),false);
        assert.equal(await page.locator('#flag-capture-value').textContent(),flag);
        assert.equal(await page.locator('#flag-capture-dialog button').count(),1);
        assert.equal(await page.locator('#flag-capture-dialog form').count(),0);
        assert.equal(await page.evaluate(()=>state.summary.score),0);assert.equal(posts.length,0);
      });
      await check(label+': copy closes automatically without filling or submitting',async()=>{
        if(insecure)assert.equal(await page.evaluate(()=>window.isSecureContext),false);
        await page.locator('#copy-captured-flag').click();
        await page.waitForFunction(()=>!document.getElementById('flag-capture-dialog').open);
        assert.equal(await page.locator('#flag-input').inputValue(),'');
        assert.equal(await page.evaluate(()=>state.summary.score),0);assert.equal(posts.length,0);
      });
      let copied;
      if(insecure){
        const reader=await context.newPage();await reader.goto(origin+'/lab/'+id,{waitUntil:'networkidle'});
        copied=await reader.evaluate(()=>navigator.clipboard.readText());await reader.close();await page.bringToFront();
      }else copied=await page.evaluate(()=>navigator.clipboard.readText());
      assert.equal(copied,flag);
      await check(label+': top-center flag remains selectable and fits the screen',async()=>{
        const banner=page.locator('#captured-flag-banner');assert.equal(await banner.isVisible(),true);
        assert.equal(await page.locator('#visible-captured-flag').textContent(),flag);
        const geometry=await page.evaluate(()=>{
          const banner=document.getElementById('captured-flag-banner').getBoundingClientRect();
          const main=document.getElementById('main').getBoundingClientRect();
          const heading=document.querySelector('.lab-heading').getBoundingClientRect();
          const range=document.createRange();range.selectNodeContents(document.getElementById('visible-captured-flag'));
          const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
          return {left:banner.left,right:banner.right,center:banner.x+banner.width/2,mainCenter:main.x+main.width/2,
            above:banner.bottom<=heading.top,selected:selection.toString(),overflow:document.documentElement.scrollWidth>innerWidth};
        });
        assert(Math.abs(geometry.center-geometry.mainCenter)<2);
        assert(geometry.left>=0&&geometry.right<=viewport.width&&geometry.above&&!geometry.overflow);
        assert.equal(geometry.selected,flag);
        if(id===9&&!insecure)await page.screenshot({path:path.join(artifacts,device+'-top-flag.png'),fullPage:true});
      });
      await check(label+': points arrive only after manual paste and Submit flag',async()=>{
        await page.locator('#flag-button').click();
        assert.equal(await page.locator('#flag-input').inputValue(),'');assert.equal(posts.length,0);
        await page.locator('#flag-input').fill(copied);
        await page.locator('#submit-flag').click();
        await page.waitForFunction(id=>state.labs[String(id)].completed===true,id);
        assert.equal(posts.length,1);
        assert.equal(await page.evaluate(()=>state.summary.score),saved.catalog[id-1].points);
        assert.equal(await page.locator('#visible-captured-flag').textContent(),flag);
      });
      await check(label+': refresh preserves the earned flag without reopening capture',async()=>{
        await page.reload({waitUntil:'networkidle'});
        assert.equal(await page.locator('#flag-capture-dialog').evaluate(dialog=>dialog.open),false);
        assert.equal(await page.locator('#visible-captured-flag').textContent(),flag);
        assert.equal(await page.evaluate(()=>state.summary.score),saved.catalog[id-1].points);
      });
      await check(label+': lab navigation isolates each displayed flag',async()=>{
        const other=id===9?8:id+1;
        await page.locator('#lab-select').selectOption(String(other));
        assert.equal(await page.locator('#captured-flag-banner').isVisible(),false);
        await page.locator('#lab-select').selectOption(String(id));
        assert.equal(await page.locator('#visible-captured-flag').textContent(),flag);
        assert.equal(await page.locator('#flag-capture-dialog').evaluate(dialog=>dialog.open),false);
        assert.deepEqual(errors,[]);
      });
      await context.close();
    }
    console.log('Flag copy browser checks passed: '+passed);
  }finally{
    if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));
  }
}
main().catch(error=>{console.error(error.stack);process.exitCode=1;});
