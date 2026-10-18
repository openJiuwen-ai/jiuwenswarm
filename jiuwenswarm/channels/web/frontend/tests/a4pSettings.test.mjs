import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

// Render the real settings component with controlled service/store boundaries.
await build({
  entryPoints: ['src/features/settings/modules/experimental/A4PSettings.tsx'],
  outfile: 'node_modules/.cache/a4p-settings.mjs',
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  loader: { '.css': 'empty' },
  plugins: [
    {
      name: 'settings-boundaries',
      setup(builder) {
        const mocks = {
          '../../../../components/ui':
            'export { Switch } from "./src/components/ui/Switch/Switch"; export { Button } from "./src/components/ui/Button/Button";',
          '../../../../stores':
            'export const useChatStore = selector => selector({activeSessionId:globalThis.a4pFixture.session});',
          '../../services/SettingsServicesProvider':
            'export const useSettingsServices = () => globalThis.a4pFixture.services;',
          '../../services/SettingsSourceProvider':
            'export const useSettingsSource = () => globalThis.a4pFixture.source;',
          '../../components': 'export { SettingRow } from "./src/features/settings/components/SettingRow";',
          'react-i18next': 'export const useTranslation = () => ({t: key => key, i18n:{resolvedLanguage:"zh"}});',
        };
        builder.onResolve({ filter: /.*/ }, (args) =>
          mocks[args.path] ? { path: args.path, namespace: 'mock' } : undefined,
        );
        builder.onLoad({ filter: /.*/, namespace: 'mock' }, (args) => ({
          contents: mocks[args.path],
          loader: 'js',
          resolveDir: process.cwd(),
        }));
      },
    },
  ],
});
const { A4PSettings } = await import('../node_modules/.cache/a4p-settings.mjs');

async function mount(options, run) {
  const dom = new JSDOM('<div id="root"></div>', { url: options.url ?? 'http://localhost:5173' });
  const old = new Map(
    ['window', 'document', 'IS_REACT_ACT_ENVIRONMENT', 'a4pFixture'].map((k) => [
      k,
      Object.getOwnPropertyDescriptor(globalThis, k),
    ]),
  );
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const calls = [],
    saves = [];
  let config = { enabled: options.enabled ?? true };
  globalThis.a4pFixture = {
    session: options.session === undefined ? 'session' : options.session,
    services: {
      isConnected: true,
      request: async (method, params) => {
        calls.push(method);
        if (method === 'a4p.config.get') {
          if (options.rejectConfig) throw new Error('config unavailable');
          return { ...config };
        }
        if (method === 'a4p.config.update') {
          const { session_id, ...update } = params;
          assert.equal(session_id, 'session');
          saves.push(update);
          if (options.rejectUpdate) throw new Error('update failed');
          if (options.update) return options.update(update);
          config = { ...config, ...update };
          return config;
        }
        throw new Error(`Unexpected RPC: ${method}`);
      },
    },
  };
  const root = createRoot(document.getElementById('root'));
  const render = async () => act(async () => root.render(createElement(A4PSettings)));
  const get = (id) => document.querySelector(`[data-testid="settings-a4p-${id}"]`);
  const click = async (id) => {
    await act(async () => get(id).click());
    await render();
  };
  try {
    await render();
    await run({
      get,
      click,
      render,
      calls,
      saves,
      dom,
    });
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const [key, descriptor] of old) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
}

test('settings exposes only the A4P switch and saves enabled directly', async () => {
  await mount({ url: 'http://192.168.1.100:9000', enabled: false }, async ({ get, click, calls, saves }) => {
    assert.equal(document.querySelectorAll('[role="switch"]').length, 1);
    assert.equal(get('signature'), null);
    assert.equal(get('passkeys'), null);
    assert.deepEqual(calls, ['a4p.config.get']);
    await click('enabled');
    assert.deepEqual(saves, [{ enabled: true }]);
    assert.equal(get('enabled').getAttribute('aria-checked'), 'true');
    assert.equal(get('operation-error'), null);
  });
});

test('config load failure blocks toggles and surfaces error', async () => {
  await mount({ rejectConfig: true }, async ({ get, click, saves }) => {
    assert.equal(get('enabled').disabled, true);
    assert.equal(get('operation-error').textContent, 'config unavailable');
    await click('enabled');
    assert.deepEqual(saves, []);
  });
});

test('failed update retains confirmed state and permits retry', async () => {
  await mount({ rejectUpdate: true }, async ({ get, click, saves }) => {
    await click('enabled');
    assert.equal(get('enabled').getAttribute('aria-checked'), 'true');
    assert.equal(get('enabled').disabled, false);
    assert.equal(get('operation-error').textContent, 'update failed');
    assert.deepEqual(saves, [{ enabled: false }]);
  });
});

test('pending update blocks the toggle and applies canonical response', async () => {
  let resolve;
  await mount(
    {
      update: () =>
        new Promise((r) => {
          resolve = r;
        }),
    },
    async ({ get, click, saves }) => {
      await click('enabled');
      assert.equal(get('enabled').disabled, true);
      await click('enabled');
      assert.equal(saves.length, 1);
      await act(async () => resolve({ enabled: false }));
      assert.equal(get('enabled').getAttribute('aria-checked'), 'false');
    },
  );
});

test('missing session blocks config requests', async () => {
  await mount({ session: null }, async ({ get, calls }) => {
    assert.deepEqual(calls, []);
    assert.equal(get('enabled').disabled, true);
    assert.equal(get('register'), null);
  });
});

test('session switch ignores a stale update response', async () => {
  let resolve;
  await mount(
    {
      update: () =>
        new Promise((r) => {
          resolve = r;
        }),
    },
    async ({ get, click, render }) => {
      await click('enabled');
      a4pFixture.session = 'next-session';
      await render();
      await act(async () => resolve({ enabled: false }));
      assert.equal(get('enabled').getAttribute('aria-checked'), 'true');
    },
  );
});
