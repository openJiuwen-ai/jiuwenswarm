import assert from 'node:assert/strict';
import test from 'node:test';

import {
  JIUWENMATE_METHODS,
  createJiuwenMateApi,
  findModel,
  toColleague,
  toModelCatalog,
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
