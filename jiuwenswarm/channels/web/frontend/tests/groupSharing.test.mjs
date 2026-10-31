import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import React, { act } from 'react';
import { JSDOM } from 'jsdom';
import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

const browser = new JSDOM('<!doctype html>', { url: 'http://localhost' });
globalThis.window = browser.window;
globalThis.document = browser.window.document;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { createRoot } = await import('react-dom/client');
const { createJiuwenMateApi } = await import('../node_modules/.cache/group-sharing/jiuwenmateApi.js');
const { ColleagueWorkspacePage } = await import('../node_modules/.cache/group-sharing/pages/ColleagueWorkspacePage.js');
const locales = Object.fromEntries(['en', 'zh'].map((language) => [language, {
  translation: JSON.parse(readFileSync(new URL(`../src/i18n/locales/${language}.json`, import.meta.url), 'utf8')),
}]));
await i18n.use(initReactI18next).init({ lng: 'en', resources: locales, interpolation: { escapeValue: false } });

const target = { channel: 'feishu', app_id: 'app-destination', kind: 'group', id: 'destination', name: 'Destination team' };
// GroupMessageQuery.model_dump(mode='json'), returned unchanged by GroupShareRepository._approval_view.
const groupMessageQuery = {
  query: 'budget query', since: null, until: null, sender_id: null, cursor: null,
  limit: 50, group_ids: [], group_names: [],
};
const approval = {
  approval_id: 'approval-a', request_id: 'request-a', agent_id: 'runtime-feishu', config_id: 'config-web',
  status: 'pending', can_decide: true, requester_name: 'Requester Alice',
  source_groups: [{ group_id: 'source-a', name: 'Source team', platform: 'dingtalk' }],
  content_types: ['messages'], artifacts: [],
  target, query: groupMessageQuery, summary: 'Confidential budget summary',
  messages: [{ group_id: 'source-a', group_name: 'Source team', message_id: 'message-a', sender_name: 'Sender Bob', text: '<script>secret exact text</script>\nline two' }],
  created_at: '2026-10-27T00:00:00Z', expires_at: '2099-10-27T00:10:00Z', decided_at: null,
};
const grant = {
  grant_id: 'grant-a', source_group_id: 'source-a', source_group_name: 'Source team', target,
  content_types: ['messages'], source_platform: 'dingtalk',
  created_at: '2026-10-27T00:01:00Z', status: 'active',
};
const snapshot = (approvals = [approval], grants = [grant]) => ({ approvals, grants, conversation_id: 'sharing-approvals' });
const colleague = {
  id: 'config-web', configId: 'config-web', name: 'Colleague', description: '', persona: '', category: 'custom', tags: [],
  capabilities: { model: '', skills: [], connectors: [], plugins: [], knowledge: [], starterPrompts: [] },
  imConfigured: true, disabled: false, joinedAt: '2026-10-27T00:00:00Z', currentVersion: 'v1',
  connections: [
    { channel: 'web', connectionKey: 'web', agentId: 'config-web', workspaceDir: null },
    { channel: 'feishu', connectionKey: 'app-a', agentId: 'runtime-feishu', workspaceDir: null },
    { channel: 'dingtalk', connectionKey: 'app-b', agentId: 'runtime-dingtalk', workspaceDir: null },
  ],
};
const flush = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

