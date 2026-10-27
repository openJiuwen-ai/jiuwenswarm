import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  localStorage: dom.window.localStorage,
  sessionStorage: dom.window.sessionStorage,
  CustomEvent: dom.window.CustomEvent,
  IS_REACT_ACT_ENVIRONMENT: true,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  cancelAnimationFrame: clearTimeout,
});
const resources = JSON.parse(readFileSync(new URL('../src/i18n/locales/en.json', import.meta.url)));
await i18next
  .use(initReactI18next)
  .init({ lng: 'en', showSupportNotice: false, resources: { en: { translation: resources } } });
const { AppMarketPanel } = await import('../node_modules/.cache/app-market-panel/components/AppMarketPanel/index.mjs');
const { webClient } = await import('../node_modules/.cache/app-market-panel/services/webClient.mjs');
const { applyBrowserDisplaySettings } = await import(
  '../node_modules/.cache/app-market-panel/features/desktopBrowserDisplay.mjs'
);
const app = {
  app_id: 'gomoku',
  name: 'Gomoku',
  version: '1.2.3',
  description: 'Play five in a row',
  icon_url: '/gomoku.png',
};
const running = {
  instance_id: 'active',
  app_id: 'gomoku',
  port: 8080,
  state: 'running',
  detail: '',
  created_at: '2026-10-23',
  url: 'http://127.0.0.1:8080/gomoku.html',
};
const find = (id) => document.querySelector(`[data-testid="${id}"]`);
const click = async (element) => act(async () => element.click());
const tick = async () =>
  act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
};

async function mount(handler) {
  const calls = [];
  const opened = [];
  const previous = webClient.request;
  const previousInterval = globalThis.setInterval;
  const intervals = [];
  globalThis.setInterval = (callback) => {
    intervals.push(callback);
    return 987654;
  };
  webClient.request = async (method, params) => {
    calls.push({ method, params });
    return handler(method, params);
  };
  window.open = (...args) => {
    opened.push(args);
    return null;
  };
  window.confirm = () => true;
  const root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(AppMarketPanel)));
  return {
    calls,
    opened,
    poll: async () => {
      await act(async () => intervals[0]());
      await tick();
    },
    close: async () => {
      await act(async () => root.unmount());
      webClient.request = previous;
      globalThis.setInterval = previousInterval;
    },
  };
}

const response = (method, instances = []) => (method === 'app_market.list' ? { apps: [app] } : { instances });

test('application cards reuse market layout and display metadata with an accessible stopped icon', async () => {
  const ctx = await mount((method) => response(method));
  try {
    assert.ok(document.querySelector('.card-grid-auto .page-card'));
    assert.ok(document.querySelector('.categoryTabs'));
    assert.equal(document.querySelector('h2'), null);
    assert.match(document.body.textContent, /Gomoku/);
    assert.match(document.body.textContent, /v1.2.3/);
    assert.match(document.body.textContent, /Play five in a row/);
    assert.equal(find('app-market-card-avatar').getAttribute('src'), '/gomoku.png');
    assert.equal(find('app-market-status').getAttribute('aria-label'), 'Not started');
    assert.equal(find('app-market-status').getAttribute('title'), 'Not started');
    assert.equal(find('app-market-delete').getAttribute('aria-label'), 'Delete');
  } finally {
    await ctx.close();
  }
});

test('active instance wins over history and the single toggle stops the running app', async () => {
  const ctx = await mount((method) =>
    response(method, [running, { ...running, instance_id: 'history', state: 'stopped', created_at: '2026-10-24' }]),
  );
  try {
    assert.equal(find('app-market-status').getAttribute('aria-label'), 'Running');
    assert.equal(find('app-market-toggle').getAttribute('aria-label'), 'Stop');
    assert.equal(document.querySelectorAll('[data-testid="app-market-toggle"]').length, 1);
    assert.equal(find('app-market-open'), null);
    await click(find('app-market-toggle'));
    assert.equal(ctx.calls.filter(({ method }) => method === 'app_market.stop').length, 1);
    assert.equal(ctx.opened.length, 0);
    assert.equal(ctx.calls.filter(({ method }) => method === 'app_market.start').length, 0);
  } finally {
    await ctx.close();
  }
});

test('starting is immediate, prevents duplicate starts and opens the returned entry on success', async () => {
  const pending = deferred();
  let instances = [];
  const ctx = await mount((method) => (method === 'app_market.start' ? pending.promise : response(method, instances)));
  try {
    await click(find('app-market-toggle'));
    assert.equal(find('app-market-status').getAttribute('aria-label'), 'Starting');
    assert.equal(find('app-market-toggle').disabled, true);
    await click(find('app-market-toggle'));
    assert.equal(ctx.calls.filter(({ method }) => method === 'app_market.start').length, 1);
    instances = [running];
    await act(async () => pending.resolve(running));
    await tick();
    assert.equal(find('app-market-status').getAttribute('aria-label'), 'Running');
    assert.equal(ctx.opened[0][0], running.url);
    assert.equal(find('app-market-toggle').getAttribute('aria-label'), 'Stop');
    assert.equal(find('app-market-open'), null);
  } finally {
    await ctx.close();
  }
});

