import assert from 'node:assert/strict';
import test from 'node:test';
import { createQwenOmniSessionUpdate, createQwenOmniToolResultEvents } from '../../../../channels/web/frontend/node_modules/.cache/realtime-duplex/replyLanguageProtocol.mjs';

for (const [language, expected, announcement] of [
  ['en', /Speak to the user in English/, /one or two sentences of English/],
  ['zh-CN', /Speak to the user in Simplified Chinese/, /one or two sentences of Simplified Chinese/],
  ['match', /same language as their latest utterance/, /same language as the original user question/],
  ['invalid', /same language as their latest utterance/, /same language as the original user question/],
  [undefined, /same language as their latest utterance/, /same language as the original user question/],
]) {
  test(`Qwen language ${language} governs both conversation and task announcement`, () => {
    const config = createQwenOmniSessionUpdate({ inputRate: 16000, outputRate: 24000, replyLanguage: language });
    assert.match(config.session.instructions, expected);
    assert.doesNotMatch(config.session.instructions, /preferred response language/);
    const events = createQwenOmniToolResultEvents('call', { status: 'completed', summary: 'done' }, { jobId: 'job', question: 'original task' }, language);
    assert.equal(events[0].item.call_id, 'call');
    assert.match(events[1].item.content[0].text, announcement);
    assert.match(events[1].item.content[0].text, /original task/);
    assert.equal(events[2].type, 'response.create');
  });
}
