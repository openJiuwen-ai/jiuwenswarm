import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import React, { act } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
// Bridge jsdom's window.setTimeout to the global one so node:test mock timers
// fast-forward the toast timer used via window.setTimeout (same pattern as
// the other test files in this suite).
dom.window.setTimeout = (fn, ms, ...rest) => setTimeout(fn, ms, ...rest);
dom.window.clearTimeout = (id, ...rest) => clearTimeout(id, ...rest);
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  localStorage: dom.window.localStorage,
  sessionStorage: dom.window.sessionStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
after(() => dom.window.close());
const { createRoot } = await import('react-dom/client');
await i18next.use(initReactI18next).init({
  lng: 'en', showSupportNotice: false, resources: { en: { translation: {} } },
});
const { EnterpriseSkillSourcePanel } = await import(
  '../node_modules/.cache/skill-search-races/features/EnterpriseSkillSourcePanel/index.js'
);

function deferred() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

const bodyText = () => document.getElementById('root').textContent;

const sourceSkill = (name) => ({
  source_id: 'src-mock', skill_id: name, version_id: 'v1', name,
  display_name: name, short_description: `desc of ${name}`, version: '1.0.0',
});

// The panel is the only reachable online-search entry in current develop
// (the three modals are transitional dead entries after the marketplace
// migration), so its latest-wins guard deserves CI coverage of its own.
function makeImpl({ search }) {
  return async (method, params) => {
    if (method === 'skills.source.providers') {
      return {
        success: true,
        providers: [{ source_id: 'src-mock', display_name: 'Mock Source', enabled: true, capabilities: ['search'] }],
      };
    }
    if (method === 'skills.updates.check') return { success: true, items: [] };
    if (method === 'skills.source.search') return search(params);
    return { success: true };
  };
}

async function renderPanel(sessionId, query) {
  const rootEl = createRoot(document.getElementById('root'));
  const render = (q) => act(async () => {
    rootEl.render(React.createElement(EnterpriseSkillSourcePanel, {
      sessionId, externalSearchQuery: q,
    }));
  });
  await render(query);
  await act(async () => {});
  return { rootEl, render };
}

test('Enterprise 技能广场连续搜索：晚到的旧响应不覆盖新结果', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const slow = deferred();
  globalThis.__webRequestImpl = makeImpl({
    search: (params) => {
      if (params.q === 'alpha') {
        return slow.promise.then(() => ({ success: true, items: [sourceSkill('alpha-skill')], total: 1 }));
      }
      return { success: true, items: [sourceSkill('beta-skill')], total: 1 };
    },
  });
  const { rootEl, render } = await renderPanel('ent-late-success', 'alpha');
  await render('beta');
  await act(async () => {});
  assert.ok(bodyText().includes('beta-skill'), 'beta 结果应先落地');
  await act(async () => { slow.resolve(); });
  await act(async () => {});
  assert.ok(bodyText().includes('beta-skill'), '晚到的 alpha 响应不应覆盖 beta 结果');
  assert.ok(!bodyText().includes('alpha-skill'), 'alpha 结果不应出现');
  rootEl.unmount();
  await act(async () => {});
});

test('Enterprise 技能广场晚到失败：searchCache 缓存回退被序号守卫丢弃', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  // 查询序列 alpha(成功，写缓存) → beta(成功) → alpha(慢失败) → gamma(快成功)。
  // alpha 的晚到失败若未被序号丢弃，catch 会用 searchCache 里 alpha 的旧快照
  // 覆盖 gamma 的新结果并弹错误提示。
  let alphaSearched = false;
  let betaSearched = false;
  const lateFailure = deferred();
  globalThis.__webRequestImpl = makeImpl({
    search: (params) => {
      if (params.q === 'alpha') {
        if (betaSearched) {
          return lateFailure.promise.then(() => { throw new Error('upstream broke'); });
        }
        alphaSearched = true;
        return { success: true, items: [sourceSkill('alpha-skill')], total: 1 };
      }
      if (params.q === 'beta') {
        betaSearched = true;
        return { success: true, items: [sourceSkill('beta-skill')], total: 1 };
      }
      return { success: true, items: [sourceSkill('gamma-skill')], total: 1 };
    },
  });
  assert.ok(alphaSearched === false, 'sanity');
  const { rootEl, render } = await renderPanel('ent-late-failure', 'alpha');
  assert.ok(bodyText().includes('alpha-skill'), 'alpha 首次搜索应成功并落地');
  await render('beta');
  await act(async () => {});
  assert.ok(bodyText().includes('beta-skill'), 'beta 结果应落地');
  await render('alpha');
  await act(async () => {});
  await render('gamma');
  await act(async () => {});
  assert.ok(bodyText().includes('gamma-skill'), 'gamma 结果应落地');
  await act(async () => { lateFailure.resolve(); });
  await act(async () => {});
  assert.ok(bodyText().includes('gamma-skill'), '晚到的 alpha 失败不应触发缓存回退覆盖 gamma');
  assert.ok(!bodyText().includes('alpha-skill'), 'alpha 的缓存快照不应被恢复');
  assert.ok(!bodyText().includes('upstream broke'), '晚到的失败不应弹错误提示');
  rootEl.unmount();
  await act(async () => {});
});
