import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import i18n from 'i18next';
import React, { act } from 'react';
import { JSDOM } from 'jsdom';
import { initReactI18next } from 'react-i18next';

// React DOM decides at import time whether it runs in a browser, which controls input event handling.
const browser = new JSDOM('<!doctype html>', { url: 'http://localhost' });
globalThis.window = browser.window;
globalThis.document = browser.window.document;
globalThis.MutationObserver = browser.window.MutationObserver;
// JSDOM has no layout observer; these tests exercise request routing rather than resize.
globalThis.ResizeObserver = class { observe() {} disconnect() {} };
const { createRoot } = await import('react-dom/client');
const { ColleagueWorkspacePage } = await import('../node_modules/.cache/jiuwenmate-profile/pages/ColleagueWorkspacePage.js');

const zh = JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8'));
await i18n.use(initReactI18next).init({ lng: 'zh', resources: { zh: { translation: zh } }, interpolation: { escapeValue: false } });

const colleague = {
  id: 'config-web',
  configId: 'config-web',
  name: '测试同事',
  description: '',
  persona: '',
  category: 'custom',
  tags: [],
  capabilities: { model: 'deepseek#0', skills: [], connectors: [], plugins: [], knowledge: [], starterPrompts: [] },
  imConfigured: true,
  disabled: false,
  joinedAt: '2026-10-26T00:00:00Z',
  currentVersion: 'v1',
  connections: [
    { channel: 'web', connectionKey: 'web', agentId: 'config-web', workspaceDir: null },
    { channel: 'feishu', connectionKey: 'app-a', agentId: 'runtime-feishu', workspaceDir: null },
    { channel: 'dingtalk', connectionKey: 'app-b', agentId: 'runtime-dingtalk', workspaceDir: null },
  ],
};

const webResult = {
  id: 'new-web-result',
  title: 'Web 新结果',
  pinned: false,
  updatedAt: '2026-10-26T02:00:00Z',
  lastMessage: '定时任务完成',
};

