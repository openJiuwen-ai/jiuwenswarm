import assert from 'node:assert/strict';
import test from 'node:test';

import {
  JIUWENMATE_METHODS,
  JIUWENMATE_STREAM_EVENT,
  applyStreamEvent,
  chatPhase,
  createJiuwenMateApi,
  findModel,
  isPending,
  mergeChatRequest,
  formatSharedFileSize,
  latestSharedFiles,
  toActivity,
  toChannelMessage,
  toChatRequest,
  toColleague,
  toModelCatalog,
  upsertConversation,
  selectableColleagueResources,
  selectablePluginPackages,
  selectableKnowledgePages,
  toWireSettings,
  withColleagueEdit,
  withResourceSelection,
} from '../node_modules/.cache/jiuwenmate-api/jiuwenmateApi.js';

test('DingTalk authorization methods keep their RPC names, scope and response envelopes', async () => {
  const calls = [];
  const status = { status: 'connected', cli_version: '1.0.65', account: 'tester' };
  const api = createJiuwenMateApi(async (method, params) => {
    calls.push({ method, params });
    return method === 'jiuwenmate.dingtalk_auth.begin' ? { authorization: status } : { dingtalk_auth: status };
  });
  assert.deepEqual(await api.dingtalkAuthStatus('agent-1', 'ding-app'), status);
  assert.deepEqual(await api.beginDingTalkAuth('agent-1', 'ding-app'), status);
  assert.deepEqual(await api.pollDingTalkAuth('agent-1', 'ding-app', 'operation'), status);
  assert.deepEqual(await api.cancelDingTalkAuth('agent-1', 'ding-app', 'operation'), status);
  assert.deepEqual(await api.disconnectDingTalkAuth('agent-1', 'ding-app'), status);
  assert.deepEqual(calls.map(({ method }) => method), ['status', 'begin', 'poll', 'cancel', 'disconnect']
    .map((action) => `jiuwenmate.dingtalk_auth.${action}`));
  assert.ok(calls.every(({ params }) => params.agent_id === 'agent-1' && params.connection_key === 'ding-app'));
  assert.equal(calls[2].params.operation_id, 'operation');
  assert.equal(calls[3].params.operation_id, 'operation');
});

const wireColleague = {
  agent_id: 'agent-1',
  name: '小九',
  description: '通用办公助理',
  persona: '你是小九',
  tags: ['效率提升'],
  category: 'custom',
  avatar_url: '',
  capabilities: {
    model: null,
    skills: ['report-writer'],
    connectors: [],
    plugins: [],
    knowledge: [],
    starter_prompts: ['帮我整理本周工作进展。'],
  },
  is_default: true,
  current_version: 'v1',
  has_unpublished_changes: false,
  enabled: true,
  workspace_dir: '/data/jiuwenmate/workspaces/local/agent-1',
  created_at: '2026-10-14T08:00:00+00:00',
  updated_at: '2026-10-14T08:00:00+00:00',
  im_configured: true,
};

const wireCatalog = {
  models: [
    { selection_key: 'deepseek#0', display_name: 'DeepSeek', model_name: 'deepseek-chat', provider: 'DeepSeek', is_default: true },
    { selection_key: 'kimi#1', display_name: '', model_name: 'kimi-k2', provider: 'Moonshot', is_default: false },
  ],
  current_selection: 'deepseek#0',
  current_display_name: 'DeepSeek',
};

const draft = {
  name: '  品牌顾问 ',
  description: '提供品牌定位建议。',
  tags: ['效率提升', ' '],
  category: 'content',
  avatarUrl: 'data:image/png;base64,AAAA',
  capabilities: {
    model: 'kimi#1',
    skills: ['report-writer'],
    connectors: [],
    plugins: [],
    knowledge: [],
    starterPrompts: ['写一份周报', ''],
  },
};

function recorder(responses) {
  const calls = [];
  const request = async (method, params) => {
    calls.push([method, params]);
    if (!(method in responses)) throw new Error(`unexpected ${method}`);
    const response = responses[method];
    if (response instanceof Error) throw response;
    return response;
  };
  return { calls, api: createJiuwenMateApi(request) };
}

test('DingTalk binding preserves an omitted robot code and sends an explicit clear', async () => {
  const { calls, api } = recorder({ 'channel.dingtalk.bind_agent': {} });
  const colleague = toColleague(wireColleague);

  await api.bindConnection(colleague, 'dingtalk', { appId: 'ding-app', appSecret: 'new-secret' });
  await api.bindConnection(colleague, 'dingtalk', { appId: 'ding-app', appSecret: 'new-secret', robotCode: '' });

  assert.deepEqual(calls, [
    ['channel.dingtalk.bind_agent', {
      config_id: colleague.configId,
      client_id: 'ding-app',
      client_secret: 'new-secret',
    }],
    ['channel.dingtalk.bind_agent', {
      config_id: colleague.configId,
      client_id: 'ding-app',
      client_secret: 'new-secret',
      robot_code: '',
    }],
  ]);
});