test('stop shows stopping immediately and returns to stopped when the operation completes', async () => {
  const pending = deferred();
  let instances = [running];
  const ctx = await mount((method) => (method === 'app_market.stop' ? pending.promise : response(method, instances)));
  try {
    await click(find('app-market-toggle'));
    assert.equal(find('app-market-status').getAttribute('aria-label'), 'Stopping');
    assert.equal(find('app-market-toggle').disabled, true);
    instances = [{ ...running, state: 'stopped' }];
    await act(async () => pending.resolve({ ...running, state: 'stopped' }));
    await tick();
    assert.equal(find('app-market-status').getAttribute('aria-label'), 'Not started');
    assert.equal(find('app-market-toggle').getAttribute('aria-label'), 'Start');
  } finally {
    await ctx.close();
  }
});

test('start failure displays the server detail and restores stopped without opening a tab', async () => {
  const ctx = await mount((method) => {
    if (method === 'app_market.start') throw new Error('Port 8080 is occupied');
    return response(method);
  });
  try {
    await click(find('app-market-toggle'));
    assert.match(document.querySelector('[role="alert"]').textContent, /Port 8080 is occupied/);
    assert.equal(find('app-market-status').getAttribute('aria-label'), 'Not started');
    assert.equal(ctx.opened.length, 0);
  } finally {
    await ctx.close();
  }
});

test('initial load failures are visible instead of appearing as an empty successful list', async () => {
  const ctx = await mount((method) => {
    if (method === 'app_market.list') throw new Error('Connection lost');
    return response(method);
  });
  try {
    assert.match(document.querySelector('[role="alert"]').textContent, /Connection lost/);
  } finally {
    await ctx.close();
  }
});

test('polling updates an externally stopped app without restarting it', async () => {
  let instances = [running];
  const ctx = await mount((method) => response(method, instances));
  try {
    instances = [{ ...running, state: 'stopped' }];
    await ctx.poll();
    assert.equal(find('app-market-status').getAttribute('aria-label'), 'Not started');
    assert.ok(find('app-market-toggle'));
    assert.equal(ctx.calls.filter(({ method }) => method === 'app_market.start').length, 0);
  } finally {
    await ctx.close();
  }
});

test('an old poll cannot overwrite a newly started instance', async () => {
  const oldPoll = deferred();
  let first = true;
  const ctx = await mount((method) => {
    if (method === 'app_market.start') return running;
    if (method === 'app_market.instances') {
      if (first) {
        first = false;
        return { instances: [] };
      }
      return oldPoll.promise;
    }
    return response(method);
  });
  try {
    await ctx.poll();
    await click(find('app-market-toggle'));
    await act(async () => oldPoll.resolve({ instances: [] }));
    assert.equal(find('app-market-status').getAttribute('aria-label'), 'Running');
  } finally {
    await ctx.close();
  }
});

test('desktop startup opens the running URL inside the existing browser panel', async () => {
  const navigations = [];
  window.jiuwenDesktop = {
    isElectron: true,
    browser: {
      navigate: async (...args) => {
        navigations.push(args);
      },
      setHeadless: async (value) => value,
    },
  };
  await applyBrowserDisplaySettings({ headless: false });
  const ctx = await mount((method) => (method === 'app_market.start' ? running : response(method)));
  try {
    await click(find('app-market-toggle'));
    assert.equal(ctx.opened.length, 0);
    assert.equal(navigations.length, 1);
    const url = new URL(navigations[0][0]);
    assert.equal(url.pathname, '/gomoku.html');
    assert.equal(url.port, '8080');
  } finally {
    await applyBrowserDisplaySettings({ headless: true });
    delete window.jiuwenDesktop;
    await ctx.close();
  }
});

test('delete retains confirmation and cancellation does not remove the app', async () => {
  const ctx = await mount((method) => response(method));
  try {
    window.confirm = () => false;
    await click(find('app-market-delete'));
    assert.equal(ctx.calls.filter(({ method }) => method === 'app_market.delete').length, 0);
    assert.ok(find('app-market-card'));
    window.confirm = () => true;
    await click(find('app-market-delete'));
    assert.equal(ctx.calls.filter(({ method }) => method === 'app_market.delete').length, 1);
    assert.equal(find('app-market-card'), null);
  } finally {
    await ctx.close();
  }
});

test('a non-running start response is shown as a failure and never opens the app', async () => {
  const ctx = await mount((method) =>
    method === 'app_market.start'
      ? { ...running, state: 'stopped', detail: 'Entry exited before startup completed' }
      : response(method),
  );
  try {
    await click(find('app-market-toggle'));
    assert.match(document.querySelector('[role="alert"]').textContent, /Entry exited before startup completed/);
    assert.equal(find('app-market-status').getAttribute('aria-label'), 'Not started');
    assert.equal(ctx.opened.length, 0);
  } finally {
    await ctx.close();
  }
});
