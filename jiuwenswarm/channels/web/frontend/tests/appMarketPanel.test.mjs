import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import React, { act } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  localStorage: dom.window.localStorage,
  sessionStorage: dom.window.sessionStorage,
  Event: dom.window.Event,
  CustomEvent: dom.window.CustomEvent,
  MutationObserver: dom.window.MutationObserver,
  ResizeObserver: class { observe() {} disconnect() {} },
  IS_REACT_ACT_ENVIRONMENT: true,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  cancelAnimationFrame: clearTimeout,
});
const { createRoot } = await import('react-dom/client');
window.requestAnimationFrame = globalThis.requestAnimationFrame;
window.cancelAnimationFrame = globalThis.cancelAnimationFrame;
const resources = JSON.parse(readFileSync(new URL('../src/i18n/locales/en.json', import.meta.url)));
const zhResources = JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url)));
await i18next
  .use(initReactI18next)
  .init({
    lng: 'en',
    showSupportNotice: false,
    resources: { en: { translation: resources }, zh: { translation: zhResources } },
  });
const { AppMarketPanel } = await import('../node_modules/.cache/app-market-panel/components/AppMarketPanel/index.mjs');
const { AppMarketPage } = await import('../node_modules/.cache/app-market-panel/components/AppMarketPanel/AppMarketPage.mjs');
const { webClient } = await import('../node_modules/.cache/app-market-panel/services/webClient.mjs');
const { WebHttpClient } = await import('../node_modules/.cache/app-market-panel/services/webHttpClient.mjs');
const { AppMarketSessionPreview, AppMarketPreviewReload } = await import('../node_modules/.cache/app-market-panel/components/AppMarketPanel/AppMarketSessionPreview.mjs');
const { useSessionStore } = await import('../node_modules/.cache/app-market-panel/stores/sessionStore.mjs');
const { webAppViews } = await import('../node_modules/.cache/app-market-panel/components/AppMarketPanel/webAppViews.mjs');
const { closeAppMarketView, trackAppMarketView, hasAppMarketView } = await import('../node_modules/.cache/app-market-panel/components/AppMarketPanel/appMarketViews.mjs');
const { ToastStack } = await import('../node_modules/.cache/app-market-panel/components/ui/Toast/Toast.mjs');
const { toast } = await import('../node_modules/.cache/app-market-panel/components/ui/Toast/toastStore.mjs');
test.afterEach(async () => {
  await closeAppMarketView('gomoku');
  await closeAppMarketView('second');
  await act(async () => toast.closeAll());
});
const app = {
  app_id: 'gomoku',
  source_session: 'source-session',
  name: 'Gomoku',
  version: '1.2.3',
  description: 'Play five in a row',
  icon_url: '/gomoku.png',
  entry_path: '/workspace/apps/gomoku/index.html',
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
const secondApp = {
  ...app,
  app_id: 'second',
  name: 'Second',
  entry_path: '/workspace/apps/second/index.html',
};
const secondInstance = { ...running, app_id: 'second', instance_id: 'second-active', port: 8081,
  url: 'http://127.0.0.1:8081/index.html', web_url: 'http://workswarm-app-second.localhost:8081/index.html' };
const card = id => document.querySelector(`[data-testid="app-market-card"][data-variant="${id}"]`);
const find = (id) => document.querySelector(`[data-testid="${id}"]`);
const actionNotice = () => document.querySelector('[data-testid="app-market-action-notice"]:not(.ui-toast--closing)');
const click = async (element) => act(async () => element.click());
const tick = async () =>
  act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
const more = async (id = 'gomoku') => {
  const trigger = card(id)?.querySelector('[data-testid="app-market-more"]');
  assert.ok(trigger, `more trigger missing for ${id}`);
  if (trigger.getAttribute('aria-expanded') !== 'true') await click(trigger);
};
const closeMore = async (id = 'gomoku') => {
  const trigger = card(id)?.querySelector('[data-testid="app-market-more"]');
  if (trigger?.getAttribute('aria-expanded') === 'true') await click(trigger);
  await act(async () => { await new Promise((r) => setTimeout(r, 150)); });
};
const menuClick = async (testId, id = 'gomoku') => {
  await more(id);
  const item = find(testId);
  assert.ok(item, `${testId} missing from more menu`);
  await click(item);
  await act(async () => { await new Promise((r) => setTimeout(r, 150)); });
};
const menuActionExists = async (testId, id = 'gomoku') => {
  await more(id);
  const exists = Boolean(find(testId));
  await closeMore(id);
  return exists;
};
const menuText = async (testId, id = 'gomoku') => {
  await more(id);
  const text = find(testId)?.textContent;
  await closeMore(id);
  return text;
};

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
  await act(async () => root.render(
    React.createElement(React.Fragment, null, React.createElement(AppMarketPanel, props), React.createElement(ToastStack))
  ));
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

for (const desktop of [false, true]) {
  test(`${desktop ? 'Electron' : 'Web'} installs from the second market page using the default directory`, async () => {
    if (desktop) window.jiuwenDesktop = { isElectron: true };
    const ctx = await mount((method, params) => {
      if (method === 'app_market.market_list') {
        const id = `app-${params.page}`;
        return { total: 51, items: [{ asset_id: id, display_name: id, public_latest_version: '1', tags: [] }] };
      }
      if (method === 'app_market.market_detail') return { asset_id: params.asset_id, version: '1', checksum_sha256: 'a'.repeat(64) };
      if (method === 'app_market.install_dir_default') return { path: '/workspace/apps' };
      if (method === 'app_market.install') return { job_id: 'install-job' };
      if (method === 'app_market.install_status') return { state: 'done', phase: 'done' };
      return response(method);
    });
    try {
      await click(document.querySelector('button[data-variant="market"]'));
      await click(document.querySelector(`button[aria-label="${i18next.t('common.pagination.next')}"]`));
      await click(find('app-market-install'));
      assert.equal(find('app-market-install-dir-input').value, '/workspace/apps');
      await click(find('app-market-install-dir').querySelector('button:last-child'));
      assert.deepEqual(ctx.calls.find(call => call.method === 'app_market.install')?.params, {
        asset_id: 'app-2', version: '1', install_dir: '/workspace/apps', expected_sha256: 'a'.repeat(64),
      });
    } finally {
      await ctx.close();
      delete window.jiuwenDesktop;
    }
  });
}

test('market reinstalls when the registered directory is missing', async () => {
  const ctx = await mount((method, params) => {
    if (method === 'app_market.market_list') {
      return {
        total: 1,
        items: [{
          asset_id: 'app-stale', display_name: 'Stale', public_latest_version: '1', tags: [],
          installed: true, installed_version: '1', installed_app_id: 'hub-app-stale',
          work_dir_missing: true,
        }],
      };
    }
    if (method === 'app_market.market_detail') {
      return {
        asset_id: 'app-stale', version: '1', checksum_sha256: 'a'.repeat(64),
        installed: true, installed_app_id: 'hub-app-stale', installed_work_dir_missing: true,
      };
    }
    if (method === 'app_market.delete') return { deleted: true };
    if (method === 'app_market.install_dir_default') return { path: '/workspace/apps' };
    if (method === 'app_market.install') return { job_id: 'install-job' };
    if (method === 'app_market.install_status') return { state: 'done', phase: 'done' };
    return response(method);
  });
  try {
    await click(document.querySelector('button[data-variant="market"]'));
    assert.equal(find('app-market-install').textContent.trim(), i18next.t('appMarket.reinstall'));
    await click(find('app-market-install'));
    await click(find('app-market-install-dir').querySelector('button:last-child'));
    const methods = ctx.calls.map(call => call.method);
    const deleted = methods.indexOf('app_market.delete');
    const installed = methods.indexOf('app_market.install');
    assert.ok(deleted >= 0, 'stale registration is removed');
    assert.ok(installed > deleted, 'install runs after deregistration');
    assert.equal(
      ctx.calls.find(call => call.method === 'app_market.delete')?.params.app_id,
      'hub-app-stale',
    );
    assert.equal(
      ctx.calls.find(call => call.method === 'app_market.install')?.params.asset_id,
      'app-stale',
    );
  } finally {
    await ctx.close();
  }
});

test('market cards open a detail page and return to the plaza', async () => {
  const ctx = await mount((method, params) => {
    if (method === 'app_market.market_list') {
      return {
        total: 1,
        items: [{
          asset_id: 'market-app', name: 'market-app', display_name: 'Market App',
          short_desc: 'short summary', public_latest_version: '2.0.0', icon_uri: '', tags: [],
          installed: false, installed_version: '', installed_app_id: '',
        }],
      };
    }
    if (method === 'app_market.market_detail') {
      return {
        asset_id: params.asset_id, version: '2.0.0', detail_desc: 'full package details',
        file_size: 2048, source_size: 4096, permissions: ['ai'], requires_project: true,
        has_server_script: false, checksum_sha256: 'a'.repeat(64),
      };
    }
    return response(method);
  });
  try {
    await click(document.querySelector('button[data-variant="market"]'));
    await tick();
    assert.equal(find('app-market-detail-page'), null);
    await click(find('app-market-market-card'));
    await tick();
    const page = find('app-market-detail-page');
    assert.ok(page);
    assert.match(page.textContent, /full package details/);
    assert.match(page.textContent, /2\.0 KB/);
    assert.match(page.textContent, /ai/);
    assert.doesNotMatch(page.textContent, /SHA-256|asset_id/i);
    assert.equal(find('app-market-market-card'), null);
    assert.equal(find('app-market-detail-back').textContent.trim(), 'Back to App Plaza');
    await click(find('app-market-detail-back'));
    assert.ok(find('app-market-market-card'));
    assert.equal(ctx.calls.filter(({ method }) => method === 'app_market.market_detail').length, 1);
  } finally {
    await ctx.close();
  }
});

test('market detail refreshes after an install completes and the list reports a new version', async () => {
  let hubVersion = '1.0.0';
  const marketItem = () => ({
    asset_id: 'listed', display_name: 'Listed', public_latest_version: hubVersion, tags: [],
  });
  const ctx = await mount((method, params) => {
    if (method === 'app_market.market_list') return { total: 1, items: [marketItem()] };
    if (method === 'app_market.market_detail') {
      return { asset_id: params.asset_id, version: hubVersion, display_name: 'Listed' };
    }
    if (method === 'app_market.install_dir_default') return { path: '/workspace/apps' };
    if (method === 'app_market.install') return { job_id: 'install-job' };
    if (method === 'app_market.install_status') return { state: 'done', phase: 'done' };
    return response(method);
  });
  try {
    await click(document.querySelector('button[data-variant="market"]'));
    await tick();
    await click(find('app-market-market-card'));
    await tick();
    // Assert on the detail grid only: the EntityHeader tags come from the list
    // item, so a whole-page match would pass even with a stale cached detail.
    const detailGrid = () => find('app-market-detail-grid').textContent;
    assert.match(detailGrid(), /v1\.0\.0/);
    await click(find('app-market-detail-back'));
    // The Hub publishes v2 and the user installs it; the install flow refreshes
    // the list, so the card label and the detail grid must both show v2.0.0.
    hubVersion = '2.0.0';
    await click(find('app-market-install'));
    await click(find('app-market-install-dir').querySelector('button:last-child'));
    await tick();
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 1100)); });
    await tick();
    await click(document.querySelector('button[data-variant="market"]'));
    await tick();
    await tick();
    assert.match(find('app-market-market-card').textContent, /v2\.0\.0/);
    await click(find('app-market-market-card'));
    await tick();
    assert.match(detailGrid(), /v2\.0\.0/);
    assert.doesNotMatch(detailGrid(), /v1\.0\.0/);
  } finally {
    await ctx.close();
  }
});