test('server colleagues map to page colleagues with the persisted enabled state', () => {
  const colleague = toColleague(wireColleague);
  assert.equal(colleague.id, 'agent-1');
  assert.equal(colleague.persona, '你是小九');
  assert.equal(colleague.capabilities.model, '');
  assert.deepEqual(colleague.capabilities.starterPrompts, ['帮我整理本周工作进展。']);
  assert.equal(colleague.avatarUrl, undefined);
  assert.equal(colleague.joinedAt, wireColleague.created_at);
  assert.equal(colleague.isDefault, true);
  assert.equal(colleague.currentVersion, 'v1');
  assert.equal(colleague.workspaceDir, wireColleague.workspace_dir);
  assert.equal(toColleague({ ...wireColleague, workspace_dir: undefined }).workspaceDir, null);
  assert.equal(colleague.imConfigured, true);
  assert.equal(colleague.disabled, false);
  assert.equal(colleague.permissionProfile, 'default');
  assert.equal(toColleague({ ...wireColleague, enabled: false }).disabled, true);
  assert.equal(toColleague({ ...wireColleague, category: 'unknown' }).category, 'custom');
});

test('the model catalog keeps selection keys and falls back to the model name', () => {
  const catalog = toModelCatalog(wireCatalog);
  assert.deepEqual(catalog.models, [
    { id: 'deepseek#0', label: 'DeepSeek', provider: 'DeepSeek' },
    { id: 'kimi#1', label: 'kimi-k2', provider: 'Moonshot' },
  ]);
  assert.equal(findModel(catalog, '')?.id, 'deepseek#0');
  assert.equal(findModel(catalog, 'missing'), null);
});

test('draft settings trim blanks, drop uploaded images and keep only listed models', () => {
  const { models } = toModelCatalog(wireCatalog);
  const settings = toWireSettings(draft, models);
  assert.equal(settings.name, '品牌顾问');
  assert.deepEqual(settings.tags, ['效率提升']);
  assert.equal(settings.avatar_url, '');
  assert.equal(settings.capabilities.model, 'kimi#1');
  assert.deepEqual(settings.capabilities.starter_prompts, ['写一份周报']);
  assert.equal('persona' in settings, false);
  assert.equal(withColleagueEdit(toColleague(wireColleague), models, { persona: '新的人设' }).persona, '新的人设');

  const unlisted = toWireSettings({ ...draft, capabilities: { ...draft.capabilities, model: 'deepseek-v3.2' } }, models);
  assert.equal(unlisted.capabilities.model, null);
  assert.equal(toWireSettings({ ...draft, avatarUrl: '/assets/bee.png' }, models).avatar_url, '/assets/bee.png');
  assert.equal(toWireSettings({ ...draft, avatarUrl: undefined, avatarHue: 110 }, models).avatar_url, 'hue:110');
  assert.equal(toWireSettings({ ...draft, avatarHue: 110 }, models).avatar_url, 'hue:110');
  const tinted = toColleague({ ...wireColleague, is_default: false, avatar_url: 'hue:110' });
  assert.equal(tinted.avatarHue, 110);
  assert.equal(tinted.avatarUrl, undefined);
});

test('loading colleagues ensures the default colleague before listing', async () => {
  const { calls, api } = recorder({
    [JIUWENMATE_METHODS.ensureDefault]: { colleague: wireColleague },
    [JIUWENMATE_METHODS.list]: { colleagues: [wireColleague] },
  });
  const colleagues = await api.loadColleagues();
  assert.deepEqual(
    calls.map(([method]) => method),
    [JIUWENMATE_METHODS.ensureDefault, JIUWENMATE_METHODS.list],
  );
  assert.deepEqual(
    colleagues.map((item) => item.name),
    ['小九'],
  );
});

test('create, enable, delete and publish send agent ids and settings without a namespace', async () => {
  const { calls, api } = recorder({
    [JIUWENMATE_METHODS.create]: { colleague: { ...wireColleague, agent_id: 'agent-2', is_default: false } },
    [JIUWENMATE_METHODS.delete]: { deleted: true },
    [JIUWENMATE_METHODS.setEnabled]: { colleague: { ...wireColleague, enabled: false } },
    [JIUWENMATE_METHODS.publish]: {
      version: { agent_id: 'agent-2', config_version: 'v2', published_at: '2026-10-14T09:00:00+00:00' },
    },
  });
  const created = await api.createColleague(draft, toModelCatalog(wireCatalog).models);
  assert.equal(created.id, 'agent-2');
  assert.equal(calls[0][1].settings.capabilities.model, 'kimi#1');

  assert.equal((await api.publish('agent-2')).configVersion, 'v2');
  assert.equal((await api.setColleagueEnabled('agent-2', false)).disabled, true);
  await api.deleteColleague('agent-2', 'unbind');
  assert.deepEqual(calls.slice(1), [
    [JIUWENMATE_METHODS.publish, { agent_id: 'agent-2' }],
    [JIUWENMATE_METHODS.setEnabled, { agent_id: 'agent-2', enabled: false }],
    [JIUWENMATE_METHODS.delete, { agent_id: 'agent-2', mode: 'unbind' }],
  ]);
  assert.equal(
    calls.some(([, params]) => params && 'namespace_id' in params),
    false,
  );
});

