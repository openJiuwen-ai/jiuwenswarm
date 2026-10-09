import assert from 'node:assert/strict';
import test from 'node:test';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

// This file lives in the plugin's tree, which has no node_modules of its own; the
// frontend's are used, resolved from the directory the npm script runs in.
const need = createRequire(pathToFileURL(resolve(process.cwd(), 'package.json')));
const load = (name) => import(pathToFileURL(need.resolve(name)));
const { build } = await load('esbuild');
const { JSDOM } = await load('jsdom');

// react-dom decides at module load whether a DOM exists, so the DOM comes first
// and react-dom is imported afterwards.
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: 'http://localhost/' });
for (const [k, v] of Object.entries({
  window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
  HTMLElement: dom.window.HTMLElement, HTMLInputElement: dom.window.HTMLInputElement,
  Event: dom.window.Event, KeyboardEvent: dom.window.KeyboardEvent, IS_REACT_ACT_ENVIRONMENT: true,
})) {
  Object.defineProperty(globalThis, k, { configurable: true, writable: true, value: v });
}
const { act, createElement } = await load('react');
const { createRoot } = await load('react-dom/client');

const outfile = resolve('node_modules/.cache/clouddoc-adopt-by-link/AdoptByLink.mjs');
await build({
  stdin: {
    contents: "export * from '../../../extensions/co_scribe/frontend/DocsPanel/AdoptByLink';",
    resolveDir: process.cwd(),
    loader: 'tsx',
  },
  outfile,
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  jsx: 'automatic',
  plugins: [{
    name: 'translations',
    setup(b) {
      b.onResolve({ filter: /^react-i18next$/ }, () => ({ path: 'i18n', namespace: 'test' }));
      b.onLoad({ filter: /.*/, namespace: 'test' }, () => ({
        contents: 'export const useTranslation = () => ({ t: (key) => key });',
      }));
    },
  }],
});
const { AdoptByLink } = await import(pathToFileURL(outfile));

function typeInto(input, value) {
  const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set;
  setter.call(input, value);
  input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
}

async function mount(adopt) {
  const calls = [];
  const copied = [];
  const root = createRoot(document.getElementById('root'));
  await act(async () => root.render(createElement(AdoptByLink, {
    adopt: async (url) => { calls.push(url); return adopt(url); },
    copyAddress: (address) => { copied.push(address); },
    copied: false,
  })));
  const q = (sel) => document.querySelector(sel);
  return {
    calls,
    copied,
    input: () => q('[data-testid="docs-adopt-input"]'),
    button: () => q('[data-testid="docs-adopt-button"]'),
    result: () => q('[data-testid="docs-add-result"]'),
    unmount: () => act(async () => root.unmount()),
  };
}

test('the Adopt button is rendered, labelled, and disabled until a link is typed', async () => {
  const ui = await mount(async () => ({ result: 'ok', watch: 'off' }));
  assert.ok(ui.button(), 'a visible button, not Enter alone');
  assert.equal(ui.button().textContent, 'docs.addAdopt');
  assert.equal(ui.button().disabled, true);
  await act(async () => ui.button().click());
  assert.deepEqual(ui.calls, [], 'nothing is sent for an empty field');
  await act(async () => typeInto(ui.input(), '  https://docs.google.com/document/d/1AAAABBBBCCCC/edit '));
  assert.equal(ui.button().disabled, false);
  await ui.unmount();
});

test('clicking Adopt adopts and reports the watch off under the default policy', async () => {
  const ui = await mount(async () => ({ result: 'ok', doc_id: '1AAAABBBBCCCC', watch: 'off' }));
  await act(async () => typeInto(ui.input(), 'https://docs.google.com/document/d/1AAAABBBBCCCC/edit'));
  await act(async () => ui.button().click());
  assert.deepEqual(ui.calls, ['https://docs.google.com/document/d/1AAAABBBBCCCC/edit']);
  const r = ui.result();
  assert.ok(r, 'the result is shown for a successful adoption too');
  assert.equal(r.dataset.result, 'ok');
  assert.equal(r.dataset.watch, 'off');
  assert.ok(r.textContent.includes('docs.add.ok.title'));
  assert.ok(r.textContent.includes('docs.add.ok.detailOff'));
  assert.ok(!r.textContent.includes('docs.add.ok.detailOn'));
  assert.equal(ui.input().value, '', 'the field clears after an adoption');
  await ui.unmount();
});

test('when the deployment policy issued a watch, the result says so', async () => {
  const ui = await mount(async () => ({ result: 'ok', doc_id: 'FsTok1', watch: 'apply_scoped' }));
  await act(async () => typeInto(ui.input(), 'https://acme.feishu.cn/docx/FsTok1'));
  await act(async () => ui.button().click());
  const r = ui.result();
  assert.equal(r.dataset.watch, 'apply_scoped');
  assert.ok(r.textContent.includes('docs.add.ok.detailOn'));
  assert.ok(!r.textContent.includes('docs.add.ok.detailOff'));
  await ui.unmount();
});

test('a refusal keeps the link, shows the reason, and offers a re-check', async () => {
  const ui = await mount(async () => ({ result: 'not_shared' }));
  await act(async () => typeInto(ui.input(), 'https://acme.feishu.cn/docx/FsTok1'));
  await act(async () => ui.button().click());
  const r = ui.result();
  assert.equal(r.dataset.result, 'not_shared');
  assert.ok(r.textContent.includes('docs.add.not_shared.title'));
  assert.ok(r.textContent.includes('docs.copyAddress'));
  assert.equal(ui.input().value, 'https://acme.feishu.cn/docx/FsTok1', 'the link stays for a re-check');
  await act(async () => r.querySelector('[data-testid="docs-adopt-recheck"]').click());
  assert.equal(ui.calls.length, 2);
  await ui.unmount();
});

test('Enter still submits', async () => {
  const ui = await mount(async () => ({ result: 'ok', watch: 'off' }));
  await act(async () => typeInto(ui.input(), 'https://acme.feishu.cn/docx/FsTok1'));
  await act(async () => ui.input().dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
  assert.equal(ui.calls.length, 1);
  await ui.unmount();
});


test('"not shared" copies the address of the connection that answered, not the selected one', async () => {
  // Google selected in the panel, a Feishu link pasted: the gateway probed the
  // Feishu connection and names it in the verdict; the copy action must use that.
  const ui = await mount(async () => ({
    result: 'not_shared', connection_id: 'feishu:ou_bot', agent_address: 'ou_bot',
  }));
  await act(async () => typeInto(ui.input(), 'https://acme.feishu.cn/docx/FsTokNotSharedYet1'));
  await act(async () => ui.button().click());
  const copy = ui.result().querySelector('[data-testid="docs-adopt-copy-address"]');
  assert.equal(copy.dataset.address, 'ou_bot');
  await act(async () => copy.click());
  assert.deepEqual(ui.copied, ['ou_bot']);
  await ui.unmount();
});
