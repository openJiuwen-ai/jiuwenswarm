import assert from 'node:assert/strict';
import test from 'node:test';

import {
  resolveAvailableModelSelection,
  resolveChatModelSelection,
  resolveConfiguredModelName,
  resolveNewConversationModelSelection,
  useSessionStore,
} from '../node_modules/.cache/chat-model-selection/sessionStore.mjs';

const models = [
  {
    model_id: 'mdl-main',
    model_name: 'main-model',
    alias: '主对话默认模型',
    api_base: 'https://example.test/main',
    api_key: 'main-key',
    model_provider: 'OpenAI',
  },
  {
    model_id: 'mdl-agent',
    model_name: 'single-agent-model',
    alias: '单Agent模型',
    api_base: 'https://example.test/agent',
    api_key: 'agent-key',
    model_provider: 'OpenAI',
  },
];

test('new conversations restore a valid cached model or model-group route', () => {
  const selectableModels = [
    { ...models[0], model_id: 'mdl-main' },
    { ...models[1], model_id: 'mdl-agent' },
  ];
  const groups = [{
    model_group_id: 'grp-fast',
    enabled: true,
    routes: [{ route_id: 'route-main', model_id: 'mdl-main', enabled: true }],
  }];

  assert.deepEqual(
    resolveNewConversationModelSelection(selectableModels, groups, { type: 'model', id: 'mdl-agent' }),
    { type: 'model', id: 'mdl-agent' },
  );
  assert.deepEqual(
    resolveNewConversationModelSelection(selectableModels, groups, {
      type: 'model_group', id: 'grp-fast', route_id: 'route-main',
    }),
    { type: 'model_group', id: 'grp-fast', route_id: 'route-main' },
  );
});

test('invalid cached selections fall back default-group -> custom -> free -> empty', () => {
  const custom = { ...models[0], model_id: 'mdl-custom', is_free: false };
  const free = { ...models[1], model_id: 'mdl-free', is_free: true };
  const missing = { type: 'model', id: 'missing' };
  const defaultGroup = {
    model_group_id: 'grp-default',
    is_default: true,
    enabled: true,
    routes: [{ route_id: 'route-main', model_id: 'mdl-custom', enabled: true }],
  };

  assert.deepEqual(
    resolveNewConversationModelSelection([free, custom], [defaultGroup], { type: 'model_group', id: 'grp-gone' }),
    { type: 'model_group', id: 'grp-default' },
  );
  assert.deepEqual(resolveNewConversationModelSelection([free, custom], [], missing), {
    type: 'model', id: 'mdl-custom',
  });
  assert.deepEqual(resolveNewConversationModelSelection([free], [], missing), {
    type: 'model', id: 'mdl-free',
  });
  assert.equal(resolveNewConversationModelSelection([], [], missing), null);
});

test('deleted historical model_group selections resolve to the catalog fallback', () => {
  const custom = { ...models[0], model_id: 'mdl-custom', is_free: false };
  const dead = { type: 'model_group', id: 'mgp-deleted' };
  assert.deepEqual(resolveAvailableModelSelection(dead, [custom], []), {
    type: 'model', id: 'mdl-custom',
  });
  assert.equal(resolveAvailableModelSelection(dead, [custom], [
    { model_group_id: 'mgp-deleted', enabled: true, routes: [{ model_id: 'mdl-custom' }] },
  ])?.id, 'mgp-deleted');
});

test('sending a group selection and recreating a conversation preserves the cached group', () => {
  const previousStorage = globalThis.localStorage;
  const values = new Map();
  globalThis.localStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  };
  const store = useSessionStore.getState();
  const selection = { type: 'model_group', id: 'grp-cached', route_id: 'route-main' };
  const groups = [{
    model_group_id: selection.id,
    enabled: true,
    routes: [{ route_id: 'route-main', model_id: 'mdl-main', enabled: true }],
  }];
  try {
    store.removeRuntime('new');
    store.setAvailableModels(models, 'main-model');
    store.setAvailableModelGroups(groups);
    store.ensureRuntime('new');
    store.setSelectedModelSelection('new', selection);
    store.ensureRuntime('sent-group');
    store.setSelectedModelSelection('sent-group', store.getRuntime('new').selectedModelSelection);
    // App initializes defaults when switching from the draft to the sent session.
    store.initializeDefaultModel('sent-group');
    assert.deepEqual(store.getRuntime('sent-group').selectedModelSelection, selection);
    assert.equal(store.getRuntime('sent-group').selectedModelName, null);
    store.removeRuntime('new');
    assert.deepEqual(store.ensureRuntime('new').selectedModelSelection, selection);
    // Entering chat must not mistake a group's intentionally empty model name
    // for an uninitialized selection.
    store.initializeDefaultModel('new');
    assert.deepEqual(store.getRuntime('new').selectedModelSelection, selection);
    assert.equal(store.getRuntime('new').selectedModelName, null);

    // Draft groups survive catalog refresh; after the user picks a model, invalid
    // cache falls back custom -> free -> empty.
    store.setAvailableModelGroups([]);
    assert.deepEqual(store.getRuntime('new').selectedModelSelection, selection);
    store.setSelectedModelName('new', 'single-agent-model');
    store.setAvailableModelGroups([]);
    assert.deepEqual(store.getRuntime('new').selectedModelSelection, { type: 'model', id: 'mdl-main' });
    store.setAvailableModels([{ ...models[1], is_free: true }], 'single-agent-model');
    assert.deepEqual(store.getRuntime('new').selectedModelSelection, { type: 'model', id: 'mdl-agent' });
    store.setAvailableModels([], null);
    assert.equal(store.getRuntime('new').selectedModelSelection, null);
  } finally {
    store.removeRuntime('new');
    store.removeRuntime('sent-group');
    store.setAvailableModelGroups([]);
    store.setAvailableModels(models, 'main-model');
    if (previousStorage === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = previousStorage;
  }
});

