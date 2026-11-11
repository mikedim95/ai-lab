const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8').replace(/\nstart\(\);\s*$/, '');
const flag = 'PANOPTIS{existing-fixture-flag}';

function studentUI({ completed = false, exploited = true, attempt = 1 } = {}) {
  const elements = new Map(), confirmations = [];
  class Element {
    constructor() { this.value = ''; this.textContent = ''; this.children = []; this.style = {}; }
    replaceChildren(...children) { this.children = children; }
    append(...children) { this.children.push(...children); }
    setAttribute() {}
    showModal() {}
    focus() {}
  }
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const context = vm.createContext({ document: { getElementById: get, createElement: () => new Element(),
    createTextNode: text => ({textContent:text}), querySelector: () => null },
    window: {confirm: text => { confirmations.push(text); return true; }}, setTimeout: () => 0, clearTimeout() {},
    completed, exploited, attempt, flag });
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  run(`state.current = 3; state.catalog = Array.from({length:9},(_,i)=>({id:i+1,points:i===2?150:100}));
    state.labs['3'] = {id:3,attempt,messages:[],completed,exploited,flag:completed||exploited?flag:null,defense_level:0};
    state.summary = {score:completed?150:0,completed:completed?[3]:[]};
    drawLab = drawFlag; updateControls = drawConversation = drawNavigation = () => {}; drawFlag();`);
  get('flag-input').value = flag;
  const result = ({ awarded = true, newAward = true, points = 150, attempt = 1, verified = true, defense = 0 } = {}) => ({
    lab: {id:3,attempt,messages:[],completed:awarded,exploited:verified,flag:awarded||verified?flag:null,
      defense_level:defense,last_result:null},
    summary: {score:awarded?150:0,completed:awarded?[3]:[]},
    flag_submission: {newly_completed:newAward,points_awarded:points},
  });
  const respond = response => { context.fetch = async () => ({ok:true,json:async()=>response}); };
  return {get,run,context,result,respond,confirmations};
}
const submit = ui => ui.run('submitFlag({preventDefault(){}})');

test('a fresh locked Lab 3 has no captured flag or completion explanation', () => {
  const ui = studentUI({exploited:false});
  assert.equal(ui.get('captured-panel').hidden, true);
  assert.equal(ui.get('lab3-completion-explanation').hidden, true);
  assert.equal(ui.run('state.summary.score'), 0);
});

test('verified capture alone shows the flag without completion or points', () => {
  const ui = studentUI();
  assert.equal(ui.get('captured-panel').hidden, false);
  assert.equal(ui.get('captured-flag').textContent, flag);
  assert.equal(ui.get('lab3-completion-explanation').hidden, true);
  assert.equal(ui.get('flag-form').hidden, false);
  assert.equal(ui.run('state.summary.score'), 0);
});

test('the first backend-confirmed award displays exactly 150 points and the learning explanation', async () => {
  const ui = studentUI();
  ui.respond(ui.result());
  await submit(ui);
  assert.equal(ui.get('flag-heading').textContent, 'Lab complete.');
  assert.equal(ui.get('flag-feedback').textContent, '+150 points. Well played.');
  assert.equal(ui.get('flag-form').hidden, true);
  assert.equal(ui.get('lab3-completion-explanation').hidden, false);
  assert.equal(ui.run('state.summary.score'), 150);
});

test('duplicate and replayed successful submissions cannot claim another award', async () => {
  const ui = studentUI();
  ui.respond(ui.result());
  await submit(ui);
  for (const response of [ui.result({newAward:false,points:0}), ui.result()]) {
    ui.respond(response);
    await submit(ui);
    assert.doesNotMatch(ui.get('flag-feedback').textContent, /\+150/);
    assert.match(ui.get('flag-feedback').textContent, /already complete.*already awarded/);
    assert.equal(ui.run('state.summary.score'), 150);
  }
});

