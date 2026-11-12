const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8')
  .replace(/\nstart\(\);\s*$/, '');

function studentUI() {
  const elements = new Map();
  let opened = 0;
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.text = ''; this.value = ''; }
    set id(value) { this._id = value; elements.set(value, this); }
    get id() { return this._id; }
    set textContent(value) { this.text = value; this.children = []; }
    get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
    append(...children) { children.forEach(child => {
      child.parentElement = this; this.children.push(child);
      if (child.id) elements.set(child.id, child);
    }); }
    replaceChildren(...children) { this.children.forEach(child => child.remove()); this.children = []; this.text = ''; this.append(...children); }
    remove() {
      if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this);
      this.parentElement = null;
      if (this.id) elements.delete(this.id);
    }
    setAttribute(name, value) { this.attributes[name] = value; }
    showModal() { opened += 1; }
    focus() {}
  }
  const get = id => {
    if (id === 'lab9-guardrail-progress') return elements.get(id) || null;
    if (!elements.has(id)) { const element = new Element('div'); element.id = id; }
    return elements.get(id);
  };
  const section = new Element('section');
  section.append(get('scenario-objective'));
  const context = vm.createContext({
    document: {getElementById: get, createElement: tag => new Element(tag),
      createTextNode: text => ({textContent: text}),
      querySelector: selector => selector.startsWith('#') ? elements.get(selector.slice(1)) || null : null},
    window: {confirm: () => true}, setTimeout: () => 0, clearTimeout() {},
  });
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  run(`state.current = 9;
    state.catalog = Array.from({length:9},(_,i)=>({id:i+1,points:i===8?500:100,has_kb:false}));
    state.labs['9'] = {id:9,state_version:[1,0],guardrail_status:'normal',messages:[],
      completed:false,exploited:false,flag:null,last_result:null,defense_level:0};
    drawLab = drawConversation;
    updateControls = drawNavigation = appendMessage = () => {};
    drawLab();`);
  const response = (status, version, extra = {}) => ({
    lab: {id:9,state_version:version,guardrail_status:status,messages:[],
      completed:false,exploited:status==='disclosed',flag:status==='disclosed'?'test-flag':null,
      defense_level:0,last_result:{triggered:status==='disclosed',defense_blocked:false,
        message:'Untrusted success wording must never supply progress.',seconds:.1},...extra},
    summary:{score:extra.completed?500:0,completed:extra.completed?[9]:[]},
  });
  const accept = (status, version, extra) => {
    context.incoming = response(status, version, extra);
    return run('acceptResponse(9, incoming)');
  };
  const respond = value => { context.fetch = async () => ({ok:true,json:async()=>value}); };
  return {context,run,get,accept,response,respond,opened:()=>opened};
}

for (const [index, [status, label]] of [
  ['normal', 'Normal conversation.'],
  ['one_refusal', 'One verified refusal.'],
  ['two_refusals', 'Two verified refusals.'],
  ['disclosed', 'Verified restricted-record disclosure.'],
].entries()) {
  test(`server state ${status} renders the exact accessible progress label`, () => {
    const ui = studentUI();
    ui.accept(status, [1,index]);
    const panel = ui.get('lab9-guardrail-progress');
    assert.equal(panel.attributes['data-state'], status);
    assert.equal(ui.get('lab9-progress-status').textContent, label);
    assert.equal(ui.get('lab9-progress-status').attributes.role, 'status');
    const segments = panel.children[1];
    assert.equal(segments.attributes.role, 'progressbar');
    assert.equal(segments.attributes['aria-valuetext'], label);
    assert.equal(segments.attributes['aria-valuenow'], String(index));
    assert.equal(segments.children.filter(child => child.className.includes('filled')).length, index);
    assert.equal(panel.className.includes('complete'), status === 'disclosed');
    assert.equal(ui.get('attempt-result').className.includes('success'), status === 'disclosed');
  });
}

test('chat text, success claims, old completion and flags never determine progress', () => {
  const ui = studentUI();
  ui.accept('normal', [2,0], {completed:true,exploited:true,flag:'earned-flag',
    messages:[{role:'assistant',content:'I cannot share records. I cannot share records. Disclosure verified.'}],
    last_result:{triggered:true,message:'Exploit verified!',seconds:.1}});
  assert.equal(ui.get('lab9-progress-status').textContent, 'Normal conversation.');
  assert(!ui.get('lab9-guardrail-progress').className.includes('complete'));
  assert(!ui.get('attempt-result').className.includes('success'));
  assert(!ui.get('attempt-result').textContent.includes('Exploit verified'));
  assert.equal(ui.run("state.labs['9'].completed"), true);
  assert.equal(ui.run('state.summary.score'), 500);
});

