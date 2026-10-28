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
  signingOptions: {},
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
      return { pending: pending(`${params.requestId}-updated`, params.selectedActionIndexes, params.requestId) };
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

test('selection changes stay local and approval regenerates the final selection once', async () => {
  await mount(async ({ fixture, get, boxes, click }) => {
    assert.equal(get('modify-range'), null);
    await click(boxes()[2]);
    await click(boxes()[2]);
    await click(boxes()[0]);
    assert.deepEqual(fixture.calls, []);
    assert.deepEqual(
      boxes().map((box) => box.checked),
      [false, true, true],
    );
    assert.equal(get('approve').disabled, false);
    await click(get('approve'));
    assert.deepEqual(fixture.calls, [
      {
        method: 'a4p.authorization.reprepare',
        params: { requestId: 'abc', session_id: 's', selectedActionIndexes: [1, 2] },
      },
      { method: 'a4p.authorization.complete', params: { requestId: 'abc-updated', session_id: 's' } },
    ]);
    assert.equal(get('card'), null);
  });
});

test('restoring the prepared selection approves without regeneration', async () => {
  await mount(async ({ fixture, get, boxes, click }) => {
    await click(boxes()[2]);
    await click(boxes()[2]);
    assert.deepEqual(fixture.calls, []);
    await click(get('approve'));
    assert.deepEqual(fixture.calls, [
      { method: 'a4p.authorization.complete', params: { requestId: 'abc', session_id: 's' } },
    ]);
  });
});

test('empty selection allows rejection only and does not regenerate', async () => {
  await mount(async ({ get, boxes, click, fixture }) => {
    for (const box of boxes()) await click(box);
    assert.equal(get('approve').disabled, true);
    assert.equal(get('reject').disabled, false);
    assert.deepEqual(fixture.calls, []);
    await click(get('reject'));
    assert.equal(fixture.calls.length, 1);
    assert.equal(fixture.calls[0].method, 'a4p.authorization.reject');
  });
});

for (const newer of [pending('ac', [0, 2], 'ab'), null]) {
  test(`late regeneration does not complete a superseded or ended request (${newer?.requestId})`, async () => {
    await mount(async ({ fixture, update, get, boxes, click }) => {
      let resolve;
      fixture.request = (method, params) => {
        fixture.calls.push({ method, params });
        return new Promise((r) => {
          resolve = r;
        });
      };
      await click(boxes()[2]);
      assert.deepEqual(fixture.calls, []);
      await click(get('approve'));
      assert.equal(get('candidates').disabled, true);
      assert.equal(get('approve').disabled, true);
      assert.equal(get('reject').disabled, true);
      await click(boxes()[0]);
      assert.equal(boxes()[0].checked, true);
      await act(async () => update(newer));
      await act(async () => resolve({ pending: pending('ab', [0, 1], 'abc') }));
      assert.equal(fixture.calls.length, 1);
      assert.equal(fixture.state.runtimes.s.pendingA4PAuthorization, newer);
    });
  });
}

test('replacement push before response continues approval with the new request', async () => {
  await mount(async ({ fixture, update, get, boxes, click }) => {
    let resolve;
    fixture.request = async (method, params) => {
      fixture.calls.push({ method, params });
      if (method.endsWith('reprepare'))
        return new Promise((r) => {
          resolve = r;
        });
      return {};
    };
    await click(boxes()[2]);
    await click(get('approve'));
    const replacement = pending('ab', [0, 1], 'abc');
    await act(async () => update(replacement));
    assert.equal(get('approve').disabled, true);
    await act(async () => resolve({ pending: replacement }));
    assert.deepEqual(fixture.calls[1], {
      method: 'a4p.authorization.complete',
      params: { requestId: 'ab', session_id: 's' },
    });
    assert.equal(get('card'), null);
  });
});

test('failed regeneration never completes and keeps the local selection for retry', async () => {
  await mount(async ({ fixture, get, boxes, click }) => {
    const request = fixture.request;
    fixture.request = async (method, params) => {
      fixture.calls.push({ method, params });
      throw new Error('connection lost');
    };
    await click(boxes()[2]);
    await click(get('approve'));
    assert.equal(fixture.calls.length, 1);
    assert.equal(fixture.calls[0].method, 'a4p.authorization.reprepare');
    assert.equal(get('approve').disabled, false);
    assert.equal(boxes()[2].checked, false);
    assert.match(get('body').textContent, /connection lost/);
    fixture.request = request;
    await click(get('approve'));
    assert.equal(fixture.calls[1].method, 'a4p.authorization.reprepare');
    assert.equal(fixture.calls[2].method, 'a4p.authorization.complete');
    assert.equal(get('card'), null);
  });
});