test('market detail refreshes when the list reports a new version after tab switches', async () => {
  let hubVersion = '1.0.0';
  const marketItem = () => ({
    asset_id: 'listed', display_name: 'Listed', public_latest_version: hubVersion, tags: [],
  });
  const ctx = await mount((method, params) => {
    if (method === 'app_market.market_list') return { total: 1, items: [marketItem()] };
    if (method === 'app_market.market_detail') {
      return { asset_id: params.asset_id, version: hubVersion, display_name: 'Listed' };
    }
    return response(method);
  });
  try {
    await click(document.querySelector('button[data-variant="market"]'));
    await tick();
    await click(find('app-market-market-card'));
    await tick();
    // Assert on the detail grid only: the EntityHeader tags come from the list
    // item, so a whole-page match would pass even with a stale cached detail.
    const detailGrid = () => find('app-market-detail-grid').textContent;
    assert.match(detailGrid(), /v1\.0\.0/);
    await click(find('app-market-detail-back'));
    // The Hub publishes v2 without any install; a plaza refresh must update the
    // detail grid too instead of replaying the cached v1 detail.
    hubVersion = '2.0.0';
    await click(document.querySelector('button[data-variant="all"]'));
    await tick();
    await click(document.querySelector('button[data-variant="market"]'));
    await tick();
    assert.match(find('app-market-market-card').textContent, /v2\.0\.0/);
    await click(find('app-market-market-card'));
    await tick();
    assert.match(detailGrid(), /v2\.0\.0/);
    assert.doesNotMatch(detailGrid(), /v1\.0\.0/);
    // An unchanged version must reuse the cache: one extra detail open, no new request.
    const detailCalls = ctx.calls.filter(({ method }) => method === 'app_market.market_detail').length;
    await click(find('app-market-detail-back'));
    await click(find('app-market-market-card'));
    await tick();
    assert.match(detailGrid(), /v2\.0\.0/);
    assert.equal(ctx.calls.filter(({ method }) => method === 'app_market.market_detail').length, detailCalls);
  } finally {
    await ctx.close();
  }
});

test('market detail refreshes when an install finishes while its detail page is open', async () => {
  let hubVersion = '1.0.0';
  const job = deferred();
  const ctx = await mount((method, params) => {
    if (method === 'app_market.market_list') {
      return { total: 1, items: [{ asset_id: 'listed', display_name: 'Listed', public_latest_version: hubVersion, tags: [] }] };
    }
    if (method === 'app_market.market_detail') {
      return { asset_id: params.asset_id, version: params.version || hubVersion, display_name: 'Listed' };
    }
    if (method === 'app_market.install_dir_default') return { path: '/workspace/apps' };
    if (method === 'app_market.install') return { job_id: 'install-job' };
    if (method === 'app_market.install_status') return job.promise;
    return response(method);
  });
  try {
    await click(document.querySelector('button[data-variant="market"]'));
    await tick();
    await click(find('app-market-install'));
    await click(find('app-market-install-dir').querySelector('button:last-child'));
    await tick();
    // The user opens the detail page while the install job is still running.
    await click(find('app-market-market-card'));
    await tick();
    const detailPage = () => find('app-market-detail-page');
    const detailGrid = () => find('app-market-detail-grid').textContent;
    assert.doesNotMatch(detailPage().textContent, /Loading\.\.\./);
    // Assert on the detail grid only: the EntityHeader tags come from the list
    // item, so a whole-page match would pass even with a stale cached detail.
    assert.match(detailGrid(), /v1\.0\.0/);
    hubVersion = '2.0.0';
    job.resolve({ state: 'done', phase: 'done' });
    for (let i = 0; i < 5; i += 1) await tick();
    // Dropping the stale cache must not leave the open page on the placeholder.
    assert.doesNotMatch(detailPage().textContent, /Loading\.\.\./);
    assert.match(detailGrid(), /v2\.0\.0/);
    assert.doesNotMatch(detailGrid(), /v1\.0\.0/);
  } finally {
    await ctx.close();
  }
});

test('a late stale detail response cannot overwrite the refreshed version', async () => {
  let hubVersion = '1.0.0';
  const firstDetail = deferred();
  let firstDetailPending = true;
  const ctx = await mount((method, params) => {
    if (method === 'app_market.market_list') {
      return { total: 1, items: [{ asset_id: 'listed', display_name: 'Listed', public_latest_version: hubVersion, tags: [] }] };
    }
    if (method === 'app_market.market_detail') {
      // The first (v1) request hangs; every later request answers immediately
      // with the version the caller asked for.
      if (firstDetailPending) return firstDetail.promise;
      return { asset_id: params.asset_id, version: params.version || hubVersion, display_name: 'Listed' };
    }
    return response(method);
  });
  try {
    await click(document.querySelector('button[data-variant="market"]'));
    await tick();
    await click(find('app-market-market-card'));
    await tick();
    // The first detail request is still hanging, so the page shows loading.
    assert.match(find('app-market-detail-page').textContent, /Loading\.\.\./);
    await click(find('app-market-detail-back'));
    // The Hub publishes v2 and the plaza refreshes; the pinned v1 entry no
    // longer matches the list version, so reopening re-requests v2.
    hubVersion = '2.0.0';
    firstDetailPending = false;
    await click(document.querySelector('button[data-variant="all"]'));
    await tick();
    await click(document.querySelector('button[data-variant="market"]'));
    await tick();
    await click(find('app-market-market-card'));
    await tick();
    assert.match(find('app-market-detail-page').textContent, /v2\.0\.0/);
    // The v1 response arrives after the v2 cache entry is already in place;
    // it must not put v1 back on the screen.
    firstDetail.resolve({ asset_id: 'listed', version: '1.0.0', display_name: 'Listed' });
    for (let i = 0; i < 5; i += 1) await tick();
    assert.doesNotMatch(find('app-market-detail-page').textContent, /v1\.0\.0/);
    assert.match(find('app-market-detail-page').textContent, /v2\.0\.0/);
  } finally {
    await ctx.close();
  }
});