async function renderWorkspace(initial = snapshot(), conversations = [], people = [colleague]) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  dom.window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  const timers = new Map();
  let timerId = 0;
  dom.window.setInterval = (callback) => { timers.set(++timerId, callback); return timerId; };
  dom.window.clearInterval = (id) => timers.delete(id);
  const state = { result: initial, list: null, decide: null, revoke: null, conversations, conversationsByAgent: {} };
  const calls = [];
  const api = createJiuwenMateApi(async (method, params) => {
    calls.push({ method, params });
    if (method === 'jiuwenmate.sharing.list') return state.list ? state.list(params) : state.result;
    if (method === 'jiuwenmate.sharing.decide') {
      if (state.decide) return state.decide(params);
      state.result = snapshot([{ ...approval, status: { once: 'approved_once', permanent: 'approved_permanent', reject: 'rejected' }[params.decision], can_decide: false }]);
      return state.result;
    }
    if (method === 'jiuwenmate.sharing.revoke') {
      if (state.revoke) return state.revoke(params);
      state.result = snapshot([], [{ ...grant, status: 'revoked' }]);
      return state.result;
    }
    if (method === 'jiuwenmate.conversation.list') return { conversations: state.conversationsByAgent[params.agent_id] ?? state.conversations };
    if (method === 'jiuwenmate.chat.history') return { requests: [] };
    throw new Error(`Unexpected RPC: ${method}`);
  });
  const root = createRoot(dom.window.document.getElementById('root'));
  let epoch = 1;
  const render = async () => {
    await act(async () => root.render(React.createElement(ColleagueWorkspacePage, {
      colleagues: people, selectedColleagueId: colleague.id, chat: api, connectionEpoch: epoch,
      onSelectColleague() {}, onCreateSchedule() {}, onRenameColleague: async () => {},
    })));
    await flush();
  };
  await render();
  const find = (id) => dom.window.document.querySelector(`[data-testid="jiuwenmate-sharing-${id}"]`);
  return {
    state, calls, find, dom,
    async click(id) { assert.ok(find(id), `Missing sharing ${id}`); await act(async () => find(id).click()); await flush(); },
    async poll() { await act(async () => { for (const tick of [...timers.values()]) tick(); }); await flush(); },
    async reconnect() { epoch += 1; await render(); },
    async choose(id) {
      await act(async () => {
        const select = dom.window.document.querySelector('[data-testid="jiuwenmate-workspace-connection-select"]');
        select.value = id;
        select.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
      });
      await flush();
    },
    async close() {
      await act(async () => root.unmount());
      dom.window.close();
      globalThis.window = browser.window;
      globalThis.document = browser.window.document;
    },
  };
}

test('sharing RPCs use the owner config ID and fixed snake_case payloads through the existing request channel', async () => {
  const calls = [];
  const api = createJiuwenMateApi(async (method, params) => { calls.push({ method, params }); return snapshot(); });
  assert.ok(api.sharing, 'The existing client API must expose business sharing RPCs');
  assert.equal((await api.sharing.list('config-web')).conversation_id, 'sharing-approvals');
  for (const decision of ['once', 'permanent', 'reject']) await api.sharing.decide('config-web', 'approval-a', decision);
  await api.sharing.revoke('config-web', 'grant-a');
  assert.deepEqual(calls, [
    { method: 'jiuwenmate.sharing.list', params: { agent_id: 'config-web' } },
    ...['once', 'permanent', 'reject'].map((decision) => ({ method: 'jiuwenmate.sharing.decide', params: { agent_id: 'config-web', approval_id: 'approval-a', decision } })),
    { method: 'jiuwenmate.sharing.revoke', params: { agent_id: 'config-web', grant_id: 'grant-a' } },
  ]);
});

test('a never-used private chat discovers a durable inbox without exposing previews or invoking a model', async () => {
  const view = await renderWorkspace();
  try {
    assert.ok(view.calls.some((call) => call.method === 'jiuwenmate.sharing.list'));
    assert.ok(view.find('open'));
    assert.equal(view.find('card'), null);
    assert.doesNotMatch(view.dom.window.document.body.textContent, /Confidential|budget query|Requester Alice/);
    await view.click('open');
    assert.equal(view.find('card').dataset.variant, 'approval-a');
    assert.ok(view.find('agent-message'), 'The approval should appear as an Agent-authored message');
    assert.ok(view.find('agent-message').classList.contains('jiuwenmate-chat-message--colleague'));
    assert.ok(view.find('agent-message').querySelector('[data-testid="jiuwenmate-colleague-avatar"]'));
    assert.equal(view.find('owner-decision'), null);
    assert.ok(!view.find('grants'), 'Permanent authorizations belong on the colleague permissions page');
    for (const value of ['Source team', 'Requester Alice', 'Destination team', 'budget query', 'Confidential budget summary', 'all members']) {
      assert.ok(view.find('inbox').textContent.includes(value), value);
    }
    assert.equal(view.find('messages').open, false);
    await act(async () => { view.find('messages').open = true; });
    assert.ok(view.find('message').textContent.includes('<script>secret exact text</script>\nline two'));
    assert.equal(view.find('message').querySelector('script'), null);
    assert.equal(view.dom.window.document.querySelector('[data-testid="jiuwenmate-composer-input"]'), null);
    assert.ok(!view.calls.some((call) => call.method === 'jiuwenmate.chat.send' || call.method === 'jiuwenmate.permission.decide'));
  } finally { await view.close(); }
});

