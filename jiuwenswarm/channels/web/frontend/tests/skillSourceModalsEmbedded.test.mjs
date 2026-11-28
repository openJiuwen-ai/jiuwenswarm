import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import React, { act } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
// Bridge jsdom's window.setTimeout to the global one so node:test mock timers
// fast-forward timers used via window.setTimeout (same pattern as the other
// test files in this suite).
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
const { SkillNetSearchModal } = await import(
  '../node_modules/.cache/skill-search-races/features/SkillNetSearchModal/index.js'
);
const { TeamSkillsHubModal } = await import(
  '../node_modules/.cache/skill-search-races/features/TeamSkillsHubModal/index.js'
);

function deferred() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

const bodyText = () => document.getElementById('root').textContent;

async function renderEmbedded(Component, query) {
  const rootEl = createRoot(document.getElementById('root'));
  const render = (q) => act(async () => {
    rootEl.render(React.createElement(Component, {
      open: true, embedded: true, sessionId: 's1', onClose: () => {}, externalSearchQuery: q,
    }));
  });
  await render(query);
  await act(async () => {});
  return { rootEl, render };
}

test('SkillNet embedded 连续搜索：晚到的旧响应不覆盖新结果', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const slow = deferred();
  globalThis.__webRequestImpl = async (method, params) => {
    if (method === 'skills.skillnet.search') {
      if (params.q === 'alpha') {
        return slow.promise.then(() => ({
          success: true,
          skills: [{ skill_name: 'alpha-skill', skill_description: 'd', author: 'a', stars: 1, skill_url: 'u', category: 'c' }],
        }));
      }
      return {
        success: true,
        skills: [{ skill_name: 'beta-skill', skill_description: 'd', author: 'a', stars: 1, skill_url: 'u', category: 'c' }],
      };
    }
    return { success: true };
  };
  const { rootEl, render } = await renderEmbedded(SkillNetSearchModal, 'alpha');
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

test('TeamSkillsHub embedded 连续搜索：晚到的旧响应不覆盖新结果', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const slow = deferred();
  const item = (name) => ({ asset_id: name, name, display_name: name, summary: `summary of ${name}`, version: '1.0.0', updated_at: 0 });
  globalThis.__webRequestImpl = async (method, params) => {
    if (method === 'skills.teamskillshub.search') {
      if (params.q === 'alpha') return slow.promise.then(() => ({ success: true, skills: [item('alpha-skill')] }));
      return { success: true, skills: [item('beta-skill')] };
    }
    return { success: true };
  };
  const { rootEl, render } = await renderEmbedded(TeamSkillsHubModal, 'alpha');
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