test('failed completion after regeneration retries the latest object without regenerating again', async () => {
  await mount(async ({ fixture, get, boxes, click }) => {
    const request = fixture.request;
    fixture.request = async (method, params) => {
      if (method.endsWith('complete')) {
        fixture.calls.push({ method, params });
        throw new Error('completion failed');
      }
      return request(method, params);
    };
    await click(boxes()[2]);
    await click(get('approve'));
    assert.equal(fixture.calls.length, 2);
    assert.equal(get('approve').disabled, false);
    assert.match(get('body').textContent, /completion failed/);
    fixture.request = request;
    await click(get('approve'));
    assert.deepEqual(fixture.calls[2], {
      method: 'a4p.authorization.complete',
      params: { requestId: 'abc-updated', session_id: 's' },
    });
    assert.equal(get('card'), null);
  });
});

test('failed completion keeps the mandate available for retry', async () => {
  await mount(async ({ fixture, get, click }) => {
    const request = fixture.request;
    fixture.request = async () => {
      throw new Error('connection lost');
    };
    await click(get('approve'));
    assert.equal(get('approve').disabled, false);
    fixture.request = request;
    await click(get('approve'));
    assert.equal(fixture.calls[0].method, 'a4p.authorization.complete');
    assert.deepEqual(fixture.calls[0].params, { requestId: 'abc', session_id: 's' });
    assert.equal(get('card'), null);
  });
});

for (const cron of [false, true]) {
  test(`card body contains only reason and scope (cron=${cron})`, async () => {
    await mount(async ({ update, get }) => {
      await act(async () =>
        update({
          ...pending('minimal', [0, 1]),
          uiContext: {
            reason: '保存报告',
            ...(cron ? { cronJobId: 'job-1', authorizationTarget: { schedule: { cronExpression: '0 9 * * *' } } } : {}),
          },
        }),
      );
      assert.deepEqual(
        [...get('body').children].map((el) => el.dataset.testid),
        ['a4p-authorization-reason', 'a4p-authorization-candidates'],
      );
      assert.equal(get('reason-content').textContent, '保存报告');
      assert.ok(get('header'));
      assert.ok(get('approve'));
      assert.ok(get('reject'));
    });
  });
}

test('reconnection restores original candidates and prepared selection', async () => {
  await mount(
    async ({ get, boxes }) => {
      assert.equal(boxes().length, 3);
      assert.deepEqual(
        boxes().map((b) => b.checked),
        [true, true, false],
      );
      assert.equal(get('modify-range'), null);
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
    assert.equal(get('candidates-title').textContent, '授权范围');
    assert.equal(document.querySelectorAll('fieldset').length, 1);
    assert.equal(get('prepared-scope'), null);
    assert.equal(get('selected-count').textContent, '已选 3 / 3 项');
    assert.equal(get('scope-status').textContent, '将按所选权限授权');
    await click(boxes()[2]);
    assert.equal(get('selected-count').textContent, '已选 2 / 3 项');
    assert.equal(get('scope-status').textContent, '将按所选权限授权');
    await click(boxes()[0]);
    await click(boxes()[1]);
    assert.equal(get('scope-status').textContent, '请至少选择一项权限，或拒绝本次授权');
  });
});

test('selection, parameter expansion and parameter text interaction are independent', async () => {
  await mount(async ({ get, boxes, click, update, fixture }) => {
    const toggles = [...document.querySelectorAll('[data-testid="a4p-authorization-action-toggle"]')];
    assert.ok(toggles.every((el) => el.getAttribute('aria-expanded') === 'false'));
    assert.equal(get('action-params').hidden, true);
    await click(toggles[0]);
    assert.equal(get('action-params').hidden, false);
    assert.equal(boxes()[0].checked, true);
    assert.equal(toggles[1].getAttribute('aria-expanded'), 'false');
    await click(get('action-title'));
    assert.equal(boxes()[0].checked, false);
    assert.equal(toggles[0].getAttribute('aria-expanded'), 'true');
    await click(get('action-param').querySelector('code'));
    assert.equal(boxes()[0].checked, false);
    await click(boxes()[0]);
    assert.equal(boxes()[0].checked, true);
    assert.equal(document.getElementById(toggles[0].getAttribute('aria-controls')), get('action-params'));
    assert.equal(document.getElementById(boxes()[0].getAttribute('aria-labelledby')), get('action-title'));
    assert.equal(fixture.calls.length, 0);
    assert.equal(get('footer').contains(get('selected-count')), true);
    await act(async () => update(pending('replacement', [0, 1, 2])));
    assert.equal(get('action-params').hidden, true);
    await click(get('action-toggle'));
    await act(async () => {
      fixture.state = {
        ...fixture.state,
        activeSessionId: 's2',
        runtimes: { ...fixture.state.runtimes, s2: { pendingA4PAuthorization: pending('replacement', [0, 1, 2]) } },
      };
      fixture.listeners.forEach((fn) => fn());
    });
    assert.equal(get('action-params').hidden, true);
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
    assert.equal(get('details'), null);
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
    assert.equal(get('modify-range'), null);
  });
});