test('approval source explicitly identifies the platform independently of the destination', async () => {
  const view = await renderWorkspace();
  try {
    await view.click('open');
    assert.match(view.find('sources').textContent, /DingTalk · Source team/);
    assert.match(view.find('target').textContent, /Destination team/);
  } finally { await view.close(); }
});

test('background approval polling keeps cards, expanded content and controls steady', async () => {
  const view = await renderWorkspace();
  try {
    await view.click('open');
    const card = view.find('card');
    const messages = view.find('messages');
    await act(async () => { messages.open = true; });
    const pending = deferred();
    view.state.list = () => pending.promise;
    await view.poll();
    assert.equal(view.find('inbox').getAttribute('aria-busy'), 'false');
    assert.equal(view.find('once').disabled, false);
    assert.equal(view.find('messages'), messages);
    assert.equal(messages.open, true);
    await act(async () => pending.resolve(snapshot()));
    assert.equal(view.find('card'), card);
    assert.equal(view.find('messages').open, true);
  } finally { await view.close(); }
});

for (const decision of ['once', 'permanent', 'reject']) {
  test(`${decision} updates the persistent card and prevents duplicate decisions while in flight`, async () => {
    const view = await renderWorkspace();
    try {
      await view.click('open');
      const pending = deferred();
      view.state.decide = () => pending.promise;
      await view.click(decision);
      assert.equal(view.find('card').getAttribute('aria-busy'), 'true');
      for (const action of ['once', 'permanent', 'reject']) assert.equal(view.find(action).disabled, true);
      await view.click(decision);
      assert.equal(view.calls.filter((call) => call.method === 'jiuwenmate.sharing.decide').length, 1);
      await act(async () => pending.resolve(snapshot([{ ...approval, status: { once: 'approved_once', permanent: 'approved_permanent', reject: 'rejected' }[decision], can_decide: false }])));
      assert.equal(view.find('status').dataset.variant, { once: 'approved_once', permanent: 'approved_permanent', reject: 'rejected' }[decision]);
      assert.ok(!view.find('once'), 'A completed decision is recorded as a message and has no remaining decision controls');
      const decisionMessage = view.find('owner-decision');
      assert.ok(decisionMessage, 'A persisted decision should appear as a message from the owner');
      assert.equal(decisionMessage.dataset.variant, { once: 'approved_once', permanent: 'approved_permanent', reject: 'rejected' }[decision]);
      assert.ok(decisionMessage.classList.contains('jiuwenmate-chat-message--user'));
      assert.ok(!view.calls.some((call) => call.method === 'jiuwenmate.chat.send'));
    } finally { await view.close(); }
  });
}

test('permanent authorizations are not rendered in the approval conversation', async () => {
  const view = await renderWorkspace();
  try {
    await view.click('open');
    assert.ok(!view.find('grants'), 'The approval conversation must not contain the permanent authorization list');
    assert.ok(!view.find('grant-row'));
    assert.ok(!view.find('revoke'));
    assert.ok(!view.find('permanent-scope'));
  } finally { await view.close(); }
});

test('expired, denied and decided messages stay visible without decision controls', async () => {
  for (const fields of [
    { expires_at: '2020-01-01T00:00:00Z' }, { can_decide: false },
    { status: 'expired' }, { status: 'rejected' }, { status: 'approved_once' }, { status: 'approved_permanent' },
  ]) {
    const view = await renderWorkspace(snapshot([{ ...approval, ...fields }]));
    try {
      await view.click('open');
      for (const action of ['once', 'permanent', 'reject']) assert.ok(!view.find(action));
      assert.ok(view.find('expires'));
      if (fields.status === 'expired' || fields.expires_at === '2020-01-01T00:00:00Z') {
        assert.ok(!view.find('owner-decision'));
      } else if (fields.status === 'rejected' || fields.status === 'approved_once' || fields.status === 'approved_permanent') {
        assert.equal(view.find('owner-decision')?.dataset.variant, fields.status);
      }
    } finally { await view.close(); }
  }
});

