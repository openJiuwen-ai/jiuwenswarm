import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { Simulate } from 'react-dom/test-utils';
import { JSDOM } from 'jsdom';
import { build } from 'esbuild';

const frontend = fileURLToPath(new URL('../', import.meta.url));
const output = new URL('../node_modules/.cache/personal-context-audit/', import.meta.url);
const requests = [];
globalThis.__pcAuditRequest = async (...args) => { requests.push(args); return {}; };

await build({
  entryPoints: ['src/services/personalContextApi.ts', 'src/components/PersonalContext/AddContentDrawer.tsx'],
  absWorkingDir: frontend,
  outdir: fileURLToPath(output),
  outbase: 'src',
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  plugins: [{
    name: 'audit-ui-boundaries',
    setup(plugin) {
      const mocks = new Map([
        ['./webClient', 'export const webRequest = globalThis.__pcAuditRequest; export const webClient = {};'],
        ['../../stores', 'export const usePersonalContextStore = () => globalThis.__pcAuditStore;'],
        ['react-i18next', 'export const useTranslation = () => ({ t: (key) => key });'],
        ['../../features/settings/settingsNavigation', 'export const requestSettingsModule = () => {};'],
        ['../../features/workspace/projectDirectoryPicker', 'export const selectProjectDirectory = async () => ({ ok: false });'],
        ['../../features/workspace/localFilePicker', 'export const selectLocalFiles = async () => ({ ok: false });'],
        ['../../components/ui/Toast/toastStore', 'export const toast = { open() {} };'],
      ]);
      plugin.onResolve({ filter: /.*/ }, (args) => mocks.has(args.path) ? { path: args.path, namespace: 'audit' } : null);
      plugin.onLoad({ filter: /.*/, namespace: 'audit' }, (args) => ({ contents: mocks.get(args.path) }));
    },
  }],
});
const { pcApi, isFetchStopTimeoutError } = await import(new URL('services/personalContextApi.js', output));
const { AddContentDrawer } = await import(new URL('components/PersonalContext/AddContentDrawer.js', output));

test('stop and master switch requests allow 90 seconds without changing request contracts', async () => {
  requests.length = 0;
  await pcApi.stopRuntime();
  await pcApi.setMasterEnabled(false);
  await pcApi.stopRun('notes');
  for (const [method, params, options] of requests) {
    assert.equal(options.timeoutMs, 90000, method);
    assert.ok(!('locale' in params));
  }
});

test('stop timeout classification does not depend on the backend message language', () => {
  assert.equal(isFetchStopTimeoutError({ status: 'CONTEXT_PROACTIVE_RUNTIME_TIMEOUT', operation: 'deactivate_runtime', message: '停止采集超时' }), true);
});

