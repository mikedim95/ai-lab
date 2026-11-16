const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../static/intro.js'), 'utf8');

function browserSession({ storage = new Map(), blocked = false, markup = true, lab = 1 } = {}) {
  const elements = new Map();
  let focused;
  for (const id of ['briefing-dialog', 'briefing-open', 'briefing-close', 'briefing-start', 'prompt']) {
    const listeners = new Map();
    elements.set(id, {
      open: false, shows: 0, isConnected: true, firstChild: { textContent: '' },
      addEventListener(event, fn) { listeners.set(event, fn); },
      showModal() { this.open = true; this.shows++; },
      close() { this.open = false; listeners.get('close')?.(); },
      click() { listeners.get('click')?.({ currentTarget: this }); },
      focus() { focused = id; },
    });
  }
  const window = {
    location: { pathname: `/lab/${lab}` },
    sessionStorage: {
      getItem(key) { if (blocked) throw Error('Storage disabled'); return storage.get(key) ?? null; },
      setItem(key, value) { if (blocked) throw Error('Storage disabled'); storage.set(key, value); },
    },
  };
  vm.runInNewContext(source, { window, document: { getElementById: id => markup ? elements.get(id) : null } });
  return { window, elements, show: id => window.LabBriefing.onLabShown(id), focused: () => focused };
}

test('first Lab 1 visit opens once across navigation and reloads in a browser session', () => {
  const storage = new Map();
  const page = browserSession({ storage, lab: 2 });
  assert.equal(page.show(2), false);
  page.window.location.pathname = '/lab/1';
  assert.equal(page.show(1), true);
  assert.equal(page.elements.get('briefing-dialog').open, true);
  page.elements.get('briefing-start').click();
  assert.equal(page.focused(), 'prompt');
  page.show(2);
  assert.equal(page.show(1), false);
  assert.equal(page.elements.get('briefing-dialog').shows, 1);
  assert.equal(browserSession({ storage }).show(1), false);
});

test('a new browser session receives its own briefing', () => {
  assert.equal(browserSession().show(1), true);
  assert.equal(browserSession().show(1), true);
});

test('the footer can reopen the briefing and restores keyboard focus when closed', () => {
  const page = browserSession({ lab: 7 });
  page.show(7);
  page.elements.get('briefing-open').click();
  assert.equal(page.elements.get('briefing-dialog').open, true);
  assert.equal(page.elements.get('briefing-start').firstChild.textContent, 'Continue to lab ');
  page.elements.get('briefing-close').click();
  assert.equal(page.elements.get('briefing-dialog').open, false);
  assert.equal(page.focused(), 'briefing-open');
});

test('blocked browser storage still allows dismissal without repeatedly reopening', () => {
  const page = browserSession({ blocked: true });
  assert.equal(page.show(1), true);
  page.elements.get('briefing-close').click();
  assert.equal(page.show(1), false);
  page.elements.get('briefing-open').click();
  assert.equal(page.elements.get('briefing-dialog').open, true);
});

test('pages without briefing markup keep working', () => {
  assert.equal(browserSession({ markup: false }).show(1), false);
});