test('Agent Mail requests are scoped to one employee and expose authorization lifecycle', async () => {
  const { calls, api } = recorder({
    [JIUWENMATE_METHODS.agentMailStatus]: { agent_mail: { status: 'not_connected', cli_version: '1.0.18' } },
    [JIUWENMATE_METHODS.agentMailAuthBegin]: {
      authorization: { status: 'authorizing', operation_id: 'op-1', auth_url: 'https://agent.qq.com/auth' },
    },
    [JIUWENMATE_METHODS.agentMailAuthPoll]: {
      agent_mail: { status: 'connected', cli_version: '1.0.18', email: 'employee@example.com' },
    },
    [JIUWENMATE_METHODS.agentMailAuthCancel]: { agent_mail: { status: 'not_connected', cli_version: '1.0.18' } },
  });

  assert.equal((await api.agentMailStatus('agent-1')).status, 'not_connected');
  assert.equal((await api.beginAgentMailAuth('agent-1')).operation_id, 'op-1');
  assert.deepEqual(await api.pollAgentMailAuth('agent-1', 'op-1'), {
    status: 'connected', cli_version: '1.0.18', email: 'employee@example.com',
  });
  assert.equal((await api.cancelAgentMailAuth('agent-1', 'op-1')).status, 'not_connected');
  assert.deepEqual(calls, [
    [JIUWENMATE_METHODS.agentMailStatus, { agent_id: 'agent-1' }],
    [JIUWENMATE_METHODS.agentMailAuthBegin, { agent_id: 'agent-1' }],
    [JIUWENMATE_METHODS.agentMailAuthPoll, { agent_id: 'agent-1', operation_id: 'op-1' }],
    [JIUWENMATE_METHODS.agentMailAuthCancel, { agent_id: 'agent-1', operation_id: 'op-1' }],
  ]);
});

test('server refusals reach the caller unchanged', async () => {
  const refusal = Object.assign(new Error('the default colleague cannot be deleted'), { code: 'BAD_REQUEST' });
  const { api } = recorder({ [JIUWENMATE_METHODS.delete]: refusal });
  await assert.rejects(api.deleteColleague('agent-1', 'purge'), (error) => error === refusal);
});

const wireRequest = {
  request_id: 'req-1',
  session_id: 'sess-1',
  agent_id: 'agent-1',
  message_id: 'msg-1',
  text: '今天进展',
  received_at: '2026-10-14T09:00:00+00:00',
  status: 'ended',
  outcome: 'completed',
  running: false,
  replies: ['进展如下'],
  reason: null,
  updated_at: '2026-10-14T09:00:05+00:00',
};

test('a request shows its recorded outcome, or why it has none yet', () => {
  const phase = (changes) => chatPhase(toChatRequest({ ...wireRequest, ...changes }));
  assert.equal(phase({}), 'completed');
  assert.equal(phase({ status: 'queued', outcome: null }), 'queued');
  assert.equal(phase({ status: 'dispatched', outcome: null, running: true }), 'running');
  assert.equal(phase({ status: 'dispatched', outcome: null, running: false }), 'interrupted');
  assert.equal(phase({ outcome: 'stopped' }), 'stopped');
  assert.equal(phase({ outcome: 'unknown' }), 'failed');

  assert.equal(isPending(toChatRequest({ ...wireRequest, status: 'queued', outcome: null })), true);
  assert.equal(isPending(toChatRequest({ ...wireRequest, status: 'dispatched', outcome: null })), false);
  assert.equal(isPending(toChatRequest(wireRequest)), false);
});

test('a newer request state replaces the old one in place', () => {
  const first = toChatRequest({ ...wireRequest, status: 'queued', outcome: null, replies: [] });
  const second = toChatRequest({ ...wireRequest, request_id: 'req-2' });
  const list = mergeChatRequest(mergeChatRequest([], first), second);
  const updated = mergeChatRequest(list, toChatRequest(wireRequest));
  assert.deepEqual(
    updated.map((item) => [item.requestId, item.replies]),
    [
      ['req-1', ['进展如下']],
      ['req-2', ['进展如下']],
    ],
  );
});

const wireConversation = {
  agent_id: 'agent-1',
  conversation_id: 'conv-1',
  title: '今天进展',
  last_message: '继续查看昨天的进展',
  created_at: '2026-10-14T09:00:00+00:00',
  updated_at: '2026-10-14T09:00:00+00:00',
};