test('a concurrent loser tab accepts completion without showing a second award', async () => {
  const ui = studentUI();
  ui.respond(ui.result({newAward:false,points:0}));
  await submit(ui);
  assert.equal(ui.get('lab3-completion-explanation').hidden, false);
  assert.doesNotMatch(ui.get('flag-feedback').textContent, /\+150/);
  assert.equal(ui.run('state.summary.score'), 150);
});

test('a pending flag submission sends only one request and keeps the explanation hidden', async () => {
  const ui = studentUI();
  let calls = 0, release;
  ui.context.fetch = () => { calls += 1; return new Promise(resolve => { release = () => resolve({ok:true,json:async()=>ui.result()}); }); };
  const pending = submit(ui);
  await submit(ui);
  assert.equal(calls, 1);
  assert.equal(ui.get('lab3-completion-explanation').hidden, true);
  release();
  await pending;
  assert.equal(ui.run('state.summary.score'), 150);
});

test('incorrect flags never change score or previous completion', async () => {
  for (const completed of [false, true]) {
    const ui = studentUI({completed});
    ui.context.fetch = async () => ({ok:false,json:async()=>({error:'That flag does not match this lab and attempt.'})});
    await submit(ui);
    assert.equal(ui.run("state.labs['3'].completed"), completed);
    assert.equal(ui.run('state.summary.score'), completed ? 150 : 0);
    assert.equal(ui.get('lab3-completion-explanation').hidden, !completed);
    assert.match(ui.get('flag-feedback').textContent, /does not match/);
  }
});

test('missing or inconsistent award metadata never produces a points notification', async () => {
  for (const award of [undefined, {newly_completed:true,points_awarded:300}, {newly_completed:'true',points_awarded:150}]) {
    const ui = studentUI();
    const response = ui.result();
    response.flag_submission = award;
    ui.respond(response);
    await submit(ui);
    assert.doesNotMatch(ui.get('flag-feedback').textContent, /\+150/);
    assert.equal(ui.run('state.summary.score'), 150);
  }
});

test('an unconfirmed completion response cannot reveal the learning explanation', async () => {
  const ui = studentUI();
  ui.respond(ui.result({awarded:false}));
  await submit(ui);
  assert.equal(ui.get('lab3-completion-explanation').hidden, true);
  assert.equal(ui.run('state.summary.score'), 0);
  assert.match(ui.get('flag-feedback').textContent, /did not confirm Lab 3 completion/);
});

test('new conversation retains earned completion and captured flag with current verification cleared', async () => {
  const ui = studentUI({completed:true});
  ui.respond(ui.result({newAward:false,points:0,attempt:2,verified:false}));
  await ui.run('restartLab()');
  assert.equal(ui.run("state.labs['3'].completed"), true);
  assert.equal(ui.run("state.labs['3'].exploited"), false);
  assert.equal(ui.run("state.labs['3'].last_result"), null);
  assert.equal(ui.get('captured-panel').hidden, false);
  assert.equal(ui.get('captured-flag').textContent, flag);
  assert.equal(ui.get('lab3-completion-explanation').hidden, false);
  assert.equal(ui.run('state.summary.score'), 150);
});

test('difficulty changes describe and retain earned completion while clearing current verification', async () => {
  const ui = studentUI({completed:true});
  ui.get('defense-select').value = '1';
  ui.respond(ui.result({newAward:false,points:0,attempt:2,verified:false,defense:1}));
  await ui.run('changeDefense()');
  assert.match(ui.confirmations[0], /current verification/);
  assert.match(ui.confirmations[0], /Earned completion and points stay saved/);
  assert.equal(ui.run("state.labs['3'].defense_level"), 1);
  assert.equal(ui.run("state.labs['3'].exploited"), false);
  assert.equal(ui.get('lab3-completion-explanation').hidden, false);
  assert.equal(ui.run('state.summary.score'), 150);
});

