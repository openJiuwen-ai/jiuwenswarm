import assert from 'node:assert/strict';
import test from 'node:test';

import { beginHistoryRestore, webClient } from '../node_modules/.cache/subagent-history/historyRestore.mjs';

const sessionId = 'web_ask_user_history';
const start = 1794881929473;
const question = {
  question: 'Which format?',
  header: 'Format',
  card_id: 'format-card',
  options: [{ label: 'PDF', value: 'pdf', description: 'Portable document' }],
  multi_select: false,
};
const user = { role: 'user', content: 'Prepare a report', timestamp: start };
const ask = {
  role: 'assistant',
  event_type: 'chat.ask_user_question',
  source: 'ask_user_interrupt',
  request_id: 'call-format',
  questions: [question],
  timestamp: start + 1,
};
const pause = { role: 'assistant', event_type: 'chat.final', content: 'Choose a format.', timestamp: start + 2 };
const answer = {
  role: 'assistant',
  event_type: 'chat.ask_user_answer',
  request_id: ask.request_id,
  answers: [{ question: question.question, selected_options: ['PDF'] }],
  timestamp: start + 3,
};

function restore(records, edition = 'enterprise') {
  const originalWindow = globalThis.window;
  globalThis.window = { __JIUWENSWARM_EDITION__: edition };
  let messages;
  let pending = [];
  const handle = beginHistoryRestore({
    sessionId,
    onReady: (items) => { messages = items; },
    onPendingQuestionReplay: (items) => { pending = items; },
  });
  const dispatch = (fields) => webClient.dispatchEventNow({
    type: 'event',
    event: 'history.message',
    payload: { session_id: sessionId, cursor: null, ...fields },
  });
  try {
    for (const message of [...records].reverse()) dispatch({ message });
    dispatch({ status: 'done', has_more: false, next_cursor: null, snapshot_id: null, snapshot_end: records.length });
    assert.ok(messages);
    return { messages, pending };
  } finally {
    handle.dispose();
    globalThis.window = originalWindow;
  }
}

test('enterprise restores the unanswered question with its original submit payload', () => {
  const { messages, pending } = restore([user, ask, pause]);
  assert.deepEqual(pending.map((item) => item.payload), [{
    request_id: ask.request_id,
    source: ask.source,
    questions: [question],
  }]);
  assert.equal(messages.some((message) => message.content.startsWith('qa.summary:')), false);
});

test('enterprise restores the interrupt ID when it was saved with the question', () => {
  const saved = { ...ask, interrupt_id: 'interrupt-format' };
  const { pending } = restore([user, saved, pause]);
  assert.equal(pending[0].payload.interruptId, 'interrupt-format');
  assert.equal(pending[0].payload.request_id, ask.request_id);
});

test('enterprise keeps an unanswered question read-only after a confirmed stop', () => {
  const stopped = { role: 'assistant', event_type: 'chat.ask_user_closed', timestamp: start + 3 };
  const { messages, pending } = restore([user, ask, pause, stopped]);
  assert.deepEqual(pending, []);
  assert.equal(messages.length, 3);
  assert.match(messages.at(-1).content, /^qa.summary:/);
});

test('enterprise scopes a targeted stop to its questions and keeps newer questions answerable', () => {
  const stopped = { role: 'assistant', event_type: 'chat.ask_user_closed', timestamp: start + 3 };
  const otherStopped = { ...stopped, question_request_ids: ['another-request'] };
  const matchingStopped = { ...stopped, question_request_ids: [ask.request_id] };
  assert.equal(restore([user, ask, pause, otherStopped]).pending.length, 1);
  assert.deepEqual(restore([user, ask, pause, matchingStopped]).pending, []);
  const newer = { ...ask, request_id: 'new-request', timestamp: start + 4 };
  assert.equal(restore([user, ask, pause, stopped, newer]).pending[0].payload.request_id, newer.request_id);
});

test('enterprise keeps a question answerable after a stream disconnect', () => {
  const disconnected = {
    role: 'assistant',
    event_type: 'chat.request_completed',
    status: 'cancelled',
    timestamp: start + 3,
  };
  assert.equal(restore([user, ask, pause, disconnected]).pending.length, 1);
});

test('enterprise keeps an answered question read-only', () => {
  const { messages, pending } = restore([user, ask, pause, answer]);
  assert.deepEqual(pending, []);
  assert.match(messages.at(-1).content, /^qa.summary:.*PDF/);
});

test('enterprise leaves questions from a past turn, completed tool call or forked history read-only', () => {
  const laterUser = { ...user, content: 'New task', timestamp: start + 3 };
  const toolResult = {
    role: 'assistant',
    event_type: 'chat.tool_result',
    tool_call_id: ask.request_id,
    tool_name: 'ask_user',
    result: 'cancelled',
    success: false,
    timestamp: start + 3,
  };
  const inherited = { ...ask, forked_from: 'another_session' };
  for (const records of [[user, ask, pause, laterUser], [user, ask, pause, toolResult], [user, inherited, pause]]) {
    const { messages, pending } = restore(records);
    assert.deepEqual(pending, []);
    assert.equal(messages.some((message) => message.content.startsWith('qa.summary:')), true);
  }
});

test('personal history keeps the existing read-only unanswered question', () => {
  const { messages, pending } = restore([user, ask, pause], 'personal');
  assert.deepEqual(pending, []);
  assert.match(messages.at(-1).content, /^qa.summary:/);
});
