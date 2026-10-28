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
  test(`Qwen ${language ?? 'default'} reply language applies to session and task receipt`, () => {
    const session = createQwenOmniSessionUpdate({ inputRate: 16000, outputRate: 24000, replyLanguage: language });
    assert.equal(session.session.voice, 'Tina');
    assert.match(session.session.instructions, expected);
    const events = createQwenOmniToolResultEvents(
      'call',
      { status: 'completed', summary: 'done' },
      { jobId: 'job', question: 'original task' },
      language,
    );
    assert.equal(events[0].item.call_id, 'call');
    assert.match(events[1].item.content[0].text, announcement);
    assert.match(events[1].item.content[0].text, /original task/);
  });
}


test('Qwen session and completion receipt preserve management of completed resources', () => {
  const update = createQwenOmniSessionUpdate({ inputRate: 16000, outputRate: 24000 });
  const instructions = update.session.instructions;
  assert.match(instructions, /Further actions on prior results may also be delegated to Core/);
  assert.doesNotMatch(instructions, /Cron tools|scheduled reminders/);
  assert.match(instructions, /Completed Core work can still be revised/);
  assert.doesNotMatch(instructions, /Every question about current task progress, counts or stopping requires/);
  const events = createQwenOmniToolResultEvents('created', {
    status: 'completed', summary: 'Reminder created',
  }, { jobId: 'creation-invocation', question: 'Create daily reminder' });
  assert.match(events[1].item.content[0].text, /Completion does not prohibit Core from later modifying or deleting/);
});

test('completion notification preserves invocation identity separately from resource IDs in the summary', () => {
  const events = createQwenOmniToolResultEvents('create', {
    status: 'completed', summary: 'Created resource external-resource-123',
  }, { jobId: 'core-invocation-456', question: 'Create an object' });
  // The accepted call's output is omitted on completion, so the message itself must retain job_id.
  const notice = events[1].item.content[0].text;
  assert.match(notice, /"job_id":"core-invocation-456"/);
  assert.match(notice, /external-resource-123/);
  assert.match(notice, /must never substitute for job_id/);
});
