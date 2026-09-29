import assert from 'node:assert/strict';
import test from 'node:test';
import { createQwenOmniSessionUpdate, createQwenOmniToolResultEvents } from '../../../../channels/web/frontend/node_modules/.cache/realtime-duplex/replyLanguageProtocol.mjs';

for (const [language, expected] of [
  ['en', /Speak to the user in English/],
  ['zh-CN', /Speak to the user in Simplified Chinese/],
  ['match', /same language as their latest utterance/],
  ['invalid', /same language as their latest utterance/],
  [undefined, /same language as their latest utterance/],
]) {
  test(`Qwen conversation language ${language}`, () => {
    const config = createQwenOmniSessionUpdate({ inputRate: 16000, outputRate: 24000, replyLanguage: language });
    assert.match(config.session.instructions, expected);
    assert.doesNotMatch(config.session.instructions, /preferred response language/);
  });
}

for (const [toolLanguage, announcement] of [
  ['en', /one or two sentences of English/],
  ['zh-CN', /one or two sentences of Simplified Chinese/],
  ['zh', /one or two sentences of Simplified Chinese/],
  [undefined, /one or two sentences of Simplified Chinese/],
]) {
  test(`Qwen tool announcement uses concrete tool language ${toolLanguage}`, () => {
    const events = createQwenOmniToolResultEvents(
      'call',
      { status: 'completed', summary: 'done' },
      { jobId: 'job', question: 'original task' },
      toolLanguage,
    );
    assert.equal(events[0].item.call_id, 'call');
    assert.match(events[1].item.content[0].text, announcement);
    assert.match(events[1].item.content[0].text, /original task/);
  });
}