test('chat calls send the agent, conversation, text and message id and read requests back', async () => {
  const { calls, api } = recorder({
    [JIUWENMATE_METHODS.chatSend]: {
      request: { ...wireRequest, conversation_id: 'conv-1', status: 'queued', outcome: null, replies: [] },
      conversation: wireConversation,
    },
    [JIUWENMATE_METHODS.chatHistory]: { requests: [wireRequest] },
    [JIUWENMATE_METHODS.requestGet]: { request: wireRequest },
  });
  const sent = await api.sendMessage('agent-1', 'conv-1', '今天进展', 'msg-1');
  assert.equal(sent.request.status, 'queued');
  assert.equal(sent.request.conversationId, 'conv-1');
  assert.deepEqual(sent.request.replies, []);
  assert.deepEqual(sent.conversation, {
    id: 'conv-1',
    title: '今天进展',
    updatedAt: '2026-10-14T09:00:00+00:00',
    lastMessage: '继续查看昨天的进展',
  });
  assert.deepEqual((await api.history('agent-1', 'conv-1')).map((item) => item.text), ['今天进展']);
  assert.deepEqual((await api.getRequest('req-1')).replies, ['进展如下']);
  assert.deepEqual(calls, [
    [JIUWENMATE_METHODS.chatSend, { agent_id: 'agent-1', conversation_id: 'conv-1', text: '今天进展', message_id: 'msg-1' }],
    [JIUWENMATE_METHODS.chatHistory, { agent_id: 'agent-1', conversation_id: 'conv-1' }],
    [JIUWENMATE_METHODS.requestGet, { request_id: 'req-1' }],
  ]);
});

test('a stop or cancel sends the request id and reason and reads the control back', async () => {
  const stopped = {
    ...wireRequest,
    status: 'dispatched',
    outcome: null,
    running: true,
    control: { request_id: 'req-1', reason: 'stop', status: 'accepted', detail: null },
  };
  const { calls, api } = recorder({ [JIUWENMATE_METHODS.requestAbort]: { request: stopped } });

  const controlled = await api.abortRequest('req-1', 'stop');

  assert.deepEqual(controlled.control, { reason: 'stop', status: 'accepted', detail: null });
  assert.deepEqual(calls, [[JIUWENMATE_METHODS.requestAbort, { request_id: 'req-1', reason: 'stop' }]]);
  assert.equal(toChatRequest(wireRequest).control, null);
  const failed = toChatRequest({ ...wireRequest, control: { reason: 'cancel', status: 'failed', detail: '中断失败' } });
  assert.deepEqual(failed.control, { reason: 'cancel', status: 'failed', detail: '中断失败' });
  assert.equal(toChatRequest({ ...wireRequest, control: { reason: 'pause' } }).control, null);
});

test('a message closed after a restart carries how it was judged', () => {
  const recovered = toChatRequest({
    ...wireRequest,
    outcome: 'paused',
    recovery: {
      request_id: 'req-1',
      execution_id: 'exe-1',
      decision: 'awaiting_continuation',
      outcome: 'paused',
      unverified_tool_calls: ['call-1'],
      decided_at: '2026-10-15T09:00:00+00:00',
    },
  });

  assert.deepEqual(recovered.recovery, { decision: 'awaiting_continuation', unverifiedToolCalls: ['call-1'] });
  assert.equal(toChatRequest(wireRequest).recovery, null);
});

test('a handed-off file becomes a download card and ignores foreign links', () => {
  const request = toChatRequest({
    ...wireRequest,
    files: [
      {
        output_id: 'out-file',
        name: '周报.md',
        path: 'reports/周报.md',
        size: 1280,
        caption: '本周周报',
        download_url: '/file-api/download?token=abc',
      },
      {
        output_id: 'out-bad',
        name: 'secret.md',
        size: 8,
        download_url: 'https://example.invalid/secret.md',
      },
      { name: 'missing-id.md', size: 1 },
    ],
  });

  assert.deepEqual(request.files, [
    {
      outputId: 'out-file',
      name: '周报.md',
      path: 'reports/周报.md',
      size: 1280,
      caption: '本周周报',
      downloadUrl: '/file-api/download?token=abc',
    },
    {
      outputId: 'out-bad',
      name: 'secret.md',
      path: null,
      size: 8,
      caption: null,
      downloadUrl: null,
    },
  ]);
  assert.deepEqual(toChatRequest(wireRequest).files, []);
  assert.equal(formatSharedFileSize(512), '512 B');
  assert.equal(formatSharedFileSize(1280), '1.3 KB');
  assert.equal(formatSharedFileSize(2 * 1024 * 1024), '2 MB');
  assert.equal(toChatRequest({ ...wireRequest, recovery: { decision: 'rerun' } }).recovery, null);
});

