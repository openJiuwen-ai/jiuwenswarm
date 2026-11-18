import './fixtures/browserEnvironment.mjs';
import assert from 'node:assert/strict';
import test from 'node:test';

const { useChatStore } = await import('../node_modules/.cache/history-restore-file-attach/chatStore.mjs');
const { parseHistoryJsonFileToTimelinePreview } = await import('../node_modules/.cache/history-restore-file-attach/historyRestore.mjs');
globalThis.localStorage = globalThis.window.localStorage;
const chat = () => useChatStore.getState();
const messagesOf = (sessionId) => chat().getRuntime(sessionId).messages;
function seed(sessionId, messages) {
  chat().ensureRuntime(sessionId);
  chat().replaceHistoryMessages(sessionId, messages);
}

function questionHistory(sessionId, requestId, answered = false, source = 'ask_user_interrupt') {
  const question = { question: 'Choose a SkillPack', header: 'SkillPack', options: [{ label: 'Use pack' }] };
  const records = [{
    id: `ask-${requestId}`, role: 'assistant', event_type: 'chat.ask_user_question',
    request_id: requestId, source, questions: [question], timestamp: 1,
  }];
  if (source === null) delete records[0].source;
  if (answered) records.push({
    id: `answer-${requestId}`, role: 'assistant', event_type: 'chat.ask_user_answer',
    request_id: requestId, answers: [{ question: question.question, selected_options: ['Use pack'] }], timestamp: 2,
  });
  return parseHistoryJsonFileToTimelinePreview(records, sessionId).messages;
}

const pendingQuestion = (requestId) => ({
  request_id: requestId, source: 'ask_user_interrupt',
  questions: [{ question: 'Choose a SkillPack', header: 'SkillPack', options: [{ label: 'Use pack' }] }],
});

for (const source of [null, 'legacy_question']) {
  test(`answered history cleanup does not depend on optional source (${source})`, () => {
    const sid = `answered_history_source_${source}`;
    seed(sid, questionHistory(sid, 'done', false, source));
    assert.equal(messagesOf(sid).length, 1, 'unanswered cold history stays visible');
    chat().markQuestionsAnswered(sid, ['done']);
    assert.equal(messagesOf(sid).length, 0, 'answering removes the already restored unanswered echo');
    chat().replaceHistoryMessages(sid, questionHistory(sid, 'done', false, source));
    assert.equal(messagesOf(sid).length, 0, 'replace must reject a late unanswered echo');
    chat().prependMessages(sid, questionHistory(sid, 'done', false, source));
    assert.equal(messagesOf(sid).length, 0, 'pagination must reject a late unanswered echo');
    chat().replaceHistoryMessages(sid, questionHistory(sid, 'done', true, source));
    assert.equal(messagesOf(sid).length, 1, 'the answered summary remains visible');
    seed(`${sid}_other`, questionHistory(`${sid}_other`, 'done', false, source));
    assert.equal(messagesOf(`${sid}_other`).length, 1, 'another session is not affected');
  });
}

test('switching back keeps the live question without its unanswered history echo', () => {
  const sid = 'pending_question_restore';
  seed(sid, []);
  chat().enqueuePendingQuestion(sid, pendingQuestion('pending-restore'));
  chat().setActiveSessionId('another-session');
  chat().setActiveSessionId(sid);
  chat().replaceHistoryMessages(sid, [
    ...questionHistory(sid, 'pending-restore'),
    ...questionHistory(sid, 'unrelated'),
    ...questionHistory(sid, 'answered', true),
  ]);
  assert.deepEqual(messagesOf(sid).map(message => message.id), ['qa-summary-hist-unrelated', 'qa-summary-hist-answered']);
  assert.equal(chat().getRuntime(sid).pendingQuestions[0].request_id, 'pending-restore');
  chat().prependMessages(sid, questionHistory(sid, 'pending-restore'));
  assert.equal(messagesOf(sid).length, 2, 'pagination must not reintroduce the duplicate');
});

test('a late live question supersedes only its own unanswered history summary', () => {
  const sid = 'pending_question_late';
  seed(sid, [...questionHistory(sid, 'late'), ...questionHistory(sid, 'other')]);
  assert.equal(messagesOf(sid).length, 2, 'cold history keeps read-only summaries');
  chat().enqueuePendingQuestion(sid, pendingQuestion('late'));
  assert.deepEqual(messagesOf(sid).map(message => message.id), ['qa-summary-hist-other']);
  chat().consumePendingQuestion(sid, pendingQuestion('late'));
  assert.equal(messagesOf(sid).length, 1, 'consuming the live card must not revive its old echo');
});

test('an answered live question cannot return as an unanswered history summary', () => {
  const sid = 'pending_question_answered';
  seed(sid, questionHistory(sid, 'done'));
  assert.equal(messagesOf(sid).length, 1);
  chat().markQuestionsAnswered(sid, ['done']);
  assert.equal(messagesOf(sid).length, 0, 'a late answer must remove an already restored unanswered echo');
  chat().replaceHistoryMessages(sid, questionHistory(sid, 'done'));
  assert.equal(messagesOf(sid).length, 0);
  chat().prependMessages(sid, questionHistory(sid, 'done'));
  assert.equal(messagesOf(sid).length, 0);
  chat().replaceHistoryMessages(sid, questionHistory(sid, 'done', true));
  assert.equal(messagesOf(sid).length, 1, 'answered history must still be visible');
  const otherSession = 'pending_question_different_session';
  seed(otherSession, questionHistory(otherSession, 'done'));
  assert.equal(messagesOf(otherSession).length, 1, 'request IDs are scoped to their own session');
});

