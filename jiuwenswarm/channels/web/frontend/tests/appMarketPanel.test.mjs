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
const { AppMarketPage } = await import('../node_modules/.cache/app-market-panel/components/AppMarketPanel/AppMarketPage.mjs');
const { webClient } = await import('../node_modules/.cache/app-market-panel/services/webClient.mjs');
const { AppMarketSessionPreview, AppMarketPreviewReload } = await import('../node_modules/.cache/app-market-panel/components/AppMarketPanel/AppMarketSessionPreview.mjs');
const { useSessionStore } = await import('../node_modules/.cache/app-market-panel/stores/sessionStore.mjs');
const { webAppViews } = await import('../node_modules/.cache/app-market-panel/components/AppMarketPanel/webAppViews.mjs');
const { closeAppMarketView, trackAppMarketView, hasAppMarketView } = await import('../node_modules/.cache/app-market-panel/components/AppMarketPanel/appMarketViews.mjs');
test.afterEach(async () => {
  await closeAppMarketView('gomoku');
  await closeAppMarketView('second');
});
const app = {
  app_id: 'gomoku',
  source_session: 'source-session',
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
const secondApp = { ...app, app_id: 'second', name: 'Second' };
const secondInstance = { ...running, app_id: 'second', instance_id: 'second-active', port: 8081,
  url: 'http://127.0.0.1:8081/index.html', web_url: 'http://workswarm-app-second.localhost:8081/index.html' };
const card = id => document.querySelector(`[data-testid="app-market-card"][data-variant="${id}"]`);
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

async function mount(handler, props = {}) {
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
  await act(async () => root.render(React.createElement(AppMarketPanel, props)));
  let mounted = true;
  const unmount = async () => {
    if (mounted) await act(async () => root.unmount());
    mounted = false;
  };
  return {
    calls,
    opened,
    poll: async () => {
      await act(async () => intervals[0]());
      await tick();
    },
    unmount,
    close: async () => {
      await unmount();
      webClient.request = previous;
      globalThis.setInterval = previousInterval;
    },
  };
}

const response = (method, instances = []) => (method === 'app_market.list' ? { apps: [app] } : { instances });

// 应用市场已是侧边栏一级入口（AppMarketPage）：页头 + 市场面板的独立页面，
// 不再经由扩展页 tab 承载（MarketplacePage 的 'app' tab 已移除）。
test('app market page renders a standalone page header above the market panel', async () => {
  const previous = webClient.request;
  const previousInterval = globalThis.setInterval;
  globalThis.setInterval = () => 987654;
  webClient.request = async (method) => response(method);
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(React.createElement(AppMarketPage)));
    const title = find('app-market-page-title');
    assert.ok(title, 'page title is rendered');
    assert.equal(title.textContent, i18next.t('appMarket.title'));
    assert.ok(find('app-market-panel'), 'market panel is embedded below the header');
    await tick();
    assert.ok(card('gomoku'), 'app cards render inside the standalone page');
  } finally {
    await act(async () => root.unmount());
    webClient.request = previous;
    globalThis.setInterval = previousInterval;
  }
});

for (const action of ['stop', 'delete']) {
  test(`${action} closes only the selected desktop application when two apps are running`, async () => {
    const closed = [];
    const windows = new Map();
    let instances = [running, secondInstance];
    window.jiuwenDesktop = {
      isElectron: true,
      openMarketApp: async (appId, url) => { windows.set(appId, url); return true; },
      closeMarketApp: async appId => { closed.push(appId); windows.delete(appId); return true; },
    };
    const ctx = await mount((method, params) => {
      if (method === 'app_market.list') return { apps: [app, secondApp] };
      if (method === 'app_market.stop' || method === 'app_market.delete') {
        instances = instances.filter(item => item.app_id !== secondApp.app_id);
        return { stopped: true, deleted: true };
      }
      return { instances };
    });
    try {
      await click(card('gomoku').querySelector('[data-testid="app-market-open"]'));
      const secondCard = card('second');
      await click(secondCard.querySelector('[data-testid="app-market-open"]'));
      assert.deepEqual([...windows], [[app.app_id, running.url], [secondApp.app_id, secondInstance.url]]);
      window.dispatchEvent(new CustomEvent('jiuwenclaw-desktop-browser-tab-change', {
        detail: { sessionId: '', flags: { requested: true, closed: true } },
      }));
      await tick();
      assert.equal(ctx.calls.some(({ method }) => method === 'app_market.stop'), false);
      await click(secondCard.querySelector(`[data-testid="app-market-${action === 'stop' ? 'toggle' : 'delete'}"]`));
      assert.deepEqual(closed, ['second']);
      assert.deepEqual([...windows], [[app.app_id, running.url]]);
      const requests = ctx.calls.filter(({ method }) => ['app_market.stop', 'app_market.delete'].includes(method));
      assert.deepEqual(requests, [{
        method: `app_market.${action}`,
        params: action === 'stop' ? { instance_id: 'second-active' } : { app_id: 'second' },
      }]);
      assert.equal(instances.some(item => item.instance_id === running.instance_id), true);
    } finally {
      delete window.jiuwenDesktop;
      await ctx.close();
    }
  });
}

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