test('conversation calls list, rename and delete by agent and conversation id', async () => {
  const { calls, api } = recorder({
    [JIUWENMATE_METHODS.conversationList]: { conversations: [wireConversation, { ...wireConversation, conversation_id: 'conv-0' }] },
    [JIUWENMATE_METHODS.conversationRename]: { conversation: { ...wireConversation, title: '周报' } },
    [JIUWENMATE_METHODS.conversationPin]: { conversation: { ...wireConversation, pinned: true } },
    [JIUWENMATE_METHODS.conversationDelete]: { deleted: true },
  });
  const conversations = await api.listConversations('agent-1');
  assert.deepEqual(conversations.map((item) => item.id), ['conv-1', 'conv-0']);
  assert.equal(conversations[0].lastMessage, '继续查看昨天的进展');
  assert.equal((await api.renameConversation('agent-1', 'conv-1', '周报')).title, '周报');
  assert.equal((await api.setConversationPinned('agent-1', 'conv-1', true)).pinned, true);
  await api.deleteConversation('agent-1', 'conv-1');
  assert.deepEqual(calls, [
    [JIUWENMATE_METHODS.conversationList, { agent_id: 'agent-1' }],
    [JIUWENMATE_METHODS.conversationRename, { agent_id: 'agent-1', conversation_id: 'conv-1', title: '周报' }],
    [JIUWENMATE_METHODS.conversationPin, { agent_id: 'agent-1', conversation_id: 'conv-1', pinned: true }],
    [JIUWENMATE_METHODS.conversationDelete, { agent_id: 'agent-1', conversation_id: 'conv-1' }],
  ]);
});

test('a conversation that received a message moves to the top once', () => {
  const list = [
    { id: 'conv-2', title: '乙', updatedAt: '2' },
    { id: 'conv-1', title: '甲', updatedAt: '1' },
  ];
  const moved = upsertConversation(list, { id: 'conv-1', title: '甲', updatedAt: '3' });
  assert.deepEqual(moved.map((item) => [item.id, item.updatedAt]), [['conv-1', '3'], ['conv-2', '2']]);
  assert.deepEqual(upsertConversation(list, { id: 'conv-3', title: '丙', updatedAt: '4' }).map((item) => item.id), ['conv-3', 'conv-2', 'conv-1']);
});

const running = { ...wireRequest, status: 'dispatched', outcome: null, running: true, replies: [] };

function step(kind, seq, text = '', changes = {}) {
  return { kind, agentId: 'agent-1', requestId: 'req-1', executionId: 'exe-1', seq, text, ...changes };
}

function replay(list, ...steps) {
  return steps.reduce((current, item) => applyStreamEvent(current, item), list);
}

test('pushed fragments build the draft of the current model call', () => {
  const list = [toChatRequest(running)];
  const after = replay(list, step('delta', 1, '先查'), step('delta', 2, '一下'), step('segment', 3));
  assert.deepEqual(after[0].draft, { executionId: 'exe-1', seq: 3, text: '先查一下', closed: true });

  const answer = replay(after, step('delta', 5, '**进展**'), step('delta', 6, '如下'));
  assert.deepEqual(answer[0].draft, { executionId: 'exe-1', seq: 6, text: '**进展**如下', closed: false });
  assert.equal(replay(answer, step('delta', 6, '重复'))[0].draft.text, '**进展**如下');
  assert.equal(replay(answer, step('delta', 1, '新执行', { executionId: 'exe-2' }))[0].draft.text, '新执行');
});

test('a step of an unlisted request asks for a query, and the end leaves the draft for it', () => {
  const list = [toChatRequest(running)];
  assert.equal(applyStreamEvent(list, step('delta', 1, '进展', { requestId: 'req-9' })), null);
  assert.equal(applyStreamEvent(list, step('ended', 4)), list);
  const ended = [toChatRequest(wireRequest)];
  assert.equal(applyStreamEvent(ended, step('delta', 9, '迟到')), ended);
});

test('a query older than the pushed draft keeps the draft until the request ends', () => {
  const pushed = replay([toChatRequest(running)], step('delta', 1, '进展'), step('delta', 2, '如下'));
  const stale = toChatRequest({ ...running, draft: { execution_id: 'exe-1', seq: 1, text: '进展', closed: false } });
  assert.equal(mergeChatRequest(pushed, stale)[0].draft.text, '进展如下');
  assert.equal(mergeChatRequest(pushed, toChatRequest(running))[0].draft.text, '进展如下');
  const newer = toChatRequest({ ...running, draft: { execution_id: 'exe-1', seq: 5, text: '进展如下。', closed: true } });
  assert.equal(mergeChatRequest(pushed, newer)[0].draft.seq, 5);
  const done = mergeChatRequest(pushed, toChatRequest(wireRequest));
  assert.equal(done[0].draft, null);
  assert.deepEqual(done[0].replies, ['进展如下']);
});

test('stream subscription delivers only well-formed steps', () => {
  const handlers = new Map();
  const subscribe = (event, handler) => {
    handlers.set(event, handler);
    return () => handlers.delete(event);
  };
  const api = createJiuwenMateApi(async () => ({}), subscribe);
  const seen = [];
  const unsubscribe = api.onStream((event) => seen.push(event));
  const push = handlers.get(JIUWENMATE_STREAM_EVENT);
  push({ kind: 'delta', agent_id: 'agent-1', request_id: 'req-1', execution_id: 'exe-1', seq: 2, text: '进展' });
  push({ kind: 'other', agent_id: 'agent-1', request_id: 'req-1', execution_id: 'exe-1', seq: 3 });
  push({ kind: 'delta', agent_id: 'agent-1', request_id: 'req-1', execution_id: 'exe-1' });
  assert.deepEqual(seen, [step('delta', 2, '进展')]);
  unsubscribe();
  assert.equal(handlers.size, 0);
  assert.doesNotThrow(() => createJiuwenMateApi(async () => ({})).onStream(() => undefined)());
});

