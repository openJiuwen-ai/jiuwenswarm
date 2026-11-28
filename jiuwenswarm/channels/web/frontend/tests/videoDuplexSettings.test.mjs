import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { transform } from 'esbuild';

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

test('Qwen uses defaults for blank endpoint, model and voice, but requires its own key', () => {
  assert.equal(isConfigured(qwen, {}), true);
  assert.equal(isConfigured({ ...qwen, qwen_omni_realtime_url: ' ', qwen_omni_model: '', qwen_omni_voice: '' }, {}), true);
  assert.equal(isConfigured({ ...qwen, qwen_omni_api_key: ' \t ' }, {}), false);
});

test('saved keys count as configured, but keys for another provider do not', () => {
  const draft = { ...qwen, qwen_omni_api_key: '' };
  assert.equal(isConfigured(draft, { qwen_omni_api_key: 12 }), true);
  assert.equal(isConfigured(draft, { qwen_omni_api_key: 0, joyai_api_key: 12 }), false);
});

test('Doubao uses defaults for blank fields and requires its own API key', () => {
  const draft = { video_live_provider: 'doubao', doubao_api_key: '', doubao_realtime_url: ' ', doubao_model: '', doubao_voice: '' };
  assert.equal(isConfigured(draft, { doubao_api_key: 36 }), true);
  assert.equal(isConfigured({ ...draft, doubao_api_key: 'new-key' }, {}), true);
  assert.equal(isConfigured({ ...qwen, ...draft }, { qwen_omni_api_key: 12 }), false);
});

test('saved JoyAI configurations cannot enable the realtime model settings', () => {
  assert.equal(isConfigured(joyai, {}), false);
  assert.equal(isConfigured({ ...joyai, voice_protocol: 'native_ws', voice_tts_voice: 'speaker' }, {}), false);
  assert.equal(isConfigured({ ...qwen, ...joyai }, { joyai_api_key: 8, voice_api_key: 9 }), false);
});
