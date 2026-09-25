import assert from 'node:assert/strict';
import test from 'node:test';
import { createQwenOmniSessionUpdate, createQwenOmniToolResultEvents } from '../../../../channels/web/frontend/node_modules/.cache/realtime-duplex/replyLanguageProtocol.mjs';

for (const [language, spoken, announced] of [
  ['en', /Speak to the user in English/, /one or two sentences of English/],
  ['zh-CN', /Speak to the user in Simplified Chinese/, /one or two sentences of Simplified Chinese/],
  ['match', /same language as their latest utterance/, /same language as the original user question/],
  [undefined, /same language as their latest utterance/, /same language as the original user question/],
]) {
  test(`Qwen ${language ?? 'default'} reply language applies to session and task receipt`, () => {
    const session = createQwenOmniSessionUpdate({ inputRate: 16000, outputRate: 24000, replyLanguage: language });
    assert.match(session.session.instructions, spoken);
    const events = createQwenOmniToolResultEvents(
      'call', { status: 'completed', summary: 'done' },
      { jobId: 'job', question: 'original task' }, language,
    );
    assert.match(events[1].item.content[0].text, announced);
    assert.match(events[1].item.content[0].text, /original task/);
  });
}
