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
          '../../../../stores': 'export const useChatStore = selector => selector({activeSessionId:"session"});',
          '../../services/SettingsServicesProvider':
            'export const useSettingsServices = () => globalThis.a4pFixture.services;',
          '../../services/SettingsSourceProvider':
            'export const useSettingsSource = () => globalThis.a4pFixture.source;',
          '../../components': 'export const SettingRow = ({children}) => children;',
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
  Object.defineProperty(window, 'isSecureContext', { value: options.secure ?? true, configurable: true });
  window.PublicKeyCredential = class {
    static parseCreationOptionsFromJSON(v) {
      return v;
    }
    static parseRequestOptionsFromJSON(v) {
      return v;
    }
  };
  let browserCalls = 0;
  Object.defineProperty(window.navigator, 'credentials', {
    value: {
      create: async () => {
        browserCalls++;
      },
      get: async () => {},
    },
  });
  if (options.unsupported) delete window.PublicKeyCredential.parseCreationOptionsFromJSON;
  const calls = [],
    saves = [];
  let rejectStatus = options.rejectStatus;
  globalThis.a4pFixture = {
    source: {
      values: { a4p_enabled: options.enabled ?? true, a4p_require_user_signature: options.signed ?? false },
      savingKeys: new Set(),
      save: async (update) => {
        saves.push(update);
        Object.assign(a4pFixture.source.values, update);
      },
    },
    services: {
      isConnected: true,
      request: async (method) => {
        calls.push(method);
        if (method === 'a4p.webauthn.credentials.get') {
          if (rejectStatus) throw new Error('offline');
          if (options.pending) return new Promise(() => {});
          return { expectedOrigin: 'http://localhost:5173', rpId: 'localhost', credentials: [] };
        }
        throw new Error('registration reached');
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
      browserCalls: () => browserCalls,
      recover: () => {
        rejectStatus = false;
      },
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

for (const [name, options, reason] of [
  ['HTTP IP', { url: 'http://192.168.1.100:5173', secure: false }, 'insecureContext'],
  ['wrong port', { url: 'http://localhost:9000' }, 'originMismatch'],
  ['browser', { unsupported: true }, 'unsupported'],
  ['load failure', { rejectStatus: true }, 'statusUnavailable'],
  ['loading', { pending: true }, 'statusUnavailable'],
]) {
  test(name + ': passive viewing is quiet; attempts cannot save or register', async () => {
    await mount(options, async ({ get, click, saves, calls, browserCalls }) => {
      assert.equal(get('environment-error'), null);
      assert.equal(get('operation-error'), null);
      await click('signature');
      assert.equal(get('environment-error').textContent, 'a4pSettings.' + reason);
      assert.equal(get('signature').getAttribute('aria-checked'), 'false');
      assert.deepEqual(saves, []);
      await click('register');
      assert.equal(get('environment-error').textContent, 'a4pSettings.' + reason);
      assert.deepEqual(calls, ['a4p.webauthn.credentials.get']);
      assert.equal(browserCalls(), 0);
    });
  });
}

test('enabled signature shows error and can be disabled even with A4P off', async () => {
  await mount(
    { url: 'http://remote:5173', secure: false, signed: true, enabled: false },
    async ({ get, click, saves }) => {
      assert.equal(get('environment-error').textContent, 'a4pSettings.insecureContext');
      assert.deepEqual(saves, []);
      assert.equal(get('signature').disabled, false);
      await click('signature');
      assert.deepEqual(saves, [{ a4p_require_user_signature: false }]);
      assert.equal(get('environment-error'), null);
    },
  );
});

test('supported origin allows enable and reaches registration RPC', async () => {
  await mount({}, async ({ get, click, saves, calls }) => {
    await click('signature');
    assert.deepEqual(saves, [{ a4p_require_user_signature: true }]);
    assert.equal(get('environment-error'), null);
    await click('register');
    assert.ok(calls.includes('a4p.webauthn.registration.options'));
  });
});

test('status recovery clears stale environment hint', async () => {
  await mount({ rejectStatus: true }, async ({ get, click, recover }) => {
    await click('register');
    assert.ok(get('environment-error'));
    recover();
    await click('refresh');
    assert.equal(get('environment-error'), null);
  });
});