test('a starting app does not replace a page the user opened later', async () => {
  const pending = deferred();
  const instances = [secondInstance];
  const ctx = await mount((method) => {
    if (method === 'app_market.list') return { apps: [app, secondApp] };
    if (method === 'app_market.start') return pending.promise;
    return { instances };
  });
  try {
    await click(card('gomoku').querySelector('[data-testid="app-market-toggle"]'));
    assert.equal(card('gomoku').querySelector('[data-testid="app-market-status"]').ariaLabel, 'Starting');
    // The user explicitly switches to another running app while the start is
    // still in flight; its completion must not take the pane back.
    await click(card('second').querySelector('[data-testid="app-market-open"]'));
    assert.equal(find('app-market-browser-pane').dataset.variant, 'second');
    await act(async () => pending.resolve(running));
    await tick();
    assert.equal(find('app-market-browser-pane').dataset.variant, 'second');
    assert.equal(document.querySelector('[data-testid="app-market-web-view"][data-variant="gomoku"]'), null);
    assert.equal(card('gomoku').querySelector('[data-testid="app-market-status"]').ariaLabel, 'Running');
    assert.ok(card('gomoku').querySelector('[data-testid="app-market-open"]'));
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

test('Web polling removes only the stopped instance, even after leaving the market', async () => {
  let instances = [running, secondInstance];
  const ctx = await mount(method => method === 'app_market.list' ? { apps: [app, secondApp] } : { instances });
  try {
    const frame = id => document.querySelector(`[data-testid="app-market-web-view"][data-variant="${id}"]`);
    await click(card('gomoku').querySelector('[data-testid="app-market-open"]'));
    const firstPage = frame('gomoku');
    await click(card('second').querySelector('[data-testid="app-market-open"]'));
    const secondPage = frame('second');
    assert.equal(firstPage.src, running.url);
    assert.equal(secondPage.src, secondInstance.web_url);
    assert.equal(find('app-market-browser-pane').dataset.variant, 'second');
    instances = [{ ...running, state: 'stopped' }, secondInstance];
    await ctx.poll();
    assert.equal(card('gomoku').querySelector('[data-testid="app-market-status"]').ariaLabel, 'Not started');
    assert.equal(firstPage.isConnected, false);
    assert.equal(frame('second'), secondPage);
    assert.equal(find('app-market-browser-pane').dataset.variant, 'second');

    instances = [{ ...secondInstance, instance_id: 'second-restarted' }];
    await ctx.poll();
    assert.equal(secondPage.isConnected, false);
    await click(card('second').querySelector('[data-testid="app-market-open"]'));
    const restartedPage = frame('second');
    assert.equal(restartedPage.src, secondInstance.web_url);

    await ctx.unmount();
    instances = [];
    await ctx.poll();
    assert.equal(restartedPage.isConnected, false);
    assert.equal(ctx.calls.some(({ method }) => ['app_market.start', 'app_market.stop'].includes(method)), false);
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
    assert.equal(find('app-market-web-view').src, running.url);
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

for (const desktop of [false, true]) {
  for (const started of [false, true]) {
    test(`${desktop ? 'Electron' : 'Web'} edit ${started ? 'reuses the running app' : 'starts the stopped app'} before entering a conversation`, async () => {
      const targets = [];
      const opens = [];
      const editable = { ...app, entry_path: 'C:/apps/gomoku/index.html' };
      if (desktop) window.jiuwenDesktop = {
        isElectron: true,
        openMarketApp: async (...args) => { opens.push(args); return true; },
        closeMarketApp: async () => true,
        setMarketAppViewport: async () => true,
      };
      const ctx = await mount(method => {
        if (method === 'app_market.list') return { apps: [editable] };
        if (method === 'app_market.start') return running;
        return { instances: started ? [running] : [] };
      }, { onEdit: target => targets.push(target) });
      try {
        await click(find('app-market-edit'));
        await tick();
        assert.deepEqual(targets, [{ appId: app.app_id, name: app.name, instanceId: running.instance_id,
          entryPath: editable.entry_path, sourceSession: editable.source_session }]);
        assert.equal(ctx.calls.filter(call => call.method === 'app_market.start').length, started ? 0 : 1);
        assert.equal(find('app-market-browser-pane'), null);
        if (desktop) assert.deepEqual(opens, [[app.app_id, running.url, app.name, true]]);
        else assert.equal(find('app-market-web-view').src, running.url);
      } finally { await ctx.close(); delete window.jiuwenDesktop; }
    });
  }
}

test('failed edit startup leaves the user in the market with an error', async () => {
  const targets = [];
  const ctx = await mount(method => {
    if (method === 'app_market.list') return { apps: [{ ...app, entry_path: '/apps/game/index.html' }] };
    if (method === 'app_market.start') throw new Error('Startup failed');
    return { instances: [] };
  }, { onEdit: target => targets.push(target) });
  try {
    await click(find('app-market-edit'));
    assert.deepEqual(targets, []);
    assert.match(document.querySelector('[role="alert"]').textContent, /Startup failed/);
    assert.equal(find('app-market-web-view'), null);
  } finally { await ctx.close(); }
});

test('session preview reuses the market iframe and clears its binding when the instance stops', async () => {
  let live = true;
  let target;
  const ctx = await mount(method => method === 'app_market.list'
    ? { apps: [{ ...app, entry_path: '/apps/gomoku/index.html' }] }
    : { instances: live ? [running] : [] }, { onEdit: value => { target = value; } });
  let root;
  try {
    await click(find('app-market-edit'));
    const frame = find('app-market-web-view');
    useSessionStore.getState().ensureRuntime('new');
    useSessionStore.getState().setAppMarketPreview('new', target);
    await ctx.unmount();
    root = createRoot(document.getElementById('root'));
    await act(async () => root.render(React.createElement(AppMarketSessionPreview, { sessionId: 'new', preview: target })));
    assert.equal(find('app-market-web-view'), frame);
    assert.ok(find('app-market-browser-toolbar'));
    assert.ok(find('app-market-resources'));
    const pane = find('app-market-session-preview');
    assert.equal(pane.dataset.variant, app.app_id);
    find('app-market-browser-viewport').getBoundingClientRect = () => ({ left: 0, top: 50, width: 1000, height: 700 });
    await act(async () => { pane.style.zIndex = '9999'; });
    await tick(); await tick();
    assert.equal(frame.style.zIndex, '9999');
    await act(async () => { pane.style.zIndex = ''; });
    await tick(); await tick();
    assert.equal(frame.style.zIndex, '10');
    live = false;
    await ctx.poll();
    assert.equal(find('app-market-web-view'), null);
    assert.equal(useSessionStore.getState().getRuntime('new').appMarketPreview, undefined);
  } finally {
    if (root) await act(async () => root.unmount());
    useSessionStore.getState().removeRuntime('new');
    await ctx.close();
  }
});

test('leaving the market while edit startup is pending cannot navigate to a new conversation later', async () => {
  const started = deferred();
  const targets = [];
  const ctx = await mount(method => {
    if (method === 'app_market.list') return { apps: [{ ...app, entry_path: '/apps/game/index.html' }] };
    if (method === 'app_market.start') return started.promise;
    return { instances: [] };
  }, { onEdit: target => targets.push(target) });
  try {
    await click(find('app-market-edit'));
    await ctx.unmount();
    await act(async () => started.resolve(running));
    await tick();
    assert.deepEqual(targets, []);
    assert.equal(find('app-market-web-view'), null);
  } finally { await ctx.close(); }
});

for (const succeeds of [true, false]) {
  test(`returning to an Electron edit session after closing its independent window ${succeeds ? 'recreates the page before showing its viewport' : 'clears the binding on recovery failure'}`, async () => {
    let alive = false;
    const opened = [];
    let recovery;
    window.jiuwenDesktop = {
      isElectron: true,
      openMarketApp: async (...args) => {
        opened.push(args);
        if (recovery) await recovery.promise;
        if (recovery && !succeeds) throw new Error('Cannot restore');
        alive = true;
        return true;
      },
      closeMarketApp: async () => { alive = false; return true; },
      setMarketAppViewport: async () => alive,
    };
    let target;
    const ctx = await mount(method => method === 'app_market.list'
      ? { apps: [{ ...app, entry_path: '/apps/game/index.html' }] }
      : { instances: [running] }, { onEdit: value => { target = value; } });
    let root;
    try {
      await click(find('app-market-edit'));
      await ctx.unmount();
      await window.jiuwenDesktop.openMarketApp(app.app_id, running.url, app.name);
      alive = false; // The independent window was closed while the service stayed running.
      recovery = deferred();
      useSessionStore.getState().ensureRuntime('web_edit');
      useSessionStore.getState().setAppMarketPreview('web_edit', target);
      root = createRoot(document.getElementById('root'));
      await act(async () => root.render(React.createElement(AppMarketSessionPreview, { sessionId: 'web_edit', preview: target })));
      assert.ok(find('app-market-preview-loading'));
      assert.equal(find('app-market-browser-viewport'), null);
      await act(async () => recovery.resolve());
      await tick();
      assert.deepEqual(opened.at(-1), [app.app_id, running.url, app.name, true]);
      assert.equal(alive, succeeds);
      assert.equal(Boolean(find('app-market-browser-viewport')), succeeds);
      assert.equal(Boolean(useSessionStore.getState().getRuntime('web_edit').appMarketPreview), succeeds);
    } finally {
      if (root) await act(async () => root.unmount());
      useSessionStore.getState().removeRuntime('web_edit');
      await ctx.close();
      delete window.jiuwenDesktop;
    }
  });
}

for (const desktop of [false, true]) {
  test(`${desktop ? 'Electron' : 'Web'} preview can reload repeatedly without restarting its service or changing the view identity`, async () => {
    let reloads = 0;
    if (desktop) window.jiuwenDesktop = {
      isElectron: true,
      openMarketApp: async () => true,
      closeMarketApp: async () => true,
      reloadMarketApp: async appId => { assert.equal(appId, app.app_id); reloads++; return true; },
    };
    const ctx = await mount(method => response(method, [running]));
    let root;
    const previousReload = webAppViews.reload;
    try {
      await click(find('app-market-open'));
      const frame = find('app-market-web-view');
      webAppViews.reload = appId => { reloads++; return previousReload(appId); };
      localStorage.setItem('app-market-reload-test-save', 'saved');
      await ctx.unmount();
      root = createRoot(document.getElementById('root'));
      await act(async () => root.render(React.createElement(AppMarketPreviewReload, {
        preview: { appId: app.app_id, name: app.name, instanceId: running.instance_id },
      })));
      await click(find('app-market-preview-reload'));
      await click(find('app-market-preview-reload'));
      assert.equal(reloads, 2);
      assert.equal(find('app-market-web-view'), frame);
      assert.equal(localStorage.getItem('app-market-reload-test-save'), 'saved');
      assert.equal(ctx.calls.some(call => ['app_market.start', 'app_market.stop', 'app_market.delete'].includes(call.method)), false);
    } finally {
      if (root) await act(async () => root.unmount());
      webAppViews.reload = previousReload;
      localStorage.removeItem('app-market-reload-test-save');
      await ctx.close();
      delete window.jiuwenDesktop;
    }
  });
}

for (const desktop of [false, true]) {
  test(`${desktop ? 'Electron' : 'Web'} stale editor reload cannot reset a replacement app instance`, async () => {
    let reloads = 0;
    if (desktop) window.jiuwenDesktop = {
      isElectron: true, openMarketApp: async () => true, closeMarketApp: async () => true,
      reloadMarketApp: async () => { reloads++; return true; },
    };
    const ctx = await mount(method => response(method, [running]));
    let root;
    const original = webAppViews.reload;
    try {
      await click(find('app-market-open'));
      await ctx.unmount();
      trackAppMarketView({ ...running, instance_id: 'replacement' }, () => true);
      webAppViews.reload = () => { reloads++; return true; };
      assert.equal(hasAppMarketView(app.app_id, running.instance_id), false);
      assert.equal(hasAppMarketView(app.app_id, 'replacement'), true);
      root = createRoot(document.getElementById('root'));
      await act(async () => root.render(React.createElement(AppMarketPreviewReload, {
        preview: { appId: app.app_id, name: app.name, instanceId: running.instance_id },
      })));
      await click(find('app-market-preview-reload'));
      assert.equal(reloads, 0);
      assert.equal(hasAppMarketView(app.app_id, 'replacement'), true);
    } finally {
      if (root) await act(async () => root.unmount());
      webAppViews.reload = original;
      await ctx.close();
      delete window.jiuwenDesktop;
    }
  });
}

import { transform } from 'esbuild';

// Exercise the actual App handoff with a mounted market so unmount invalidates
// the same intent callback used by production navigation.
// The handler now lives in App.tsx as the handleEditApp useCallback (shared by the
// standalone app-market page); slice the async arrow out of the useCallback and
// compile it standalone — the identifiers it closes over are exactly the deps below.
// Normalize CRLF first so the slice anchors work on Windows checkouts too.
const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8').replace(/\r\n/g, '\n');
const editStart = appSource.indexOf('async (target: AppMarketEditTarget, isCurrent: () => boolean) => {');
const editEnd = appSource.indexOf('\n    },\n    [', editStart);
// editEnd sits right before the arrow's closing "}" (which precedes the ",\n    [" of
// the deps list), so stop there and append the brace back.
const editCode = appSource.slice(editStart, editEnd) + '}';
const compiledEdit = (await transform(`export default ${editCode}`, { loader: 'tsx', format: 'cjs' })).code;
function editHandler(dependencies) {
  return new Function(...Object.keys(dependencies), `const module = { exports: {} }; ${compiledEdit}; return module.exports.default;`)(...Object.values(dependencies));
}

for (const desktop of [false, true]) {
  for (const canceled of [false, true]) {
    test(`${desktop ? 'Electron' : 'Web'} edit ${canceled ? 'does not navigate after leaving the market' : 'restores bound history and preserves its draft'}`, async () => {
      if (desktop) window.jiuwenDesktop = { isElectron: true, openMarketApp: async () => true, closeMarketApp: async () => true, setMarketAppViewport: async () => true };
      const pending = deferred();
      const restored = [];
      const changes = [];
      const sidRef = { current: 'new' };
      const store = { getModelSelectionPayload: () => ({}), setAppMarketPreview: (...args) => changes.push(['preview', ...args]) };
      const draft = { value: 'existing editor draft' };
      const handler = editHandler({
        pendingAppEditsRef: { current: new Map() }, creatingSessionRef: { current: false }, sessionIdRef: sidRef,
        request: () => pending.promise,
        useSessionStore: { getState: () => store },
        useChatStore: { getState: () => ({ setInputValue: (_, value) => { draft.value = value; } }) },
        t: key => key,
        closeSidebarPeek: () => changes.push('closePeek'),
        handleRestoreSession: async (id, mode, session, options) => {
          assert.equal(options.canActivate(), true);
          restored.push(session); sidRef.current = id;
        },
        setHeartbeatPanelOpen: () => changes.push('heartbeat'),
        setToolPanelHidden: () => changes.push('toolPanel'),
        setSingleAgentPanelActiveTab: () => changes.push('appTab'),
        setSingleAgentPanelExpanded: () => changes.push('expanded'),
      });
      const ctx = await mount(method => method === 'app_market.list' ? { apps: [{ ...app, entry_path: '/apps/game/index.html' }] } : response(method, [running]), { onEdit: handler });
      try {
        await click(find('app-market-edit'));
        if (canceled) await ctx.unmount();
        const session = { session_id: 'bound-editor', mode: 'agent', message_count: 8, project_id: '2048' };
        await act(async () => pending.resolve({ session, created: false }));
        await tick();
        assert.deepEqual(restored, canceled ? [] : [session]);
        assert.equal(draft.value, 'existing editor draft');
        assert.equal(changes.length > 0, !canceled);
      } finally { await ctx.close(); delete window.jiuwenDesktop; }
    });
  }
}
const { AppMarketEditorAction } = await import('../node_modules/.cache/app-market-panel/components/AppMarketPanel/AppMarketEditorAction.mjs');
for (const desktop of [false, true]) {
  for (const stopped of [false, true]) {
    test(`${desktop ? 'Electron' : 'Web'} editor header restores a closed preview with ${stopped ? 'a stopped' : 'a running'} app`, async () => {
      let opens = 0;
      let starts = 0;
      let live = !stopped;
      const metrics = { apps: [], 
        system: { cpuPercent: 12, memoryPercent: 34, sampledAt: Date.now() } };
      if (desktop) window.jiuwenDesktop = {
        isElectron: true, getMarketAppResources: async () => metrics, openMarketApp: async () => { opens++; return true; },
        closeMarketApp: async () => true, setMarketAppViewport: async () => true,
      };
      const ctx = await mount(method => {
        if (method === 'app_market.list') return { apps: [{ ...app, editor_session_id: 'editor' }] };
        if (method === 'app_market.start') { live = true; starts++; return running; }
        if (method === 'app_market.resources') return metrics;
        return { instances: live ? [running] : [] };
      });
      await ctx.unmount();
      const previews = [];
      let root;
      const render = async () => {
        root = createRoot(document.getElementById('root'));
        await act(async () => root.render(React.createElement(AppMarketEditorAction, { sessionId: 'editor', onOpen: preview => previews.push(preview) })));
      };
      try {
        await render();
        assert.match(find('app-market-editor-actions').textContent, /CPU 12.0% · RAM 34.0%/);
        assert.match(find('app-market-resources').textContent, desktop ? /This computer/ : /Service host/);
        await click(find('app-market-editor-open-preview'));
        assert.deepEqual(previews[0], { appId: app.app_id, name: app.name, instanceId: running.instance_id });
        await act(async () => root.unmount());
        await render();
        assert.match(find('app-market-editor-actions').textContent, /CPU 12.0% · RAM 34.0%/);
        assert.match(find('app-market-resources').textContent, desktop ? /This computer/ : /Service host/);
        await click(find('app-market-editor-open-preview'));
        assert.equal(previews.length, 2);
        assert.equal(starts, stopped ? 1 : 0);
        assert.equal(ctx.calls.some(call => call.method === 'app_market.edit_session'), false);
        if (desktop) assert.equal(opens, 2);
        else assert.ok(find('app-market-web-view'));
      } finally {
        if (root) await act(async () => root.unmount());
        await ctx.close(); delete window.jiuwenDesktop;
      }
    });
  }
}

test('unbound conversation has no app header action and canceled restore cannot open its page', async () => {
  const pending = deferred();
  const ctx = await mount(method => method === 'app_market.list'
    ? { apps: [{ ...app, editor_session_id: 'editor' }] } : pending.promise);
  await ctx.unmount();
  let root = createRoot(document.getElementById('root'));
  let opens = 0;
  try {
    await act(async () => root.render(React.createElement(AppMarketEditorAction, { sessionId: 'other', onOpen: () => opens++ })));
    assert.equal(find('app-market-editor-open-preview'), null);
    assert.equal(find('app-market-editor-actions'), null);
    await act(async () => root.unmount());
    root = createRoot(document.getElementById('root'));
    await act(async () => root.render(React.createElement(AppMarketEditorAction, { sessionId: 'editor', onOpen: () => opens++ })));
    await click(find('app-market-editor-open-preview'));
    await act(async () => root.unmount()); root = null;
    await act(async () => pending.resolve({ instances: [running] }));
    await tick();
    assert.equal(opens, 0);
    assert.equal(find('app-market-web-view'), null);
  } finally { if (root) await act(async () => root.unmount()); await ctx.close(); }
});

const { useChatStore } = await import('../node_modules/.cache/app-market-panel/stores/chatStore.mjs');
function firstEditHarness(request) {
  const sidRef = { current: 'new' };
  const restores = [];
  const requests = { current: new Map() };
  const handler = editHandler({
    pendingAppEditsRef: requests, creatingSessionRef: { current: false }, sessionIdRef: sidRef,
    request, useChatStore,
    useSessionStore: { getState: () => ({ getModelSelectionPayload: () => ({}), setAppMarketPreview: () => {} }) },
    t: (key, params) => `${key}:${params?.entryPath ?? ''}`,
    closeSidebarPeek: () => {},
    handleRestoreSession: async (id, mode, session, options) => {
      if (!options.canActivate()) return;
      restores.push(id); sidRef.current = id;
      useChatStore.getState().ensureRuntime(id);
    },
    setHeartbeatPanelOpen: () => {}, setToolPanelHidden: () => {},
    setSingleAgentPanelActiveTab: () => {}, setSingleAgentPanelExpanded: () => {},
  });
  return { handler, restores, requests };
}
const editTarget = { appId: 'gomoku', name: 'Gomoku', entryPath: '/game/index.html', instanceId: 'active' };
const editSession = { session_id: 'first-editor', mode: 'agent', message_count: 0 };

for (const desktop of [false, true]) {
  test(`${desktop ? 'Electron' : 'Web'} repeated first-edit clicks share one request and keep the source prompt`, async () => {
    useChatStore.getState().removeRuntime(editSession.session_id);
    const pending = deferred();
    let calls = 0;
    const h = firstEditHarness(() => { calls++; return pending.promise; });
    if (desktop) window.jiuwenDesktop = { isElectron: true, openMarketApp: async () => true, closeMarketApp: async () => true, setMarketAppViewport: async () => true };
    const ctx = await mount(method => method === 'app_market.list'
      ? { apps: [{ ...app, entry_path: editTarget.entryPath }] } : response(method, [running]), { onEdit: h.handler });
    try {
      await click(find('app-market-edit'));
      await click(find('app-market-edit'));
      assert.equal(calls, 1);
      await act(async () => pending.resolve({ session: editSession, created: true }));
      await tick();
      assert.deepEqual(h.restores, [editSession.session_id]);
      assert.equal(useChatStore.getState().getRuntime(editSession.session_id).inputValue, 'appMarket.editPrompt:/game/index.html');
      assert.equal(h.requests.current.size, 0);
    } finally { await ctx.close(); useChatStore.getState().removeRuntime(editSession.session_id); delete window.jiuwenDesktop; }
  });
}

test('canceling first edit preserves its prompt for later bound-session reentry without touching another draft', async () => {
  useChatStore.getState().removeRuntime(editSession.session_id);
  useChatStore.getState().ensureRuntime('unrelated-draft');
  useChatStore.getState().setInputValue('unrelated-draft', 'keep my new task');
  const pending = deferred();
  let calls = 0;
  let current = true;
  const h = firstEditHarness(() => ++calls === 1 ? pending.promise : Promise.resolve({ session: editSession, created: false }));
  const first = h.handler(editTarget, () => current);
  current = false;
  pending.resolve({ session: editSession, created: true });
  await first;
  assert.deepEqual(h.restores, []);
  await h.handler(editTarget, () => true);
  assert.deepEqual(h.restores, [editSession.session_id]);
  assert.equal(useChatStore.getState().getRuntime(editSession.session_id).inputValue, 'appMarket.editPrompt:/game/index.html');
  assert.equal(useChatStore.getState().getRuntime('unrelated-draft').inputValue, 'keep my new task');
  useChatStore.getState().removeRuntime(editSession.session_id);
  useChatStore.getState().removeRuntime('unrelated-draft');
});

for (const value of ['user draft', '']) {
  test(`a late created response preserves ${value ? 'an existing draft' : 'a deliberately cleared draft'}`, async () => {
    useChatStore.getState().removeRuntime(editSession.session_id);
    const pending = deferred();
    const h = firstEditHarness(() => pending.promise);
    const operation = h.handler(editTarget, () => true);
    useChatStore.getState().ensureRuntime(editSession.session_id);
    useChatStore.getState().setInputValue(editSession.session_id, value);
    pending.resolve({ session: editSession, created: true });
    await operation;
    assert.equal(useChatStore.getState().getRuntime(editSession.session_id).inputValue, value);
    await h.handler(editTarget, () => true);
    assert.equal(useChatStore.getState().getRuntime(editSession.session_id).inputValue, value);
    useChatStore.getState().removeRuntime(editSession.session_id);
  });
}

test('a failed coalesced edit releases its request so retry can succeed', async () => {
  useChatStore.getState().removeRuntime(editSession.session_id);
  const failure = deferred();
  let calls = 0;
  const h = firstEditHarness(() => ++calls === 1 ? failure.promise : Promise.resolve({ session: editSession, created: true }));
  const first = h.handler(editTarget, () => false);
  const second = h.handler(editTarget, () => true);
  failure.reject(new Error('offline'));
  const results = await Promise.allSettled([first, second]);
  assert.deepEqual(results.map(result => result.status), ['rejected', 'rejected']);
  assert.equal(h.requests.current.size, 0);
  await h.handler(editTarget, () => true);
  assert.equal(calls, 2);
  assert.deepEqual(h.restores, [editSession.session_id]);
  useChatStore.getState().removeRuntime(editSession.session_id);
});


test('Web market and app toolbar share service sampling and clearly exclude browser usage', async () => {
  delete window.jiuwenDesktop;
  const ctx = await mount(method => method === 'app_market.resources' ? {
    apps: [{ appId: app.app_id, cpuPercent: 2.5, memoryBytes: 10485760 }],
    system: { cpuPercent: 65, memoryPercent: 70, sampledAt: Date.now() }, sampledAt: Date.now(),
  } : response(method, [running]));
  try {
    assert.match(find('app-market-resources').textContent, /Service host \(local connection\) CPU 65.0% \u00b7 RAM 70.0%/);
    await click(find('app-market-open'));
    const metrics = document.querySelectorAll('[data-testid="app-market-resources"]');
    assert.equal(metrics.length, 2);
    assert.match(metrics[1].textContent, /Service CPU 2.5% \u00b7 RAM 10 MiB/);
    assert.match(metrics[1].textContent, /Browser page usage unavailable/);
    assert.equal(ctx.calls.filter(call => call.method === 'app_market.resources').length, 1);
  } finally { await ctx.close(); }
});

test('desktop market reads only native market metrics, including shared-process annotation', async () => {
  let reads = 0;
  window.jiuwenDesktop = { isElectron: true, getMarketAppResources: async () => {
    reads++;
    return { apps: [], system: { cpuPercent: 40, memoryPercent: 50, sampledAt: Date.now() }, sampledAt: Date.now() };
  } };
  const ctx = await mount(method => response(method));
  try {
    assert.match(find('app-market-resources').textContent, /This computer CPU 40.0% \u00b7 RAM 50.0%/);
    assert.equal(find('app-market-resources-note'), null);
    assert.equal(reads, 1);
    assert.equal(ctx.calls.filter(call => call.method === 'app_market.resources').length, 0);
  } finally { await ctx.close(); delete window.jiuwenDesktop; }
});

test('failed resource reads display unknown rather than misleading zeros', async () => {
  delete window.jiuwenDesktop;
  const ctx = await mount(method => {
    if (method === 'app_market.resources') throw new Error('Unavailable');
    return response(method);
  });
  try { assert.match(find('app-market-resources').textContent, /CPU \u2014 \u00b7 RAM \u2014/); }
  finally { await ctx.close(); }
});


const { createPressureTracker } = await import('../node_modules/.cache/app-market-panel/components/AppMarketPanel/marketResources.mjs');
const { resourceHostLabel } = await import('../node_modules/.cache/app-market-panel/components/AppMarketPanel/AppMarketResources.mjs');

test('pressure warnings require sustained fresh samples, recover with hysteresis, and reset on gaps', () => {
  const update = createPressureTracker();
  const sample = (time, cpu = 90, memory = 50) => ({ cpuPercent: cpu, memoryPercent: memory, sampledAt: time });
  assert.equal(update(sample(1000)), false);
  assert.equal(update(sample(1000)), false);
  assert.equal(update(sample(4000)), false);
  assert.equal(update(sample(7000)), true);
  assert.equal(update(sample(10000, 80)), true);
  assert.equal(update(sample(13000, 70)), false);
  assert.equal(update(sample(16000, 20, 90)), false);
  assert.equal(update(sample(19000, 20, 90)), false);
  assert.equal(update(sample(22000, 20, 90)), true);
  assert.equal(update(undefined), false);
  assert.equal(update(sample(25000)), false);
  assert.equal(update(sample(45000)), false);
  assert.equal(update(sample(48000)), false);
});

test('resource host labels never identify remote or unknown endpoints as the local machine', () => {
  for (const endpoint of ['http://localhost:5173', 'ws://127.0.0.1:19000/ws', 'ws://[::1]:19000/ws']) {
    assert.equal(resourceHostLabel(false, endpoint), 'appMarket.localServiceResources');
  }
  for (const endpoint of ['https://server.example', 'ws://192.168.1.2/ws', 'https://localhost.example', 'invalid']) {
    assert.equal(resourceHostLabel(false, endpoint), 'appMarket.serverResources');
  }
  assert.equal(resourceHostLabel(true, 'https://server.example'), 'appMarket.localMachineResources');
});


test('market shows a persistent high-usage reminder and clears it after recovery', async () => {
  const originalTimeout = globalThis.setTimeout;
  let poll;
  let count = 0;
  let usage = 90;
  globalThis.setTimeout = (fn, delay, ...args) => delay === 3000 ? (poll = fn, 0) : originalTimeout(fn, delay, ...args);
  window.jiuwenDesktop = { isElectron: true, getMarketAppResources: async () => ({
    apps: [], 
    system: { cpuPercent: usage, memoryPercent: usage, sampledAt: ++count * 3000 }, sampledAt: Date.now(),
  }) };
  let ctx;
  try {
    ctx = await mount(method => response(method));
    assert.equal(find('app-market-resources-warning'), null);
    await act(async () => poll());
    assert.equal(find('app-market-resources-warning'), null);
    await act(async () => poll());
    assert.match(find('app-market-resources-warning').textContent, /High system usage/);
    usage = 70;
    await act(async () => poll());
    assert.equal(find('app-market-resources-warning'), null);
  } finally {
    if (ctx) await ctx.close();
    globalThis.setTimeout = originalTimeout;
    delete window.jiuwenDesktop;
  }
});

test('unsubscribing and resubscribing discards late resource responses and their warning state', async () => {
  const { marketResources } = await import('../node_modules/.cache/app-market-panel/components/AppMarketPanel/marketResources.mjs');
  const old = deferred();
  let reads = 0;
  window.jiuwenDesktop = { isElectron: true, getMarketAppResources: () => {
    reads++;
    return reads === 1 ? old.promise : Promise.resolve({ apps: [], 
      system: { cpuPercent: 12, memoryPercent: 34, sampledAt: Date.now() } });
  } };
  const off = marketResources.subscribe(() => {});
  off();
  const offNew = marketResources.subscribe(() => {});
  try {
    old.resolve({ apps: [], 
      system: { cpuPercent: 99, memoryPercent: 99, sampledAt: Date.now() } });
    await tick(); await tick();
    assert.equal(reads, 2);
    assert.equal(marketResources.getSnapshot().system.cpuPercent, 12);
    assert.equal(marketResources.getSnapshot().highUsage, false);
  } finally { offNew(); delete window.jiuwenDesktop; }
});


test('system CPU and memory colors update independently at boundaries without triggering early alerts', async () => {
  const originalTimeout = globalThis.setTimeout;
  let poll;
  let system = { cpuPercent: 0, memoryPercent: 49.9, sampledAt: 3000 };
  globalThis.setTimeout = (fn, delay, ...args) => delay === 3000 ? (poll = fn, 0) : originalTimeout(fn, delay, ...args);
  window.jiuwenDesktop = { isElectron: true, getMarketAppResources: async () => ({
    apps: [], system,
  }) };
  let ctx;
  try {
    ctx = await mount(method => response(method));
    const cpu = find('app-market-system-cpu-percent');
    const memory = find('app-market-system-memory-percent');
    assert.equal(cpu.className, 'app-market-usage-low');
    assert.equal(memory.className, 'app-market-usage-low');
    system = { cpuPercent: 50, memoryPercent: 84.9, sampledAt: 6000 };
    await act(async () => poll());
    assert.equal(cpu.className, 'app-market-usage-medium');
    assert.equal(memory.className, 'app-market-usage-medium');
    system = { cpuPercent: 85, memoryPercent: 25, sampledAt: 9000 };
    await act(async () => poll());
    assert.equal(cpu.className, 'app-market-usage-high');
    assert.equal(memory.className, 'app-market-usage-low');
    assert.equal(find('app-market-resources-warning'), null);
    system = { cpuPercent: null, memoryPercent: 100, sampledAt: 12000 };
    await act(async () => poll());
    assert.equal(cpu.className, 'app-market-usage-unknown');
    assert.equal(cpu.textContent, '—');
    assert.equal(memory.className, 'app-market-usage-high');
  } finally {
    if (ctx) await ctx.close();
    globalThis.setTimeout = originalTimeout;
    delete window.jiuwenDesktop;
  }
});
