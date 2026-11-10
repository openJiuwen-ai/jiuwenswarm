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
async function renderWorkspace({ withChannels = false } = {}) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    actEnvironment: globalThis.IS_REACT_ACT_ENVIRONMENT,
  };
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const ticks = new Map();
  let nextTimerId = 0;
  dom.window.setInterval = (callback, delay) => {
    const id = ++nextTimerId;
    ticks.set(id, { callback, delay });
    return id;
  };
  dom.window.clearInterval = (id) => ticks.delete(id);
  dom.window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  const state = { webConversations: [], channels: [], channelCalls: [], channelFailure: false };
  Object.defineProperty(dom.window.document, 'visibilityState', { configurable: true, get: () => state.hidden ? 'hidden' : 'visible' });
  const channelApi = {
    listChannels: async (agentId) => {
      state.channelCalls.push(agentId);
      if (state.channelGate) await state.channelGate;
      if (state.channelFailure === true || state.channelFailure === agentId) throw new Error('Temporary channel failure');
      return state.channelsByAgent?.[agentId] ?? (agentId === 'config-web' ? state.channels : []);
    },
    channelMessages: async () => ({ messages: [], nextCursor: null }),
    channelTrace: async () => ({ rows: [], startedAt: '' }),
  };
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
      channelApi: withChannels ? channelApi : undefined,
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
    timers: ticks,
    async management() {
      await act(async () => container.querySelector('[data-testid="jiuwenmate-open-management-panel"]').click());
      await flush();
    },
    async focus() {
      await act(async () => dom.window.dispatchEvent(new dom.window.Event('focus')));
      await flush();
    },
    async visibility(hidden) {
      state.hidden = hidden;
      await act(async () => dom.window.document.dispatchEvent(new dom.window.Event('visibilitychange')));
      await flush();
    },
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
        for (const { callback } of ticks.values()) callback();
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

const joinedGroup = { id: 'group-new', platform: 'feishu', name: 'New Feishu group', messageCount: 0 };

test('joining a group updates an open channel directory without remounting the workspace', async () => {
  const page = await renderWorkspace({ withChannels: true });
  try {
    await page.management();
    page.state.channels = [joinedGroup];
    await page.poll();
    const panel = page.container.querySelector('[data-testid="jiuwenmate-management-panel"]');
    assert.ok(panel.textContent.includes(joinedGroup.name));
  } finally { await page.close(); }
});

test('opening the channel panel catches up immediately and a closed panel does not poll', async () => {
  const page = await renderWorkspace({ withChannels: true });
  try {
    page.state.channelCalls.length = 0;
    await page.poll();
    assert.equal(page.state.channelCalls.length, 0);
    page.state.channels = [joinedGroup];
    await page.management();
    assert.ok(page.state.channelCalls.length > 0);
    assert.ok(page.container.querySelector('[data-testid="jiuwenmate-management-panel"]').textContent.includes(joinedGroup.name));
    await page.management();
    page.state.channelCalls.length = 0;
    await page.poll();
    assert.equal(page.state.channelCalls.length, 0);
  } finally { await page.close(); }
});

test('channel refresh skips hidden pages and catches up on visibility, focus and reconnect', async () => {
  const page = await renderWorkspace({ withChannels: true });
  try {
    await page.management();
    await page.visibility(true);
    page.state.channelCalls.length = 0;
    await page.poll();
    assert.equal(page.state.channelCalls.length, 0);
    page.state.channels = [joinedGroup];
    await page.visibility(false);
    assert.ok(page.state.channelCalls.length > 0);
    page.state.channelCalls.length = 0;
    await page.focus();
    assert.ok(page.state.channelCalls.length > 0);
    page.state.channelCalls.length = 0;
    await page.reconnect();
    assert.ok(page.state.channelCalls.length > 0);
  } finally { await page.close(); }
});

test('channel polling waits for its previous batch instead of stacking requests', async () => {
  const page = await renderWorkspace({ withChannels: true });
  let release;
  try {
    await page.management();
    page.state.channelCalls.length = 0;
    page.state.channelGate = new Promise((resolve) => { release = resolve; });
    await page.poll();
    const firstBatch = page.state.channelCalls.length;
    assert.ok(firstBatch > 0);
    await page.poll(); await page.focus();
    assert.equal(page.state.channelCalls.length, firstBatch);
  } finally {
    release?.(); page.state.channelGate = null;
    await page.flush(); await page.close();
  }
});

test('failed channel refresh retains existing groups and successful refresh applies renames and leaves', async () => {
  const page = await renderWorkspace({ withChannels: true });
  try {
    page.state.channels = [joinedGroup];
    await page.management();
    page.state.channelFailure = true;
    await page.poll();
    let panel = page.container.querySelector('[data-testid="jiuwenmate-management-panel"]');
    assert.ok(panel.textContent.includes(joinedGroup.name));
    page.state.channelFailure = false;
    page.state.channels = [{ ...joinedGroup, name: 'Renamed Feishu group' }];
    await page.poll();
    panel = page.container.querySelector('[data-testid="jiuwenmate-management-panel"]');
    assert.ok(panel.textContent.includes('Renamed Feishu group'));
    page.state.channels = [];
    await page.poll();
    assert.ok(!panel.textContent.includes('Renamed Feishu group'));
  } finally { await page.close(); }
});

test('a failed connection keeps its known groups while other connections refresh', async () => {
  const page = await renderWorkspace({ withChannels: true });
  try {
    page.state.channelsByAgent = { 'runtime-feishu': [joinedGroup] };
    await page.management();
    page.state.channelFailure = 'runtime-feishu';
    page.state.channelsByAgent['config-web'] = [{ ...joinedGroup, id: 'other-group', name: 'Another group' }];
    await page.poll();
    const panel = page.container.querySelector('[data-testid="jiuwenmate-management-panel"]');
    assert.ok(panel.textContent.includes(joinedGroup.name));
    assert.ok(panel.textContent.includes('Another group'));
  } finally { await page.close(); }
});

test('the channel timer runs every ten seconds only while needed and is cleaned up on unmount', async () => {
  const page = await renderWorkspace({ withChannels: true });
  try {
    const channelTimers = () => [...page.timers.values()].filter(({ delay }) => delay === 10_000);
    assert.equal(channelTimers().length, 0);
    await page.management();
    assert.equal(channelTimers().length, 1);
    await page.management();
    assert.equal(channelTimers().length, 0);
    await page.management();
    assert.equal(channelTimers().length, 1);
  } finally { await page.close(); }
  assert.equal(page.timers.size, 0);
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
