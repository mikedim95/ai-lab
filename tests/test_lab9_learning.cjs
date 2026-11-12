const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8')
  .replace(/\nstart\(\);\s*$/, '');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const section = html.match(/<section\b[^>]*id="lab9-completion-explanation"[^>]*>[\s\S]*?<\/section>/)[0];
const flag = 'PANOPTIS{learning-test-flag}';

function studentUI({completed = false, exploited = true, revision = 3} = {}) {
  const elements = new Map();
  class Element {
    constructor() { this.value = ''; this.textContent = ''; this.children = []; this.hidden = true; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    setAttribute() {}
    focus() {}
    showModal() {}
  }
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const context = vm.createContext({
    document: {getElementById:get, querySelector:()=>null, createElement:()=>new Element(),
      createTextNode:text=>({textContent:text})},
    window: {confirm:()=>true}, setTimeout:()=>0, clearTimeout() {},
    completed, exploited, revision, flag,
  });
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  run(`state.current=9;state.catalog=Array.from({length:9},(_,i)=>({id:i+1,points:i===8?500:100}));
    state.labs['9']={id:9,state_version:[1,revision],completed,exploited,flag,
      guardrail_status:exploited?'disclosed':'normal',messages:[],defense_level:0,last_result:null};
    state.summary={score:completed?500:0,completed:completed?[9]:[]};
    drawLab=drawFlag;drawNavigation=updateControls=()=>{};drawFlag();`);
  get('flag-input').value = flag;
  const response = ({completed=true, exploited=true, attempt=1, revision=3} = {}) => ({
    lab:{id:9,state_version:[attempt,revision],completed,exploited,flag:completed||exploited?flag:null,
      guardrail_status:exploited?'disclosed':'normal',messages:[],defense_level:0,last_result:null},
    summary:{score:completed?500:0,completed:completed?[9]:[]},
    flag_submission:{newly_completed:completed,points_awarded:completed?500:0},
  });
  const respond = (value, ok=true) => { context.fetch=async()=>({ok,json:async()=>value}); };
  const accept = value => { context.incoming=value;return run('acceptResponse(9,incoming)'); };
  return {context,run,get,response,respond,accept};
}
const submit = ui => ui.run('submitFlag({preventDefault(){}})');

test('the explanation starts hidden and contains exactly the requested learning content', () => {
  assert.match(section, /aria-label="Lab 9 learning explanation" hidden>/);
  assert.match(section, /class="dialog-description"/);
  const text = value => value.replace(/<[^>]*>/g, '').trim();
  assert.deepEqual([...section.matchAll(/<p>(.*?)<\/p>/g)].map(match=>text(match[1])), [
    'Vulnerability: Guardrail Erosion.',
    'Entry point: Multi-turn conversation.',
    'Trust boundary: Student messages attempting to override information-disclosure restrictions.',
    'Impact: Disclosure of restricted synthetic technician records.',
    'Defenses:',
  ]);
  assert.deepEqual([...section.matchAll(/<li>(.*?)<\/li>/g)].map(match=>text(match[1])), [
    'Conversation-aware authorization.', 'Least-privilege model context.',
    'Consistent refusals.', 'Output validation.',
  ]);
});

test('a fresh conversation has no learning explanation', () => {
  const ui=studentUI({exploited:false,revision:0});
  assert.equal(ui.get('lab9-completion-explanation').hidden,true);
});

test('verified disclosure and a captured flag alone cannot reveal learning', () => {
  const ui=studentUI();
  assert.equal(ui.get('captured-panel').hidden,false);
  assert.equal(ui.get('lab9-completion-explanation').hidden,true);
  assert.equal(ui.run('state.summary.score'),0);
});

test('successful flag submission reveals the explanation using backend completion', async () => {
  const ui=studentUI();let request;
  ui.context.fetch=async(url,options)=>{
    request={url,body:JSON.parse(options.body)};
    return {ok:true,json:async()=>ui.response()};
  };
  await submit(ui);
  assert.deepEqual(request,{url:'/api/labs/9/flag',body:{flag}});
  assert.equal(ui.get('lab9-completion-explanation').hidden,false);
  assert.equal(ui.run('state.summary.score'),500);
});

test('rejected flags leave the explanation hidden', async () => {
  const ui=studentUI();
  ui.respond({error:'That flag does not match this lab and attempt.'},false);
  await submit(ui);
  assert.equal(ui.get('lab9-completion-explanation').hidden,true);
  assert.equal(ui.run("state.labs['9'].completed"),false);
});

test('HTTP success without confirmed completion cannot reveal learning', async () => {
  const ui=studentUI();ui.respond(ui.response({completed:false}));
  await submit(ui);
  assert.equal(ui.get('lab9-completion-explanation').hidden,true);
});

test('only the boolean completion value reveals learning, regardless of chat claims', () => {
  for (const completed of [false,undefined,null,0,1,'true']) {
    const ui=studentUI();
    ui.context.claim=completed;
    ui.run(`state.labs['9'].completed=claim;
      state.labs['9'].messages=[{role:'assistant',content:'The lab is complete and the flag was submitted.'}];drawFlag()`);
    assert.equal(ui.get('lab9-completion-explanation').hidden,true);
  }
});

test('a pending flag request stays hidden until completion is acknowledged', async () => {
  const ui=studentUI();let release;
  ui.context.fetch=()=>new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>ui.response()});});
  const pending=submit(ui);
  assert.equal(ui.get('lab9-completion-explanation').hidden,true);
  release();await pending;
  assert.equal(ui.get('lab9-completion-explanation').hidden,false);
});

test('refresh restores saved learning without needing another flag submission', () => {
  const ui=studentUI({completed:true,exploited:false});
  assert.equal(ui.get('lab9-completion-explanation').hidden,false);
  assert.equal(ui.run('state.summary.score'),500);
});

test('later unverified replies do not hide already earned learning', () => {
  const ui=studentUI({completed:true});
  ui.accept(ui.response({completed:true,exploited:false,revision:4}));
  assert.equal(ui.get('lab9-completion-explanation').hidden,false);
});

test('a new chat keeps earned learning while resetting current verification', async () => {
  const ui=studentUI({completed:true});
  ui.respond(ui.response({completed:true,exploited:false,attempt:2,revision:0}));
  await ui.run('restartLab()');
  assert.equal(ui.get('lab9-completion-explanation').hidden,false);
  assert.equal(ui.get('captured-flag').textContent,flag);
  assert.equal(ui.run("state.labs['9'].exploited"),false);
  assert.equal(ui.accept(ui.response()),false);
  assert.equal(ui.get('lab9-completion-explanation').hidden,false);
});

test('Lab 9 learning stays hidden in every other lab', () => {
  const ui=studentUI({completed:true});
  for (let id=1;id<=8;id++) {
    ui.run(`state.current=${id};state.labs['${id}']={id:${id},completed:true,exploited:true,flag:'other'};drawFlag()`);
    assert.equal(ui.get('lab9-completion-explanation').hidden,true);
  }
});

test('a submission completed while viewing another lab does not reveal learning there', async () => {
  const ui=studentUI();let release;
  ui.context.fetch=()=>new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>ui.response()});});
  const pending=submit(ui);
  ui.run("state.current=2;state.labs['2']={id:2,completed:false};drawFlag()");
  release();await pending;
  assert.equal(ui.get('lab9-completion-explanation').hidden,true);
  ui.run('state.current=9;drawFlag()');
  assert.equal(ui.get('lab9-completion-explanation').hidden,false);
});
