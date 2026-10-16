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
  toChatRequest,
  toColleague,
  toModelCatalog,
  upsertConversation,
  toWireSettings,
} from '../node_modules/.cache/jiuwenmate-api/jiuwenmateApi.js';

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
  workspace_dir: '/data/jiuwenmate/workspaces/local/agent-1',
  created_at: '2026-10-14T08:00:00+00:00',
  updated_at: '2026-10-14T08:00:00+00:00',
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

test('server colleagues map to page colleagues with local-only marks off', () => {
  const colleague = toColleague(wireColleague);
  assert.equal(colleague.id, 'agent-1');
  assert.equal(colleague.capabilities.model, '');
  assert.deepEqual(colleague.capabilities.starterPrompts, ['帮我整理本周工作进展。']);
  assert.equal(colleague.avatarUrl, undefined);
  assert.equal(colleague.joinedAt, wireColleague.created_at);
  assert.equal(colleague.isDefault, true);
  assert.equal(colleague.currentVersion, 'v1');
  assert.equal(colleague.workspaceDir, wireColleague.workspace_dir);
  assert.equal(toColleague({ ...wireColleague, workspace_dir: undefined }).workspaceDir, null);
  assert.equal(colleague.imConfigured, false);
  assert.equal(colleague.disabled, false);
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

  const unlisted = toWireSettings({ ...draft, capabilities: { ...draft.capabilities, model: 'deepseek-v3.2' } }, models);
  assert.equal(unlisted.capabilities.model, null);
  assert.equal(toWireSettings({ ...draft, avatarUrl: '/assets/bee.png' }, models).avatar_url, '/assets/bee.png');
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

test('create, delete and publish send agent ids and settings without a namespace', async () => {
  const { calls, api } = recorder({
    [JIUWENMATE_METHODS.create]: { colleague: { ...wireColleague, agent_id: 'agent-2', is_default: false } },
    [JIUWENMATE_METHODS.delete]: { deleted: true },
    [JIUWENMATE_METHODS.publish]: {
      version: { agent_id: 'agent-2', config_version: 'v2', published_at: '2026-10-14T09:00:00+00:00' },
    },
  });
  const created = await api.createColleague(draft, toModelCatalog(wireCatalog).models);
  assert.equal(created.id, 'agent-2');
  assert.equal(calls[0][1].settings.capabilities.model, 'kimi#1');

  assert.equal((await api.publish('agent-2')).configVersion, 'v2');
  await api.deleteColleague('agent-2');
  assert.deepEqual(calls.slice(1), [
    [JIUWENMATE_METHODS.publish, { agent_id: 'agent-2' }],
    [JIUWENMATE_METHODS.delete, { agent_id: 'agent-2' }],
  ]);
  assert.equal(
    calls.some(([, params]) => params && 'namespace_id' in params),
    false,
  );
});

test('server refusals reach the caller unchanged', async () => {
  const refusal = Object.assign(new Error('the default colleague cannot be deleted'), { code: 'BAD_REQUEST' });
  const { api } = recorder({ [JIUWENMATE_METHODS.delete]: refusal });
  await assert.rejects(api.deleteColleague('agent-1'), (error) => error === refusal);
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
  assert.deepEqual(sent.conversation, { id: 'conv-1', title: '今天进展', updatedAt: '2026-10-14T09:00:00+00:00' });
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
      size: 1280,
      caption: '本周周报',
      downloadUrl: '/file-api/download?token=abc',
    },
    {
      outputId: 'out-bad',
      name: 'secret.md',
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
    [JIUWENMATE_METHODS.conversationDelete]: { deleted: true },
  });
  assert.deepEqual((await api.listConversations('agent-1')).map((item) => item.id), ['conv-1', 'conv-0']);
  assert.equal((await api.renameConversation('agent-1', 'conv-1', '周报')).title, '周报');
  await api.deleteConversation('agent-1', 'conv-1');
  assert.deepEqual(calls, [
    [JIUWENMATE_METHODS.conversationList, { agent_id: 'agent-1' }],
    [JIUWENMATE_METHODS.conversationRename, { agent_id: 'agent-1', conversation_id: 'conv-1', title: '周报' }],
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
