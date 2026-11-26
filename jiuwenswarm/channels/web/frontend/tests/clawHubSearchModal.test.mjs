import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import React, { act } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
// Bridge jsdom's window.setTimeout to the global one so node:test mock timers
// fast-forward the message timer used via window.setTimeout. jsdom happens to
// delegate internally, but the bridge keeps the assumption explicit and in
// sync with filePreviewContent.test.mjs.
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
const { ClawHubSearchModal } = await import(
  '../node_modules/.cache/skill-search-races/features/ClawHubSearchModal/index.js'
);

function deferred() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

function skillItem(name) {
  return { slug: name, display_name: name, summary: `summary of ${name}`, version: '1.0.0', updated_at: 0, owner_handle: 'o' };
}
function setSearchImpl(handler) {
  globalThis.__webRequestImpl = async (method, params) => {
    if (method === 'skills.clawhub.get_token') return { success: true, token: 'tok-123', has_token: true };
    if (method === 'skills.clawhub.search') return handler(params);
    return { success: false };
  };
}

async function setQuery(input, value) {
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, value);
    input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  });
}
const pressEnter = (input) => act(() => {
  input.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
});
const inputEl = () => document.querySelector('[data-testid="claw-hub-search-modal-search-input"]');
const searchButton = () => document.querySelector('[data-testid="claw-hub-search-modal-search-button"]');
const errorBar = () => document.querySelector('[data-testid="claw-hub-search-modal-error-bar-modal"]');
const bodyText = () => document.getElementById('root').textContent;

test('loading 中按 Enter 不再触发第二次搜索', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let searchCalls = 0;
  const slow = deferred();
  setSearchImpl(() => {
    searchCalls += 1;
    return slow.promise.then(() => ({ success: true, skills: [skillItem('alpha-skill')] }));
  });
  const rootEl = createRoot(document.getElementById('root'));
  await act(async () => {
    rootEl.render(React.createElement(ClawHubSearchModal, { open: true, sessionId: 's1', onClose: () => {} }));
  });
  await setQuery(inputEl(), 'alpha');
  pressEnter(inputEl());
  await act(async () => {});
  assert.equal(searchCalls, 1);
  assert.equal(searchButton().disabled, true, 'loading 时按钮应禁用');
  pressEnter(inputEl());
  await act(async () => {});
  assert.equal(searchCalls, 1, 'loading 中 Enter 不应触发第二次搜索');
  await act(async () => { slow.resolve(); });
  await act(async () => {});
  rootEl.unmount();
  await act(async () => {});
});

test('embedded 连续搜索：晚到的旧响应不覆盖新结果', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const slow = deferred();
  setSearchImpl((params) => {
    if (params.q === 'alpha') return slow.promise.then(() => ({ success: true, skills: [skillItem('alpha-skill')] }));
    return Promise.resolve({ success: true, skills: [skillItem('beta-skill')] });
  });
  const rootEl = createRoot(document.getElementById('root'));
  await act(async () => {
    rootEl.render(React.createElement(ClawHubSearchModal, {
      open: true, embedded: true, sessionId: 's1', onClose: () => {}, externalSearchQuery: 'alpha',
    }));
  });
  await act(async () => {});
  await act(async () => {
    rootEl.render(React.createElement(ClawHubSearchModal, {
      open: true, embedded: true, sessionId: 's1', onClose: () => {}, externalSearchQuery: 'beta',
    }));
  });
  await act(async () => {});
  assert.ok(bodyText().includes('beta-skill'), 'beta 结果应先落地');
  await act(async () => { slow.resolve(); });
  await act(async () => {});
  assert.ok(bodyText().includes('beta-skill'), '晚到的 alpha 响应不应覆盖 beta 结果');
  assert.ok(!bodyText().includes('alpha-skill'), 'alpha 结果不应出现');
  rootEl.unmount();
  await act(async () => {});
});

test('连续两条错误消息不被上一条定时器提前清除', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  setSearchImpl((params) => ({ success: false, detail: params.q === 'q1' ? 'ERR-A' : 'ERR-B' }));
  const rootEl = createRoot(document.getElementById('root'));
  await act(async () => {
    rootEl.render(React.createElement(ClawHubSearchModal, { open: true, sessionId: 's1', onClose: () => {} }));
  });
  await setQuery(inputEl(), 'q1');
  pressEnter(inputEl());
  await act(async () => {});
  assert.ok(errorBar()?.textContent.includes('ERR-A'), '第一条错误消息应显示');
  t.mock.timers.tick(500);
  await setQuery(inputEl(), 'q2');
  pressEnter(inputEl());
  await act(async () => {});
  assert.ok(errorBar()?.textContent.includes('ERR-B'), '第二条错误消息应显示');
  t.mock.timers.tick(2500);
  await act(async () => {});
  assert.ok(errorBar()?.textContent.includes('ERR-B'), '第二条消息不应被第一条的 3s 定时器提前清除');
  t.mock.timers.tick(600);
  await act(async () => {});
  assert.equal(errorBar(), null, '第二条消息满 3s 后应正常清除');
  rootEl.unmount();
  await act(async () => {});
});