async function withDrawer({ edit = true, provider = 'browser_bookmarks', service = {} } = {}, run) {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  const globals = { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true };
  const originals = new Map(Object.keys(globals).map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  for (const [key, value] of Object.entries(globals)) Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  const submissions = [];
  globalThis.__pcAuditStore = {
    config: { master_enabled: true, collection_enabled: true },
    createService: async (...args) => submissions.push(args),
    updateService: async (...args) => submissions.push(args),
    pendingWrites: {},
    isProviderAuthorized: () => true,
  };
  const root = createRoot(dom.window.document.getElementById('root'));
  try {
    await act(async () => root.render(createElement(AddContentDrawer, {
      initialProvider: provider,
      editService: edit ? { service_id: 'notes', provider, interval_seconds: 86400, max_items_per_run: 20, source: {}, ...service } : null,
      onClose() {},
      onCreated() {},
    })));
    const change = async (input, value, validity) => {
      assert.ok(input, 'input must exist');
      await act(async () => Simulate.change(input, { target: { value, validity } }));
    };
    await run({ document: dom.window.document, submissions, change });
  } finally {
    await act(async () => root.unmount());
    delete globalThis.__pcAuditStore;
    for (const [key, value] of originals) {
      if (value) Object.defineProperty(globalThis, key, value);
      else delete globalThis[key];
    }
    dom.window.close();
  }
}

function assertFieldError(document, input) {
  assert.equal(input.getAttribute('aria-invalid'), 'true');
  const hint = document.getElementById(input.getAttribute('aria-describedby'));
  assert.ok(hint, 'error must be linked to the input');
  assert.ok(hint.classList.contains('pc-drawer__field-error'));
  assert.ok(hint.textContent);
}

test('required and overlong names show an accessible field error immediately', async () => {
  await withDrawer({ edit: false }, async ({ document, change }) => {
    const input = document.querySelector('input');
    assertFieldError(document, input);
    await change(input, 'x'.repeat(501));
    assertFieldError(document, input);
    await change(input, '有效名称');
    assert.notEqual(input.getAttribute('aria-invalid'), 'true');
    assert.equal(document.querySelector('.pc-drawer__foot-btn--primary').disabled, false);
  });
});

test('invalid source fields have visible linked errors in the local UI', async () => {
  for (const provider of ['local_files', 'zhihu_reader', 'toutiao_reader', 'github', 'gitcode']) {
    await withDrawer({ provider }, async ({ document }) => assertFieldError(document, document.querySelector('input')));
  }
});

for (const [fieldIndex, name, validUpper] of [[0, 'frequency', 365], [1, 'maxItems', 40]]) {
  test(`${name} rejects fractions, non-finite values and out-of-range values without coercion`, async () => {
    await withDrawer({}, async ({ document, submissions, change }) => {
      await act(async () => document.querySelector('.pc-drawer__advanced-toggle').click());
      const input = document.querySelectorAll('input[type="number"]')[fieldIndex];
      const submit = document.querySelector('.pc-drawer__foot-btn--primary');
      for (const value of ['1.5', '0', '-1', String(validUpper + 1), '1e309']) {
        await change(input, value);
        assert.equal(submit.disabled, true, `${name}: ${value}`);
        assertFieldError(document, input);
        await act(async () => submit.click());
        assert.equal(submissions.length, 0);
      }
      await change(input, String(validUpper));
      assert.equal(submit.disabled, false);
      await change(input, '1');
      assert.equal(submit.disabled, false);
      await act(async () => submit.click());
      assert.equal(submissions.length, 1);
      assert.equal(submissions[0][1][fieldIndex === 0 ? 'interval_seconds' : 'max_items_per_run'], fieldIndex === 0 ? 86400 : 1);
    });
  });
}

test('an existing fractional frequency remains invalid rather than being silently rounded', async () => {
  await withDrawer({ service: { interval_seconds: 5400 } }, async ({ document }) => {
    assert.equal(document.querySelector('.pc-drawer__foot-btn--primary').disabled, true);
  });
});

test('resource selection errors are associated with their control', async () => {
  for (const provider of ['feishu', 'github', 'gitcode']) {
    await withDrawer({ provider, service: { source: { owner: 'owner', repo: 'repo', resources: [] } } }, async ({ document }) => {
      assertFieldError(document, document.querySelector('.pc-drawer__multi-select-trigger'));
      if (provider !== 'feishu') assert.notEqual(document.querySelector('input').getAttribute('aria-invalid'), 'true');
    });
  }
});

test('invalid numeric input sanitized by the browser is not treated as an empty default', async () => {
  await withDrawer({}, async ({ document, change }) => {
    await act(async () => document.querySelector('.pc-drawer__advanced-toggle').click());
    const input = document.querySelectorAll('input[type="number"]')[1];
    await change(input, '', { badInput: true });
    assert.equal(document.querySelector('.pc-drawer__foot-btn--primary').disabled, true);
    assertFieldError(document, input);
    await act(async () => document.querySelector('[data-testid="personal-context-max-items-increase"]').click());
    assert.equal(document.querySelector('.pc-drawer__foot-btn--primary').disabled, false);
    await change(input, '', { badInput: true });
    await change(input, '', { badInput: false });
    assert.equal(document.querySelector('.pc-drawer__foot-btn--primary').disabled, false);
  });
});

test('empty frequency is invalid, empty max items keeps its default, and hour range is checked', async () => {
  await withDrawer({}, async ({ document, change }) => {
    await act(async () => document.querySelector('.pc-drawer__advanced-toggle').click());
    const [frequency, maxItems] = document.querySelectorAll('input[type="number"]');
    const submit = document.querySelector('.pc-drawer__foot-btn--primary');
    await change(frequency, '');
    assert.equal(submit.disabled, true);
    await change(frequency, '1');
    await change(maxItems, '');
    assert.equal(submit.disabled, false);
    await act(async () => document.querySelector('[data-testid="personal-context-frequency-hour"]').click());
    await change(frequency, '8760');
    assert.equal(submit.disabled, false);
    await change(frequency, '8761');
    assert.equal(submit.disabled, true);
    assertFieldError(document, frequency);
  });
});

test('field error styling uses semantic danger tokens and both locales include result messages', async () => {
  const css = await readFile(new URL('../src/components/PersonalContext/AddContentDrawer.css', import.meta.url), 'utf8');
  assert.match(css, /\.pc-drawer__field-error\s*\{[^}]*color:\s*var\(--color-[^)]*(?:danger|error)[^)]*\)/s);
  for (const language of ['zh', 'en']) {
    const locale = JSON.parse(await readFile(new URL(`../src/i18n/locales/${language}.json`, import.meta.url), 'utf8'));
    for (const key of ['createdNodes', 'updatedNodes', 'noNewContent', 'collectionDisabledHint', 'collectionStoppingHint', 'stateCancelled']) assert.ok(locale.personalContext.services[key], `${language}: ${key}`);
    assert.ok(locale.personalContext.addContent.frequencyRangeError);
    assert.ok(locale.personalContext.addContent.localFiles.rootDirRequired, `${language}: localFiles.rootDirRequired`);
  }
});
