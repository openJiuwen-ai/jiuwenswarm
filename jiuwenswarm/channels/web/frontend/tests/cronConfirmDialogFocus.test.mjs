import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';

const previousGlobals = new Map();
let dom;
let createRoot;
let ConfirmDialog;
let I18nextProvider;

before(async () => {
  dom = new JSDOM('<!doctype html><div id="root"></div>', { pretendToBeVisual: true });
  for (const [name, value] of Object.entries({
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    Node: dom.window.Node,
    IS_REACT_ACT_ENVIRONMENT: true,
  })) {
    previousGlobals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  ({ createRoot } = await import('react-dom/client'));
  const result = await build({
    stdin: {
      contents: `export { default as ConfirmDialog } from './src/components/CronPanel/ConfirmDialog';`,
      resolveDir: fileURLToPath(new URL('../', import.meta.url)),
      loader: 'tsx',
    },
    bundle: true,
    write: false,
    platform: 'node',
    format: 'cjs',
    jsx: 'automatic',
    packages: 'external',
    loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
    define: { 'import.meta.glob': 'testAssetGlob', 'import.meta.env': '{}' },
    banner: { js: 'const testAssetGlob = () => ({});' },
  });
  const compiled = { exports: {} };
  const require = createRequire(import.meta.url);
  new Function('require', 'module', 'exports', result.outputFiles[0].text)(require, compiled, compiled.exports);
  ({ ConfirmDialog } = compiled.exports);
  ({ I18nextProvider } = require('react-i18next'));
  const i18n = i18next.createInstance();
  await i18n.init({
    lng: 'en',
    showSupportNotice: false,
    resources: {
      en: { translation: JSON.parse(readFileSync(new URL('../src/i18n/locales/en.json', import.meta.url), 'utf8')) },
    },
  });
  globalThis.__i18n = i18n;
});

after(() => {
  dom?.window.close();
  for (const [name, descriptor] of previousGlobals) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else delete globalThis[name];
  }
});

function byTestId(testId) {
  const node = document.querySelector(`[data-testid="${testId}"]`);
  assert.ok(node, `Missing ${testId}`);
  return node;
}

async function mountDialog(t, props = {}) {
  const root = createRoot(document.getElementById('root'));
  let unmounted = false;
  const unmount = async () => {
    if (unmounted) return;
    unmounted = true;
    await act(async () => root.unmount());
  };
  t.after(unmount);
  await act(async () => root.render(
    createElement(I18nextProvider, { i18n: globalThis.__i18n },
      createElement(ConfirmDialog, {
        title: 'Delete job',
        message: 'Delete this job?',
        onConfirm: () => undefined,
        onCancel: () => undefined,
        ...props,
      }),
    ),
  ));
  return unmount;
}

function pressKey(key, init = {}) {
  const event = new dom.window.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init });
  document.dispatchEvent(event);
  return event;
}

test('moves focus into the dialog on mount', async (t) => {
  await mountDialog(t);
  assert.equal(document.activeElement, byTestId('cron-confirm-cancel-btn'));
});

test('wraps Tab and Shift+Tab inside the dialog', async (t) => {
  await mountDialog(t);
  const first = byTestId('cron-confirm-close-btn');
  const last = byTestId('cron-confirm-cancel-btn');

  last.focus();
  assert.equal(pressKey('Tab').defaultPrevented, true);
  assert.equal(document.activeElement, first);

  first.focus();
  assert.equal(pressKey('Tab', { shiftKey: true }).defaultPrevented, true);
  assert.equal(document.activeElement, last);
});

test('keeps Tab from reaching background elements', async (t) => {
  const background = document.createElement('button');
  background.textContent = 'background action';
  document.body.appendChild(background);
  t.after(() => background.remove());

  await mountDialog(t);
  const last = byTestId('cron-confirm-cancel-btn');
  last.focus();
  assert.equal(pressKey('Tab').defaultPrevented, true);
  assert.notEqual(document.activeElement, background);
  assert.equal(document.activeElement, byTestId('cron-confirm-close-btn'));
});

test('pulls focus back into the dialog when it has escaped', async (t) => {
  const background = document.createElement('button');
  document.body.appendChild(background);
  t.after(() => background.remove());

  await mountDialog(t);

  background.focus();
  assert.equal(pressKey('Tab').defaultPrevented, true);
  assert.equal(document.activeElement, byTestId('cron-confirm-close-btn'));

  background.focus();
  assert.equal(pressKey('Tab', { shiftKey: true }).defaultPrevented, true);
  assert.equal(document.activeElement, byTestId('cron-confirm-cancel-btn'));
});

test('keeps Shift+Tab inside when the dialog container itself is focused', async (t) => {
  await mountDialog(t);
  const container = byTestId('cron-confirm-dialog');
  container.focus();
  assert.equal(document.activeElement, container);

  assert.equal(pressKey('Tab', { shiftKey: true }).defaultPrevented, true);
  assert.equal(document.activeElement, byTestId('cron-confirm-cancel-btn'));
});

test('closes on Escape and restores focus to the restore target', async (t) => {
  const trigger = document.createElement('button');
  document.body.appendChild(trigger);
  t.after(() => trigger.remove());

  let cancelCount = 0;
  const unmount = await mountDialog(t, {
    restoreFocusRef: { current: trigger },
    onCancel: () => { cancelCount += 1; },
  });

  assert.equal(pressKey('Escape').defaultPrevented, true);
  assert.equal(cancelCount, 1);

  await unmount();
  assert.equal(document.activeElement, trigger);
});

test('uses the fallback when the restore target has left the document', async (t) => {
  const trigger = document.createElement('button');
  const fallback = document.createElement('button');
  document.body.append(trigger, fallback);
  t.after(() => { trigger.remove(); fallback.remove(); });

  const unmount = await mountDialog(t, {
    restoreFocusRef: { current: trigger },
    restoreFocusFallback: () => fallback,
  });
  trigger.remove();

  await unmount();
  assert.equal(document.activeElement, fallback);
});

test('ignores Escape while loading', async (t) => {
  let cancelCount = 0;
  await mountDialog(t, { loading: true, onCancel: () => { cancelCount += 1; } });
  assert.equal(pressKey('Escape').defaultPrevented, true);
  assert.equal(cancelCount, 0);
});
