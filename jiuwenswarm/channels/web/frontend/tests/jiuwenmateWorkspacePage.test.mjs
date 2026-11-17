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
async function renderWorkspace({ withChannels = false, schedules = [], intent = null, channels = [], ...colleagueOverrides } = {}, initialWebConversations = []) {
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
  const state = { webConversations: initialWebConversations, channels, channelCalls: [], channelFailure: false, schedules };
  let currentColleague = { ...colleague, ...colleagueOverrides };
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
    listArtifacts: async (agentId) => {
      calls.push(['artifacts', agentId]);
      return state.artifacts?.[agentId] ?? [];
    },
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
      if (state.sendError) throw state.sendError;
      const ended = !state.keepRunning;
      return {
        request: {
          requestId: `request-${messageId}`, conversationId, messageId, text, receivedAt: '2026-10-26T03:00:00Z',
          updatedAt: '2026-10-26T03:00:05Z',
          status: ended ? 'ended' : 'dispatched', outcome: ended ? 'completed' : null, running: !ended,
          replies: ended ? ['进展如下'] : [], files: [], reason: null, draft: null,
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
      colleagues: [currentColleague],
      selectedColleagueId: colleague.id,
      intent,
      chat,
      channelApi: withChannels ? channelApi : undefined,
      connectionEpoch,
      schedules: state.schedules,
      onUpdateSchedule: async (id, draft) => {
        calls.push(['update-schedule', id, draft]);
        if (state.scheduleSaveError) throw new Error('Schedule save failed');
        if (state.scheduleSaveGate) await state.scheduleSaveGate;
        state.schedules = state.schedules.map(plan => plan.id === id ? { ...plan, ...draft } : plan);
        render();
      },
      onSetScheduleEnabled: async (id, enabled) => {
        calls.push(['enable-schedule', id, enabled]);
        if (state.scheduleEnableError) throw new Error('Schedule enable failed');
        state.schedules = state.schedules.map(plan => plan.id === id
          ? { ...plan, enabled, lastStatus: enabled ? 'running' : 'paused' } : plan);
        render();
      },
      onRunSchedule: async id => {
        calls.push(['run-schedule', id]);
        if (state.scheduleRunError) throw new Error('Schedule run failed');
      },
      onDeleteSchedule: async id => { calls.push(['delete-schedule', id]); },
      onLoadScheduleHistory: async id => {
        calls.push(['schedule-history', id]);
        return { plan: state.schedules.find(plan => plan.id === id), runs: [] };
      },
      modelCatalog: state.modelCatalog ?? { models: [{ id: 'deepseek#0', label: 'DeepSeek', provider: 'OpenAI' },
        { id: 'kimi#1', label: 'Kimi', provider: 'OpenAI' }], defaultModelId: 'deepseek#0', defaultModelLabel: 'DeepSeek' },
      onSaveModel: async (agentId, modelId) => {
        calls.push(['save-model', agentId, modelId]);
        if (state.modelError) throw state.modelError;
        currentColleague = { ...currentColleague, hasUnpublishedChanges: false, hasUnpublishedModelChanges: false,
          capabilities: { ...currentColleague.capabilities, model: modelId } };
        render();
      },
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
    async publish() {
      currentColleague = {
        ...currentColleague, currentVersion: 'v1',
        capabilities: { ...currentColleague.capabilities, model: 'deepseek#0' },
      };
      await act(async () => render());
      await flush();
    },
    async setModel(modelId) {
      currentColleague = { ...currentColleague, capabilities: { ...currentColleague.capabilities, model: modelId } };
      await act(async () => render());
      await flush();
    },
    async replaceEmployee(changes) {
      currentColleague = { ...currentColleague, ...changes };
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

test('an unconfigured employee guides model setup, keeps the draft and cannot submit until publication', async () => {
  const page = await renderWorkspace({ currentVersion: null, capabilities: { ...colleague.capabilities, model: '' } });
  try {
    assert.match(page.container.textContent, /请先在「设置 → 模型」中配置可用模型/);
    await page.send('保留这条草稿');
    const input = page.container.querySelector('[data-testid="jiuwenmate-composer-input"]');
    assert.equal(input.value, '保留这条草稿');
    assert.equal(page.container.querySelector('[data-testid="jiuwenmate-composer-send"]').disabled, true);
    await act(async () => input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
    assert.equal(page.calls.filter(([kind]) => kind === 'send').length, 0);

    await page.publish();
    assert.doesNotMatch(page.container.textContent, /请先在「设置 → 模型」中配置可用模型/);
    await page.send('保留这条草稿');
    assert.equal(page.calls.filter(([kind]) => kind === 'send').length, 1);
  } finally {
    await page.close();
  }
});

test('hovering the send and stop buttons names the action like the personal chat', async () => {
  const page = await renderWorkspace();
  const hover = async (button) => {
    await act(async () => button.dispatchEvent(new window.MouseEvent('mouseover', { bubbles: true })));
    return document.querySelector('[role="tooltip"]')?.textContent ?? null;
  };
  const leave = async (button) => {
    await act(async () => button.dispatchEvent(new window.MouseEvent('mouseout', { bubbles: true })));
  };
  try {
    const input = page.container.querySelector('[data-testid="jiuwenmate-composer-input"]');
    await act(async () => {
      Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set.call(input, '草稿');
      input.dispatchEvent(new window.Event('input', { bubbles: true }));
    });
    const send = page.container.querySelector('[data-testid="jiuwenmate-composer-send"]');
    assert.equal(send.disabled, false);
    assert.equal(send.getAttribute('title'), null);
    assert.equal(await hover(send), '发送');
    await leave(send);
    assert.equal(document.querySelector('[role="tooltip"]'), null);

    page.state.keepRunning = true;
    await page.send('跑一个长任务');
    const stop = page.container.querySelector('[data-testid="jiuwenmate-composer-stop"]');
    assert.ok(stop);
    assert.equal(stop.getAttribute('title'), null);
    assert.equal(await hover(stop), '停止');
  } finally {
    await page.close();
  }
});

test('the composer send button keeps its dark fill and highlights on hover instead of taking the toolbar grey', () => {
  const css = readFileSync(new URL('../src/features/jiuwenmate/jiuwenmate.css', import.meta.url), 'utf8');
  const rule = css.match(/\.jiuwenmate-composer__toolbar \.jiuwenmate-composer__send:hover:not\(:disabled\) \{([^}]*)\}/);
  assert.ok(rule, 'send button needs its own hover rule that outranks the generic toolbar hover');
  assert.match(rule[1], /background: #404040;/);
  assert.match(rule[1], /color: #fff;/);
});

test('the private chat welcome shows exactly the configured starter prompts and none when they were all removed', async () => {
  const suggestions = (page) =>
    [...page.container.querySelectorAll('.jiuwenmate-chat-welcome__suggestions button')].map((button) => button.textContent);
  const configured = await renderWorkspace({
    capabilities: { ...colleague.capabilities, starterPrompts: ['写周报', '写月报', '总结会议', '第四条'] },
  });
  try {
    assert.deepEqual(suggestions(configured), ['写周报', '写月报', '总结会议']);
  } finally {
    await configured.close();
  }
  const empty = await renderWorkspace();
  try {
    assert.ok(empty.container.querySelector('[data-testid="jiuwenmate-chat-welcome"]'));
    assert.deepEqual(suggestions(empty), []);
    assert.ok(empty.container.querySelector('.jiuwenmate-chat-welcome__suggestions'), 'The empty slot keeps the welcome layout height');
  } finally {
    await empty.close();
  }
});

test('a stale published-state response shows setup guidance instead of the internal exception', async () => {
  const page = await renderWorkspace();
  try {
    page.state.sendError = Object.assign(new Error('colleague has no published configuration'), { code: 'NOT_PUBLISHED' });
    await page.send('测试消息');
    assert.match(page.container.textContent, /请先在员工管理中保存并发布配置/);
    assert.doesNotMatch(page.container.textContent, /colleague has no published configuration/);
  } finally {
    await page.close();
  }
});

test('chat model selection updates the employee configuration even in an IM workspace and follows external changes', async () => {
  const page = await renderWorkspace();
  const selectModel = async (label) => {
    await act(async () => page.container.querySelector('[data-testid="jiuwenmate-composer-model"]').click());
    const option = [...window.document.querySelectorAll('[data-testid="jiuwenmate-composer-model-option"]')].find((item) => item.textContent.includes(label));
    assert.ok(option);
    await act(async () => option.click());
    await page.flush();
  };
  try {
    await page.choose('runtime-feishu');
    await selectModel('Kimi');
    assert.deepEqual(page.calls.find(([kind]) => kind === 'save-model'), ['save-model', 'config-web', 'kimi#1']);
    assert.match(page.container.querySelector('[data-testid="jiuwenmate-composer-model"]').textContent, /Kimi/);

    await page.setModel('deepseek#0');
    assert.match(page.container.querySelector('[data-testid="jiuwenmate-composer-model"]').textContent, /DeepSeek/);
    page.state.modelError = new Error('模型保存失败');
    await selectModel('Kimi');
    assert.match(page.container.querySelector('[data-testid="jiuwenmate-composer-model"]').textContent, /DeepSeek/);
    assert.match(page.container.textContent, /模型保存失败/);
    await page.replaceEmployee({ id: 'other-employee', name: '另一位员工' });
    assert.doesNotMatch(page.container.textContent, /模型保存失败/);
  } finally {
    await page.close();
  }
});

test('a saved but unpublished model can be selected again to retry publication', async () => {
  const page = await renderWorkspace({ hasUnpublishedChanges: true, hasUnpublishedModelChanges: true });
  try {
    assert.match(page.container.querySelector('[data-testid="jiuwenmate-composer-model"]').textContent, /待生效/);
    await act(async () => page.container.querySelector('[data-testid="jiuwenmate-composer-model"]').click());
    const option = [...window.document.querySelectorAll('[data-testid="jiuwenmate-composer-model-option"]')].find((item) => item.textContent.includes('DeepSeek'));
    await act(async () => option.click());
    assert.deepEqual(page.calls.find(([kind]) => kind === 'save-model'), ['save-model', 'config-web', 'deepseek#0']);
    assert.doesNotMatch(page.container.querySelector('[data-testid="jiuwenmate-composer-model"]').textContent, /待生效/);
  } finally {
    await page.close();
  }
});

test('unpublished persona or skills do not mark the unchanged model pending or republish it', async () => {
  const page = await renderWorkspace({ hasUnpublishedChanges: true, hasUnpublishedModelChanges: false });
  try {
    assert.doesNotMatch(page.container.querySelector('[data-testid="jiuwenmate-composer-model"]').textContent, /待生效/);
    await act(async () => page.container.querySelector('[data-testid="jiuwenmate-composer-model"]').click());
    const option = window.document.querySelector('[data-testid="jiuwenmate-composer-model-option"][data-variant="deepseek#0"]');
    assert.equal(option.getAttribute('aria-current'), 'true');
    await act(async () => option.click());
    assert.equal(page.calls.some(([kind]) => kind === 'save-model'), false);
  } finally {
    await page.close();
  }
});

test('artifact panel shows existing outputs from Web and IM sessions with empty chat history', async () => {
  const page = await renderWorkspace();
  try {
    page.state.artifacts = {
      'config-web': [{ outputId: 'existing-md', name: '已有知识摘要.md', path: 'sessions/old/output/已有知识摘要.md', size: 20 }],
      'runtime-feishu': [{ outputId: 'existing-docx', name: '已有报告.docx', path: 'sessions/group/output/已有报告.docx', size: 40 }],
    };
    await act(async () => page.container.querySelector('[data-testid="jiuwenmate-open-artifacts-panel"]').click());
    await page.flush();
    const items = [...page.container.querySelectorAll('[data-testid="jiuwenmate-artifact-item"]')];
    assert.equal(items.length, 2);
    assert.ok(items.some((item) => item.textContent.includes('已有知识摘要.md')));
    assert.ok(items.some((item) => item.textContent.includes('已有报告.docx')));
    assert.equal(page.calls.filter(([kind]) => kind === 'artifacts').length, 3);
  } finally { await page.close(); }
});

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

test('chat messages carry sent and replied timestamps like the task-mode records', async () => {
  const page = await renderWorkspace();
  try {
    await page.send('今天进展');
    const sentTime = page.container.querySelector('[data-testid="jiuwenmate-chat-message-time"]');
    assert.ok(sentTime, 'the sent message shows its time');
    assert.equal(sentTime.getAttribute('dateTime'), '2026-10-26T03:00:00Z');
    assert.match(sentTime.textContent, /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
    const replyTime = page.container.querySelector('[data-testid="jiuwenmate-chat-reply-time"]');
    assert.ok(replyTime, 'the colleague reply shows its time');
    assert.equal(replyTime.getAttribute('dateTime'), '2026-10-26T03:00:05Z');
    assert.match(replyTime.textContent, /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
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

test('a trace opened from the employee center shows in the management panel beside the private chat', async () => {
  const page = await renderWorkspace({
    withChannels: true,
    channels: [joinedGroup],
    intent: { recordTab: 'channels', channelId: joinedGroup.id, trace: true },
  });
  try {
    const panel = page.container.querySelector('[data-testid="jiuwenmate-management-panel"]');
    assert.ok(panel.querySelector('[data-testid="jiuwenmate-management-channel-detail"]'));
    assert.ok(panel.querySelector('[data-testid="jiuwenmate-channel-trace-page"]'));
    assert.equal(page.container.querySelector('[data-testid="jiuwenmate-channel-view"]'), null);

    await act(async () => panel.querySelector('[data-testid="jiuwenmate-trace-back-button"]').click());
    await page.flush();
    assert.ok(page.container.querySelector('[data-testid="jiuwenmate-management-channel-detail"]'));
    assert.equal(page.container.querySelector('[data-testid="jiuwenmate-channel-trace-page"]'), null);
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


test('a newly created employee has a visible conversation before sending any message', async () => {
  const initial = { ...webResult, id: 'initial-empty', title: '新对话', lastMessage: '' };
  const page = await renderWorkspace({}, [initial]);
  try {
    const records = page.container.querySelectorAll('[data-testid="jiuwenmate-conversation-record"]');
    assert.equal(records.length, 1);
    assert.match(records[0].textContent, /测试同事/);
    assert.ok(page.calls.some(([kind, agentId, conversationId]) =>
      kind === 'history' && agentId === 'config-web' && conversationId === initial.id));
    assert.equal(page.calls.filter(([kind]) => kind === 'send').length, 0);
    await page.reconnect();
    assert.ok(page.calls.some(([kind, , conversationId]) => kind === 'history' && conversationId === initial.id));
    assert.equal(page.calls.filter(([kind]) => kind === 'send').length, 0);
  } finally {
    await page.close();
  }
});


test('the employee chat model picker does not present a deleted model or default as its selection', async () => {
  const page = await renderWorkspace({
    capabilities: { ...colleague.capabilities, model: 'removed#9' },
    hasUnpublishedModelChanges: true,
  });
  try {
    const picker = page.container.querySelector('[data-testid="jiuwenmate-composer-model"]');
    assert.doesNotMatch(picker.textContent, /removed#9|DeepSeek|Kimi/);
    assert.equal(page.container.querySelector('[data-testid="jiuwenmate-composer-model-pending"]'), null);
    await act(async () => picker.click());
    assert.equal(window.document.querySelector('[data-testid="jiuwenmate-composer-model-option"][data-variant="removed#9"]'), null);
    assert.equal(window.document.querySelector('[data-testid="jiuwenmate-composer-model-option"][aria-current="true"]'), null);
  } finally { await page.close(); }
});


test('deleting the last real model disables sending, preserves the draft and recovers after configuration', async () => {
  const page = await renderWorkspace();
  try {
    await page.send('删除模型前的消息');
    assert.equal(page.calls.filter(([kind]) => kind === 'send').length, 1);
    page.state.modelCatalog = { models: [], defaultModelId: '', defaultModelLabel: '' };
    await page.reconnect();
    await page.send('删除模型后的草稿');
    const input = page.container.querySelector('[data-testid="jiuwenmate-composer-input"]');
    assert.equal(input.value, '删除模型后的草稿');
    assert.equal(page.container.querySelector('[data-testid="jiuwenmate-composer-send"]').disabled, true);
    await act(async () => input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
    assert.equal(page.calls.filter(([kind]) => kind === 'send').length, 1);
    assert.match(page.container.textContent, /请先在「设置 → 模型」中配置可用模型/);
    page.state.modelCatalog = undefined;
    await page.reconnect();
    assert.equal(input.value, '删除模型后的草稿');
    await page.send('删除模型后的草稿');
    assert.equal(page.calls.filter(([kind]) => kind === 'send').length, 2);
  } finally { await page.close(); }
});

test('a deleted employee model cannot send through another models catalog', async () => {
  const page = await renderWorkspace({ capabilities: { ...colleague.capabilities, model: 'removed#9' } });
  try {
    await page.send('保留草稿');
    assert.equal(page.calls.filter(([kind]) => kind === 'send').length, 0);
    assert.equal(page.container.querySelector('[data-testid="jiuwenmate-composer-input"]').value, '保留草稿');
  } finally { await page.close(); }
});


const pausedPlan = {
  id: 'paused-plan', agentId: 'config-web', name: '健身提醒', prompt: '该健身了',
  cronExpr: '0 9 * * *', timezone: 'Asia/Shanghai', enabled: false,
  nextFireAt: '2099-01-01T01:00:00Z', scheduleKind: 'cron', intervalSeconds: null,
  runAt: null, startsAt: null, endsAt: null, conversationId: 'private-test', runCount: 1,
  lastStatus: 'paused', createdAt: '2026-11-15T00:00:00Z',
};

async function openSchedulePanel(page) {
  await page.management();
  const tabs = page.container.querySelectorAll('[role="tab"]');
  const schedulesTab = [...tabs].find(tab => tab.textContent === '定时任务');
  assert.ok(schedulesTab);
  await act(async () => schedulesTab.click());
}

async function chooseScheduleAction(page, action) {
  const panel = page.container.querySelector('[data-testid="jiuwenmate-management-panel"]');
  await act(async () => panel.querySelector('.jiuwenmate-menu__trigger').click());
  const button = page.container.querySelector(`[role="menuitem"][data-variant="${action}"]`);
  assert.ok(button, `Missing schedule ${action} action`);
  await act(async () => button.click());
  await page.flush();
}

test('workspace schedule controls show status and keep manual runs independent of enablement', async () => {
  const page = await renderWorkspace({ schedules: [pausedPlan] });
  try {
    await openSchedulePanel(page);
    const status = () => page.container.querySelector('[data-testid="jiuwenmate-workspace-schedule-status"]');
    assert.equal(status()?.textContent, '已暂停');
    await chooseScheduleAction(page, 'run');
    assert.equal(page.calls.filter(([kind]) => kind === 'run-schedule').length, 1);
    assert.equal(page.state.schedules[0].enabled, false);
    await chooseScheduleAction(page, 'toggle');
    assert.equal(page.state.schedules[0].enabled, true);
    assert.equal(status()?.textContent, '运行中');
    await chooseScheduleAction(page, 'toggle');
    assert.equal(page.state.schedules[0].enabled, false);
    await chooseScheduleAction(page, 'history');
    assert.ok(page.container.querySelector('[data-testid="jiuwenmate-schedule-history"]'));
  } finally { await page.close(); }
});

test('saving a paused schedule keeps it paused while save-and-enable updates before enabling', async () => {
  const page = await renderWorkspace({ schedules: [pausedPlan] });
  try {
    await openSchedulePanel(page);
    await chooseScheduleAction(page, 'edit');
    assert.match(page.container.querySelector('[data-testid="jiuwenmate-schedule-paused-hint"]').textContent, /当前任务已暂停/);
    await act(async () => page.container.querySelector('[data-testid="jiuwenmate-schedule-save"]').click());
    await page.flush();
    assert.equal(page.state.schedules[0].enabled, false);
    assert.equal(page.calls.filter(([kind]) => kind === 'enable-schedule').length, 0);
    await chooseScheduleAction(page, 'edit');
    const saveAndEnable = page.container.querySelector('[data-testid="jiuwenmate-schedule-save-enable"]');
    assert.ok(saveAndEnable);
    assert.equal(saveAndEnable.textContent, '保存并启用');
    await act(async () => saveAndEnable.click());
    await page.flush();
    assert.equal(page.state.schedules[0].enabled, true);
    assert.deepEqual(page.calls.filter(([kind]) => kind.endsWith('-schedule')).slice(-2).map(([kind]) => kind),
      ['update-schedule', 'enable-schedule']);
    assert.equal(page.container.querySelector('[data-testid="jiuwenmate-schedule-dialog"]'), null);
  } finally { await page.close(); }
});

test('a failed schedule save never enables the task and enable failure stays visible for retry', async () => {
  const page = await renderWorkspace({ schedules: [pausedPlan] });
  try {
    await openSchedulePanel(page);
    await chooseScheduleAction(page, 'edit');
    page.state.scheduleSaveError = true;
    const button = () => page.container.querySelector('[data-testid="jiuwenmate-schedule-save-enable"]');
    assert.ok(button());
    await act(async () => button().click());
    await page.flush();
    assert.equal(page.calls.filter(([kind]) => kind === 'enable-schedule').length, 0);
    assert.match(page.container.querySelector('[data-testid="jiuwenmate-schedule-dialog"]').textContent, /Schedule save failed/);
    page.state.scheduleSaveError = false;
    page.state.scheduleEnableError = true;
    await act(async () => button().click());
    await page.flush();
    assert.equal(page.state.schedules[0].enabled, false);
    assert.match(page.container.querySelector('[data-testid="jiuwenmate-schedule-dialog"]').textContent, /Schedule enable failed/);
    page.state.scheduleEnableError = false;
    await act(async () => button().click());
    await page.flush();
    assert.equal(page.state.schedules[0].enabled, true);
  } finally { await page.close(); }
});

test('an expired workspace schedule cannot enable until its time is edited', async () => {
  const page = await renderWorkspace({ schedules: [{ ...pausedPlan, lastStatus: 'completed',
    scheduleKind: 'once', runAt: '2020-01-01T00:00:00Z', nextFireAt: null }] });
  try {
    await openSchedulePanel(page);
    assert.equal(page.container.querySelector('[data-testid="jiuwenmate-workspace-schedule-status"]')?.textContent, '已过期');
    const panel = page.container.querySelector('[data-testid="jiuwenmate-management-panel"]');
    await act(async () => panel.querySelector('.jiuwenmate-menu__trigger').click());
    assert.equal(page.container.querySelector('[data-variant="toggle"][role="menuitem"]')?.disabled, true);
  } finally { await page.close(); }
});


test('save-and-enable rejects repeated clicks while the save is pending', async () => {
  const page = await renderWorkspace({ schedules: [pausedPlan] });
  let release;
  try {
    page.state.scheduleSaveGate = new Promise(resolve => { release = resolve; });
    await openSchedulePanel(page);
    await chooseScheduleAction(page, 'edit');
    const button = page.container.querySelector('[data-testid="jiuwenmate-schedule-save-enable"]');
    await act(async () => { button.click(); button.click(); });
    await page.flush();
    assert.equal(button.disabled, true);
    assert.equal(page.calls.filter(([kind]) => kind === 'update-schedule').length, 1);
    assert.equal(page.calls.filter(([kind]) => kind === 'enable-schedule').length, 0);
    await act(async () => release());
    await page.flush();
    assert.equal(page.calls.filter(([kind]) => kind === 'enable-schedule').length, 1);
    assert.equal(page.state.schedules[0].enabled, true);
  } finally { release?.(); await page.close(); }
});

test('workspace schedule action failures are visible and keep enablement unchanged', async () => {
  const page = await renderWorkspace({ schedules: [pausedPlan] });
  try {
    await openSchedulePanel(page);
    page.state.scheduleRunError = true;
    await chooseScheduleAction(page, 'run');
    const error = page.container.querySelector('[data-testid="jiuwenmate-workspace-schedule-error-dialog"]');
    assert.ok(error);
    assert.match(error.textContent, /Schedule run failed/);
    assert.equal(page.state.schedules[0].enabled, false);
  } finally { await page.close(); }
});