test('market detail returns to the plaza when a pending refresh removes its app', async () => {
  const refreshed = deferred();
  const item = { asset_id: 'listed', display_name: 'Listed', public_latest_version: '1.0.0', tags: [] };
  let marketRequests = 0;
  const ctx = await mount(method => {
    if (method === 'app_market.market_list') {
      return marketRequests++ === 0 ? { total: 1, items: [item] } : refreshed.promise;
    }
    if (method === 'app_market.market_detail') return { version: item.public_latest_version };
    return response(method);
  });
  try {
    await click(document.querySelector('button[data-variant="market"]'));
    await act(async () => {
      find('app-market-resource-actions').querySelector('button[aria-label="Refresh"]').click();
      find('app-market-market-card').click();
    });
    assert.ok(find('app-market-detail-page'));
    await act(async () => refreshed.resolve({ total: 0, items: [] }));
    assert.equal(find('app-market-detail-page'), null);
    assert.ok(find('app-market-tabs'));
    assert.ok(find('app-market-list').textContent.includes(i18next.t('appMarket.noMarketApps')));
  } finally {
    await ctx.close();
  }
});

test('install directory prompt remains usable after opening a market detail', async () => {
  const directory = deferred();
  const ctx = await mount(method => {
    if (method === 'app_market.market_list') {
      return { total: 1, items: [{ asset_id: 'listed', display_name: 'Listed', public_latest_version: '1', tags: [] }] };
    }
    if (method === 'app_market.market_detail') return { version: '1', display_name: 'Listed' };
    if (method === 'app_market.install_dir_default') return directory.promise;
    return response(method);
  });
  try {
    await click(document.querySelector('button[data-variant="market"]'));
    await click(find('app-market-install'));
    await click(find('app-market-market-card'));
    await act(async () => directory.resolve({ path: '/workspace/apps' }));
    assert.ok(find('app-market-detail-page'));
    assert.equal(find('app-market-install-dir-input').value, '/workspace/apps');
    await click(find('app-market-install-dir').querySelector('button'));
    assert.equal(find('app-market-install-dir'), null);
    assert.ok(find('app-market-detail-back'));
  } finally {
    await ctx.close();
  }
});

test('install confirm prompt separates the version from a human-readable package size', async () => {
  const prompts = [];
  let sourceSize = 4096;
  const ctx = await mount((method, params) => {
    if (method === 'app_market.market_list') {
      return {
        total: 1,
        items: [{
          asset_id: 'tiny-app', display_name: 'Tiny App', public_latest_version: '1.2.3', tags: [],
          installed: false, installed_version: '', installed_app_id: '',
        }],
      };
    }
    if (method === 'app_market.market_detail') {
      return { asset_id: params.asset_id, display_name: 'Tiny App', version: '1.2.3', source_size: sourceSize };
    }
    return response(method);
  });
  try {
    window.confirm = (message) => {
      prompts.push(message);
      return false;
    };
    await click(document.querySelector('button[data-variant="market"]'));
    await click(find('app-market-install'));
    assert.match(prompts[prompts.length - 1], /^Install Tiny App v1\.2\.3 4\.0 KB\?/);
    // Some Hub assets report no source size: the prompt must not keep a dangling separator.
    sourceSize = 0;
    await click(find('app-market-install'));
    assert.match(prompts[prompts.length - 1], /^Install Tiny App v1\.2\.3\?/);
  } finally {
    await ctx.close();
  }
});

const response = (method, instances = []) => (method === 'app_market.list' ? { apps: [app] } : { instances });

test('installed app details load AI role cards and show the session policy', async () => {
  const ctx = await mount((method) => {
    if (method !== 'app_market.ai_roles') return response(method);
    return { session_groups: [{
      id: 'council', mode: 'team', leader: 'mayor', session_description: 'New team for each game.',
      roles: [
        { id: 'mayor', name: 'Mayor', description: 'Leads the council', avatar_url: 'data:image/gif;base64,R0lGODlh' },
        { id: 'doctor', name: 'Doctor', description: 'Advises the council' },
      ],
    }] };
  });
  try {
    assert.equal(ctx.calls.some(({ method }) => method === 'app_market.ai_roles'), false);
    await click(card('gomoku'));
    assert.deepEqual(ctx.calls.filter(({ method }) => method === 'app_market.ai_roles'), [
      { method: 'app_market.ai_roles', params: { app_id: 'gomoku' } },
    ]);
    const section = find('app-market-ai-roles');
    assert.match(section.textContent, /AI Roles.*Team session.*New team for each game\./s);
    const group = section.querySelector('[data-testid="app-market-ai-role-group"]');
    assert.equal(group.getAttribute('data-mode'), 'team');
    assert.equal(
      section.querySelector('.app-market-ai-role-grid').style.getPropertyValue('--app-market-ai-role-columns'),
      '2',
    );
    const cards = section.querySelectorAll('[data-testid="app-market-ai-role-card"]');
    assert.equal(cards.length, 2);
    assert.match(cards[0].textContent, /Mayor.*Leader.*Team.*Leads the council/s);
    assert.ok(cards[0].querySelector('img[src^="data:image/gif"]'));
    assert.match(cards[1].textContent, /Doctor.*Team.*Advises the council/s);
    assert.equal(cards[1].querySelector('img'), null);
    await click(find('app-market-detail-back'));
    assert.equal(find('app-market-ai-roles'), null);
  } finally {
    await ctx.close();
  }
});

test('independent AI roles share a grid with notes in their cards while team roles stay grouped', async () => {
  const teamRoles = ['mayor', 'doctor', 'fisherman', 'pilot', 'judge', 'nurse'].map((id, index) => ({
    id, name: `Role ${id}`, description: `Duty ${index}`,
  }));
  const ctx = await mount(method => {
    if (method !== 'app_market.ai_roles') return response(method);
    return { session_groups: [
      { id: 'council', mode: 'team', leader: 'mayor', session_description: 'Shared team.', roles: teamRoles },
      { id: 'assistant', mode: 'agent', session_description: 'Single role.', roles: [
        { id: 'helper', name: 'Helper', description: 'Answers questions' },
      ] },
      { id: 'guide', mode: 'agent', session_description: 'New session each visit.', roles: [
        { id: 'guide', name: 'Guide', description: 'Shows the way' },
      ] },
    ] };
  });
  try {
    await click(card('gomoku'));
    const groups = document.querySelectorAll('[data-testid="app-market-ai-role-group"]');
    assert.equal(groups.length, 1);
    assert.equal(groups[0].getAttribute('data-mode'), 'team');
    const teamGrid = groups[0].querySelector('[data-testid="app-market-ai-team-grid"]');
    assert.equal(teamGrid.style.getPropertyValue('--app-market-ai-role-columns'), '4');
    const teamCards = groups[0].querySelectorAll('[data-testid="app-market-ai-role-card"]');
    assert.equal(teamCards.length, 6);
    for (const item of teamCards) assert.match(item.textContent, /Team/);
    assert.match(teamCards[0].textContent, /Leader/);
    const agentGrid = find('app-market-ai-agent-grid');
    assert.equal(agentGrid.style.getPropertyValue('--app-market-ai-role-columns'), '2');
    assert.doesNotMatch(agentGrid.textContent, /Team|Leader|Single-role session/);
    const agentCards = agentGrid.querySelectorAll('[data-testid="app-market-ai-role-card"]');
    assert.equal(agentCards.length, 2);
    assert.match(agentCards[0].textContent, /Helper.*Answers questions.*Single role\./s);
    assert.match(agentCards[1].textContent, /Guide.*Shows the way.*New session each visit\./s);
  } finally {
    await ctx.close();
  }
});

for (const cleanupCode of ['filesystem_error', 'filesystem_busy']) {
  test(`uninstall removes the card and reports ${cleanupCode}`, async () => {
    const directory = '/workspace/apps/gomoku';
    const error = 'Could not remove application directory';
    const archive = cleanupCode === 'filesystem_busy' ? '/workspace/apps/.app-archive/gomoku-abc123' : '';
    const ctx = await mount(method => {
      if (method === 'app_market.list') return { apps: [{ ...app, source: 'hub' }] };
      if (method === 'app_market.uninstall') {
        return { cleanup_code: cleanupCode, cleanup_warning: error, work_dir: directory, preserved_dir: archive };
      }
      return response(method);
    });
    try {
      await click(card('gomoku'));
      await click(find('app-market-more'));
      await click(find('app-market-delete'));
      assert.equal(find('app-market-detail-page'), null);
      assert.ok(find('app-market-tabs'));
      assert.equal(card('gomoku'), null);
      assert.deepEqual(ctx.calls.find(call => call.method === 'app_market.uninstall')?.params, {
        app_id: 'gomoku', remove_files: true,
      });
      const expectedNotice = archive
        ? i18next.t('appMarket.cleanupWithArchive', {
            message: i18next.t('appMarket.cleanupFilesystemBusy', { directory }), directory: archive,
          })
        : i18next.t('appMarket.cleanupFailed', { directory, error });
      assert.equal(actionNotice().textContent, expectedNotice);
      assert.equal(actionNotice().dataset.variant, 'error');
      assert.equal(actionNotice().parentElement, find('ui-toast-stack-right'));
    } finally {
      await ctx.close();
    }
  });
}