test('polling discovers new requests, reconnect refreshes, and reopening restores records without normal chat history', async () => {
  const view = await renderWorkspace(snapshot([], []));
  try {
    await view.click('open');
    assert.ok(view.find('empty'));
    view.state.result = snapshot();
    await view.poll();
    assert.ok(view.find('card'));
    view.state.result = snapshot([{ ...approval, status: 'expired', can_decide: false }]);
    await view.reconnect();
    assert.equal(view.find('status').dataset.variant, 'expired');
  } finally { await view.close(); }
  const reopened = await renderWorkspace(snapshot([{ ...approval, status: 'approved_once', can_decide: false }]));
  try {
    await reopened.click('open');
    assert.equal(reopened.find('status').dataset.variant, 'approved_once');
    const decisionMessage = reopened.find('owner-decision');
    assert.ok(decisionMessage, 'A saved approval should be restored as an owner message after reopening');
    assert.equal(decisionMessage.dataset.variant, 'approved_once');
  } finally { await reopened.close(); }
});

test('list failures are recoverable and block stale actions; decision errors never claim approval', async () => {
  const view = await renderWorkspace();
  try {
    await view.click('open');
    view.state.list = async () => { throw new Error('offline'); };
    await view.poll();
    assert.ok(view.find('load-error'));
    assert.equal(view.find('once').disabled, true);
    view.state.list = null;
    await view.click('refresh');
    assert.equal(view.find('load-error'), null);
    view.state.decide = async () => { throw new Error('decision failed'); };
    await view.click('once');
    assert.ok(view.find('action-error'));
    assert.equal(view.find('status').dataset.variant, 'pending');
  } finally { await view.close(); }
});

test('IM workspaces never query or render sharing cards, and late Web results cannot leak there', async () => {
  const view = await renderWorkspace();
  try {
    await view.click('open');
    const pending = deferred();
    view.state.list = () => pending.promise;
    await view.poll();
    for (const runtime of ['runtime-feishu', 'runtime-dingtalk']) {
      await view.choose(runtime);
      const before = view.calls.filter((call) => call.method.startsWith('jiuwenmate.sharing.')).length;
      await view.poll();
      assert.equal(view.calls.filter((call) => call.method.startsWith('jiuwenmate.sharing.')).length, before);
      assert.equal(view.find('inbox'), null);
      assert.equal(view.find('open'), null);
      assert.doesNotMatch(view.dom.window.document.body.textContent, /Confidential|budget query|secret exact text/);
    }
    await act(async () => pending.resolve(snapshot()));
    assert.equal(view.find('card'), null);
  } finally { await view.close(); }
});

test('an invalid private conversation or foreign config snapshot fails closed', async () => {
  for (const result of [{ ...snapshot(), conversation_id: 'public' }, snapshot([{ ...approval, config_id: 'other-owner' }])]) {
    const view = await renderWorkspace(result);
    try {
      await view.click('open');
      assert.equal(view.find('card'), null);
      assert.ok(view.find('load-error'));
    } finally { await view.close(); }
  }
});

test('sharing UI has matching Chinese and English resources and renders translated actions', async () => {
  assert.ok(locales.en.translation.jiuwenmate.sharing);
  assert.deepEqual(Object.keys(locales.en.translation.jiuwenmate.sharing).sort(), Object.keys(locales.zh.translation.jiuwenmate.sharing).sort());
  await i18n.changeLanguage('zh');
  const view = await renderWorkspace();
  try {
    await view.click('open');
    assert.match(view.find('once').textContent, /一次/);
    assert.match(view.find('permanent').textContent, /永久/);
    assert.match(view.find('reject').textContent, /拒绝/);
    await view.click('once');
    assert.match(view.find('owner-decision').textContent, /批准本次跨群分享/);
    await act(async () => { await i18n.changeLanguage('en'); });
    assert.match(view.find('owner-decision').textContent, /approved this cross-group share once/);
    assert.doesNotMatch(view.find('inbox').textContent, /jiuwenmate\.sharing\./);
  } finally { await view.close(); await i18n.changeLanguage('en'); }
});

test('a backend-created private conversation opens approval cards even with zero ordinary Requests', async () => {
  const view = await renderWorkspace(snapshot(), [{
    conversation_id: 'sharing-approvals', title: 'Approvals', updated_at: '2026-10-27T00:00:00Z',
    last_message: 'Confidential budget summary',
  }]);
  try {
    assert.ok(view.find('card'));
    const record = view.dom.window.document.querySelector('[data-testid="jiuwenmate-conversation-record"]');
    assert.doesNotMatch(record.textContent, /Confidential/);
    assert.ok(!view.calls.some((call) => call.method === 'jiuwenmate.chat.history' && call.params.conversation_id === 'sharing-approvals'));
    assert.equal(view.dom.window.document.querySelector('[data-testid="jiuwenmate-composer-input"]'), null);
  } finally { await view.close(); }
});

