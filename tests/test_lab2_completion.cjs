const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8')
  .replace(/\nstart\(\);\s*$/, '');

function studentUI({ lab = 2, completed = false } = {}) {
  const elements = new Map();
  const confirmations = [];
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
  const context = vm.createContext({
    document: { getElementById: get, createElement: () => new Element(),
      createTextNode: text => ({ textContent: text }), querySelector: () => null },
    window: { confirm: text => { confirmations.push(text); return true; } },
    setTimeout: () => 0, clearTimeout() {},
  });
  vm.runInContext(source, context);
  const run = text => vm.runInContext(text, context);
  context.lab = lab;
  context.completed = completed;
  run(`
    state.current = lab;
    state.catalog = Array.from({length: 9}, (_, i) => ({id: i+1, points: 100}));
    state.labs[String(lab)] = {id: lab, messages: [], completed, exploited: true,
      flag: 'test-captured-flag', defense_level: 0, verified_instruction_count: 3,
      required_instruction_count: 3, extraction_completed: true};
    if (lab === 2) attachLab2Version({lab: state.labs['2']}, completed ? '1.1.1' : '1.1.0');
    state.summary = {score: completed ? 100 : 0, completed: completed ? [lab] : []};
    savedDrawConversation = drawConversation;
    drawLab = drawFlag;
    drawConversation = drawNavigation = updateControls = drawLab2Progress = () => {};
    drawFlag();
  `);
  get('flag-input').value = 'test-captured-flag';
  const result = ({ newAward = true, version = '1.1.1', currentProgress = 3,
    awarded = true, points = 100, defense = 0 } = {}) => ({
    lab: {id: lab, messages: [], completed: awarded, exploited: true,
      flag: 'test-captured-flag', defense_level: defense,
      verified_instruction_count: currentProgress, required_instruction_count: 3,
      extraction_completed: currentProgress === 3},
    summary: {score: awarded ? 100 : 0, completed: awarded ? [lab] : []},
    flag_submission: {newly_completed: newAward, points_awarded: points},
    version,
  });
  const respond = response => {
    const { version, ...body } = response;
    context.fetch = async () => ({ok: true, headers: {get: () => version}, json: async () => body});
  };
  return {context, run, get, result, respond, confirmations};
}

const submit = ui => ui.run('submitFlag({preventDefault() {}})');

test('a first authoritative Lab 2 award displays completion, existing points, and captured flag', async () => {
  const ui = studentUI();
  ui.respond(ui.result());
  await submit(ui);
  assert.equal(ui.run("state.labs['2'].completed"), true);
  assert.equal(ui.run('state.summary.score'), 100);
  assert.match(ui.run('state.flagFeedback.get(2).message'), /^\+100 points/);
  assert.equal(ui.get('flag-heading').textContent, 'Submit a flag');
  assert.equal(ui.get('captured-flag').textContent, '');
  assert.equal(ui.get('flag-form').hidden, false);
});

test('a duplicate correct submission does not show another points award', async () => {
  const ui = studentUI();
  ui.respond(ui.result());
  await submit(ui);
  ui.respond(ui.result({newAward: false, points: 0}));
  await submit(ui);
  assert.equal(ui.run('state.summary.score'), 100);
  assert.doesNotMatch(ui.run('state.flagFeedback.get(2).message'), /\+100/);
  assert.match(ui.run('state.flagFeedback.get(2).message'), /already complete.*already awarded/);
});

test('replaying the winning response after client completion cannot repeat its award notification', async () => {
  const ui = studentUI();
  ui.respond(ui.result());
  await submit(ui);
  await submit(ui);
  assert.doesNotMatch(ui.run('state.flagFeedback.get(2).message'), /\+100/);
  assert.equal(ui.run('state.summary.score'), 100);
});

test('a concurrent loser tab accepts authoritative completion without claiming a new award', async () => {
  const ui = studentUI();
  ui.respond(ui.result({newAward: false, points: 0}));
  await submit(ui);
  assert.equal(ui.run("state.labs['2'].completed"), true);
  assert.equal(ui.run('state.summary.score'), 100);
  assert.doesNotMatch(ui.run('state.flagFeedback.get(2).message'), /\+100/);
});