test('cleaning local files names the directory and reports the archive', async () => {
  const archived = '/workspace/apps/.app-archive/gomoku-abc123';
  const ctx = await mount((method) => {
    if (method === 'app_market.delete') return { deleted: true, preserved_dir: archived };
    return response(method);
  });
  try {
    await menuClick('app-market-delete');
    assert.deepEqual(ctx.calls.find(call => call.method === 'app_market.delete')?.params, {
      app_id: 'gomoku', remove_files: true, confirm_work_dir: '/workspace/apps/gomoku',
    });
    assert.equal(
      actionNotice().textContent,
      i18next.t('appMarket.deleteCleanArchived', { directory: archived }),
    );
    assert.equal(actionNotice().dataset.variant, 'success');
  } finally {
    await ctx.close();
  }
});

test('known cleanup refusals use localized explanations', async () => {
  const directory = '/workspace/apps/gomoku';
  const ctx = await mount((method) => {
    if (method === 'app_market.list') return { apps: [{ ...app, source: 'hub' }] };
    if (method === 'app_market.uninstall') {
      return {
        cleanup_warning: 'The application directory is still used by another application',
        cleanup_code: 'directory_in_use',
        work_dir: directory,
      };
    }
    return response(method);
  });
  try {
    await menuClick('app-market-delete');
    assert.equal(
      actionNotice().textContent,
      i18next.t('appMarket.cleanupKeptInUse', { directory }),
    );
    assert.equal(actionNotice().dataset.variant, 'error');
  } finally {
    await ctx.close();
  }
});

test('reinstall from installed app details returns to the plaza', async () => {
  const hubApp = { ...app, source: 'hub', work_dir_missing: true };
  const ctx = await mount(method => {
    if (method === 'app_market.list') return { apps: [hubApp] };
    if (method === 'app_market.delete') return { deleted: true };
    if (method === 'app_market.market_list') {
      return { total: 1, items: [{ asset_id: 'available', display_name: app.name, public_latest_version: '1', tags: [] }] };
    }
    return response(method);
  });
  try {
    await click(card('gomoku'));
    await click(find('app-market-more'));
    await click(find('app-market-reinstall'));
    assert.deepEqual(ctx.calls.find(call => call.method === 'app_market.delete')?.params, { app_id: 'gomoku' });
    assert.equal(find('app-market-detail-page'), null);
    assert.ok(find('app-market-tabs'));
    assert.ok(find('app-market-market-card'));
    assert.equal(actionNotice().textContent, i18next.t('appMarket.reinstallHint'));
  } finally {
    await ctx.close();
  }
});

test('updating from installed app details shows progress and allows cancelling', async () => {
  const statusPolled = deferred();
  const terminal = deferred();
  let statusRequests = 0;
  const ctx = await mount(method => {
    if (method === 'app_market.list') return { apps: [{ ...app, source: 'hub', hub_asset_id: 'listed' }] };
    if (method === 'app_market.market_detail') return { version: '2.0.0' };
    if (method === 'app_market.update') return { job_id: 'update-job' };
    if (method === 'app_market.install_status') {
      if (statusRequests++ === 0) return { state: 'running', phase: 'downloading' };
      statusPolled.resolve();
      return terminal.promise;
    }
    if (method === 'app_market.install_cancel') terminal.resolve({ state: 'cancelled', phase: 'downloading' });
    return response(method);
  });
  try {
    await click(card('gomoku'));
    await click(find('app-market-more'));
    await click(find('app-market-update'));
    assert.ok(find('app-market-detail-page'));
    assert.equal(find('app-market-tabs'), null);
    assert.match(find('app-market-install-progress').textContent, /Downloading/);
    await click(find('app-market-install-cancel'));
    assert.deepEqual(ctx.calls.find(call => call.method === 'app_market.install_cancel')?.params, { job_id: 'update-job' });
    await act(async () => statusPolled.promise);
    assert.equal(find('app-market-install-progress'), null);
    assert.ok(find('app-market-detail-back'));
  } finally {
    terminal.resolve({ state: 'cancelled', phase: 'downloading' });
    if (statusRequests) await act(async () => statusPolled.promise);
    await ctx.close();
  }
});

// 应用市场已是侧边栏一级入口（AppMarketPage）：列表页保留「应用」页头与副标题，
// 进入详情或运行中的应用页面时收起页头，避免与内容自带标题重复。
test('app market page places refresh in the header and search next to the tabs', async () => {
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
    assert.equal(find('app-market-page-subtitle').textContent, i18next.t('appMarket.subtitle'));
    const header = find('common-page-header');
    const toolbar = find('app-market-toolbar');
    const actions = find('app-market-toolbar-actions');
    assert.ok(header.contains(find('app-market-refresh')));
    assert.deepEqual([...toolbar.children], [find('app-market-tabs'), actions]);
    assert.ok(actions.contains(find('app-market-search-input')));
    await tick();
    assert.ok(card('gomoku'), 'app cards render inside the standalone page');
  } finally {
    await act(async () => root.unmount());
    webClient.request = previous;
    globalThis.setInterval = previousInterval;
  }
});

test('opening app details hides page chrome and keeps run and more actions', async () => {
  const previous = webClient.request;
  const previousInterval = globalThis.setInterval;
  globalThis.setInterval = () => 987654;
  webClient.request = async (method) => response(method);
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(React.createElement(AppMarketPage)));
    await tick();
    assert.ok(find('app-market-page-title'));
    assert.ok(find('app-market-tabs'));
    await click(card('gomoku'));
    await tick();
    assert.equal(find('app-market-page-title'), null);
    assert.equal(find('app-market-page-subtitle'), null);
    assert.equal(find('app-market-tabs'), null);
    assert.equal(find('app-market-resource-actions'), null);
    assert.ok(find('app-market-detail-page'));
    assert.ok(find('app-market-toggle'));
    assert.ok(find('app-market-more'));
    await click(find('app-market-detail-back'));
    await tick();
    assert.ok(find('app-market-page-title'));
    assert.ok(find('app-market-tabs'));
  } finally {
    await act(async () => root.unmount());
    webClient.request = previous;
    globalThis.setInterval = previousInterval;
  }
});

test('opening the running app page hides the page title until My Apps is selected', async () => {
  delete window.jiuwenDesktop;
  const previous = webClient.request;
  const previousInterval = globalThis.setInterval;
  globalThis.setInterval = () => 987654;
  webClient.request = async (method) => response(method, [running]);
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(React.createElement(AppMarketPage)));
    await tick();
    await tick();
    assert.ok(find('app-market-page-title'));
    await click(card('gomoku'));
    await tick();
    assert.ok(find('app-market-browser-pane'));
    assert.equal(find('app-market-page-title'), null);
    assert.equal(find('app-market-page-subtitle'), null);
    await click(find('app-market-tab-my-apps'));
    await tick();
    assert.equal(find('app-market-browser-pane'), null);
    assert.ok(find('app-market-page-title'));
  } finally {
    await act(async () => root.unmount());
    webClient.request = previous;
    globalThis.setInterval = previousInterval;
  }
});

test('installed app cards show the summary and details show the product introduction and HTTPS source', async () => {
  const description = 'Local integration.\n\n> Product introduction.\n\n[Official source](https://example.com/product)';
  const ctx = await mount(method => method === 'app_market.list'
    ? { apps: [{ ...app, description }] } : response(method));
  try {
    assert.match(card('gomoku').textContent, /Local integration/);
    assert.doesNotMatch(card('gomoku').textContent, /Product introduction|Official source/);

    await click(card('gomoku'));
    const introduction = find('app-market-detail-introduction');
    assert.equal(introduction.querySelector('blockquote').textContent.trim(), 'Product introduction.');
    assert.equal(introduction.querySelector('a').href, 'https://example.com/product');
    assert.equal(introduction.querySelector('a').target, '_blank');
  } finally { await ctx.close(); }
});

test('running cards switch the pane and leave app details in the menu', async () => {
  delete window.jiuwenDesktop;
  const ctx = await mount(method => method === 'app_market.list'
    ? { apps: [app, secondApp] } : response(method, [running, secondInstance]));
  try {
    await click(card('gomoku'));
    const page = find('app-market-web-view');
    assert.equal(find('app-market-browser-pane').dataset.variant, 'gomoku');
    assert.equal(find('app-market-detail-page'), null);

    await click(card('second'));
    assert.equal(find('app-market-browser-pane').dataset.variant, 'second');
    assert.equal(find('app-market-detail-page'), null);
    await click(card('gomoku'));
    assert.equal(find('app-market-browser-pane').dataset.variant, 'gomoku');
    assert.strictEqual(document.querySelector('[data-testid="app-market-web-view"][data-variant="gomoku"]'), page);

    await menuClick('app-market-view-details', 'gomoku');
    assert.ok(find('app-market-detail-page'));
    assert.equal(ctx.calls.some(({ method }) => ['app_market.start', 'app_market.stop'].includes(method)), false);
  } finally { await ctx.close(); }
});