test('a late winning response cannot restore a previous attempt after reset', async () => {
  const ui = studentUI();
  let release;
  ui.context.fetch = () => new Promise(resolve => { release = () => resolve({ok:true,json:async()=>ui.result()}); });
  const pending = submit(ui);
  ui.context.newer = ui.result({newAward:false,points:0,attempt:2,verified:false});
  ui.run("acceptResponse(3,newer); state.flagFeedback.set(3,{message:'Current completion'})");
  release();
  await pending;
  assert.equal(ui.run("state.labs['3'].attempt"), 2);
  assert.equal(ui.run("state.labs['3'].exploited"), false);
  assert.equal(ui.run('state.flagFeedback.get(3).message'), 'Current completion');
});

test('an older uncompleted response cannot undo a same-attempt backend completion', () => {
  const ui = studentUI({completed:true});
  ui.context.old = ui.result({awarded:false,newAward:false,points:0});
  assert.equal(ui.run('acceptResponse(3,old)'), false);
  assert.equal(ui.run("state.labs['3'].completed"), true);
  assert.equal(ui.get('lab3-completion-explanation').hidden, false);
  assert.equal(ui.run('state.summary.score'), 150);
});

test('a stale incorrect-flag error cannot overwrite a newer successful completion', async () => {
  const ui = studentUI();
  let release;
  ui.context.fetch = () => new Promise(resolve => { release = () => resolve({ok:false,json:async()=>({error:'Old rejection'})}); });
  const pending = submit(ui);
  ui.context.newer = ui.result();
  ui.run("acceptResponse(3,newer); state.flagFeedback.set(3,{message:'Current completion'})");
  release();
  await pending;
  assert.equal(ui.run('state.flagFeedback.get(3).message'), 'Current completion');
  assert.equal(ui.get('lab3-completion-explanation').hidden, false);
});

test('an old flag error after an uncompleted reset cannot pollute the new attempt', async () => {
  const ui = studentUI();
  let release;
  ui.context.fetch = () => new Promise(resolve => { release = () => resolve({ok:false,json:async()=>({error:'Old rejection'})}); });
  const pending = submit(ui);
  ui.context.newer = ui.result({awarded:false,attempt:2,verified:false});
  ui.run('acceptResponse(3,newer)');
  release();
  await pending;
  assert.equal(ui.run('state.flagFeedback.has(3)'), false);
  assert.equal(ui.run("state.labs['3'].attempt"), 2);
  assert.equal(ui.run('state.summary.score'), 0);
});

test('finishing a Lab 3 submission while viewing Lab 2 does not reveal the Lab 3 explanation there', async () => {
  const ui = studentUI();
  let release;
  const response = ui.result();
  response.summary = {score:250,completed:[2,3]};
  ui.context.fetch = () => new Promise(resolve => { release = () => resolve({ok:true,json:async()=>response}); });
  const pending = submit(ui);
  ui.run("state.labs['2'] = {id:2,completed:true,exploited:true,flag:'other-lab-flag'}; state.current = 2; drawFlag()");
  release();
  await pending;
  assert.equal(ui.run("state.labs['3'].completed"), true);
  assert.equal(ui.run("state.labs['2'].completed"), true);
  assert.equal(ui.get('lab3-completion-explanation').hidden, true);
  assert.equal(ui.run('state.summary.score'), 250);
  ui.run('state.current = 3; drawFlag()');
  assert.equal(ui.get('lab3-completion-explanation').hidden, false);
});

test('refresh restores the earned flag and explanation even when the current attempt is unverified', () => {
  const ui = studentUI({completed:true,exploited:false,attempt:4});
  assert.equal(ui.get('flag-heading').textContent, 'Lab complete.');
  assert.equal(ui.get('captured-panel').hidden, false);
  assert.equal(ui.get('captured-flag').textContent, flag);
  assert.equal(ui.get('lab3-completion-explanation').hidden, false);
  assert.equal(ui.run("state.labs['3'].exploited"), false);
});
