/* Frontend browser checks with temporary public state and simulated flag API.
 * No live model, student database, or verification rules are changed.
 * Optional: PLAYWRIGHT_MODULE, CHROMIUM_PATH, PANOPTIS_BROWSER_ARTIFACTS.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const staticRoot = path.resolve(process.env.PANOPTIS_BROWSER_STATIC_ROOT || path.join(root, 'static'));
const artifacts = process.env.PANOPTIS_BROWSER_ARTIFACTS || fs.mkdtempSync(path.join(os.tmpdir(), 'lab9-learning-browser-'));
const flag = 'PANOPTIS{learning-browser-fixture}';
const paragraphs = [
  'Vulnerability: Guardrail Erosion.',
  'Entry point: Multi-turn conversation.',
  'Trust boundary: Student messages attempting to override information-disclosure restrictions.',
  'Impact: Disclosure of restricted synthetic technician records.',
  'Defenses:',
];
const defenses = ['Conversation-aware authorization.', 'Least-privilege model context.',
  'Consistent refusals.', 'Output validation.'];

async function main() {
  const fixture = spawnSync(process.env.PANOPTIS_TEST_PYTHON || 'python3', ['-S', '-B', '-c', `
import json, re, sys, tempfile
from pathlib import Path
import catalog, server
html = Path(sys.argv[1]).read_text()
explanation = re.search(r'<section\\b[^>]*id="lab9-completion-explanation"[^>]*>[\\s\\S]*?</section>', html).group()
assert all(field['value'].casefold() not in explanation.casefold() for field in catalog.LAB9_PROTECTED_FIELDS), 'Protected values must not appear in the explanation'
with tempfile.TemporaryDirectory() as directory:
    app = server.Application(Path(directory))
    sid, data = app.store.get(None)
    print(json.dumps(app.state(sid, data)))
`, path.join(staticRoot, 'index.html')], {
    cwd:process.env.PANOPTIS_TEST_PROJECT_ROOT || root, encoding:'utf8',
    env:{...process.env, PYTHONDONTWRITEBYTECODE:'1'},
  });
  assert.equal(fixture.status, 0, 'Public fixture setup failed: ' + fixture.stderr);
  const initial = JSON.parse(fixture.stdout);
  fs.mkdirSync(artifacts, {recursive:true});
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, 'http://test').pathname;
    const filename = /^\/lab\/[1-9]\/?$/.test(pathname) || pathname === '/' ? 'index.html' : pathname.slice(1);
    const target = path.resolve(staticRoot, filename);
    if (!target.startsWith(staticRoot + path.sep)) { response.writeHead(404).end(); return; }
    const types = {'.js':'text/javascript', '.css':'text/css', '.html':'text/html', '.svg':'image/svg+xml'};
    fs.readFile(target, (error, content) => {
      if (error) { response.writeHead(404).end(); return; }
      response.writeHead(200, {'Content-Type':types[path.extname(target)] || 'application/octet-stream'});
      response.end(content);
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const url = 'http://127.0.0.1:' + server.address().port;
  let browser, passed = 0;
  const check = async(name, fn) => { await fn(); passed++; console.log('PASS ' + name); };
  try {
    browser = await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH
      ? {executablePath:process.env.CHROMIUM_PATH, args:['--no-sandbox']} : {})});
    for (const [name, viewport] of Object.entries({desktop:{width:1440,height:1000},
      desktopShort:{width:1280,height:600}, mobile:{width:390,height:844}, mobileSmall:{width:360,height:740}})) {
      const context = await browser.newContext({viewport, ...(name.startsWith('mobile') ? {isMobile:true,hasTouch:true} : {})});
      const saved = structuredClone(initial), errors = [], unexpected = [];
      let releaseFlag, receivedFlag;
      const flagReceived = new Promise(resolve => { receivedFlag = resolve; });
      await context.route('**/api/**', async route => {
        const request = route.request(), pathname = new URL(request.url()).pathname;
        if (pathname === '/api/state' && request.method() === 'GET') return route.fulfill({json:saved});
        if (pathname === '/api/health' && request.method() === 'GET') return route.fulfill({json:{ai_ready:true,model:'isolated-ui-test'}});
        if (pathname === '/api/labs/9/flag' && request.method() === 'POST') {
          assert.deepEqual(Object.keys(request.postDataJSON()), ['flag']);
          if (request.postDataJSON().flag !== flag) return route.fulfill({status:400,json:{error:'That flag does not match this lab and attempt.'}});
          const newlyCompleted = !saved.labs['9'].completed;
          if (newlyCompleted) await new Promise(resolve => { releaseFlag = resolve; receivedFlag(); });
          saved.labs['9'].completed = true;
          saved.summary = {...saved.summary,score:500,completed:[9]};
          return route.fulfill({json:{lab:saved.labs['9'],summary:saved.summary,
            flag_submission:{newly_completed:newlyCompleted,points_awarded:newlyCompleted?500:0}}});
        }
        if (['/api/labs/9/reset','/api/labs/9/defense'].includes(pathname) && request.method() === 'POST') {
          const record = saved.labs['9'];
          saved.labs['9'] = {...record,state_version:[record.state_version[0]+1,0],
            exploited:false,guardrail_status:'normal',messages:[],last_result:null,
            defense_level:pathname.endsWith('/defense')?request.postDataJSON().level:record.defense_level};
          return route.fulfill({json:{lab:saved.labs['9'],summary:saved.summary}});
        }
        unexpected.push(request.method() + ' ' + pathname);
        return route.abort();
      });
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(error.message));
      page.on('dialog', dialog => dialog.accept());
      await page.goto(url + '/lab/9', {waitUntil:'networkidle'});
      const explanation = page.locator('#lab9-completion-explanation');
      const openFlag = async() => {
        if (!(await page.locator('#flag-dialog').evaluate(dialog=>dialog.open))) await page.locator('#flag-button').click();
      };
      const closeFlag = () => page.locator('[data-close="flag-dialog"]').click();
      await check(name + ': fresh conversation hides learning', async() => {
        await openFlag(); assert.equal(await explanation.isVisible(), false);
      });
      await check(name + ': verified capture alone keeps learning hidden', async() => {
        saved.labs['9'] = {...saved.labs['9'],exploited:true,flag,guardrail_status:'disclosed',state_version:[1,3]};
        await page.evaluate(result=>acceptResponse(9,result), {lab:saved.labs['9'],summary:saved.summary});
        assert.equal(await explanation.isVisible(), false);
        assert.equal(await page.locator('#submit-flag').isEnabled(), true);
      });
      await check(name + ': rejected flag does not reveal learning', async() => {
        await page.locator('#flag-input').fill('PANOPTIS{incorrect-test-flag}');
        await page.locator('#submit-flag').click();
        await page.waitForFunction(()=>!state.flagBusy.has(9));
        assert.match(await page.locator('#flag-feedback').textContent(), /does not match/);
        assert.equal(await explanation.isVisible(), false);
      });
      await check(name + ': successful submission reveals learning only after acknowledgement', async() => {
        await page.locator('#flag-input').fill(flag);
        await page.locator('#submit-flag').click();
        await flagReceived;
        await page.waitForFunction(()=>state.flagBusy.has(9));
        assert.equal(await explanation.isVisible(), false);
        assert.equal(typeof releaseFlag, 'function');
        releaseFlag();
        await page.waitForFunction(()=>state.labs['9'].completed === true);
        assert.equal(await explanation.isVisible(), true);
        assert.deepEqual((await explanation.locator('p').allTextContents()).map(text=>text.trim()), paragraphs);
        assert.deepEqual((await explanation.locator('li').allTextContents()).map(text=>text.trim()), defenses);
      });
      await check(name + ': existing dialog design remains readable and scrollable', async() => {
        const dialog = page.locator('#flag-dialog');
        assert.equal(await dialog.evaluate(element=>element.scrollWidth <= element.clientWidth), true);
        const typography = await explanation.evaluate(element=>({font:parseFloat(getComputedStyle(element).fontSize),line:parseFloat(getComputedStyle(element).lineHeight)}));
        assert(typography.font >= 13 && typography.line >= typography.font * 1.4);
        for (const item of await explanation.locator('p, li').all()) {
          await item.scrollIntoViewIfNeeded();
          const box = await item.boundingBox(), bounds = await dialog.boundingBox();
          assert(box && box.width > 0 && box.x >= bounds.x && box.x + box.width <= bounds.x + bounds.width);
          assert(box.y >= 0 && box.y + box.height <= viewport.height);
        }
        await dialog.screenshot({path:path.join(artifacts,name + '-learning.png')});
      });
      await check(name + ': refresh restores saved learning without resubmitting', async() => {
        await page.reload({waitUntil:'networkidle'});
        await openFlag();
        assert.equal(await explanation.isVisible(), true);
        assert.equal(await page.locator('#flag-form').isVisible(), false);
        assert.deepEqual((await explanation.locator('li').allTextContents()).map(text=>text.trim()), defenses);
      });
      await check(name + ': duplicate submission does not report another award', async() => {
        await page.evaluate(()=>{document.getElementById('flag-input').value=state.labs['9'].flag;});
        await page.evaluate(()=>submitFlag({preventDefault(){}}));
        assert.equal(await page.locator('#flag-feedback').textContent(),'Lab already completed');
        assert.equal(await page.evaluate(()=>state.summary.score),500);
      });
      await check(name + ': new chat preserves earned flag and learning with normal progress', async() => {
        await closeFlag();
        await page.locator('#restart-lab').click();
        await page.waitForFunction(()=>state.labs['9'].state_version[0]===2);
        assert.equal(await page.locator('#lab9-progress-status').textContent(),'Normal conversation.');
        await openFlag();
        assert.equal(await explanation.isVisible(),true);
        assert.equal(await page.locator('#captured-flag').textContent(),flag);
        assert.equal(await page.locator('#captured-panel').isVisible(),true);
        assert.equal(await page.evaluate(()=>state.summary.score),500);
      });
      await check(name + ': difficulty change preserves the earned flag and clears current verification', async() => {
        await closeFlag();
        await page.locator('#defense-select').selectOption('1');
        await page.waitForFunction(()=>state.labs['9'].state_version[0]===3);
        assert.equal(await page.locator('#lab9-progress-status').textContent(),'Normal conversation.');
        await openFlag();
        assert.equal(await explanation.isVisible(),true);
        assert.equal(await page.locator('#captured-flag').textContent(),flag);
        assert.equal(await page.evaluate(()=>state.summary.score),500);
      });
      await check(name + ': another lab never displays Lab 9 learning', async() => {
        await closeFlag();
        await page.locator('#lab-select').selectOption('8');
        await openFlag();
        assert.equal(await explanation.isVisible(), false);
      });
      await check(name + ': no browser errors or unexpected API calls', async() => {
        assert.deepEqual(errors, []); assert.deepEqual(unexpected, []);
      });
      await context.close();
    }
    console.log('Lab 9 learning browser checks passed: ' + passed);
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error=>{console.error(error.stack);process.exitCode=1;});