test('activity payload keeps counts and drops a member without an id', () => {
  const activity = toActivity({
    generated_at: '2026-10-16T00:00:00+00:00',
    members: [
      {
        agent_id: 'mate_a',
        calls: 2,
        tokens: 65,
        tasks: 2,
        replies: 1,
        failures: 1,
        companion_days: 16,
        last_response_at: '2026-10-16T10:00:00+00:00',
        days: [{ date: '2026-10-16', tasks: 2, minutes: 90, calls: 2, tokens: 65 }],
        hours: [{ date: '2026-10-16', hour: 10, minutes: 0, tasks: 1, tokens: 65 }],
        channels: [{ channel: 'web', calls: 2, tokens: 65, tasks: 2, replies: 1, failures: 1 }],
        groups: [],
        kinds: [{ date: '2026-10-16', conversation: 1, channel: 0, schedule: 1 }],
        history: [
          {
            request_id: 'req_plan',
            title: '喝水提醒',
            kind: 'schedule',
            channel: 'web',
            status: 'completed',
            tokens: 12,
            updated_at: '2026-10-16T03:01:00+00:00',
            started_at: '2026-10-16T03:00:00+00:00',
          },
        ],
      },
      { calls: 9 },
    ],
  });
  assert.equal(activity.members.length, 1);
  assert.equal(activity.members[0].tokens, 65);
  assert.equal(activity.members[0].replies, 1);
  assert.equal(activity.members[0].failures, 1);
  assert.deepEqual(activity.members[0].hours, [{ date: '2026-10-16', hour: 10, minutes: 0, tasks: 1, tokens: 65 }]);
  assert.deepEqual(activity.members[0].kinds, [{ date: '2026-10-16', conversation: 1, channel: 0, schedule: 1 }]);
  assert.deepEqual(activity.members[0].history, [
    {
      requestId: 'req_plan',
      title: '喝水提醒',
      kind: 'schedule',
      channel: 'web',
      status: 'completed',
      tokens: 12,
      updatedAt: '2026-10-16T03:01:00+00:00',
      startedAt: '2026-10-16T03:00:00+00:00',
    },
  ]);
});

test('installed skill and connected MCP names are the only selectable colleague resources', () => {
  const selected = selectableColleagueResources(
    {
      skills: [
        { name: 'report-writer', display_name: '报告', description: '整理周报', installed: true, enabled: true, skill_type: 'skill' },
        { name: 'disabled-skill', installed: true, enabled: false, skill_type: 'skill' },
        { name: 'mcp-skill', source: 'mcp', installed: true, skill_type: 'skill' },
        { name: 'workflow-pack', installed: true, skill_type: 'skillpack' },
        { name: 'not-installed', installed: false, skill_type: 'skill' },
      ],
    },
    {
      items: [
        { name: 'feishu', display_name: '飞书', connection_state: 'connected' },
        { name: 'idle', display_name: '未连接', connection_state: 'disconnected' },
        { name: 'feishu', display_name: '飞书重复', connection_state: 'connected' },
      ],
    },
  );
  assert.deepEqual(selected.skills, [{ name: 'report-writer', label: '报告', description: '整理周报', skillType: 'skill' }]);
  assert.deepEqual(selected.connectors, [{ name: 'feishu', label: '飞书', description: '' }]);
  assert.deepEqual(selected.plugins, []);
  assert.deepEqual(
    selectablePluginPackages({
      packages: [
        {
          id: '440c3c6e0e654b1eb9ec32c17bcd2923',
          packageName: 'office-document-toolkit',
          installed: true,
          displayName: { zh: '办公文档能力包', en: 'Office Document Toolkit' },
          displayDescription: { zh: '整理办公文档', en: 'Office documents' },
        },
        {
          id: 'coding-guard',
          installed: true,
          displayName: { zh: '编程防护', en: 'Coding Guard' },
          displayDescription: { zh: '审查代码', en: '' },
        },
        { id: 'skill-creator', installed: false, displayName: { zh: '技能创建', en: 'Skill creator' } },
        { id: '', installed: true, displayName: { zh: '空', en: '' } },
      ],
    }),
    [
      {
        name: 'office-document-toolkit',
        label: '办公文档能力包',
        description: '整理办公文档',
        alias: '440c3c6e0e654b1eb9ec32c17bcd2923',
      },
      { name: 'coding-guard', label: '编程防护', description: '审查代码' },
    ],
  );
});

test('published personal-context pages are the selectable employee knowledge', () => {
  assert.deepEqual(
    selectableKnowledgePages({
      pages: [
        { name: 'topics/description.md', label: 'topics/description.md', source: '知乎专栏', updated_at: '2026-08-20 16:38:24' },
        { name: '', label: '空' },
        { name: 'topics/description.md', label: '重复' },
      ],
    }),
    [{ name: 'topics/description.md', label: 'topics/description.md', description: '', source: '知乎专栏', updatedAt: '2026-08-20 16:38:24' }],
  );
});

