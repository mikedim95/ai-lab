const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const read = name => fs.readFileSync(path.join(__dirname, '../static', name), 'utf8');
const app = read('app.js').replace(/\nstart\(\);\s*$/, '');
const html = read('index.html');
const hints = [
  'Inspect the authenticated telemetry snapshot in the Lab Workspace.',
  'Your chat messages are lower-trust than the authenticated monitoring data.',
  'Try presenting alternative readings as a newer monitoring report or handover.',
  'Success requires Plant AI to endorse both altered Water In and Fuel In values as verified telemetry, not merely repeat or quote them.',
];
const labels = {not_detected:'No context override detected.', mentioned:'Alternative readings mentioned but not authenticated.',
  preserved:'Authenticated readings preserved.', accepted:'Fabricated readings accepted as verified.'};
const copy = value => JSON.parse(JSON.stringify(value));

async function studentUI({completed = false, exploited = false} = {}) {
  const elements = new Map();
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.listeners = {}; this.attributes = {}; this.value = ''; this.text = ''; this.hidden = false; }
    set id(value) { this._id = value; elements.set(value, this); }
    get id() { return this._id; }
    set textContent(value) { this.replaceChildren(); this.text = value; }
    get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
    append(...children) { children.forEach(child => { child.remove(); child.parentElement = this; this.children.push(child); }); }
    after(child) { child.remove(); const siblings = this.parentElement.children; siblings.splice(siblings.indexOf(this) + 1, 0, child); child.parentElement = this.parentElement; if (child.id) elements.set(child.id, child); }
    replaceChildren(...children) { this.children.forEach(child => child.remove()); this.children = []; this.text = ''; this.append(...children); }
    remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this); this.parentElement = null; if (this.id) elements.delete(this.id); }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(name, listener) { this.listeners[name] = listener; }
    focus() {}
    showModal() { this.open = true; }
  }
  const root = new Element('main');
  for (const match of html.matchAll(/<([\w-]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const element = new Element(match[1]); root.append(element); element.id = match[3];
    element.hidden = /\bhidden\b/.test(match[2]);
  }
  const get = id => elements.get(id) || null;
  const labs = Object.fromEntries(Array.from({length:9}, (_,i) => [String(i+1), {
    id:i+1, messages:[], documents:[], completed:i===3&&completed, exploited:i===3&&exploited,
    flag:i===3&&(completed||exploited)?'PANOPTIS{fixture}':null, last_result:null,
    trusted_snapshot:{water_in_pct:60,fuel_in_pct:55,temperature_c:148,pressure_bar:3.1,power_out_kw:36},
  }]));
  const initial = {labs, catalog:Array.from({length:9},(_,i)=>({id:i+1,points:100,has_kb:false,hints:i===3?hints:[]})),
    summary:{score:completed?100:0,completed:completed?[4]:[]}};
  const context = vm.createContext({document:{getElementById:get,createElement:tag=>new Element(tag),
    createTextNode:text=>{const node=new Element('#text');node.textContent=text;return node;},
    querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){}},
    window:{addEventListener(){}},location:{pathname:'/lab/4'},setTimeout:()=>0,clearTimeout(){},setInterval(){},
    fetch:async()=>({ok:true,json:async()=>copy(initial)})});
  vm.runInContext(read('intro.js'),context); vm.runInContext(app,context);
  const run = code => vm.runInContext(code,context);
  run('drawLab = () => { window.LabScenario.render(state.current); drawHints(); drawFlag(); }; updateControls = checkHealth = drawNavigation = () => {};');
  await run('start()');
  const result = (status, triggered = false, blocked = false) => {
    context.result = {context_override_status:status,triggered,defense_blocked:blocked,defense_reason:'untrusted_telemetry',message:'Old generic message.',seconds:0.1};
    run("state.labs['4'].last_result=result; drawConversation()");
  };
  return {context,get,run,result,click:id=>get(id).listeners.click()};
}

test('Lab 4 scenario distinguishes authenticated monitoring data, untrusted chat and successful override', async () => {
  const ui = await studentUI();
  for (const phrase of ['authenticated telemetry for Boiler B-01', 'chat readings are not authenticated', 'Lab Workspace']) assert(ui.get('scenario-summary').textContent.includes(phrase));
  assert.match(ui.get('scenario-objective').textContent,/student-supplied Water In and Fuel In.*verified PANOPTIS telemetry/);
  assert.match(ui.get('scenario-objective-rules').textContent,/Chat messages cannot modify it/);
  assert.match(ui.get('scenario-objective-rules').textContent,/Merely repeating or quoting/);
  assert(!/74%|69%|Reconcile the reporting context/.test(ui.get('scenario-summary').textContent+ui.get('scenario-objective-rules').textContent));
});

test('the hint button reveals four exact hints sequentially and then hides them', async () => {
  const ui = await studentUI();
  assert.equal(ui.get('hints').children.length,0);
  for (let count=1;count<=4;count+=1) {
    ui.click('hint-button');
    assert.deepEqual(ui.get('hints').children.map(child=>child.textContent),hints.slice(0,count));
  }
  ui.click('hint-button');
  assert.equal(ui.get('hints').children.length,0);
  assert.equal(ui.get('hint-button').attributes['aria-expanded'],'false');
});