/** Renders the real page with a recording chat API and timers the test drives itself. */
async function renderWorkspace() {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    actEnvironment: globalThis.IS_REACT_ACT_ENVIRONMENT,
  };
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const ticks = [];
  dom.window.setInterval = (callback) => {
    ticks.push(callback);
    return ticks.length;
  };
  dom.window.clearInterval = () => undefined;
  dom.window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  const state = { webConversations: [] };
  const calls = [];
  const chat = {
    listConversations: async (agentId) => {
      calls.push(['list', agentId]);
      if (state.listGate) await state.listGate;
      return agentId === 'config-web' ? state.webConversations : [];
    },
    history: async (agentId, conversationId) => {
      calls.push(['history', agentId, conversationId]);
      return [];
    },
    sendMessage: async (agentId, conversationId, text, messageId) => {
      calls.push(['send', agentId, conversationId, text]);
      return {
        request: {
          requestId: `request-${messageId}`, conversationId, messageId, text, receivedAt: '2026-10-26T03:00:00Z',
          status: 'ended', outcome: 'completed', running: false, replies: [], files: [], reason: null, draft: null,
          control: null, recovery: null, approval: null,
        },
        conversation: { id: conversationId, title: text, updatedAt: '2026-10-26T03:00:00Z', lastMessage: text },
      };
    },
    getRequest: async () => null,
    abortRequest: async () => null,
    decidePermission: async () => null,
    onStream: () => () => undefined,
    renameConversation: async () => null,
    setConversationPinned: async () => null,
    deleteConversation: async () => undefined,
  };
  const container = dom.window.document.getElementById('root');
  const root = createRoot(container);
  const flush = () => act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  let connectionEpoch = 1;
  const render = () => {
    root.render(React.createElement(ColleagueWorkspacePage, {
      colleagues: [colleague],
      selectedColleagueId: colleague.id,
      chat,
      connectionEpoch,
      onSelectColleague: () => undefined,
      onCreateSchedule: () => undefined,
      onRenameColleague: async () => undefined,
    }));
  };
  await act(async () => render());
  await flush();
  await flush();
  const select = () => container.querySelector('[data-testid="jiuwenmate-workspace-connection-select"]');
  return {
    calls,
    state,
    container,
    select,
    flush,
    async reconnect() {
      connectionEpoch += 1;
      await act(async () => render());
      await flush();
    },
    async choose(agentId) {
      await act(async () => {
        select().value = agentId;
        select().dispatchEvent(new dom.window.Event('change', { bubbles: true }));
      });
      await flush();
    },
    /** One round of the page's two-second poll. */
    async poll() {
      await act(async () => {
        for (const tick of ticks) tick();
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      await flush();
    },
    async send(text) {
      const input = container.querySelector('[data-testid="jiuwenmate-composer-input"]');
      await act(async () => {
        Object.getOwnPropertyDescriptor(dom.window.HTMLTextAreaElement.prototype, 'value').set.call(input, text);
        input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
      });
      await act(async () => {
        container.querySelector('[data-testid="jiuwenmate-composer-send"]').click();
      });
      await flush();
    },
    async close() {
      await act(async () => root.unmount());
      dom.window.close();
      if (previous.window === undefined) delete globalThis.window;
      else globalThis.window = previous.window;
      if (previous.document === undefined) delete globalThis.document;
      else globalThis.document = previous.document;
      if (previous.actEnvironment === undefined) delete globalThis.IS_REACT_ACT_ENVIRONMENT;
      else globalThis.IS_REACT_ACT_ENVIRONMENT = previous.actEnvironment;
    },
  };
}

test('a Web result found while an IM workspace is chosen is offered, and messages still go to the IM agent', async () => {
  const page = await renderWorkspace();
  try {
    await page.choose('runtime-feishu');
    assert.equal(page.select().value, 'runtime-feishu');
    assert.ok(page.calls.some(([kind, agentId]) => kind === 'history' && agentId === 'runtime-feishu'));

    page.state.webConversations = [webResult];
    page.calls.length = 0;
    await page.poll();

    assert.equal(page.select().value, 'runtime-feishu');
    const histories = page.calls.filter(([kind]) => kind === 'history');
    assert.ok(histories.length > 0 && histories.every(([, agentId]) => agentId === 'runtime-feishu'));
    const offer = page.container.querySelector('[data-testid="jiuwenmate-workspace-web-result"]');
    assert.ok(offer, 'the Web result is offered');

    await page.send('飞书空间里的问题');
    const sent = page.calls.find(([kind]) => kind === 'send');
    assert.deepEqual(sent?.slice(0, 2), ['send', 'runtime-feishu']);

    page.calls.length = 0;
    await act(async () => offer.click());
    await page.flush();
    assert.equal(page.select().value, 'config-web');
    assert.ok(page.calls.some(([kind, agentId, id]) => kind === 'history' && agentId === 'config-web' && id === 'new-web-result'));
    assert.equal(page.container.querySelector('[data-testid="jiuwenmate-workspace-web-result"]'), null);
  } finally {
    await page.close();
  }
});

test('in the Web workspace a new Web result still opens by itself', async () => {
  const page = await renderWorkspace();
  try {
    assert.equal(page.select().value, 'config-web');
    page.state.webConversations = [webResult];
    page.calls.length = 0;
    await page.poll();

    assert.equal(page.select().value, 'config-web');
    assert.ok(page.calls.some(([kind, agentId, id]) => kind === 'history' && agentId === 'config-web' && id === 'new-web-result'));
    assert.equal(page.container.querySelector('[data-testid="jiuwenmate-workspace-web-result"]'), null);
  } finally {
    await page.close();
  }
});


test('directory polling waits for its previous batch instead of stacking requests', async () => {
  const page = await renderWorkspace();
  let release;
  try {
    page.state.listGate = new Promise((resolve) => { release = resolve; });
    page.calls.length = 0;
    await page.poll(); await page.poll(); await page.poll();
    assert.equal(page.calls.filter(([kind]) => kind === 'list').length, 1);
  } finally {
    release(); page.state.listGate = null;
    await page.flush(); await page.close();
  }
});

test('unchanged directory polls avoid whole histories but retain a bounded missed-push fallback', async (t) => {
  let now = Date.now();
  t.mock.method(Date, 'now', () => now);
  const page = await renderWorkspace();
  try {
    page.calls.length = 0;
    await page.poll(); await page.poll();
    assert.equal(page.calls.filter(([kind]) => kind === 'history').length, 0);
    now += 31000;
    await page.poll();
    assert.equal(page.calls.filter(([kind]) => kind === 'history').length, 1);
  } finally { await page.close(); }
});


test('reconnect discovers Web results created offline while preserving the selected IM workspace', async () => {
  const page = await renderWorkspace();
  try {
    await page.choose('runtime-feishu');
    page.state.webConversations = [webResult];
    await page.reconnect();
    await page.poll();
    assert.equal(page.select().value, 'runtime-feishu');
    assert.ok(page.container.querySelector('[data-testid="jiuwenmate-workspace-web-result"]'));
  } finally { await page.close(); }
});