test('polling cannot overwrite an in-flight decision, and a lost committed response is reconciled before retry', async () => {
  const view = await renderWorkspace();
  try {
    await view.click('open');
    const pending = deferred();
    view.state.decide = () => pending.promise;
    await view.click('permanent');
    const lists = view.calls.filter((call) => call.method === 'jiuwenmate.sharing.list').length;
    await view.poll();
    assert.equal(view.calls.filter((call) => call.method === 'jiuwenmate.sharing.list').length, lists);
    const requery = deferred();
    view.state.list = () => requery.promise;
    await act(async () => pending.reject(new Error('lost response')));
    assert.equal(view.find('once').disabled, true);
    await view.click('once');
    assert.equal(view.calls.filter((call) => call.method === 'jiuwenmate.sharing.decide').length, 1);
    await act(async () => requery.resolve(snapshot([{ ...approval, status: 'approved_permanent', can_decide: false }], [grant, { ...grant, grant_id: 'grant-b', source_group_id: 'source-b', source_group_name: 'Second source' }])));
    assert.equal(view.find('status').dataset.variant, 'approved_permanent');
    assert.ok(!view.find('once'));
    assert.equal(view.find('owner-decision')?.dataset.variant, 'approved_permanent');
    assert.ok(!view.find('grant-row'));
  } finally { await view.close(); }
});

test('deadlines expire locally even when the server keeps returning pending, including epoch timestamps', async () => {
  const originalNow = Date.now;
  Date.now = () => 1793059200000;
  const view = await renderWorkspace(snapshot([{ ...approval, expires_at: 1793059201 }]));
  try {
    await view.click('open');
    assert.equal(view.find('once').disabled, false);
    Date.now = () => 1793059202000;
    await view.poll();
    assert.equal(view.find('status').dataset.variant, 'expired');
    assert.ok(!view.find('once'));
    assert.ok(!view.calls.some((call) => call.method === 'jiuwenmate.sharing.decide'));
  } finally { Date.now = originalNow; await view.close(); }
});

test('late decision responses cannot repopulate an IM workspace', async () => {
  const view = await renderWorkspace();
  try {
    await view.click('open');
    const pending = deferred();
    view.state.decide = () => pending.promise;
    await view.click('once');
    await view.choose('runtime-feishu');
    await act(async () => pending.resolve(snapshot([{ ...approval, status: 'approved_once', can_decide: false }])));
    assert.equal(view.find('card'), null);
    assert.doesNotMatch(view.dom.window.document.body.textContent, /Confidential|budget query/);
  } finally { await view.close(); }
});

test('the global conversation poll discovers an unselected colleague inbox without a Request or a preview stream', async () => {
  const other = {
    ...colleague, id: 'config-other', configId: 'config-other', name: 'Other colleague',
    connections: [{ channel: 'web', connectionKey: 'web', agentId: 'config-other', workspaceDir: null }],
  };
  const view = await renderWorkspace(snapshot([], []), [], [colleague, other]);
  try {
    await view.choose('runtime-feishu');
    view.state.conversationsByAgent['config-other'] = [{
      conversation_id: 'sharing-approvals', title: 'Sharing approvals', last_message: null,
      updated_at: '2026-10-27T00:00:00Z',
    }];
    view.calls.length = 0;
    await view.poll();
    const records = view.dom.window.document.querySelector('[data-testid="jiuwenmate-conversation-record-list"]');
    assert.match(records.textContent, /Other colleague/);
    assert.match(records.textContent, /Cross-group sharing approvals/);
    assert.ok(view.calls.some((call) => call.method === 'jiuwenmate.conversation.list' && call.params.agent_id === 'config-other'));
    assert.ok(view.calls.some((call) => call.method === 'jiuwenmate.conversation.list' && call.params.agent_id === 'config-web'));
    assert.ok(!view.calls.some((call) => call.method.startsWith('jiuwenmate.sharing.')));
    assert.equal(view.find('card'), null);
    assert.doesNotMatch(view.dom.window.document.body.textContent, /Confidential|budget query|secret exact text/);
    assert.equal(view.dom.window.document.querySelector('[data-testid="jiuwenmate-workspace-connection-select"]').value, 'runtime-feishu');
  } finally { await view.close(); }
});

