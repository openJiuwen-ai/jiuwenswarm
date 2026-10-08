import assert from 'node:assert/strict';
import test from 'node:test';
import {
  catalogModels,
  credentialsOptional,
  defaultGeneration,
  generationApiBase,
  generationVendor,
  isEditableBaseVendor,
  isGenerationSlotConfigured,
} from '../node_modules/.cache/generation-models/generationModels.mjs';

test('DashScope lists wan 3.0 video and qwen-image 3.0 image models', () => {
  assert.deepEqual(
    catalogModels('video_gen', 'dashscope').map((entry) => entry.model),
    ['wan3.0-video', 'wan3.0-video-prime'],
  );
  assert.deepEqual(
    catalogModels('visual_gen', 'dashscope').map((entry) => entry.model),
    ['qwen-image-3.0', 'qwen-image-3.0-pro'],
  );
  assert.ok(catalogModels('video_gen', 'dashscope').every((entry) => entry.protocol === 'dashscope'));
});

test('Alibaba presets and DashScope hosts use the native /api/v1 catalog', () => {
  assert.equal(generationVendor('alibaba', 'https://dashscope.aliyuncs.com/compatible-mode/v1'), 'dashscope');
  assert.equal(generationVendor(undefined, 'https://dashscope-intl.aliyuncs.com/api/v1'), 'dashscope');
  assert.equal(
    generationApiBase('alibaba', 'https://coding.dashscope.aliyuncs.com/v1'),
    'https://dashscope.aliyuncs.com/api/v1',
  );
  assert.equal(generationApiBase('minimax', 'https://api.minimax.io/v1'), 'https://api.minimax.io/v1');
  assert.equal(isEditableBaseVendor('alibaba'), true);
});

test('vLLM-Omni has an editable base and may leave key and model empty', () => {
  assert.equal(generationVendor('vllm-omni', 'http://127.0.0.1:8091/v1'), 'vllm-omni');
  assert.equal(isEditableBaseVendor('vllm-omni'), true);
  assert.equal(credentialsOptional('vllm-omni'), true);
  assert.equal(credentialsOptional('dashscope'), false);
  assert.deepEqual(defaultGeneration('video_gen', 'vllm-omni'), { protocol: 'vllm-omni', model: '' });
  assert.ok(catalogModels('video_gen', 'vllm-omni').some((entry) => entry.model === 'MiniMaxAI/MiniMax-H3'));
});

test('a slot counts as configured without key and model only for vLLM-Omni', () => {
  const base = {
    video_gen_provider: 'OpenAI',
    video_gen_protocol: 'vllm-omni',
    video_gen_api_base: 'http://gpu-box:8091/v1',
  };
  assert.equal(isGenerationSlotConfigured(base, 'video_gen'), true);
  assert.equal(isGenerationSlotConfigured({ ...base, video_gen_protocol: 'dashscope' }, 'video_gen'), false);
  assert.equal(
    isGenerationSlotConfigured(
      { ...base, video_gen_protocol: 'dashscope', video_gen_api_key: 'sk', video_gen_model: 'wan3.0-video' },
      'video_gen',
    ),
    true,
  );
  assert.equal(isGenerationSlotConfigured({ ...base, video_gen_api_base: '' }, 'video_gen'), false);
});
