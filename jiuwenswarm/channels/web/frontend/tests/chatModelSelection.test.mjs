import assert from 'node:assert/strict';
import test from 'node:test';

import {
  resolveChatModelSelection,
  resolveConfiguredModelName,
  useSessionStore,
} from '../node_modules/.cache/chat-model-selection/sessionStore.mjs';

const models = [
  {
    model_name: 'main-model',
    alias: '主对话默认模型',
    api_base: 'https://example.test/main',
    api_key: 'main-key',
    model_provider: 'OpenAI',
  },
  {
    model_name: 'single-agent-model',
    alias: '单Agent模型',
    api_base: 'https://example.test/agent',
    api_key: 'agent-key',
    model_provider: 'OpenAI',
  },
];

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

test('pending new conversation follows the default model when it changes', () => {
  const store = useSessionStore.getState();
  // 初始默认模型为 main-model
  store.setAvailableModels(models, 'main-model');
  store.ensureRuntime('new');
  store.setSelectedModelName('new', 'main-model');

  // 用户在配置面板把默认模型切换为 single-agent-model 后刷新模型列表
  store.setAvailableModels(models, 'single-agent-model');

  // 未发送的新建会话应跟随新默认模型
  assert.equal(useSessionStore.getState().getRuntime('new')?.selectedModelName, 'single-agent-model');
  assert.equal(useSessionStore.getState().getEffectiveModelName('new'), 'single-agent-model');

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
