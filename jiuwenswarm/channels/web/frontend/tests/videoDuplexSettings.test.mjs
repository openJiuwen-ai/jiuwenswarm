import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { build, transform } from 'esbuild';

const source = readFileSync('src/features/settings/modules/experimental/videoDuplexSettingsValidation.ts', 'utf8');
const { code } = await transform(source, { loader: 'ts', format: 'esm' });
const { isConfigured } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);

const qwen = {
  video_live_provider: 'qwen_omni',
  qwen_omni_realtime_url: 'wss://example.test/realtime',
  qwen_omni_model: 'qwen-omni',
  qwen_omni_api_key: 'key',
  qwen_omni_voice: '',
};
const joyai = {
  video_live_provider: 'joyai',
  joyai_api_base: 'https://example.test',
  joyai_model: 'joyai',
  joyai_api_key: 'key',
  voice_protocol: 'openai_http',
  voice_asr_endpoint: 'https://example.test/asr',
  voice_tts_endpoint: 'https://example.test/tts',
  voice_api_key: 'voice-key',
  voice_asr_model: 'asr',
  voice_tts_model: 'tts',
  voice_tts_voice: '',
};

test('Qwen needs its own endpoint, model and key; default voice remains optional', () => {
  assert.equal(isConfigured(qwen, {}), true);
  for (const key of ['qwen_omni_realtime_url', 'qwen_omni_model', 'qwen_omni_api_key']) {
    assert.equal(isConfigured({ ...qwen, [key]: ' \t ' }, {}), false, key);
  }
});

test('vLLM-Omni accepts explicitly unauthenticated deployments without a Qwen key', () => {
  const values = { video_live_provider: 'vllm_omni_ws', vllm_omni_realtime_url: 'ws://localhost:8099/v1/realtime',
    vllm_omni_model: 'model', vllm_omni_api_key: '' };
  assert.equal(isConfigured(values, {}), true);
  assert.equal(isConfigured({ ...values, vllm_omni_model: '' }, {}), false);
});

test('duplex settings participate in the shared source contract and request router', async () => {
  const result = await build({ entryPoints: ['src/features/settings/services/settingsSourceContract.ts'],
    bundle: true, platform: 'node', format: 'esm', write: false });
  const contract = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
  assert.equal(contract.isSettingsSource('duplex'), true);
  assert.equal(contract.isSettingsSourceComponent('duplex', 'video_live_provider', 'select'), true);
  assert.equal(contract.isSettingsSourceComponent('duplex', 'vllm_omni_model', 'input'), true);
  assert.equal(contract.isSettingsSourceComponent('duplex', 'vllm_omni_model', 'switch'), false);
  const routed = await build({ entryPoints: ['src/features/settings/services/settingsContract.ts'],
    bundle: true, platform: 'node', format: 'esm', write: false });
  const settings = await import(`data:text/javascript;base64,${Buffer.from(routed.outputFiles[0].text).toString('base64')}`);
  assert.ok(settings.OPEN_SOURCE_SETTINGS_REQUEST_METHODS.includes('video.duplex.settings.get'));
  assert.ok(settings.OPEN_SOURCE_SETTINGS_REQUEST_METHODS.includes('video.duplex.settings.update'));
});

test('saved keys count as configured, but keys for another provider do not', () => {
  const draft = { ...qwen, qwen_omni_api_key: '' };
  assert.equal(isConfigured(draft, { qwen_omni_api_key: 12 }), true);
  assert.equal(isConfigured(draft, { qwen_omni_api_key: 0, joyai_api_key: 12 }), false);
});

test('saved JoyAI configurations cannot enable the Qwen-only frontend', () => {
  assert.equal(isConfigured(joyai, {}), false);
  assert.equal(isConfigured({ ...joyai, voice_protocol: 'native_ws', voice_tts_voice: 'speaker' }, {}), false);
  assert.equal(isConfigured({ ...qwen, ...joyai }, { joyai_api_key: 8, voice_api_key: 9 }), false);
});
