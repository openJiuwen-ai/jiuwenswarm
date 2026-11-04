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
  MutationObserver: dom.window.MutationObserver,
  ResizeObserver: class { observe() {} disconnect() {} },
  IS_REACT_ACT_ENVIRONMENT: true,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  cancelAnimationFrame: clearTimeout,
});
window.requestAnimationFrame = globalThis.requestAnimationFrame;
window.cancelAnimationFrame = globalThis.cancelAnimationFrame;
const resources = JSON.parse(readFileSync(new URL('../src/i18n/locales/en.json', import.meta.url)));
await i18next
  .use(initReactI18next)
  .init({ lng: 'en', showSupportNotice: false, resources: { en: { translation: resources } } });
const { AppMarketPanel } = await import('../node_modules/.cache/app-market-panel/components/AppMarketPanel/index.mjs');
const { webClient } = await import('../node_modules/.cache/app-market-panel/services/webClient.mjs');
const { webAppViews } = await import('../node_modules/.cache/app-market-panel/components/AppMarketPanel/webAppViews.mjs');
test.afterEach(() => webAppViews.close('gomoku'));
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
    assert.equal(find('app-market-status-text').textContent, 'Not started');
    assert.ok(find('app-market-pane-hint'));
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
    assert.equal(find('app-market-toggle').getAttribute('aria-label'), 'Stop app');
    assert.equal(document.querySelectorAll('[data-testid="app-market-toggle"]').length, 1);
    assert.ok(find('app-market-open'));
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
    assert.equal(find('app-market-web-view').getAttribute('src'), running.url);
    assert.equal(ctx.opened.length, 0);
    assert.equal(find('app-market-toggle').getAttribute('aria-label'), 'Stop app');
    assert.ok(find('app-market-open'));
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

test('desktop startup and reopen use the app view without conversation or headless dependencies', async () => {
  const navigations = [];
  window.jiuwenDesktop = {
    isElectron: true,
    openMarketApp: async (...args) => {
      navigations.push(args);
      return true;
    },
  };
  const ctx = await mount((method) => (method === 'app_market.start' ? running : response(method)));
  try {
    await click(find('app-market-toggle'));
    assert.equal(ctx.opened.length, 0);
    assert.equal(navigations.length, 1);
    assert.deepEqual(navigations[0], [app.app_id, running.url, app.name]);
    assert.equal(find('app-market-browser-pane'), null);
    assert.equal(find('app-market-web-view'), null);
    assert.equal(find('app-market-status-text').textContent, 'Running');
    assert.equal(find('app-market-open').textContent, 'Open / restore');
    assert.equal(find('app-market-toggle').getAttribute('aria-label'), 'Stop app');
    assert.match(find('app-market-pane-hint').textContent, /Closing a window keeps the app running/);
    await click(find('app-market-open'));
    assert.equal(navigations.length, 2);
    assert.equal(ctx.calls.filter(({ method }) => method === 'app_market.start').length, 1);
  } finally {
    delete window.jiuwenDesktop;
    await ctx.close();
  }
});

