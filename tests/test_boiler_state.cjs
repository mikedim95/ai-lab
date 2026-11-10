const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8')
  .replace(/\nstart\(\);\s*$/, '');

function record(attempt, revision, water = 60, fuel = 55, power = 36) {
  return { boiler_version: [attempt, revision], exploited: false, messages: [],
    boiler: { water, fuel, power, temperature: 148, pressure: 3.1, level: 62, running: true } };
}

function studentUI(initial) {
  let displayed;
  const context = vm.createContext({
    document: { hidden: false, querySelector: () => null },
    initial,
    render: boiler => { displayed = { ...boiler }; },
  });
  vm.runInContext(source, context);
  vm.runInContext(`
    state.labs['1'] = initial;
    boilerPanel = { setState: render };
    drawLab = () => boilerPanel.setState(currentLab().boiler);
  `, context);
  return { context, run: code => vm.runInContext(code, context), displayed: () => displayed };
}

test('a delayed status poll cannot undo a successful chat write', async () => {
  const ui = studentUI(record(1, 1));
  let finishPoll;
  ui.context.fetch = () => new Promise(resolve => { finishPoll = resolve; });
  const pending = ui.run('refreshBoiler()');
  ui.context.response = { lab: record(1, 2, 70, 65), summary: {} };
  ui.run('acceptResponse(1, response)');
  finishPoll({ ok: true, json: async () => ({ labs: { '1': record(1, 1) } }) });
  await pending;
  assert.equal(ui.displayed().water, 70);
  assert.equal(ui.displayed().fuel, 65);
  assert.equal(ui.run("state.labs['1'].boiler.water"), 70);
});

test('a delayed chat response keeps newer polled readings and still saves the chat', async () => {
  const ui = studentUI(record(1, 1));
  ui.context.fetch = async () => ({ ok: true,
    json: async () => ({ labs: { '1': record(1, 3, 70, 65, 41) } }) });
  await ui.run('refreshBoiler()');
  ui.context.response = { lab: { ...record(1, 2, 70, 65, 38),
    messages: [{ role: 'assistant', content: 'Inputs updated.' }] }, summary: {} };
  ui.run('acceptResponse(1, response)');
  assert.equal(ui.displayed().power, 41);
  assert.equal(ui.run("state.labs['1'].messages[0].content"), 'Inputs updated.');
});

test('a new attempt accepts defaults and discards old attempt polls', async () => {
  const ui = studentUI(record(1, 50, 70, 65));
  ui.context.response = { lab: record(2, 0), summary: {} };
  ui.run('acceptResponse(1, response)');
  assert.equal(ui.displayed().water, 60);
  ui.context.fetch = async () => ({ ok: true,
    json: async () => ({ labs: { '1': record(1, 51, 70, 65) } }) });
  await ui.run('refreshBoiler()');
  assert.equal(ui.displayed().water, 60);
  assert.equal(ui.displayed().fuel, 55);
});
