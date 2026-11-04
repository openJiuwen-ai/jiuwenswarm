const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../jiuwenswarm/channels/desktop/electron/main.cjs'), 'utf8');
const start = source.indexOf('function hideBrowserView(');
const end = source.indexOf('const BROWSER_ENDPOINTS_FILE_NAME', start);
assert.ok(start > 0 && end > start, 'Production visibility functions must be exercised');

function harness(ensure) {
  const views = new Map();
  const context = vm.createContext({
    browserViews: views, browserHeadless: true, activePaneSessionId: '', lastBrowserBounds: null,
    normalizeBrowserSessionId: id => id, ensureBrowserView: ensure || (async id => views.get(id)),
    emitBrowserState() {}, console,
  });
  vm.runInContext(source.slice(start, end), context);
  return context;
}

function page(id) {
  return {
    targetId: id, visible: true, leases: new Map([['lease', process.pid]]),
    view: {
      setVisible(value) { this.visible = value; },
      webContents: { focus() { this.focused = true; } },
    },
  };
}

test('unloaded/headless settings deny display without creating a browser page', () => {
  const runtime = harness(() => assert.fail('Display request must not create a headless page'));
  assert.equal(runtime.setBrowserPaneVisible('session', true), false);
  assert.equal(runtime.browserViews.size, 0);
  assert.throws(() => runtime.setBrowserHeadless('false'), /headless must be boolean/);
});

test('headless hides every member/session without closing pages or releasing leases', async () => {
  const runtime = harness();
  for (let i = 0; i < 10; i++) runtime.browserViews.set(`page-${i}`, page(`target-${i}`));
  runtime.setBrowserHeadless(true);
  for (const entry of runtime.browserViews.values()) {
    assert.equal(entry.visible, false);
    assert.equal(entry.view.visible, false);
    assert.equal(entry.leases.size, 1);
  }
  assert.equal(runtime.browserViews.size, 10);
  assert.equal(runtime.setBrowserPaneVisible('page-1', true, false), false);
  runtime.setBrowserHeadless(false);
  runtime.setBrowserPaneVisible('page-1', true);
  await Promise.resolve();
  assert.equal(runtime.browserViews.get('page-1').visible, true);
  assert.equal(runtime.browserViews.get('page-1').targetId, 'target-1');
  assert.equal(runtime.browserViews.get('page-2').visible, false);
});

test('late page creation cannot reveal a view after switching to headless', async () => {
  let resolve;
  const pending = new Promise(done => { resolve = done; });
  const runtime = harness(() => pending);
  const entry = page('same-target');
  runtime.browserViews.set('session', entry);
  runtime.setBrowserHeadless(false);
  runtime.setBrowserPaneVisible('session', true);
  runtime.setBrowserHeadless(true);
  resolve(entry);
  await pending;
  await Promise.resolve();
  assert.equal(entry.visible, false);
  assert.equal(entry.view.visible, false);
  assert.equal(entry.view.webContents.focused, undefined);
  assert.equal(runtime.activePaneSessionId, '');
});
