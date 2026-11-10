const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8')
  .replace(/\nstart\(\);\s*$/, '');

function studentUI(lab = 1, completed = true) {
  const elements = new Map();
  let opened = 0;
  const context = vm.createContext({
    document: {
      getElementById: id => {
        if (!elements.has(id)) elements.set(id, { hidden: false, value: '',
          showModal: () => { opened += 1; } });
        return elements.get(id);
      },
      querySelector: () => null,
    },
    lab, completed,
  });
  vm.runInContext(source, context);
  vm.runInContext(`
    state.current = lab;
    state.catalog = Array.from({ length: 9 }, (_, i) => ({ id: i + 1, points: 100 }));
    state.labs[String(lab)] = { completed, exploited: completed, flag: 'PANOPTIS{real-flag}' };
    drawLab = drawFlag;
    updateControls = () => {};
    drawFlag();
  `, context);
  return { context, run: code => vm.runInContext(code, context),
    get: id => elements.get(id), opened: () => opened };
}

test('Lab 1 keeps the real captured flag and normal submission available after automatic completion', () => {
  const ui = studentUI();
  assert.equal(ui.get('flag-button').textContent, '✓ Completed');
  assert.equal(ui.get('captured-panel').hidden, false);
  assert.equal(ui.get('captured-flag').textContent, 'PANOPTIS{real-flag}');
  assert.equal(ui.get('use-captured-flag').hidden, false);
  assert.equal(ui.get('flag-form').hidden, false);
  assert.equal(ui.get('award-points').hidden, true);
});

test('fresh verified success opens the existing flag dialog and displays completion', () => {
  const ui = studentUI(1, false);
  ui.context.response = { lab: { completed: true, exploited: true,
    flag: 'PANOPTIS{verified-flag}', boiler_version: [1, 2] }, summary: { score: 100, completed: [1] } };
  ui.run('acceptResponse(1, response)');
  assert.equal(ui.opened(), 1);
  assert.equal(ui.get('flag-heading').textContent, 'Lab complete.');
  assert.equal(ui.get('captured-flag').textContent, 'PANOPTIS{verified-flag}');
  assert.equal(ui.run('state.summary.score'), 100);
});

test('normal flag resubmission reports acceptance without claiming another points award', async () => {
  const ui = studentUI();
  ui.get('flag-input').value = 'PANOPTIS{real-flag}';
  let submitted;
  ui.context.fetch = async (url, options) => {
    submitted = { url, body: JSON.parse(options.body) };
    return { ok: true, json: async () => ({ lab: { completed: true, exploited: true,
      flag: 'PANOPTIS{real-flag}', boiler_version: [1, 2] }, summary: { score: 100, completed: [1] } }) };
  };
  await ui.run('submitFlag({ preventDefault() {} })');
  assert.deepEqual(submitted, { url: '/api/labs/1/flag', body: { flag: 'PANOPTIS{real-flag}' } });
  assert.match(ui.get('flag-feedback').textContent, /Flag accepted/);
  assert.match(ui.get('flag-feedback').textContent, /already awarded/);
  assert.equal(ui.run('state.summary.score'), 100);
  assert.equal(ui.get('flag-form').hidden, false);
});

test('completed flags in every other lab retain their existing display behavior', () => {
  for (let id = 2; id <= 9; id += 1) {
    const ui = studentUI(id);
    assert.equal(ui.get('captured-panel').hidden, false);
    assert.equal(ui.get('flag-form').hidden, true);
    assert.equal(ui.get('use-captured-flag').hidden, true);
    assert.equal(ui.get('award-points').hidden, false);
  }
});