test('team mode keeps the explicitly selected model (not locked to default anymore)', () => {
  const selected = resolveChatModelSelection(
    models,
    '单Agent模型',
    '主对话默认模型',
  );

  assert.equal(selected?.model_name, 'single-agent-model');
});

test('single-agent mode keeps the explicitly selected model', () => {
  const selected = resolveChatModelSelection(
    models,
    '单Agent模型',
    '主对话默认模型',
  );

  assert.equal(selected?.model_name, 'single-agent-model');
});

test('a missing selection falls back to the configured default model', () => {
  const selected = resolveChatModelSelection(
    models,
    null,
    '主对话默认模型',
  );

  assert.equal(selected?.model_name, 'main-model');
});

test('scheduled tasks resolve the configured default alias to its model ID', () => {
  assert.equal(
    resolveConfiguredModelName(models, '主对话默认模型'),
    'main-model',
  );
});

test('an unavailable configured default does not silently select another model', () => {
  assert.equal(resolveConfiguredModelName(models, 'missing-model'), null);
});

test('team runtime keeps the user-selected model and sends it as effective model name', () => {
  const sessionId = 'team-selected-model-test';
  const store = useSessionStore.getState();
  store.ensureRuntime(sessionId);
  store.setMode(sessionId, 'team');
  store.setAvailableModels(models, 'main-model');
  store.setSelectedModelName(sessionId, '单Agent模型');

  // 切到 team 后用户自选的模型应原样保留（不再被 setMode/setAvailableModels 重置）
  assert.equal(useSessionStore.getState().getRuntime(sessionId)?.selectedModelName, '单Agent模型');
  // 发给后端的 model_name 解析为该模型条目的 model_name
  assert.equal(useSessionStore.getState().getEffectiveModelName(sessionId), 'single-agent-model');

  // 模型列表刷新（models.updated）不应冲掉 team 会话的自选模型
  store.setAvailableModels(models, 'main-model');
  assert.equal(useSessionStore.getState().getRuntime(sessionId)?.selectedModelName, '单Agent模型');
  assert.equal(useSessionStore.getState().getEffectiveModelName(sessionId), 'single-agent-model');

  useSessionStore.getState().removeRuntime(sessionId);
});

test('pending new conversation keeps its last valid selection when the configured default changes', () => {
  const store = useSessionStore.getState();
  // 初始默认模型为 main-model
  store.setAvailableModels(models, 'main-model');
  store.ensureRuntime('new');
  store.setSelectedModelName('new', 'main-model');

  // 用户在配置面板把默认模型切换为 single-agent-model 后刷新模型列表
  store.setAvailableModels(models, 'single-agent-model');

  // 新建会话优先保留上次有效选择，不再跟随后端默认模型漂移。
  assert.equal(useSessionStore.getState().getRuntime('new')?.selectedModelName, 'main-model');
  assert.equal(useSessionStore.getState().getEffectiveModelName('new'), 'main-model');

  useSessionStore.getState().removeRuntime('new');
});

test('pending new conversation keeps a manually selected model when the default changes', () => {
  const store = useSessionStore.getState();
  store.setAvailableModels(models, 'main-model');
  store.ensureRuntime('new');
  // 用户在新建会话里手动选了非默认模型
  store.setSelectedModelName('new', 'single-agent-model');

  store.setAvailableModels(models, 'main-model');

  // 手动选择不应被默认模型刷新冲掉
  assert.equal(useSessionStore.getState().getRuntime('new')?.selectedModelName, 'single-agent-model');

  useSessionStore.getState().removeRuntime('new');
});