test('a pending card is discoverable before native bind, but becomes actionable only after can_decide is refreshed', async () => {
  const view = await renderWorkspace(snapshot([{ ...approval, can_decide: false }]));
  try {
    assert.match(view.find('pending').textContent, /1/);
    await view.click('open');
    assert.ok(!view.find('once'));
    assert.ok(!view.calls.some((call) => call.method === 'jiuwenmate.sharing.decide'));
    view.state.result = snapshot();
    await view.poll();
    assert.equal(view.find('once').disabled, false);
  } finally { await view.close(); }
});

test('repository owner views preserve the complete GroupMessageQuery and nullable message text across all sharing RPCs', async () => {
  const ownerView = snapshot([{
    ...approval,
    query: {
      query: '发布', since: '2026-10-01T00:00:00Z', until: '2026-10-27T00:00:00Z',
      sender_id: 'sender-a', cursor: 'next-page', limit: 25,
      group_ids: ['source-a', 'source-b'], group_names: ['研发群', '产品群'],
    },
    messages: [{ group_id: 'source-a', group_name: '研发群', message_id: 'message-0', sender_name: '张三', text: null }],
  }]);
  const api = createJiuwenMateApi(async () => structuredClone(ownerView));
  for (const received of [
    await api.sharing.list('config-web'),
    await api.sharing.decide('config-web', 'approval-a', 'once'),
    await api.sharing.revoke('config-web', 'grant-a'),
  ]) {
    assert.deepEqual(received.approvals[0].query, {
      query: '发布', since: '2026-10-01T00:00:00Z', until: '2026-10-27T00:00:00Z',
      sender_id: 'sender-a', cursor: 'next-page', limit: 25,
      group_ids: ['source-a', 'source-b'], group_names: ['研发群', '产品群'],
    });
    assert.equal(received.approvals[0].messages[0].text, null);
  }
});

test('the private card shows readable query criteria and a neutral placeholder for a null message body', async () => {
  const view = await renderWorkspace(snapshot([{
    ...approval,
    query: {
      query: '发布', since: '2026-10-01T00:00:00Z', until: '2026-10-27T00:00:00Z',
      sender_id: 'sender-a', cursor: 'next-page', limit: 25,
      group_ids: ['source-a', 'source-b'], group_names: ['研发群', '产品群'],
    },
    messages: [{ group_id: 'source-a', group_name: '研发群', message_id: 'message-0', sender_name: '张三', text: null }],
  }]));
  try {
    await view.click('open');
    assert.equal(view.find('load-error'), null);
    assert.ok(view.find('card'));
    const summary = view.find('query').textContent;
    for (const value of ['发布', '2026-10-01T00:00:00Z', '2026-10-27T00:00:00Z', 'sender-a', 'next-page', '25', 'source-a', 'source-b', '研发群', '产品群']) {
      assert.ok(summary.includes(value), `Missing query criterion: ${value}`);
    }
    assert.doesNotMatch(summary, /\[object Object\]/);
    assert.match(view.find('message-text').textContent, /No text content/);
    assert.equal(view.find('once').disabled, false);
    await view.click('once');
    assert.equal(view.find('status').dataset.variant, 'approved_once');
  } finally { await view.close(); }
});

test('a filters-only repository query and a null target app ID render with Chinese labels', async () => {
  await i18n.changeLanguage('zh');
  const view = await renderWorkspace(snapshot([{
    ...approval, query: { ...groupMessageQuery, query: null, sender_id: 'sender-only' },
    target: { ...target, app_id: null },
    messages: [{ ...approval.messages[0], text: null }],
  }], [{ ...grant, target: { ...target, app_id: null } }]));
  try {
    await view.click('open');
    assert.equal(view.find('load-error'), null);
    assert.match(view.find('query').textContent, /发送者.*sender-only/);
    assert.match(view.find('query').textContent, /50/);
    assert.match(view.find('message-text').textContent, /无文本正文/);
    assert.doesNotMatch(view.find('inbox').textContent, /\[object Object\]|jiuwenmate\.sharing\.|undefined|null/);
    assert.equal(view.find('once').disabled, false);
    assert.ok(!view.find('revoke'));
  } finally { await view.close(); await i18n.changeLanguage('en'); }
});


