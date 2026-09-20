import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

test('task chat uses general ASR while staying separate from JoyAI ASR', () => {
  const contract = source('src/features/settings/services/settingsContract.ts');
  const hook = source('src/features/taskAsr/useTaskAsr.ts');
  const input = source('src/components/ChatPanel/InputArea.tsx');

  assert.match(contract, /asr_api_base[\s\S]*ASR_API_BASE/);
  assert.match(contract, /asr_api_key[\s\S]*ASR_API_KEY/);
  assert.match(contract, /asr_model[\s\S]*ASR_MODEL_NAME/);
  assert.doesNotMatch(contract.match(/asr_api_base[\s\S]*asr_model[^\n]*/)?.[0] ?? '', /VOICE_ASR|JOYAI/);

  assert.match(hook, /navigator\.mediaDevices\.getUserMedia/);
  assert.match(hook, /'task\.asr\.transcribe'/);
  assert.match(input, /data-testid="chat-panel-input-microphone"/);
  assert.match(input, /isListening && 'chat-input-btn--recording'/);
  assert.match(input, /aria-pressed=\{isListening\}/);
});
