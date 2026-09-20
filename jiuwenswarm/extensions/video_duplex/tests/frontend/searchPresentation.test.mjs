import assert from 'node:assert/strict';
import test from 'node:test';
import {
  assistantSpeechText,
  searchAwareToolStatus,
} from '../../../../channels/web/frontend/node_modules/.cache/search-presentation/searchPresentation.js';

test('searchAwareToolStatus keeps concurrent background searches visible', () => {
  assert.equal(
    searchAwareToolStatus('', [
      { status: 'running' },
      { status: 'running' },
    ]),
    '2 项正在后台搜索，可继续提问…',
  );
  assert.equal(
    searchAwareToolStatus('JoyAI 正在根据搜索资料生成回答…', [
      { status: 'queued' },
      { status: 'running' },
    ]),
    'JoyAI 正在根据搜索资料生成回答；另有 1 项正在后台搜索，可继续提问…',
  );
});

test('searchAwareToolStatus clears only after all background searches finish', () => {
  assert.equal(searchAwareToolStatus('', [{ status: 'queued' }]), '');
  assert.equal(
    searchAwareToolStatus('搜索回答生成失败', [{ status: 'failed' }]),
    '搜索回答生成失败',
  );
});

test('assistantSpeechText removes markdown, citations, URLs, and caps long speech', () => {
  assert.equal(
    assistantSpeechText('**香港天气**大致多云。[来源1] https://example.com'),
    '香港天气大致多云。',
  );
  const spoken = assistantSpeechText(`第一句很有用。${'补充内容'.repeat(80)}`, 40);
  assert.ok(spoken.length <= 41);
  assert.match(spoken, /。$/);
});
