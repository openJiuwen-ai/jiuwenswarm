import assert from 'node:assert/strict';
import test from 'node:test';

import { createSessionEventGate, getEventSessionId } from '../node_modules/.cache/session-event-gate/services/sessionEventGate.js';

function event(name, sessionId, payload = {}) {
  return {
    type: 'event',
    event: name,
    payload: sessionId ? { ...payload, session_id: sessionId } : payload,
  };
}

test('extracts direct and nested session ids used by live conversation events', () => {
  assert.equal(getEventSessionId(event('chat.delta', 'session-direct')), 'session-direct');
  assert.equal(getEventSessionId(event('team.event', '', { payload: { event: { session_id: 'session-nested' } } })), 'session-nested');
});

test('replays one session live events in arrival order after history restoration', async () => {
  const received = [];
  const gate = createSessionEventGate(incoming => received.push(incoming));
  const release = gate.suspend('session-a');

  gate.dispatch(event('chat.delta', 'session-a', { content: 'first' }));
  gate.dispatch(event('chat.tool_call', 'session-a', { id: 'tool-1' }));
  gate.dispatch(event('chat.delta', 'session-b', { content: 'other session' }));

  assert.deepEqual(
    received.map(incoming => incoming.payload.content),
    ['other session'],
  );

  release();
  await Promise.resolve();

  assert.deepEqual(
    received.map(incoming => incoming.event),
    ['chat.delta', 'chat.delta', 'chat.tool_call'],
  );
  assert.deepEqual(
    received.slice(1).map(incoming => incoming.payload.content ?? incoming.payload.id),
    ['first', 'tool-1'],
  );
});

test('applies the persisted history snapshot before appending the live stream tail', async () => {
  const transcript = [];
  const gate = createSessionEventGate(incoming => {
    if (incoming.event === 'history.message') {
      transcript.splice(0, transcript.length, incoming.payload.message.content);
      return;
    }
    if (incoming.event === 'chat.delta') {
      transcript.push(incoming.payload.content);
    }
  });
  const release = gate.suspend('session-a');

  gate.dispatch(event('chat.delta', 'session-a', { content: 'live tail' }));
  gate.dispatch(event('history.message', 'session-a', {
    message: { content: 'persisted prefix' },
  }));

  assert.deepEqual(transcript, ['persisted prefix']);

  release();
  await Promise.resolve();

  assert.deepEqual(transcript, ['persisted prefix', 'live tail']);
});

test('keeps history and error control events immediate while live events are suspended', () => {
  const received = [];
  const gate = createSessionEventGate(incoming => received.push(incoming.event));
  gate.suspend('session-a');

  gate.dispatch(event('chat.delta', 'session-a'));
  gate.dispatch(event('history.message', 'session-a'));
  gate.dispatch(event('chat.error', 'session-a'));
  gate.dispatch(event('security.alert', 'session-a'));

  assert.deepEqual(received, ['history.message', 'chat.error', 'security.alert']);
});

test('transfers queued events across overlapping history restore generations', async () => {
  const received = [];
  const gate = createSessionEventGate(incoming => received.push(incoming.payload.content));
  const releaseFirst = gate.suspend('session-a');

  gate.dispatch(event('chat.delta', 'session-a', { content: 'before replacement' }));
  const releaseSecond = gate.suspend('session-a');
  releaseFirst();
  await Promise.resolve();
  assert.deepEqual(received, []);

  gate.dispatch(event('chat.delta', 'session-a', { content: 'during replacement' }));
  releaseSecond();
  releaseSecond();
  await Promise.resolve();

  assert.deepEqual(received, ['before replacement', 'during replacement']);
});

test('releases the gate when a cancelled activation abandons the restore (JW-80)', async () => {
  // performSessionRestore 挂起闸门后,若二次 canActivate 检查失败,
  // 必须与元数据加载失败等路径一样调用 release,否则该会话事件永久积压。
  const received = [];
  const gate = createSessionEventGate(incoming => received.push(incoming.payload.content));
  const release = gate.suspend('session-a');

  gate.dispatch(event('chat.delta', 'session-a', { content: 'queued while suspended' }));

  // canActivate 失败:释放闸门(修复后的 App 行为),而不是直接丢弃 release。
  release();
  await Promise.resolve();

  assert.deepEqual(received, ['queued while suspended']);
});

test('keeps events suspended indefinitely when the release is leaked (JW-80 leak symptom)', async () => {
  const received = [];
  const gate = createSessionEventGate(incoming => received.push(incoming.payload.content));
  gate.suspend('session-a');

  gate.dispatch(event('chat.delta', 'session-a', { content: 'first' }));
  gate.dispatch(event('chat.delta', 'session-a', { content: 'second' }));
  await Promise.resolve();
  await Promise.resolve();

  // 泄漏(修复前的 App 行为):depth 永不归零,事件全部积压、永不下发。
  assert.deepEqual(received, []);
});