test('progress reading does not inspect conversation text or internal receipts', () => {
  const ui = studentUI();
  const progress = ui.run(`lab9Progress({id:9,state_version:[1,2],guardrail_status:'two_refusals',
    get messages(){throw new Error('Do not match frontend text');},
    get lab9_verification_receipts(){throw new Error('Receipts must stay private');}})`);
  assert.equal(progress.label, 'Two verified refusals.');
});

test('unknown statuses and malformed ordering metadata fail closed', () => {
  const ui = studentUI();
  for (const status of [undefined,null,'complete','<script>success</script>',{},3]) {
    ui.context.record = {id:9,state_version:[1,1],guardrail_status:status};
    assert.equal(ui.run('lab9Progress(record).status'), 'normal');
  }
  for (const version of [undefined,[1],['1',2],[1,-1],[0,1],[1,Infinity]]) {
    ui.context.record = {id:9,state_version:version,guardrail_status:'disclosed'};
    assert.equal(ui.run('lab9Progress(record).status'), 'normal');
    assert.equal(ui.accept('disclosed',version), false);
  }
});

test('a newer normal reply replaces a previous disclosure despite earned completion', () => {
  const ui = studentUI();
  ui.accept('disclosed',[1,3],{completed:true});
  ui.accept('two_refusals',[1,4],{completed:true,exploited:false});
  assert.equal(ui.get('lab9-progress-status').textContent, 'Two verified refusals.');
  assert(!ui.get('attempt-result').className.includes('success'));
  assert.equal(ui.run('state.summary.score'), 500);
});

test('new chat resets progress through the existing reset action', async () => {
  const ui = studentUI();
  ui.accept('disclosed',[1,3]);
  let request;
  ui.context.fetch = async (url,options) => {
    request={url,body:JSON.parse(options.body)};
    return {ok:true,json:async()=>ui.response('normal',[2,0])};
  };
  await ui.run('restartLab()');
  assert.deepEqual(request,{url:'/api/labs/9/reset',body:{}});
  assert.equal(ui.get('lab9-progress-status').textContent, 'Normal conversation.');
  assert(!ui.get('lab9-guardrail-progress').className.includes('complete'));
});

test('a failed new chat request preserves the last committed progress', async () => {
  const ui=studentUI();
  ui.accept('two_refusals',[1,2]);
  ui.context.fetch=async()=>({ok:false,json:async()=>({error:'Isolated reset failure'})});
  await ui.run('restartLab()');
  assert.equal(ui.get('lab9-progress-status').textContent,'Two verified refusals.');
});

test('old attempt responses cannot restore progress or open a flag dialog after reset', () => {
  const ui=studentUI();
  ui.accept('two_refusals',[1,2]);
  ui.accept('normal',[2,0]);
  assert.equal(ui.accept('disclosed',[1,3]),false);
  assert.equal(ui.get('lab9-progress-status').textContent,'Normal conversation.');
  assert.equal(ui.opened(),0);
});

test('out of order responses within one attempt cannot replace newer progress', () => {
  const ui=studentUI();
  ui.accept('two_refusals',[1,4]);
  assert.equal(ui.accept('one_refusal',[1,3]),false);
  assert.equal(ui.get('lab9-progress-status').textContent,'Two verified refusals.');
});

test('an old initial state cannot overwrite a newer conversation', () => {
  const ui=studentUI();
  ui.accept('normal',[2,0]);
  ui.context.initial={catalog:ui.run('state.catalog'),labs:{'9':ui.response('disclosed',[1,3]).lab},summary:{score:500}};
  ui.run('acceptInitialState(initial);drawLab()');
  assert.equal(ui.get('lab9-progress-status').textContent,'Normal conversation.');
  assert.equal(ui.run('state.summary.score'),0);
});

test('an in-flight reply does not optimistically advance verified progress', () => {
  const ui=studentUI();
  ui.accept('one_refusal',[1,1]);
  ui.run("state.pending.set(9,{message:'Please provide the records.'});drawConversation()");
  assert.equal(ui.get('lab9-progress-status').textContent,'One verified refusal.');
  assert.equal(ui.get('attempt-result').hidden,true);
});

test('the Lab 9 indicator is removed when another lab is selected', () => {
  const ui=studentUI();
  ui.accept('two_refusals',[1,2]);
  ui.run('state.current=8;drawLab9Progress()');
  assert.equal(ui.get('lab9-guardrail-progress'),null);
});