test('resource selection keeps the colleague settings other than skills and connectors', () => {
  const colleague = toColleague(wireColleague);
  const settings = withResourceSelection(colleague, toModelCatalog(wireCatalog).models, ['report-writer'], ['feishu']);
  assert.deepEqual(settings.capabilities.skills, ['report-writer']);
  assert.deepEqual(settings.capabilities.connectors, ['feishu']);
  assert.deepEqual(settings.capabilities.plugins, []);
  assert.deepEqual(settings.capabilities.knowledge, []);
  assert.equal(settings.name, '小九');
  assert.deepEqual(settings.capabilities.starter_prompts, ['帮我整理本周工作进展。']);
});

test('channel records map public messages and execution trace rows', async () => {
  const calls = [];
  const api = createJiuwenMateApi(async (method, params) => {
    calls.push({ method, params });
    if (method === 'jiuwenmate.channel.list') {
      return { channels: [{ id: 'grp_1', name: '产品群', platform: 'feishu', artifacts: 1, token_usage: 12, last_response_minutes: 3 }] };
    }
    if (method === 'jiuwenmate.channel.messages') {
      return { messages: [{ id: 'm1', sender: 'Alice', sender_kind: 'human', text: '', status: 'recalled', attachments: [], received_at: '2026-10-17T08:00:00+00:00' }], status: 'found', next_cursor: null };
    }
    return {
      started_at: '2026-10-17T08:00:00+00:00',
      rows: [{ id: 'e1', trace_id: 'e1', turn: 1, role: 'tool', lane: 'tools', content: 'write_file', start_ms: 0, duration_ms: 4, input_tokens: 0, output_tokens: 0 }],
    };
  });
  const [channel] = await api.listChannels('mate_1');
  assert.equal(channel.name, '产品群');
  assert.equal(channel.tokenUsage, 12);
  const messages = await api.channelMessages('mate_1', 'grp_1');
  assert.equal(messages.messages[0].status, 'recalled');
  assert.equal(messages.messages[0].fromColleague, false);
  assert.equal(toChannelMessage({ id: 'm2', sender: '小九', sender_kind: 'agent', own: true, text: '我来处理' }).fromColleague, true);
  assert.equal(toChannelMessage({ id: 'm3', sender: '小八', sender_kind: 'agent', own: false, text: '我是另一位' }).fromColleague, false);
  const trace = await api.channelTrace('mate_1', 'grp_1');
  assert.equal(trace.rows[0].role, 'tool');
  assert.equal(trace.rows[0].lane, 'tools');
  assert.deepEqual(calls.map((item) => item.method), [
    'jiuwenmate.channel.list',
    'jiuwenmate.channel.messages',
    'jiuwenmate.channel.trace',
  ]);
});

test('schedule API preserves interval, validity, completed state and real run history', async () => {
  const calls = [];
  const wire = {
    plan_id: 'p1', agent_id: 'a1', name: '日报', prompt: '整理', cron_expr: '',
    timezone: 'Asia/Shanghai', schedule_kind: 'interval', interval_seconds: 3600,
    run_at: null, starts_at: '2026-10-18T00:00:00Z', ends_at: '2026-10-19T00:00:00Z',
    enabled: false, next_fire_at: null, conversation_id: 'schedule:p1', run_count: 2, last_status: 'completed',
  };
  const api = createJiuwenMateApi(async (method, params) => {
    calls.push({ method, params });
    if (method === 'jiuwenmate.schedule.list') return { plans: [wire] };
    if (method === 'jiuwenmate.schedule.history') return { plan: wire, runs: [
      { request_id: 'r1', scheduled_for: '2026-10-18T01:00:00Z', status: 'failed', description: '连接超时' },
    ] };
    if (method === 'jiuwenmate.schedule.run') return { request_id: 'r2' };
    return { plan: wire };
  });
  const [plan] = await api.listSchedules();
  assert.deepEqual(calls[0].params, {});
  assert.equal(plan.scheduleKind, 'interval');
  assert.equal(plan.intervalSeconds, 3600);
  assert.equal(plan.nextFireAt, null);
  assert.equal(plan.lastStatus, 'completed');
  assert.equal(plan.runCount, 2);
  assert.equal(plan.conversationId, 'schedule:p1');
  await api.updateSchedule('p1', plan);
  assert.equal(calls[1].params.starts_at, wire.starts_at);
  assert.equal(calls[1].params.interval_seconds, 3600);
  assert.equal(calls[1].params.schedule_kind, 'interval');
  assert.equal(await api.runSchedule('p1'), 'r2');
  const history = await api.scheduleHistory('p1');
  assert.equal(history.runs[0].status, 'failed');
  assert.equal(history.runs[0].description, '连接超时');
  assert.equal(history.plan.runCount, 2);
});