test('the browser submits one request while its flag submission is pending', async () => {
  const ui = studentUI();
  let release;
  let calls = 0;
  let request;
  const {version, ...body} = ui.result();
  ui.context.fetch = (url, options) => {
    calls += 1;
    request = {url, body: JSON.parse(options.body)};
    return new Promise(resolve => { release = () => resolve({ok: true,
      headers: {get: () => version}, json: async () => body}); });
  };
  const first = submit(ui);
  const duplicate = submit(ui);
  assert.equal(calls, 1);
  release();
  await Promise.all([first, duplicate]);
  assert.deepEqual(request, {url: '/api/labs/2/flag', body: {flag: 'test-captured-flag'}});
  assert.equal(ui.run('state.summary.score'), 100);
});

test('incorrect flag errors preserve an already earned Lab 2 completion and score', async () => {
  const ui = studentUI({completed: true});
  ui.context.fetch = async () => ({ok: false, json: async () => ({error: 'Incorrect flag.'})});
  await submit(ui);
  assert.equal(ui.run("state.labs['2'].completed"), true);
  assert.equal(ui.run('state.summary.score'), 100);
  assert.equal(ui.get('captured-flag').textContent, '');
  assert.equal(ui.run('state.flagFeedback.get(2).error'), true);
});

test('missing or inconsistent award metadata cannot generate a points notification', async () => {
  for (const award of [undefined, {newly_completed: true, points_awarded: 200},
    {newly_completed: 'true', points_awarded: 100}]) {
    const ui = studentUI();
    const response = ui.result();
    response.flag_submission = award;
    ui.respond(response);
    await submit(ui);
    assert.doesNotMatch(ui.run('state.flagFeedback.get(2).message'), /\+100/);
    assert.equal(ui.run('state.summary.score'), 100);
  }
});

test('an unconfirmed completion response cannot alter the client award state', async () => {
  const ui = studentUI();
  ui.respond(ui.result({awarded: false}));
  await submit(ui);
  assert.equal(ui.run("state.labs['2'].completed"), false);
  assert.equal(ui.run('state.summary.score'), 0);
  assert.equal(ui.run('state.flagFeedback.get(2).error'), true);
  assert.match(ui.run('state.flagFeedback.get(2).message'), /did not confirm/);
});

test('a chat reset clears current progress, completion and points', async () => {
  const ui = studentUI({completed: true});
  const reset=ui.result({awarded:false,newAward:false,points:0,version:'2.0.0',currentProgress:0});
  reset.lab.exploited=false;reset.lab.flag=null;ui.respond(reset);
  await ui.run('restartLab()');
  assert.equal(ui.run("state.labs['2'].verified_instruction_count"), 0);
  assert.equal(ui.run("state.labs['2'].completed"), false);
  assert.equal(ui.run('state.summary.score'), 0);
  assert.equal(ui.get('captured-flag').textContent, '');
  assert.equal(ui.get('flag-heading').textContent, 'Submit a flag');
});

test('Lab 2 difficulty changes explain and retain the captured flag and earned points', async () => {
  const ui = studentUI({completed: true});
  ui.get('defense-select').value = '1';
  ui.respond(ui.result({newAward: false, points: 0, version: '2.0.1', currentProgress: 0, defense: 1}));
  await ui.run('changeDefense()');
  assert.match(ui.confirmations[0], /clears this lab’s chat and current extraction progress/);
  assert.match(ui.confirmations[0], /captured flag and earned points stay saved/);
  assert.equal(ui.run("state.labs['2'].completed"), true);
  assert.equal(ui.run('state.summary.score'), 100);
  assert.equal(ui.run("state.labs['2'].defense_level"), 1);
  assert.equal(ui.get('captured-flag').textContent, '');
});

