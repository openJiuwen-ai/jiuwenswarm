import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

await build({
  entryPoints: ['src/features/A4PAuthorizationModal.tsx'],
  outfile: 'node_modules/.cache/a4p-authorization.mjs',
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  loader: { '.css': 'empty' },
  plugins: [
    {
      name: 'authorization-boundaries',
      setup(builder) {
        const mocks = {
          '../stores': `import {useSyncExternalStore} from 'react';
        export const useChatStore = selector => selector(useSyncExternalStore(
          listener => {globalThis.a4pCard.listeners.add(listener); return () => globalThis.a4pCard.listeners.delete(listener)},
          () => globalThis.a4pCard.state));
        useChatStore.getState = () => globalThis.a4pCard.state;
        export const useSessionStore = selector => selector({isConnected: globalThis.a4pCard.connected});`,
          '../services/webClient':
            'export const webClient = {request: (...args) => globalThis.a4pCard.request(...args)};',
          './a4p/webauthn':
            'export const getPasskeyAssertion = async () => {globalThis.a4pCard.signatures++; if(globalThis.a4pCard.cancelSignature) throw new Error("cancelled"); return {signed:true}};',
          'react-i18next':
            'export const useTranslation = () => ({t: (key, values) => globalThis.a4pCard.translate(key, values)});',
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
const { A4PAuthorizationCard } = await import('../node_modules/.cache/a4p-authorization.mjs');
const locale = JSON.parse(await readFile('src/i18n/locales/zh.json', 'utf8'));
const actions = ['pwd', 'ls', 'date'].map((command) => ({ name: 'bash', params: { command } }));
const pending = (id, indexes, replacesRequestId) => ({
  requestId: id,
  originalActions: actions,
  preparedActionIndexes: indexes,
  replacesRequestId,
  mandate: { intent: { actions: indexes.map((i) => actions[i]) } },
  signingOptions: { signatureMethod: 'webauthn', methodOptions: {} },
  uiContext: {},
  kind: 'intent',
});
async function mount(run, options = {}) {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost:5173', pretendToBeVisual: true });
  const old = new Map(
    ['window', 'document', 'IS_REACT_ACT_ENVIRONMENT', 'a4pCard'].map((k) => [
      k,
      Object.getOwnPropertyDescriptor(globalThis, k),
    ]),
  );
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const fixture = {
    translate: (key, values = {}) => {
      const text = key.split('.').reduce((value, part) => value?.[part], locale) ?? key;
      return text.replace(/{{(\w+)}}/g, (_, name) => String(values[name]));
    },
    listeners: new Set(),
    signatures: 0,
    calls: [],
    connected: options.connected ?? false,
  };
  const update = (request) => {
    fixture.state = { ...fixture.state, runtimes: { s: { pendingA4PAuthorization: request } } };
    fixture.listeners.forEach((fn) => fn());
  };
  fixture.state = {
    activeSessionId: 's',
    runtimes: { s: { pendingA4PAuthorization: pending('abc', [0, 1, 2]) } },
    setPendingA4PAuthorization: (_s, p) => update(p),
  };
  fixture.request = async (method, params) => {
    fixture.calls.push({ method, params });
    if (method === 'a4p.authorization.reprepare')
      return { pending: pending('ab', params.selectedActionIndexes, params.requestId) };
    return {};
  };
  if (options.request) fixture.request = options.request;
  globalThis.a4pCard = fixture;
  const root = createRoot(document.getElementById('root'));
  const get = (id) => document.querySelector(`[data-testid="a4p-authorization-${id}"]`);
  const boxes = () => [...document.querySelectorAll('input[type="checkbox"]')];
  const click = async (el) =>
    act(async () => {
      el.click();
    });
  try {
    await act(async () => root.render(createElement(A4PAuthorizationCard)));
    await run({ fixture, update, get, boxes, click });
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const [key, desc] of old) desc ? Object.defineProperty(globalThis, key, desc) : delete globalThis[key];
  }
}

test('selection gates modification and signing; C survives and can be restored', async () => {
  await mount(async ({ fixture, get, boxes, click }) => {
    assert.equal(get('modify-range').disabled, true);
    await click(boxes()[2]);
    assert.equal(get('modify-range').disabled, false);
    assert.equal(get('approve').disabled, true);
    await click(boxes()[2]);
    assert.equal(get('modify-range').disabled, true);
    assert.equal(get('approve').disabled, false);
    await click(boxes()[2]);
    await click(get('modify-range'));
    assert.equal(fixture.signatures, 0);
    assert.equal(boxes().length, 3);
    assert.deepEqual(
      boxes().map((b) => b.checked),
      [true, true, false],
    );
    assert.equal(get('modify-range').disabled, true);
    assert.equal(get('approve').disabled, false);
    await click(boxes()[2]);
    fixture.request = async (method, params) => {
      fixture.calls.push({ method, params });
      return method.endsWith('reprepare') ? { pending: pending('abc2', params.selectedActionIndexes, 'ab') } : {};
    };
    await click(get('modify-range'));
    assert.deepEqual(fixture.calls[1].params.selectedActionIndexes, [0, 1, 2]);
    assert.equal(fixture.signatures, 0);
    await click(get('approve'));
    assert.equal(fixture.signatures, 1);
    assert.equal(fixture.calls[2].params.requestId, 'abc2');
    assert.equal(get('card'), null);
  });
});

test('empty selection allows rejection only', async () => {
  await mount(async ({ get, boxes, click, fixture }) => {
    for (const box of boxes()) await click(box);
    assert.equal(get('modify-range').disabled, true);
    assert.equal(get('approve').disabled, true);
    assert.equal(get('reject').disabled, false);
    await click(get('reject'));
    assert.equal(fixture.calls[0].method, 'a4p.authorization.reject');
    assert.equal(fixture.signatures, 0);
  });
});

test('in-flight modification locks controls; late response cannot overwrite newer push', async () => {
  await mount(async ({ fixture, update, get, boxes, click }) => {
    let resolve;
    fixture.request = () =>
      new Promise((r) => {
        resolve = r;
      });
    await click(boxes()[2]);
    await click(get('modify-range'));
    assert.equal(get('candidates').disabled, true);
    assert.equal(get('approve').disabled, true);
    await act(async () => update(pending('ac', [0, 2], 'ab')));
    await act(async () => resolve({ pending: pending('ab', [0, 1], 'abc') }));
    assert.deepEqual(
      boxes().map((b) => b.checked),
      [true, false, true],
    );
    assert.equal(fixture.state.runtimes.s.pendingA4PAuthorization.requestId, 'ac');
  });
});

test('signature cancellation keeps latest mandate available for retry', async () => {
  await mount(async ({ fixture, get, click }) => {
    fixture.cancelSignature = true;
    await click(get('approve'));
    assert.equal(fixture.calls.length, 0);
    assert.equal(get('approve').disabled, false);
    fixture.cancelSignature = false;
    await click(get('approve'));
    assert.equal(fixture.calls[0].method, 'a4p.authorization.complete');
  });
});

test('reconnection restores original candidates and prepared selection', async () => {
  await mount(
    async ({ get, boxes }) => {
      assert.equal(boxes().length, 3);
      assert.deepEqual(
        boxes().map((b) => b.checked),
        [true, true, false],
      );
      assert.equal(get('modify-range').disabled, true);
    },
    { connected: true, request: async () => ({ pending: pending('ab', [0, 1], 'abc') }) },
  );
});

// Exercise the real store as well: component fixtures intentionally mock subscriptions only.
await build({
  entryPoints: ['src/stores/chatStore.ts'],
  outfile: 'node_modules/.cache/a4p-chat-store.mjs',
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  define: { 'import.meta.env': '{}' },
});
const { useChatStore } = await import('../node_modules/.cache/a4p-chat-store.mjs');
test('store ignores superseded pushes and terminal-before-request delivery', () => {
  const store = useChatStore.getState();
  store.ensureRuntime('a4p-store');
  const set = (value, ended) => store.setPendingA4PAuthorization('a4p-store', value, ended);
  const current = () => useChatStore.getState().runtimes['a4p-store'].pendingA4PAuthorization;
  set(pending('old', [0, 1, 2]));
  set(pending('new', [0, 1], 'old'));
  set(pending('old', [0, 1, 2]));
  assert.equal(current().requestId, 'new');
  set(null, 'old');
  assert.equal(current().requestId, 'new');
  set(null, 'future');
  set(pending('future', [0], 'new'));
  assert.equal(current().requestId, 'new');
  set(null, 'new');
  set(pending('new', [0, 1], 'old'));
  assert.equal(current(), null);
});

test('one permission scope, selected count and draft/ready/empty status', async () => {
  await mount(async ({ get, boxes, click }) => {
    assert.equal(get('candidates-title').textContent, '权限范围');
    assert.equal(document.querySelectorAll('fieldset').length, 1);
    assert.equal(get('prepared-scope'), null);
    assert.equal(get('selected-count').textContent, '已选 3 / 3 项');
    assert.equal(get('scope-status').textContent, '将按所选权限授权');
    await click(boxes()[2]);
    assert.equal(get('selected-count').textContent, '已选 2 / 3 项');
    assert.match(get('scope-status').textContent, /范围已修改/);
    await click(get('modify-range'));
    assert.equal(get('scope-status').textContent, '将按所选权限授权');
    await click(boxes()[0]);
    await click(boxes()[1]);
    assert.equal(get('scope-status').textContent, '请至少选择一项权限，或拒绝本次授权');
  });
});

test('clicking card, title or parameter toggles once, with native accessible checkboxes', async () => {
  await mount(async ({ get, boxes, click }) => {
    const card = get('action');
    await click(card);
    assert.equal(boxes()[0].checked, false);
    assert.equal(card.dataset.variant, 'unselected');
    await click(get('action-title'));
    assert.equal(boxes()[0].checked, true);
    await click(get('action-param').querySelector('code'));
    assert.equal(boxes()[0].checked, false);
    assert.equal(document.getElementById(boxes()[0].getAttribute('aria-labelledby')), get('action-title'));
    assert.equal(document.getElementById(boxes()[0].getAttribute('aria-describedby')), get('action-params'));
  });
});

test('long commands, primary parameter and unknown tool parameters are displayed in full', async () => {
  await mount(async ({ update, get }) => {
    const command = 'echo ' + 'long-path-segment/'.repeat(40);
    const custom = [
      { name: 'bash', params: { timeout: 123, command, cwd: '/workspace/test' } },
      { name: 'custom_tool', params: { target: '/a/b', options: { mode: 'read', names: ['one', 'two'] } } },
    ];
    await act(async () =>
      update({ ...pending('custom', [0, 1]), originalActions: custom, mandate: { intent: { actions: custom } } }),
    );
    const rows = [...document.querySelectorAll('[data-testid="a4p-authorization-action"]')];
    assert.equal(rows.length, 2);
    const parameters = [...rows[0].querySelectorAll('[data-param]')];
    assert.deepEqual(
      parameters.map((item) => item.dataset.param),
      ['command', 'timeout', 'cwd'],
    );
    assert.equal(parameters[0].querySelector('code').textContent, command);
    assert.equal(rows[1].querySelector('[data-testid="a4p-authorization-action-title"]').textContent, 'custom_tool');
    assert.equal(
      rows[1].querySelector('[data-param="options"] code').textContent,
      JSON.stringify(custom[1].params.options),
    );
    assert.equal(get('details').textContent.includes(command), false);
  });
});

test('only the A4P body scrolls; header and actions stay outside it', async () => {
  await mount(async ({ get }) => {
    assert.equal(get('body').contains(get('footer')), false);
    assert.equal(get('body').contains(get('header')), false);
    assert.equal(get('body').contains(get('candidates')), true);
    assert.equal(get('body').tabIndex, 0);
    assert.equal(get('footer').contains(get('approve')), true);
    assert.ok(get('card').style.getPropertyValue('--a4p-card-max-height').endsWith('px'));
  });
});

test('A4P height budget responds to viewport resizing without losing selection', async () => {
  await mount(async ({ get, boxes, click }) => {
    await click(boxes()[2]);
    const before = parseFloat(get('card').style.getPropertyValue('--a4p-card-max-height'));
    Object.defineProperty(window, 'innerHeight', { value: 360, configurable: true });
    await act(async () => {
      window.dispatchEvent(new window.Event('resize'));
      await new Promise((resolve) => window.requestAnimationFrame(resolve));
    });
    assert.ok(parseFloat(get('card').style.getPropertyValue('--a4p-card-max-height')) < before);
    assert.equal(boxes()[2].checked, false);
    assert.equal(get('modify-range').disabled, false);
  });
});
