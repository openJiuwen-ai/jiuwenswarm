import assert from 'node:assert/strict';
import test from 'node:test';

import {
  cronModelPayloadFields,
  modelNameForSelection,
  resolveCronDefaultHint,
} from '../node_modules/.cache/cron-model-selection/cronModelSelection.mjs';

// 复刻真实故障场景：目录里两条 model_name 相同（不同 api_key）的配置。
// 源 chat-session 按已存 model_selection（model_id）正常工作；cron 任务
// 若只存 model_name，执行侧名字反查命中多条 → "ambiguous legacy model name"。
const duplicateNamedModels = [
  { model_id: 'mdl-first', model_name: 'deepseek-flash', api_base: 'https://a', api_key: 'k1', model_provider: 'OpenAI' },
  { model_id: 'mdl-second', model_name: 'deepseek-flash', api_base: 'https://b', api_key: 'k2', model_provider: 'OpenAI' },
  { model_id: 'mdl-glm', model_name: 'GLM-5.3', alias: 'GLM 稳定版', api_base: 'https://c', api_key: 'k3', model_provider: 'OpenAI' },
];

test('modelNameForSelection: 按 model_id 命中重名目录中的选中条目', () => {
  assert.equal(modelNameForSelection(duplicateNamedModels, { type: 'model', id: 'mdl-second' }), 'deepseek-flash');
  assert.equal(modelNameForSelection(duplicateNamedModels, { type: 'model', id: 'mdl-glm' }), 'GLM-5.3');
});

test('modelNameForSelection: 无选择/无 model_id/未命中返回 null', () => {
  assert.equal(modelNameForSelection(duplicateNamedModels, null), null);
  assert.equal(modelNameForSelection(duplicateNamedModels, { type: 'model', id: 'mdl-not-exist' }), null);
  // cron 不支持分组选择（后端拒绝 type != "model"），按无模型处理
  assert.equal(modelNameForSelection(duplicateNamedModels, { type: 'model_group', id: 'group-1' }), null);
});

test('cronModelPayloadFields: 回归——稳定选择存在时 payload 必须携带 model_selection', () => {
  const fields = cronModelPayloadFields({
    modelName: 'deepseek-flash',
    modelSelection: { type: 'model', id: 'mdl-second' },
  });
  assert.deepEqual(fields, {
    model_name: 'deepseek-flash',
    model_selection: { type: 'model', id: 'mdl-second' },
  });
});

test('cronModelPayloadFields: 仅名称选择（登录免费模型等无 model_id 条目）保持旧行为', () => {
  const fields = cronModelPayloadFields({ modelName: 'free-model', modelSelection: null });
  assert.deepEqual(fields, { model_name: 'free-model' });
});

test('cronModelPayloadFields: 未选模型时不携带任何模型字段', () => {
  assert.deepEqual(cronModelPayloadFields({ modelName: null, modelSelection: null }), {});
});

test('resolveCronDefaultHint: 默认模型名严格命中（alias 或 model_name）时展示具体模型', () => {
  // 展示名与 picker 同源（getModelDisplayName）：alias 优先，用户才能把提示
  // 对应到菜单条目——重名目录只有 alias 能区分默认命中的是哪一条
  assert.deepEqual(resolveCronDefaultHint(duplicateNamedModels, 'GLM-5.3'), {
    kind: 'named',
    modelName: 'GLM 稳定版',
  });
  // 命中条目没有 alias 时回退 model_name（登录免费模型等）
  assert.deepEqual(resolveCronDefaultHint(duplicateNamedModels, 'deepseek-flash'), {
    kind: 'named',
    modelName: 'deepseek-flash',
  });
});

test('resolveCronDefaultHint: 失配/未配置默认时不兜底首个模型，返回通用文案', () => {
  // 社区检视 P3：不得把目录第一个模型当默认展示（会被误读且与触发时解析无关）
  assert.deepEqual(resolveCronDefaultHint(duplicateNamedModels, 'renamed-model'), { kind: 'generic' });
  assert.deepEqual(resolveCronDefaultHint(duplicateNamedModels, null), { kind: 'generic' });
});

test('resolveCronDefaultHint: 目录为空时返回警示态（任务触发时可能失败）', () => {
  assert.deepEqual(resolveCronDefaultHint([], 'GLM-5.3'), { kind: 'empty' });
  assert.deepEqual(resolveCronDefaultHint([], null), { kind: 'empty' });
});