test('Web app uses an embedded iframe and preserves it across hiding and market navigation', async () => {
  let instances = [running];
  const handler = method => {
    if (method === 'app_market.stop') { instances = [{ ...running, state: 'stopped' }]; return { stopped: true }; }
    return response(method, instances);
  };
  let ctx = await mount(handler);
  try {
    await click(find('app-market-open'));
    const frame = find('app-market-web-view');
    assert.equal(frame.tagName, 'IFRAME');
    assert.equal(frame.getAttribute('src'), running.url);
    assert.equal(frame.getAttribute('title'), app.name);
    assert.match(frame.getAttribute('sandbox'), /allow-scripts/);
    assert.doesNotMatch(frame.getAttribute('sandbox'), /allow-top-navigation/);
    assert.equal(ctx.opened.length, 0);
    await click(find('app-market-browser-hide'));
    assert.equal(frame.isConnected, true);
    assert.equal(frame.style.visibility, 'hidden');
    assert.equal(ctx.calls.some(({ method }) => method === 'app_market.stop'), false);
    await click(find('app-market-open'));
    assert.equal(find('app-market-web-view'), frame);
    await ctx.close(); ctx = null;
    assert.equal(frame.isConnected, true);
    assert.equal(frame.style.visibility, 'hidden');
    ctx = await mount(handler);
    await click(find('app-market-open'));
    assert.equal(find('app-market-web-view'), frame);
    await click(find('app-market-browser-close'));
    assert.equal(frame.isConnected, false);
    assert.equal(find('app-market-status-text').textContent, 'Running');
    await click(find('app-market-open'));
    assert.notEqual(find('app-market-web-view'), frame);
    await click(find('app-market-toggle'));
    assert.equal(find('app-market-web-view'), null);
    assert.equal(find('app-market-browser-pane'), null);
    assert.equal(find('app-market-status-text').textContent, 'Not started');
  } finally { if (ctx) await ctx.close(); }
});

test('desktop window guidance can be dismissed across visits without starting or stopping an app', async () => {
  localStorage.removeItem('app-market-desktop-window-hint-dismissed');
  window.jiuwenDesktop = { isElectron: true, openMarketApp: async () => true };
  let ctx;
  try {
    ctx = await mount((method) => response(method, [running]));
    assert.ok(find('app-market-pane-hint'));
    await click(find('app-market-pane-hint-dismiss'));
    assert.equal(find('app-market-pane-hint'), null);
    assert.equal(ctx.calls.some(({ method }) => ['app_market.start', 'app_market.stop'].includes(method)), false);
    await ctx.close();
    ctx = null;
    ctx = await mount((method) => response(method, [running]));
    assert.equal(find('app-market-pane-hint'), null);
    assert.equal(find('app-market-open').textContent, 'Open / restore');
  } finally {
    if (ctx) await ctx.close();
    localStorage.removeItem('app-market-desktop-window-hint-dismissed');
    delete window.jiuwenDesktop;
  }
});

test('Web embedded pane hides for modal overlays and restores without stealing focus', async () => {
  const calls = [];
  const original = webAppViews.setViewport;
  webAppViews.setViewport = (...args) => { calls.push(args); return original(...args); };
  const ctx = await mount(method => response(method, [running]));
  try {
    await click(find('app-market-open'));
    const viewport = find('app-market-browser-viewport');
    viewport.getBoundingClientRect = () => ({ left: 400, top: 100, width: 500, height: 600 });
    window.dispatchEvent(new dom.window.Event('resize'));
    await tick(); await tick();
    assert.equal(find('app-market-web-view').style.visibility, 'visible');
    const modal = document.createElement('div');
    modal.setAttribute('role', 'dialog'); modal.setAttribute('aria-modal', 'true');
    await act(async () => document.body.append(modal));
    await tick(); await tick();
    assert.equal(find('app-market-web-view').style.visibility, 'hidden');
    await act(async () => modal.remove());
    await tick(); await tick();
    assert.equal(find('app-market-web-view').style.visibility, 'visible');
    assert.equal(calls.at(-1)[2], false);
    await click(find('app-market-browser-expand'));
    assert.ok(find('app-market-panel').classList.contains('app-market-layout-expanded'));
  } finally { await ctx.close(); webAppViews.setViewport = original; }
});

test('desktop without its open API reports an update error instead of using Web embedding', async () => {
  window.jiuwenDesktop = { isElectron: true };
  const ctx = await mount(method => response(method, [running]));
  try {
    await click(find('app-market-open'));
    assert.equal(ctx.opened.length, 0);
    assert.equal(find('app-market-browser-pane'), null);
    assert.equal(find('app-market-web-view'), null);
    assert.match(document.querySelector('[role="alert"]').textContent, /Restart with the updated desktop app/);
  } finally { await ctx.close(); delete window.jiuwenDesktop; }
});