test('sharing RPCs reject legacy string or incomplete queries instead of accepting a different approval scope', async () => {
  for (const query of ['keywords only', { query: 'budget query' }, { ...groupMessageQuery, limit: 0 }]) {
    const api = createJiuwenMateApi(async () => snapshot([{ ...approval, query }]));
    await assert.rejects(api.sharing.list('config-web'), /Invalid private sharing snapshot/);
    await assert.rejects(api.sharing.decide('config-web', 'approval-a', 'once'), /Invalid private sharing snapshot/);
    await assert.rejects(api.sharing.revoke('config-web', 'grant-a'), /Invalid private sharing snapshot/);
  }
});

test('a late list response from before reconnect cannot restore pending over a newer decision', async () => {
  const view = await renderWorkspace();
  try {
    await view.click('open');
    const pending = deferred();
    view.state.list = () => pending.promise;
    await view.poll();
    view.state.list = null;
    view.state.result = snapshot([{ ...approval, status: 'rejected', can_decide: false }]);
    await view.reconnect();
    assert.equal(view.find('status').dataset.variant, 'rejected');
    await act(async () => pending.resolve(snapshot()));
    assert.equal(view.find('status').dataset.variant, 'rejected');
    assert.ok(!view.find('once'));
  } finally { await view.close(); }
});

test('owner workspace decisions address the configuration without a normal chat send', async () => {
  const view = await renderWorkspace();
  try {
    await view.click('open');
    await view.click('permanent');
    await view.poll();
    const sharingCalls = view.calls.filter((call) => call.method.startsWith('jiuwenmate.sharing.'));
    assert.ok(sharingCalls.some((call) => call.method.endsWith('.decide')));
    assert.ok(sharingCalls.every((call) => call.params.agent_id === 'config-web'));
    assert.ok(!view.calls.some((call) => ['jiuwenmate.chat.send', 'jiuwenmate.permission.decide'].includes(call.method)));
    assert.ok(!view.calls.some((call) => call.method === 'jiuwenmate.chat.history' && call.params.conversation_id === 'sharing-approvals'));
    assert.equal(view.find('owner-decision')?.dataset.variant, 'approved_permanent');
    assert.ok(!view.find('grant-row'));
  } finally { await view.close(); }
});

test('all nested sharing translations and interpolation fields match in Chinese and English', async () => {
  const leaves = (value, prefix = '') => Object.entries(value).flatMap(([key, child]) =>
    typeof child === 'string' ? [[`${prefix}${key}`, child]] : leaves(child, `${prefix}${key}.`));
  const en = Object.fromEntries(leaves(locales.en.translation.jiuwenmate.sharing));
  const zh = Object.fromEntries(leaves(locales.zh.translation.jiuwenmate.sharing));
  assert.deepEqual(Object.keys(en).sort(), Object.keys(zh).sort());
  for (const key of Object.keys(en)) {
    assert.ok(en[key].trim() && zh[key].trim(), key);
    assert.deepEqual([...en[key].matchAll(/\{\{(.*?)\}\}/g)].map((match) => match[1]).sort(),
      [...zh[key].matchAll(/\{\{(.*?)\}\}/g)].map((match) => match[1]).sort(), key);
  }
});

test('unread sharing conversation shows a dot and only opening the inbox marks it read', async () => {
  const view = await renderWorkspace(snapshot([], []), [{
    conversation_id: 'normal', title: 'Normal chat', updated_at: '2026-10-29T01:00:00Z',
  }, {
    conversation_id: 'sharing-approvals', title: '跨群分享审批', unread: true,
    updated_at: '2026-10-29T00:00:00Z',
  }]);
  try {
    assert.ok(view.dom.window.document.querySelector('[data-testid="jiuwenmate-sharing-unread"]'));
    assert.ok(!view.calls.some((call) => call.params.mark_read === true));
    Object.defineProperty(view.dom.window.document, 'visibilityState', { configurable: true, value: 'visible' });
    await act(async () => view.dom.window.document.querySelector('[data-testid="jiuwenmate-sharing-unread"]').closest('button').click());
    await flush();
    assert.ok(view.calls.some((call) => call.method === 'jiuwenmate.sharing.list' && call.params.mark_read === true), JSON.stringify(view.calls));
  } finally { await view.close(); }
});
