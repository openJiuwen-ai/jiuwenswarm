import assert from 'node:assert/strict';
import { test, after } from 'node:test';
import { JSDOM } from 'jsdom';
const dom = new JSDOM('<html><body></body></html>', { url: 'http://localhost/' });
for (const [key, value] of Object.entries({ window: dom.window, document: dom.window.document, navigator: dom.window.navigator, IS_REACT_ACT_ENVIRONMENT: true })) {
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
}
after(() => dom.window.close());
const { act, createElement } = await import('react');
const { createRoot } = await import('react-dom/client');
const { SystemPluginSwitch, systemPluginApi } = await import('../node_modules/.cache/system-plugin-switch.mjs');
const click = async (element) => act(async () => element.click());
async function scenario(checked, packages, check) {
  const host = document.createElement('div'); document.body.append(host);
  const root = createRoot(host); const calls = [];
  systemPluginApi.catalog = async () => { if (packages instanceof Error) throw packages; return packages; };
  await act(async () => root.render(createElement(SystemPluginSwitch, { packageId: 'target', checked, onChange: async value => { calls.push(value); } })));
  try { await check(host, calls); } finally { await act(async () => root.unmount()); host.remove(); }
}
const other = { id: 'other', installed: true, enabled: true };
test('first system plugin enables without confirmation', async () => scenario(false, [], async (host, calls) => {
  await click(host.querySelector('button')); assert.deepEqual(calls, [true]);
  assert.equal(document.querySelector('[data-testid="connector-market-confirm-dialog"]'), null);
}));
test('second plugin requires confirmation every time and cancel never enables', async () => scenario(false, [other], async (host, calls) => {
  await click(host.querySelector('button')); assert.deepEqual(calls, []);
  await click(document.querySelector('[data-testid="connector-market-confirm-cancel"]')); assert.deepEqual(calls, []);
  await click(host.querySelector('button'));
  await click(document.querySelector('[data-testid="connector-market-confirm-ok"]')); assert.deepEqual(calls, [true]);
  await click(host.querySelector('button')); assert.ok(document.querySelector('[data-testid="connector-market-confirm-dialog"]'));
}));
test('third plugin also requires confirmation', async () => scenario(false, [other, { ...other, id: 'third' }], async (host, calls) => {
  await click(host.querySelector('button')); assert.deepEqual(calls, []);
  assert.ok(document.querySelector('[data-testid="connector-market-confirm-dialog"]'));
}));
test('disabling does not require confirmation', async () => scenario(true, [other], async (host, calls) => {
  await click(host.querySelector('button')); assert.deepEqual(calls, [false]);
}));
test('failed status query never enables the plugin', async () => scenario(false, new Error('offline'), async (host, calls) => {
  await click(host.querySelector('button')); assert.deepEqual(calls, []);
}));