test('a failed desktop open keeps the app details available for retry', async () => {
  const firstOpen = deferred();
  let opens = 0;
  window.jiuwenDesktop = {
    isElectron: true,
    openMarketApp: async () => {
      opens++;
      return opens === 1 ? firstOpen.promise : true;
    },
    closeMarketApp: async () => true,
  };
  const ctx = await mount(method => response(method, [running]));
  try {
    await menuClick('app-market-view-details');
    const details = find('app-market-detail-page');
    assert.ok(details);
    await click(find('app-market-more'));
    await click(find('app-market-open'));
    assert.strictEqual(find('app-market-detail-page'), details);

    await act(async () => firstOpen.resolve(false));
    assert.match(actionNotice().textContent, /Failed to open/i);
    assert.strictEqual(find('app-market-detail-page'), details);

    await click(find('app-market-more'));
    await click(find('app-market-open'));
    assert.equal(opens, 2);
    assert.equal(find('app-market-detail-page'), null);
    assert.ok(card('gomoku'));
  } finally { await ctx.close(); delete window.jiuwenDesktop; }
});

test('pin action persists through the app market RPC and refreshes the list', async () => {
  let pinned = false;
  const ctx = await mount((method, params) => {
    if (method === 'app_market.list') {
      return { apps: pinned ? [{ ...app, pinned: true }, secondApp] : [app, secondApp] };
    }
    if (method === 'app_market.pin') {
      pinned = params.pinned;
      return { app_id: params.app_id, pinned: params.pinned };
    }
    return response(method);
  });
  try {
    await menuClick('app-market-pin');
    assert.deepEqual(
      ctx.calls.find(({ method }) => method === 'app_market.pin')?.params,
      { app_id: 'gomoku', pinned: true },
    );
    assert.equal(
      actionNotice().textContent,
      i18next.t('appMarket.pinSuccess', { name: 'Gomoku' }),
    );
    assert.equal(ctx.calls.filter(({ method }) => method === 'app_market.list').length, 2);
  } finally {
    await ctx.close();
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
      await menuClick('app-market-open', 'gomoku');
      const secondCard = card('second');
      await menuClick('app-market-open', 'second');
      assert.deepEqual([...windows], [[app.app_id, running.url], [secondApp.app_id, secondInstance.url]]);
      window.dispatchEvent(new CustomEvent('jiuwenclaw-desktop-browser-tab-change', {
        detail: { sessionId: '', flags: { requested: true, closed: true } },
      }));
      await tick();
      assert.equal(ctx.calls.some(({ method }) => method === 'app_market.stop'), false);
      await click(secondCard.querySelector(`[data-testid="app-market-${action === 'stop' ? 'toggle' : 'more'}"]`));
      if (action === 'delete') {
        await menuClick('app-market-delete', 'second');
      }
      assert.deepEqual(closed, ['second']);
      assert.deepEqual([...windows], [[app.app_id, running.url]]);
      const requests = ctx.calls.filter(({ method }) => ['app_market.stop', 'app_market.delete'].includes(method));
      assert.deepEqual(requests, [{
        method: `app_market.${action}`,
        params: action === 'stop' ? { instance_id: 'second-active' } : {
          app_id: 'second', remove_files: true, confirm_work_dir: '/workspace/apps/second',
        },
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
    assert.ok(find('app-market-tabs'));
    assert.equal(document.querySelector('h2'), null);
    assert.match(document.body.textContent, /Gomoku/);
    assert.match(document.body.textContent, /v1.2.3/);
    assert.match(document.body.textContent, /Play five in a row/);
    assert.equal(find('app-market-card-avatar').getAttribute('src'), '/gomoku.png');
    assert.equal(find('app-market-status'), null);
    assert.equal(await menuText('app-market-delete'), 'Delete and clean files');
    assert.equal(find('app-market-status-text'), null);
    assert.ok(find('app-market-pane-hint'));
  } finally {
    await ctx.close();
  }
});

test('App Plaza appears before My Apps, which opens by default', async () => {
  const ctx = await mount((method) => response(method));
  try {
    const tabs = [...document.querySelectorAll('button[data-testid^="app-market-tab-"]')]
      .map(button => [button.dataset.testid, button.textContent.trim()]);
    assert.deepEqual(tabs, [['app-market-tab-plaza', 'App Plaza'], ['app-market-tab-my-apps', 'My Apps']]);
    assert.ok(card('gomoku'));
    assert.equal(ctx.calls.some(({ method }) => method === 'app_market.market_list'), false);
  } finally {
    await ctx.close();
  }
});

const typeSearch = async (value) => {
  const input = find('app-market-search-input');
  assert.ok(input, 'search input is rendered');
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, value);
    input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  });
};
const wait = async (ms) => {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, ms)); });
};

test('My Apps shows both sources and searches both by name and description', async () => {
  const hubApp = { ...app, source: 'hub', description: 'Play five in a row' };
  const localApp = { ...secondApp, source: 'local', description: 'A local sandbox app' };
  const ctx = await mount((method) => {
    if (method === 'app_market.list') return { apps: [hubApp, localApp] };
    return response(method);
  });
  try {
    assert.ok(card('gomoku'));
    assert.ok(card('second'));
    assert.equal(find('app-market-source-filter'), null);
    await typeSearch('five in a row');
    assert.ok(card('gomoku'));
    assert.equal(card('second'), null);
    await typeSearch('sec');
    assert.equal(card('gomoku'), null);
    assert.ok(card('second'));
    await typeSearch('nothing matches');
    assert.match(find('app-market-list').textContent, new RegExp(i18next.t('common.noMatches')));
    await click(find('app-market-search-clear'));
    assert.ok(card('gomoku'));
    assert.ok(card('second'));
    assert.equal(ctx.calls.some(({ method }) => method === 'app_market.market_list'), false,
      'local filtering never queries the plaza');
  } finally {
    await ctx.close();
  }
});

test('App Plaza search sends one debounced query and returns to the first page', async () => {
  const ctx = await mount((method, params) => {
    if (method === 'app_market.market_list') {
      const id = `app-${params.query || 'all'}-${params.page}`;
      return { total: 51, items: [{ asset_id: id, display_name: id, public_latest_version: '1', tags: [] }] };
    }
    return response(method);
  });
  try {
    await click(document.querySelector('button[data-variant="market"]'));
    await tick();
    await click(document.querySelector(`button[aria-label="${i18next.t('common.pagination.next')}"]`));
    await tick();
    const before = ctx.calls.filter(({ method }) => method === 'app_market.market_list');
    assert.equal(before.at(-1).params.page, 2);

    await typeSearch('g');
    await typeSearch('ga');
    await typeSearch('game');
    await wait(350);

    const after = ctx.calls.filter(({ method }) => method === 'app_market.market_list');
    assert.equal(after.length, before.length + 1, 'debounce collapses keystrokes into one request');
    assert.deepEqual(
      { query: after.at(-1).params.query, page: after.at(-1).params.page },
      { query: 'game', page: 1 },
    );
  } finally {
    await ctx.close();
  }
});

test('app cards keep only run and more visible until the menu opens', async () => {
  const ctx = await mount((method) => response(method, [running]));
  try {
    const cardElement = card('gomoku');
    assert.ok(cardElement.querySelector('[data-testid="app-market-toggle"]'));
    assert.ok(cardElement.querySelector('[data-testid="app-market-more"]'));
    assert.equal(cardElement.querySelector('[data-testid="app-market-open"]'), null);
    assert.equal(cardElement.querySelector('[data-testid="app-market-delete"]'), null);
    await more();
    assert.ok(find('app-market-open'));
    assert.ok(find('app-market-delete'));
  } finally {
    await ctx.close();
  }
});

test('app cards open a detail page with the app directory and return to My Apps', async () => {
  const ctx = await mount((method) => {
    if (method === 'app_market.list') {
      return { apps: [{ ...app, entry_path: 'C:\\workspace\\gomoku\\index.html' }] };
    }
    return response(method);
  });
  try {
    assert.equal(find('app-market-detail-page'), null);
    await click(card('gomoku'));
    const page = find('app-market-detail-page');
    assert.ok(page);
    assert.match(page.textContent, /Local/);
    assert.match(page.textContent, /C:\\workspace\\gomoku/);
    assert.doesNotMatch(page.textContent, /App ID|Entry|artifact digest|Package digest/i);
    assert.equal(card('gomoku'), null);
    assert.equal(find('app-market-detail-back').textContent.trim(), 'Back to My Apps');
    await click(find('app-market-detail-back'));
    assert.ok(card('gomoku'));
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
    assert.ok(await menuActionExists('app-market-open'));
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
    assert.ok(await menuActionExists('app-market-open'));
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
    await menuClick('app-market-open', 'second');
    assert.equal(find('app-market-browser-pane').dataset.variant, 'second');
    await act(async () => pending.resolve(running));
    await tick();
    assert.equal(find('app-market-browser-pane').dataset.variant, 'second');
    assert.equal(document.querySelector('[data-testid="app-market-web-view"][data-variant="gomoku"]'), null);
    assert.equal(card('gomoku').querySelector('[data-testid="app-market-status"]').ariaLabel, 'Running');
    assert.ok(await menuActionExists('app-market-open', 'gomoku'));
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
    assert.equal(find('app-market-status'), null);
    assert.equal(find('app-market-toggle').getAttribute('aria-label'), 'Start');
  } finally {
    await ctx.close();
  }
});