for (const succeeds of [true, false]) {
  test(`desktop stop ${succeeds ? 'success closes only the app view after acknowledgement' : 'failure keeps the app view'}`, async () => {
    const stopped = deferred();
    const closed = [];
    let instances = [running];
    window.jiuwenDesktop = {
      isElectron: true,
      closeMarketApp: async id => { closed.push(id); return true; },
    };
    const ctx = await mount(method => method === 'app_market.stop' ? stopped.promise : response(method, instances));
    try {
      await click(find('app-market-toggle'));
      assert.deepEqual(closed, []);
      assert.equal(find('app-market-status-text').textContent, 'Stopping');
      if (succeeds) {
        instances = [{ ...running, state: 'stopped' }];
        await act(async () => stopped.resolve({ stopped: true }));
        await tick();
        assert.deepEqual(closed, [app.app_id]);
        assert.equal(find('app-market-status-text').textContent, 'Not started');
      } else {
        await act(async () => stopped.reject(new Error('Stop rejected')));
        await tick();
        assert.deepEqual(closed, []);
        assert.equal(find('app-market-status-text').textContent, 'Running');
        assert.match(document.querySelector('[role="alert"]').textContent, /Stop rejected/);
      }
    } finally {
      delete window.jiuwenDesktop;
      await ctx.close();
    }
  });
}

test('desktop window cleanup failure does not undo a successful service stop', async () => {
  let instances = [running];
  window.jiuwenDesktop = {
    isElectron: true,
    closeMarketApp: async () => { throw new Error('Window unavailable'); },
  };
  const ctx = await mount(method => {
    if (method === 'app_market.stop') { instances = [{ ...running, state: 'stopped' }]; return { stopped: true }; }
    return response(method, instances);
  });
  try {
    await click(find('app-market-toggle'));
    assert.equal(find('app-market-status-text').textContent, 'Not started');
    assert.match(document.querySelector('[role="alert"]').textContent, /app stopped.*window could not close/i);
    assert.doesNotMatch(document.querySelector('[role="alert"]').textContent, /Failed to stop/);
  } finally {
    delete window.jiuwenDesktop;
    await ctx.close();
  }
});

test('a pending desktop page load does not hold the application startup lock', async () => {
  const pageLoad = deferred();
  let instances = [];
  window.jiuwenDesktop = {
    isElectron: true,
    openMarketApp: () => pageLoad.promise,
  };
  const ctx = await mount((method) => {
    if (method === 'app_market.start') {
      instances = [running];
      return running;
    }
    return response(method, instances);
  });
  try {
    await click(find('app-market-toggle'));
    assert.equal(find('app-market-status').getAttribute('aria-label'), 'Running');
    assert.equal(find('app-market-toggle').disabled, false);
    assert.equal(find('app-market-delete').disabled, false);
    assert.ok(find('app-market-open'));
    await act(async () => pageLoad.reject(new Error('Page load failed')));
    assert.match(document.querySelector('[role="alert"]').textContent, /Page load failed/);
    assert.equal(find('app-market-status').getAttribute('aria-label'), 'Running');
    assert.equal(ctx.opened.length, 0);
    instances = [{ ...running, state: 'stopped' }];
    await click(find('app-market-toggle'));
    assert.equal(ctx.calls.filter(({ method }) => method === 'app_market.stop').length, 1);
    assert.equal(find('app-market-status').getAttribute('aria-label'), 'Not started');
  } finally {
    await act(async () => pageLoad.resolve(true));
    delete window.jiuwenDesktop;
    await ctx.close();
  }
});

test('desktop open failures remain visible without falling back to the system browser', async () => {
  window.jiuwenDesktop = {
    isElectron: true,
    openMarketApp: async () => { throw new Error('Entry unavailable'); },
  };
  const ctx = await mount((method) => response(method, [running]));
  try {
    await click(find('app-market-open'));
    assert.match(document.querySelector('[role="alert"]').textContent, /Entry unavailable/);
    assert.equal(ctx.opened.length, 0);
    assert.equal(find('app-market-status').getAttribute('aria-label'), 'Running');
  } finally {
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