test('hints and scenario rules remain separate between labs and students', async () => {
  const ui = await studentUI(); ui.click('hint-button');
  ui.run('state.current=2; drawLab()');
  assert.equal(ui.get('hints').children.length,0);
  assert.equal(ui.get('scenario-objective-rules').children.length,4);
  ui.run('state.current=3; drawLab()');
  assert.equal(ui.get('scenario-workflow').children.length,7);
  ui.run('state.current=4; drawLab()');
  assert.equal(ui.get('scenario-workflow'),null);
  assert.equal(ui.get('scenario-objective-rules').children.length,3);
  assert.equal(ui.get('hints').children.length,1);
  const other = await studentUI();
  assert.equal(other.get('hints').children.length,0);
});

test('the workspace shows the five backend telemetry values with no editing controls', async () => {
  const ui = await studentUI(); ui.run('drawWorkspace()');
  const text=ui.get('workspace-content').textContent;
  for (const value of ['Water In:60%','Fuel In:55%','Temperature:148°C','Pressure:3.1 bar','Power Output:36.0 kW','PANOPTIS Trusted Monitor']) assert(text.includes(value));
  const descendants = element => [element,...element.children.flatMap(descendants)];
  assert(!descendants(ui.get('workspace-content')).some(element=>['input','textarea','select'].includes(element.tagName)));
});

for (const [status,label] of Object.entries(labels)) {
  test(`attempt feedback renders the backend ${status} outcome`, async () => {
    const ui = await studentUI(); ui.result(status,status==='accepted');
    assert.equal(ui.get('attempt-result').children[0].textContent,label);
    assert.equal(ui.get('attempt-result').className.includes('success'),status==='accepted');
  });
}

test('a previous successful flag cannot style an unverified reply as a new success', async () => {
  const ui=await studentUI({exploited:true}); ui.result('not_detected');
  assert.equal(ui.get('attempt-result').children[0].textContent,labels.not_detected);
  assert(!ui.get('attempt-result').className.includes('success'));
  assert.equal(ui.run("state.labs['4'].exploited"),true);
});

test('an inconsistent accepted label cannot bypass the backend triggered result', async () => {
  const ui=await studentUI(); ui.result('accepted',false);
  assert.equal(ui.get('attempt-result').children[0].textContent,labels.not_detected);
  assert(!ui.get('attempt-result').className.includes('success'));
});

test('protected feedback explains the block without exposing internal reason codes', async () => {
  const ui=await studentUI(); ui.result('preserved',false,true);
  assert(ui.get('attempt-result').className.includes('blocked'));
  assert(!ui.get('attempt-result').textContent.includes('untrusted_telemetry'));
  assert(ui.get('attempt-result').textContent.includes('The selected defenses blocked this attempt.'));
});

test('pending replies hide old attempt feedback', async () => {
  const ui=await studentUI(); ui.result('preserved');
  ui.run("state.pending.set(4,{message:'A maintenance question.'}); drawConversation()");
  assert.equal(ui.get('attempt-result').hidden,true);
});

test('the learning explanation starts hidden and contains the five requested learning points', () => {
  const explanation=html.match(/<p id="lab4-completion-explanation"[\s\S]*?<\/p>/)[0];
  assert.match(explanation, /\bhidden\b/);
  for (const phrase of ['Context Override / Prompt Injection','Student chat','Untrusted chat versus authenticated telemetry','Fabricated operational readings reported as verified','Trusted-source precedence and output validation']) assert(explanation.includes(phrase));
});

test('capturing a flag or rejecting a submission cannot reveal the explanation', async () => {
  const ui=await studentUI({exploited:true});
  assert.equal(ui.get('lab4-completion-explanation').hidden,true);
  ui.get('flag-input').value='PANOPTIS{wrong}';
  ui.context.fetch=async()=>({ok:false,json:async()=>({error:'That flag does not match this lab and attempt.'})});
  await ui.run('submitFlag({preventDefault(){}})');
  assert.equal(ui.get('lab4-completion-explanation').hidden,true);
  assert.equal(ui.run('state.summary.score'),0);
});

test('only backend-confirmed flag completion reveals the explanation and restores it after refresh', async () => {
  const ui=await studentUI({exploited:true}); let release;
  ui.get('flag-input').value='PANOPTIS{fixture}';
  ui.context.fetch=()=>new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>({lab:{id:4,completed:true,exploited:true,flag:'PANOPTIS{fixture}'},summary:{score:100,completed:[4]}})});});
  const pending=ui.run('submitFlag({preventDefault(){}})');
  assert.equal(ui.get('lab4-completion-explanation').hidden,true);
  release(); await pending;
  assert.equal(ui.get('lab4-completion-explanation').hidden,false);
  assert.equal(ui.run('state.summary.score'),100);
  const restored=await studentUI({completed:true,exploited:true});
  assert.equal(restored.get('lab4-completion-explanation').hidden,false);
});

test('Lab 4 explanation stays hidden in every other lab', async () => {
  const ui=await studentUI({completed:true,exploited:true});
  for (const id of [1,2,3,5,6,7,8,9]) {
    ui.run(`state.current=${id}; state.labs['${id}'].completed=true; drawLab()`);
    assert.equal(ui.get('lab4-completion-explanation').hidden,true);
  }
});