test('start failure on the detail page displays the server error as a toast', async () => {
  const ctx = await mount((method) => {
    if (method === 'app_market.start') throw new Error('Port 8080 is occupied');
    return response(method);
  });
  try {
    await click(card('gomoku'));
    await click(find('app-market-toggle'));
    assert.match(actionNotice().textContent, /Port 8080 is occupied/);
    assert.equal(actionNotice().parentElement, find('ui-toast-stack-right'));
    assert.ok(find('app-market-detail-back'));
    assert.equal(find('app-market-status'), null);
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
    assert.match(actionNotice().textContent, /Connection lost/);
  } finally {
    await ctx.close();
  }
});

test('Web polling removes only the stopped instance, even after leaving the market', async () => {
  let instances = [running, secondInstance];
  const ctx = await mount(method => method === 'app_market.list' ? { apps: [app, secondApp] } : { instances });
  try {
    const frame = id => document.querySelector(`[data-testid="app-market-web-view"][data-variant="${id}"]`);
    await menuClick('app-market-open', 'gomoku');
    const firstPage = frame('gomoku');
    await menuClick('app-market-open', 'second');
    const secondPage = frame('second');
    assert.equal(firstPage.src, running.url);
    assert.equal(secondPage.src, secondInstance.web_url);
    assert.equal(find('app-market-browser-pane').dataset.variant, 'second');
    instances = [{ ...running, state: 'stopped' }, secondInstance];
    await ctx.poll();
    assert.equal(card('gomoku').querySelector('[data-testid="app-market-status"]'), null);
    assert.equal(firstPage.isConnected, false);
    assert.equal(frame('second'), secondPage);
    assert.equal(find('app-market-browser-pane').dataset.variant, 'second');

    instances = [{ ...secondInstance, instance_id: 'second-restarted' }];
    await ctx.poll();
    assert.equal(secondPage.isConnected, false);
    await menuClick('app-market-open', 'second');
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
    assert.equal(await menuText('app-market-open'), 'Open / restore');
    assert.equal(find('app-market-toggle').getAttribute('aria-label'), 'Stop app');
    assert.match(find('app-market-pane-hint').textContent, /Closing a window keeps the app running/);
    await menuClick('app-market-open');
    assert.equal(navigations.length, 2);
    assert.equal(ctx.calls.filter(({ method }) => method === 'app_market.start').length, 1);
  } finally {
    delete window.jiuwenDesktop;
    await ctx.close();
  }
});

test('Electron keeps an already open pane visible and can hide, close and reopen it', async () => {
  const opens = [];
  const closes = [];
  window.jiuwenDesktop = {
    isElectron: true,
    openMarketApp: async (...args) => { opens.push(args); return true; },
    setMarketAppViewport: async () => true,
    closeMarketApp: async id => { closes.push(id); return true; },
  };
  const ctx = await mount(method => response(method, [running]));
  try {
    await menuClick('app-market-open');
    const pane = find('app-market-browser-pane');
    assert.ok(pane);
    assert.deepEqual(opens, [[app.app_id, running.url, app.name, true]]);
    await menuClick('app-market-open');
    assert.equal(find('app-market-browser-pane'), pane);
    assert.equal(opens.length, 1, 'reopening a mounted view must not hide it via Electron IPC');
    await click(find('app-market-browser-expand'));
    assert.ok(find('app-market-panel').classList.contains('app-market-layout-expanded'));
    await click(find('app-market-browser-expand'));
    assert.ok(!find('app-market-panel').classList.contains('app-market-layout-expanded'));
    await click(find('app-market-browser-hide'));
    assert.equal(find('app-market-browser-pane'), null);
    assert.deepEqual(closes, []);
    await menuClick('app-market-open');
    assert.ok(find('app-market-browser-pane'));
    assert.equal(opens.length, 2);
    await click(find('app-market-browser-close'));
    assert.equal(find('app-market-browser-pane'), null);
    assert.deepEqual(closes, [app.app_id]);
    await menuClick('app-market-open');
    assert.ok(find('app-market-browser-pane'));
    assert.equal(opens.length, 3);
    assert.equal(ctx.calls.some(({ method }) => method === 'app_market.stop'), false);
  } finally {
    await ctx.close();
    delete window.jiuwenDesktop;
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
    await menuClick('app-market-open');
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
    await menuClick('app-market-open');
    assert.equal(find('app-market-web-view'), frame);
    await ctx.close(); ctx = null;
    assert.equal(frame.isConnected, true);
    assert.equal(frame.style.visibility, 'hidden');
    ctx = await mount(handler);
    await menuClick('app-market-open');
    assert.equal(find('app-market-web-view'), frame);
    await click(find('app-market-browser-close'));
    assert.equal(frame.isConnected, false);
    assert.equal(find('app-market-status-text').textContent, 'Running');
    await menuClick('app-market-open');
    assert.notEqual(find('app-market-web-view'), frame);
    await click(find('app-market-toggle'));
    assert.equal(find('app-market-web-view'), null);
    assert.equal(find('app-market-browser-pane'), null);
    assert.equal(find('app-market-status-text'), null);
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
    assert.equal(await menuText('app-market-open'), 'Open / restore');
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
    await menuClick('app-market-open');
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
    await menuClick('app-market-open');
    assert.equal(ctx.opened.length, 0);
    assert.equal(find('app-market-browser-pane'), null);
    assert.equal(find('app-market-web-view'), null);
    assert.match(actionNotice().textContent, /Restart with the updated desktop app/);
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
        assert.equal(find('app-market-status-text'), null);
      } else {
        await act(async () => stopped.reject(new Error('Stop rejected')));
        await tick();
        assert.deepEqual(closed, []);
        assert.equal(find('app-market-status-text').textContent, 'Running');
        assert.match(actionNotice().textContent, /Stop rejected/);
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
    assert.equal(find('app-market-status-text'), null);
    assert.match(actionNotice().textContent, /app stopped.*window could not close/i);
    assert.doesNotMatch(actionNotice().textContent, /Failed to stop/);
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
    await more();
    assert.equal(find('app-market-delete').disabled, false);
    await closeMore();
    assert.ok(await menuActionExists('app-market-open'));
    await act(async () => pageLoad.reject(new Error('Page load failed')));
    assert.match(actionNotice().textContent, /Page load failed/);
    assert.equal(find('app-market-status').getAttribute('aria-label'), 'Running');
    assert.equal(ctx.opened.length, 0);
    instances = [{ ...running, state: 'stopped' }];
    await click(find('app-market-toggle'));
    assert.equal(ctx.calls.filter(({ method }) => method === 'app_market.stop').length, 1);
    assert.equal(find('app-market-status'), null);
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
    await menuClick('app-market-open');
    assert.match(actionNotice().textContent, /Entry unavailable/);
    assert.equal(ctx.opened.length, 0);
    assert.equal(find('app-market-status').getAttribute('aria-label'), 'Running');
  } finally {
    delete window.jiuwenDesktop;
    await ctx.close();
  }
});

test('delete keeps app details on cancellation and returns to the list on success', async () => {
  const ctx = await mount((method) => response(method));
  try {
    await click(card('gomoku'));
    window.confirm = () => false;
    await click(find('app-market-more'));
    await click(find('app-market-delete'));
    assert.equal(ctx.calls.filter(({ method }) => method === 'app_market.delete').length, 0);
    assert.ok(find('app-market-detail-page'));
    window.confirm = () => true;
    await click(find('app-market-more'));
    await click(find('app-market-delete'));
    assert.equal(ctx.calls.filter(({ method }) => method === 'app_market.delete').length, 1);
    assert.equal(find('app-market-detail-page'), null);
    assert.ok(find('app-market-tabs'));
    assert.equal(find('app-market-card'), null);
    assert.deepEqual(ctx.calls.find(call => call.method === 'app_market.delete')?.params, {
      app_id: 'gomoku', remove_files: true, confirm_work_dir: '/workspace/apps/gomoku',
    });
    const notice = actionNotice();
    assert.equal(notice.textContent, i18next.t('appMarket.deleteCleanSuccess'));
    assert.equal(notice.parentElement, find('ui-toast-stack-right'));
    assert.equal(find('app-market-list').contains(notice), false);
  } finally {
    await ctx.close();
  }
});

