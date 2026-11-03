import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
for (const [key, value] of Object.entries({
  window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
  localStorage: dom.window.localStorage, IS_REACT_ACT_ENVIRONMENT: true,
})) {
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
}
after(() => dom.window.close());

const { act, createElement } = await import('react');
const { createRoot } = await import('react-dom/client');
const {
  applyBrowserDisplaySettings, browserDisplayRevision, isDesktopBrowserHeadless, useDesktopBrowserHeadless,
  SettingsServicesProvider, SettingsSourceProvider, useSettingsSource,
} = await import('../node_modules/.cache/browser-display/exports.mjs');

let calls;
beforeEach(async () => {
  calls = [];
  window.jiuwenDesktop = { isElectron: true, browser: { setHeadless: async value => { calls.push(value); return value; } } };
  await applyBrowserDisplaySettings({ headless: true });
  calls.length = 0;
});

test('only an explicit saved false enables the panel; missing/invalid settings stay hidden', async () => {
  for (const headless of [undefined, null, 'false', true]) {
    await applyBrowserDisplaySettings({ headless });
    assert.equal(isDesktopBrowserHeadless(), true);
  }
  await applyBrowserDisplaySettings({ headless: false });
  assert.equal(isDesktopBrowserHeadless(), false);
  assert.deepEqual(calls, [true, true, true, true, false]);
});

test('an older startup/settings read cannot overwrite a subsequently saved display mode', async () => {
  const revision = browserDisplayRevision();
  await applyBrowserDisplaySettings({ headless: false });
  await applyBrowserDisplaySettings({ headless: true }, revision);
  assert.equal(isDesktopBrowserHeadless(), false);
  assert.deepEqual(calls, [false]);
});

test('a delayed headed IPC reply cannot undo a newer headless setting', async () => {
  let resolve;
  window.jiuwenDesktop.browser.setHeadless = value => value
    ? Promise.resolve(true)
    : new Promise(done => { resolve = done; });
  const pending = applyBrowserDisplaySettings({ headless: false });
  assert.equal(isDesktopBrowserHeadless(), true);
  await applyBrowserDisplaySettings({ headless: true });
  resolve(false);
  await pending;
  assert.equal(isDesktopBrowserHeadless(), true);
});

test('older Electron shells still get renderer hiding and non-Electron clients are untouched', async () => {
  window.jiuwenDesktop.browser = {};
  await applyBrowserDisplaySettings({ headless: false });
  assert.equal(isDesktopBrowserHeadless(), false);
  await applyBrowserDisplaySettings({ headless: true });
  assert.equal(isDesktopBrowserHeadless(), true);
  window.jiuwenDesktop = undefined;
  await applyBrowserDisplaySettings({ headless: false });
  assert.equal(isDesktopBrowserHeadless(), true);
});

async function mountSettings(request) {
  let settings;
  function Probe() {
    settings = useSettingsSource();
    return createElement('output', null, useDesktopBrowserHeadless() ? 'hidden' : 'visible');
  }
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => root.render(createElement(
    SettingsServicesProvider, { isConnected: true, connectionState: 'ready', request },
    createElement(SettingsSourceProvider, { source: 'browser' }, createElement(Probe)),
  )));
  return {
    container,
    save: value => settings.save({ headless: value }, 'browser.headless'),
    async close() {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

test('real settings provider projects reads and successful saves, not unsaved optimistic values', async () => {
  let resolveSave;
  const view = await mountSettings(method => method === 'path.get'
    ? Promise.resolve({ headless: false })
    : new Promise(resolve => { resolveSave = resolve; }));
  try {
    assert.equal(view.container.textContent, 'visible');
    let saving;
    await act(async () => { saving = view.save(true); });
    assert.equal(view.container.textContent, 'visible');
    await act(async () => { resolveSave({ headless: true }); await saving; });
    assert.equal(view.container.textContent, 'hidden');
    assert.deepEqual(calls, [false, true]);
  } finally {
    await view.close();
  }
});

test('failed settings saves do not change the effective browser display mode', async () => {
  const view = await mountSettings(async method => {
    if (method === 'path.get') return { headless: true };
    throw new Error('save failed');
  });
  try {
    await act(async () => assert.rejects(view.save(false), /save failed/));
    assert.equal(view.container.textContent, 'hidden');
    assert.deepEqual(calls, [true]);
  } finally {
    await view.close();
  }
});