test('a stale winning response cannot replace a newer reset or claim another award', async () => {
  const ui = studentUI();
  let release;
  const {version, ...body} = ui.result();
  ui.context.fetch = () => new Promise(resolve => { release = () => resolve({ok: true,
    headers: {get: () => version}, json: async () => body}); });
  const pending = submit(ui);
  ui.context.newer = ui.result({newAward: false, points: 0, version: '2.0.1', currentProgress: 0});
  ui.run("acceptResponse(2, attachLab2Version(newer, newer.version)); state.flagFeedback.set(2, {message: 'Current completion'})");
  release();
  await pending;
  assert.equal(ui.run('state.flagFeedback.get(2).message'), 'Current completion');
  assert.equal(ui.run("state.labs['2'].verified_instruction_count"), 0);
  assert.equal(ui.run('state.summary.score'), 100);
});

test('a stale incorrect-flag error cannot replace newer successful completion feedback', async () => {
  const ui = studentUI();
  let release;
  ui.context.fetch = () => new Promise(resolve => { release = () => resolve({ok: false,
    json: async () => ({error: 'Old rejection'})}); });
  const pending = submit(ui);
  ui.context.newer = ui.result({version: '1.1.1'});
  ui.run("acceptResponse(2, attachLab2Version(newer, newer.version)); state.flagFeedback.set(2, {message: 'Current completion'})");
  release();
  await pending;
  assert.equal(ui.run('state.flagFeedback.get(2).message'), 'Current completion');
  assert.equal(ui.run('state.summary.score'), 100);
});

test('Lab 1 retains its existing duplicate-submission wording and difficulty warning', async () => {
  const ui = studentUI({lab: 1, completed: true});
  ui.respond(ui.result({newAward: false, points: 0}));
  await submit(ui);
  assert.equal(ui.run('state.flagFeedback.get(1).message'), 'Flag accepted. Lab 1 is complete; points were already awarded.');
  ui.get('defense-select').value = '1';
  await ui.run('changeDefense()');
  assert.equal(ui.confirmations[0], 'Changing difficulty starts a new attempt and clears this lab’s chat, captured flag, and points. Your knowledge base stays saved. Continue?');
});

test('other labs retain the existing successful-submission presentation', async () => {
  const ui = studentUI({lab: 5});
  ui.respond(ui.result());
  await submit(ui);
  assert.equal(ui.run('state.flagFeedback.get(5).message'), '+100 points. Well played.');
});

test('a later unverified Lab 2 reply does not present a prior award as a new exploit', () => {
  const ui = studentUI({completed: true});
  ui.run("state.labs['2'].last_result = {triggered: false, seconds: 0.1, defense_blocked: false, message: 'Exploit verified.'}; savedDrawConversation()");
  assert.equal(ui.get('attempt-result').className, 'attempt-result');
  assert.match(ui.get('attempt-result').children[0].textContent, /did not verify an extraction/);
  assert.match(ui.get('attempt-result').children[0].textContent, /previously captured flag.*earned points.*saved/);
  assert.equal(ui.run("state.labs['2'].completed"), true);
  assert.equal(ui.run('state.summary.score'), 100);
});

test('a currently verified Lab 2 reply retains the existing success presentation', () => {
  const ui = studentUI();
  ui.run("state.labs['2'].last_result = {triggered: true, seconds: 0.1, defense_blocked: false, message: 'Exploit verified.'}; savedDrawConversation()");
  assert.equal(ui.get('attempt-result').className, 'attempt-result success');
  assert.equal(ui.get('attempt-result').children[0].textContent, 'Exploit verified.');
});

test('a Protected-mode blocked reply retains its block result after a prior award', () => {
  const ui = studentUI({completed: true});
  ui.run("state.labs['2'].last_result = {triggered: false, seconds: 0.1, defense_blocked: true, message: 'Hardened defenses blocked this attempt.'}; savedDrawConversation()");
  assert.equal(ui.get('attempt-result').className, 'attempt-result blocked');
  assert.equal(ui.get('attempt-result').children[0].textContent, 'Hardened defenses blocked this attempt.');
});