test('remove keeps app files and unregisters without cleanup', async () => {
  const ctx = await mount((method) => response(method));
  try {
    await menuClick('app-market-remove');
    assert.deepEqual(ctx.calls.find(call => call.method === 'app_market.delete')?.params, {
      app_id: 'gomoku', remove_files: false,
    });
    assert.equal(find('app-market-card'), null);
    assert.equal(actionNotice().textContent, i18next.t('appMarket.removeKeepSuccess'));
  } finally {
    await ctx.close();
  }
});

test('remove for a Hub app uninstalls but keeps files', async () => {
  const ctx = await mount((method) => {
    if (method === 'app_market.list') return { apps: [{ ...app, source: 'hub' }] };
    return response(method);
  });
  try {
    await menuClick('app-market-remove');
    assert.deepEqual(ctx.calls.find(call => call.method === 'app_market.uninstall')?.params, {
      app_id: 'gomoku', remove_files: false,
    });
    assert.equal(find('app-market-card'), null);
    assert.equal(actionNotice().textContent, i18next.t('appMarket.removeKeepSuccess'));
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
    assert.match(actionNotice().textContent, /Entry exited before startup completed/);
    assert.equal(find('app-market-status'), null);
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
        await menuClick('app-market-edit');
        await tick();
        assert.deepEqual(targets, [{ appId: app.app_id, name: app.name, instanceId: running.instance_id,
          entryPath: editable.entry_path, sourceSession: editable.source_session,
          version: app.version, description: app.description }]);
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
    await menuClick('app-market-edit');
    assert.deepEqual(targets, []);
    assert.match(actionNotice().textContent, /Startup failed/);
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
    await menuClick('app-market-edit');
    const frame = find('app-market-web-view');
    useSessionStore.getState().ensureRuntime('new');
    useSessionStore.getState().setAppMarketPreview('new', target);
    await ctx.unmount();
    root = createRoot(document.getElementById('root'));
    await act(async () => root.render(React.createElement(AppMarketSessionPreview, { sessionId: 'new', preview: target })));
    assert.equal(find('app-market-web-view'), frame);
    assert.ok(find('app-market-browser-toolbar'));
    assert.equal(find('app-market-resources'), null);
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
    await menuClick('app-market-edit');
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
      await menuClick('app-market-edit');
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
      await menuClick('app-market-open');
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
      await menuClick('app-market-open');
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
  dependencies = { appEditTokensRef: { current: new Map() }, generateUuidV4: randomUUID, isEnterprise: () => false, ...dependencies };
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
        handleRestoreSession: async (id, mode, session, seedUserMessage, options) => {
          assert.equal(seedUserMessage, undefined);
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
        await menuClick('app-market-edit');
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
      const ctx = await mount((method, params) => {
        if (method === 'app_market.editor_app') {
          assert.equal(params.editor_session_id, 'editor');
          return { app };
        }
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
        // The editor header is a crowded chat header, so it must not mount the
        // server resource monitor that the market page uses.
        assert.equal(find('app-market-resources'), null);
        assert.match(find('app-market-editor-actions').textContent, /Open app preview/);
        await click(find('app-market-editor-open-preview'));
        assert.deepEqual(previews[0], { appId: app.app_id, name: app.name, instanceId: running.instance_id });
        await act(async () => root.unmount());
        await render();
        assert.equal(find('app-market-resources'), null);
        assert.match(find('app-market-editor-actions').textContent, /Open app preview/);
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
  const ctx = await mount((method, params) => {
    if (method === 'app_market.editor_app') {
      return { app: params.editor_session_id === 'editor' ? app : null };
    }
    return method === 'app_market.list' ? { apps: [app] } : pending.promise;
  });
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
function firstEditHarness(request, language = 'en') {
  const sidRef = { current: 'new' };
  const restores = [];
  const requests = { current: new Map() };
  const reveals = [];
  const modeChanges = [];
  const projectLoads = [];
  const workspace = {
    workMode: 'code',
    projects: [{ project_id: 'game-project' }],
    expandedProjectIds: { 'game-project': false },
    setWorkMode: async (mode) => {
      modeChanges.push(mode);
      workspace.workMode = mode;
      workspace.expandedProjectIds = {};
    },
    toggleProjectExpanded: (id) => {
      workspace.expandedProjectIds[id] = !workspace.expandedProjectIds[id];
    },
    loadProjectSessions: async (id) => projectLoads.push(id),
  };
  const handler = editHandler({
    pendingAppEditsRef: requests, creatingSessionRef: { current: false }, sessionIdRef: sidRef,
    request, useChatStore, toast: { open: () => {} },
    useSessionStore: { getState: () => ({ getModelSelectionPayload: () => ({}), setAppMarketPreview: () => {} }) },
    t: i18next.getFixedT(language),
    useWorkspaceStore: { getState: () => workspace },
    setAppEditSessionToReveal: (id) => reveals.push(id),
    closeSidebarPeek: () => {},
    handleRestoreSession: async (id, mode, session, seedUserMessage, options) => {
      if (!options.canActivate()) return;
      restores.push(id); sidRef.current = id;
      useChatStore.getState().ensureRuntime(id);
    },
    setHeartbeatPanelOpen: () => {}, setToolPanelHidden: () => {},
    setSingleAgentPanelActiveTab: () => {}, setSingleAgentPanelExpanded: () => {},
  });
  return { handler, restores, requests, reveals, workspace, modeChanges, projectLoads };
}
const editTarget = { appId: 'gomoku', name: 'Gomoku', entryPath: '/game/index.html', instanceId: 'active' };
const editSession = {
  session_id: 'first-editor', mode: 'agent', message_count: 0, project_id: 'game-project', work_mode: 'code',
};

for (const desktop of [false, true]) {
  test(`${desktop ? 'Electron' : 'Web'} repeated first-edit clicks share one request and keep the source prompt`, async () => {
    useChatStore.getState().removeRuntime(editSession.session_id);
    const pending = deferred();
    let calls = 0;
    const h = firstEditHarness(() => { calls++; return pending.promise; }, desktop ? 'zh' : 'en');
    if (desktop) window.jiuwenDesktop = { isElectron: true, openMarketApp: async () => true, closeMarketApp: async () => true, setMarketAppViewport: async () => true };
    const ctx = await mount(method => method === 'app_market.list'
      ? { apps: [{ ...app, entry_path: editTarget.entryPath }] } : response(method, [running]), { onEdit: h.handler });
    try {
      await menuClick('app-market-edit');
      await menuClick('app-market-edit');
      assert.equal(calls, 1);
      await act(async () => pending.resolve({ session: editSession, created: true }));
      await tick();
      assert.deepEqual(h.restores, [editSession.session_id]);
      const draft = useChatStore.getState().getRuntime(editSession.session_id).inputValue;
      assert.ok(draft.includes(editTarget.entryPath));
      assert.match(draft, /workswarm-app-builder/);
      assert.match(draft, desktop ? /应用开发技能/ : /WorkSwarm built-in capabilities catalog/);
      assert.equal(h.workspace.expandedProjectIds[editSession.project_id], true);
      assert.deepEqual(h.reveals, [editSession.session_id]);
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
  assert.equal(h.workspace.expandedProjectIds[editSession.project_id], false);
  assert.deepEqual(h.reveals, []);
  await h.handler(editTarget, () => true);
  assert.deepEqual(h.restores, [editSession.session_id]);
  assert.match(useChatStore.getState().getRuntime(editSession.session_id).inputValue, /\/game\/index\.html/);
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
  const tokens = [];
  const h = firstEditHarness((_, params) => {
    tokens.push(params.edit_token);
    return tokens.length === 1 ? failure.promise : Promise.resolve({ session: editSession, created: true });
  });
  h.workspace.workMode = 'work';
  const first = h.handler(editTarget, () => false);
  const second = h.handler(editTarget, () => true);
  failure.reject(new Error('offline'));
  const results = await Promise.allSettled([first, second]);
  assert.deepEqual(results.map(result => result.status), ['rejected', 'rejected']);
  assert.equal(h.requests.current.size, 0);
  await h.handler(editTarget, () => true);
  assert.equal(tokens.length, 2);
  assert.ok(tokens[0]);
  assert.equal(tokens[1], tokens[0]);
  assert.deepEqual(h.restores, [editSession.session_id]);
  assert.deepEqual(h.modeChanges, ['code']);
  assert.equal(h.workspace.expandedProjectIds[editSession.project_id], true);
  await h.handler(editTarget, () => true);
  assert.notEqual(tokens[2], tokens[0]);
  assert.equal(h.workspace.expandedProjectIds[editSession.project_id], true);
  assert.deepEqual(h.projectLoads, [editSession.project_id]);
  useChatStore.getState().removeRuntime(editSession.session_id);
});


test('Web market shows machine totals and running cards show only app memory', async () => {
  delete window.jiuwenDesktop;
  const ctx = await mount(method => method === 'app_market.resources' ? {
    apps: [{ appId: app.app_id, cpuPercent: 2.5, memoryBytes: 10485760 }],
    system: { cpuPercent: 65, memoryPercent: 70, sampledAt: Date.now() }, sampledAt: Date.now(),
  } : response(method, [running]));
  try {
    assert.match(find('app-market-resources').textContent, /This computer \(total\) CPU 65.0% \u00b7 RAM 70.0%/);
    const tags = card('gomoku').querySelectorAll('[data-testid="entity-header-tag"]');
    assert.deepEqual([...tags].map(tag => tag.textContent), ['v1.2.3', 'RAM 10 MiB']);
    await menuClick('app-market-open');
    const metrics = document.querySelectorAll('[data-testid="app-market-resources"]');
    assert.equal(metrics.length, 1);
    assert.doesNotMatch(find('app-market-browser-toolbar').textContent, /Service CPU|Browser page usage unavailable/);
    assert.equal(ctx.calls.filter(call => call.method === 'app_market.resources').length, 1);
  } finally { await ctx.close(); }
});

test('desktop cards show service memory before a renderer exists and totals stay native', async () => {
  let reads = 0;
  window.jiuwenDesktop = { isElectron: true, getMarketAppResources: async () => {
    reads++;
    return { apps: [], system: { cpuPercent: 40, memoryPercent: 50, sampledAt: Date.now() }, sampledAt: Date.now() };
  } };
  const ctx = await mount(method => method === 'app_market.resources' ? {
    apps: [{ appId: app.app_id, cpuPercent: null, memoryBytes: 25165824 }],
    system: { cpuPercent: 65, memoryPercent: 70, sampledAt: Date.now() }, sampledAt: Date.now(),
  } : response(method, [running]));
  try {
    assert.match(find('app-market-resources').textContent, /This computer \(total\) CPU 40.0% \u00b7 RAM 50.0%/);
    const tags = card('gomoku').querySelectorAll('[data-testid="entity-header-tag"]');
    assert.deepEqual([...tags].map(tag => tag.textContent), ['v1.2.3', 'RAM 24 MiB']);
    assert.equal(find('app-market-resources-note'), null);
    assert.equal(reads, 1);
    assert.equal(ctx.calls.filter(call => call.method === 'app_market.resources').length, 1);
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


test('desktop native totals and warnings keep updating while service memory is pending', async () => {
  const originalTimeout = globalThis.setTimeout;
  const service = deferred();
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
    ctx = await mount(method => method === 'app_market.resources' ? service.promise : response(method, [running]));
    assert.equal(find('app-market-system-cpu-percent').textContent, '90.0%');
    assert.doesNotMatch(card('gomoku').textContent, /RAM/);
    assert.equal(find('app-market-resources-warning'), null);
    await act(async () => poll());
    assert.equal(find('app-market-resources-warning'), null);
    await act(async () => poll());
    assert.match(find('app-market-resources-warning').textContent, /High system usage/);
    usage = 70;
    await act(async () => poll());
    assert.equal(find('app-market-resources-warning'), null);
    assert.equal(ctx.calls.filter(call => call.method === 'app_market.resources').length, 1);

    await act(async () => service.resolve({
      apps: [{ appId: app.app_id, cpuPercent: null, memoryBytes: 25165824 }], sampledAt: Date.now(),
    }));
    assert.match(card('gomoku').textContent, /RAM 24 MiB/);
    assert.equal(find('app-market-system-cpu-percent').textContent, '70.0%');
  } finally {
    if (ctx) await ctx.close();
    service.resolve(null);
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
  let ctx;
  try {
    ctx = await mount(method => method === 'app_market.resources'
      ? { apps: [], system, sampledAt: system.sampledAt } : response(method));
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
  }
});

test('My Apps and the market tab collapse the running app pane without stopping it', async () => {
  delete window.jiuwenDesktop;
  const ctx = await mount(method => {
    if (method === 'app_market.market_list') return { page: 1, page_size: 50, total: 0, items: [] };
    return response(method, [running]);
  });
  try {
    await menuClick('app-market-open');
    assert.ok(find('app-market-browser-pane'));
    const frame = find('app-market-web-view');
    // Re-selecting the active tab goes home and hides the page; it is not stopped.
    await click(find('app-market-tab-my-apps'));
    await tick();
    assert.equal(find('app-market-browser-pane'), null);
    assert.equal(frame.isConnected, true);
    assert.equal(frame.style.visibility, 'hidden');
    assert.ok(card('gomoku'));

    await menuClick('app-market-open');
    assert.ok(find('app-market-browser-pane'));
    await click(find('app-market-tab-plaza'));
    assert.equal(find('app-market-browser-pane'), null);
    assert.equal(ctx.calls.some(call => call.method === 'app_market.stop'), false);
  } finally {
    await ctx.close();
  }
});

test('switching tabs while an app is starting prevents the late preview', async () => {
  const pending = deferred();
  const ctx = await mount(method => {
    if (method === 'app_market.market_list') return { page: 1, page_size: 50, total: 0, items: [] };
    if (method === 'app_market.start') return pending.promise;
    return response(method, []);
  });
  try {
    await menuClick('app-market-toggle');
    await click(document.querySelector('[data-variant="market"]'));
    await act(async () => pending.resolve(running));
    assert.equal(find('app-market-browser-pane'), null);
    assert.equal(ctx.calls.some(call => call.method === 'app_market.stop'), false);
  } finally {
    await ctx.close();
  }
});

test('running app cards sort first by start time', async () => {
  const firstRunning = { ...running, started_at: '2026-10-23T10:00:00Z' };
  const secondRunning = { ...secondInstance, state: 'running', started_at: '2026-10-23T11:00:00Z' };
  const ctx = await mount(method => {
    if (method === 'app_market.list') return { apps: [app, secondApp] };
    return { instances: [firstRunning, secondRunning] };
  });
  try {
    await tick();
    const variants = [...document.querySelectorAll('[data-testid="app-market-card"]')]
      .map(element => element.getAttribute('data-variant'));
    assert.deepEqual(variants, ['second', 'gomoku']);
  } finally {
    await ctx.close();
  }
});

test('stop reports through a right-side toast outside the card list and dismisses automatically', async () => {
  let instances = [running];
  const ctx = await mount(method => {
    if (method === 'app_market.stop') {
      instances = [{ ...running, state: 'stopped' }];
      return { stopped: true };
    }
    return response(method, instances);
  });
  try {
    await menuClick('app-market-toggle');
    const notice = actionNotice();
    assert.match(notice.textContent, /App stopped/);
    assert.equal(notice.parentElement, find('ui-toast-stack-right'));
    assert.equal(find('app-market-list').contains(notice), false);
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 3300)); });
    assert.equal(find('app-market-action-notice'), null);
  } finally {
    await ctx.close();
  }
});

test('Swarm chat marks player input over both transports and leaves Team observation unmarked', async () => {
  const originalSocket = globalThis.WebSocket;
  const originalFetch = globalThis.fetch;
  const socketRequests = [];
  const firstSend = deferred();
  let socket;
  class TestSocket {
    static OPEN = 1;

    constructor() {
      socket = this;
      queueMicrotask(() => {
        this.readyState = TestSocket.OPEN;
        this.onopen?.();
      });
    }

    addEventListener(event, callback) {
      if (event === 'close') this.onClose = callback;
    }

    send(raw) {
      const request = JSON.parse(raw);
      socketRequests.push(request);
      firstSend.resolve(request);
    }

    close() {
      this.readyState = 3;
      const event = { code: 1000, reason: '', wasClean: true };
      this.onclose?.(event);
      this.onClose?.(event);
    }
  }
  globalThis.WebSocket = TestSocket;
  const http = new WebHttpClient();
  try {
    await webClient.connect();
    const reply = webClient.request('chat.send', { session_id: 'npc', query: 'hello' });
    const request = await firstSend.promise;
    socket.onmessage({ data: JSON.stringify({ type: 'res', id: request.id, ok: true, payload: {} }) });
    await reply;
    assert.equal(request.params.chat_surface, 'swarm');
    await webClient.sendFireAndForget('chat.send', {
      session_id: 'npc',
      query: '',
      attach_only: true,
    }, { isStream: true });
    assert.equal(socketRequests.at(-1).params.chat_surface, undefined);

    let httpBody;
    globalThis.fetch = async (_url, options) => {
      if (options.method === 'POST') {
        httpBody = JSON.parse(options.body);
        return new Response('\n', { headers: { 'content-type': 'text/event-stream' } });
      }
      return Response.json({ ok: true, payload: {} });
    };
    await http.connect();
    await http.request('chat.send', { session_id: 'npc', query: 'hello' });
    assert.equal(httpBody.chat_surface, 'swarm');
  } finally {
    await http.disconnect();
    await webClient.disconnect();
    globalThis.fetch = originalFetch;
    globalThis.WebSocket = originalSocket;
  }
});