test('group reply mode survives edits and defaults to adaptive for existing colleagues', () => {
  assert.equal(toColleague(wireColleague).groupReplyMode, 'adaptive');
  const colleague = toColleague({ ...wireColleague, group_reply_mode: 'passive' });
  assert.equal(colleague.groupReplyMode, 'passive');
  assert.equal(withColleagueEdit(colleague, [], { name: 'Renamed' }).group_reply_mode, 'passive');
  assert.equal(withColleagueEdit(colleague, [], { groupReplyMode: 'adaptive' }).group_reply_mode, 'adaptive');
  assert.equal(withColleagueEdit(colleague, [], { permissionProfile: 'automatic' }).permission_profile, 'automatic');
  assert.equal(withColleagueEdit(toColleague({ ...wireColleague, permission_profile: 'full_access' }), [], { name: 'Renamed' }).permission_profile, 'full_access');
});

test('permission approval is mapped and one decision targets its original conversation', async () => {
  const approval = {
    approval_id: 'a1b2c3d4e5f6',
    request_id: 'req-1',
    expires_at: '2026-10-24T08:15:00+00:00',
    status: 'pending',
    intercepted: [{ tool_call_id: 'call-1', tool_name: 'write_file', arguments: 'path=report.md', arguments_truncated: false }],
  };
  const { calls, api } = recorder({
    [JIUWENMATE_METHODS.permissionDecide]: {
      request: { ...wireRequest, conversation_id: 'conv-1', outcome: 'paused', approval: { ...approval, status: 'approved' } },
    },
  });

  const updated = await api.decidePermission('a1b2c3d4e5f6', 'conv-1', 'approve', 'call-1');

  assert.equal(updated.approval.status, 'approved');
  assert.deepEqual(updated.approval.intercepted, [
    { toolCallId: 'call-1', toolName: 'write_file', arguments: 'path=report.md', argumentsTruncated: false },
  ]);
  assert.deepEqual(calls, [[JIUWENMATE_METHODS.permissionDecide, {
    approval_id: 'a1b2c3d4e5f6', conversation_id: 'conv-1', decision: 'approve', tool_call_id: 'call-1',
  }]]);
});


test('a colleague lists its Web connection first and each bound IM app with its own agent', () => {
  const item = toColleague({
    ...wireColleague,
    config_id: 'agent-1',
    connections: [
      { channel: 'web', connection_key: 'web', agent_id: 'agent-1', workspace_dir: '/data/agent-1' },
      { channel: 'dingtalk', connection_key: 'ding-app', agent_id: 'agent-ding', workspace_dir: '/data/agent-ding' },
      { channel: 'wecom', connection_key: 'other', agent_id: 'agent-other' },
    ],
  });
  assert.equal(item.configId, 'agent-1');
  assert.deepEqual(item.connections.map((connection) => [connection.channel, connection.connectionKey, connection.agentId]), [
    ['web', 'web', 'agent-1'],
    ['dingtalk', 'ding-app', 'agent-ding'],
  ]);
  assert.equal(item.connections[1].workspaceDir, '/data/agent-ding');
  // A server that lists no connections still gives the colleague its Web connection.
  const plain = toColleague(wireColleague);
  assert.equal(plain.configId, 'agent-1');
  assert.deepEqual(plain.connections, [
    { channel: 'web', connectionKey: 'web', agentId: 'agent-1', workspaceDir: wireColleague.workspace_dir },
  ]);
});

test('binding and unbinding an IM app name the colleague configuration and the app', async () => {
  const { calls, api } = recorder({
    'channel.dingtalk.bind_agent': { binding: {} },
    'channel.feishu.bind_agent': { binding: {} },
    'channel.dingtalk.unbind_agent': { unbound: true },
    'channel.feishu.unbind_agent': { unbound: true },
  });
  const colleague = toColleague({ ...wireColleague, config_id: 'agent-1' });
  await api.bindConnection(colleague, 'dingtalk', { appId: 'ding-app', appSecret: 'test-secret', robotCode: 'robot' });
  await api.bindConnection(colleague, 'feishu', { appId: 'cli-app', appSecret: 'test-secret' });
  await api.unbindConnection({ channel: 'dingtalk', connectionKey: 'ding-app', agentId: 'agent-ding', workspaceDir: null });
  await api.unbindConnection({ channel: 'feishu', connectionKey: 'cli-app', agentId: 'agent-feishu', workspaceDir: null });
  await api.unbindConnection({ channel: 'web', connectionKey: 'web', agentId: 'agent-1', workspaceDir: null });
  assert.deepEqual(calls, [
    ['channel.dingtalk.bind_agent', {
      config_id: 'agent-1', client_id: 'ding-app', client_secret: 'test-secret', robot_code: 'robot',
    }],
    ['channel.feishu.bind_agent', { config_id: 'agent-1', app_id: 'cli-app', app_secret: 'test-secret' }],
    ['channel.dingtalk.unbind_agent', { client_id: 'ding-app' }],
    ['channel.feishu.unbind_agent', { app_id: 'cli-app' }],
  ]);
});
