import assert from 'node:assert/strict';
import test from 'node:test';
import { ChatRetryRequests, canRetryRequest } from '../node_modules/.cache/chat-retry/chatRetry.mjs';

const user = (id) => ({ id, role: 'user', content: 'hello', timestamp: new Date().toISOString() });
const payload = () => ({
  session_id: 'session-a',
  content: 'original prompt',
  model_name: 'original-model',
  media_items: [{ path: '/test/document.pdf', type: 'document' }],
  skills: ['original-skill'],
  metadata: { settings: { temperature: 0.2 } },
});

test('captures independent model, attachment and settings snapshot', () => {
  const store = new ChatRetryRequests();
  const original = payload();
  store.remember('req-1', original, 'user-1');
  original.model_name = 'different-model';
  original.media_items[0].path = '/different.pdf';
  original.skills.length = 0;
  original.metadata.settings.temperature = 1;
  assert.deepEqual(store.get('req-1').payload, payload());
});

test('matches the right turn and rejects a different conversation or later prompt', () => {
  const store = new ChatRetryRequests();
  store.remember('req-1', payload(), 'user-1');
  const saved = store.get('req-1');
  assert.equal(canRetryRequest(saved, 'session-a', [user('user-1')]), true);
  assert.equal(canRetryRequest(saved, 'session-b', [user('user-1')]), false);
  assert.equal(canRetryRequest(saved, 'session-a', [user('user-1'), user('user-2')]), false);
  assert.equal(canRetryRequest(saved, 'session-a', []), false);
  assert.equal(canRetryRequest(undefined, 'session-a', [user('user-1')]), false);
});

test('never retries a turn after tool work', () => {
  const store = new ChatRetryRequests();
  store.remember('req-1', payload(), 'user-1');
  for (const tool of [{ role: 'tool' }, { role: 'assistant', toolCall: { id: 'tool-1' } }]) {
    assert.equal(canRetryRequest(store.get('req-1'), 'session-a', [user('user-1'), tool]), false);
  }
});

test('new attempt replaces old ID and remains recoverable after another failure', () => {
  const store = new ChatRetryRequests();
  store.remember('req-1', payload(), 'user-1');
  const saved = store.get('req-1');
  store.remember('req-2', saved.payload, saved.userMessageId);
  assert.equal(store.get('req-1'), undefined);
  assert.equal(canRetryRequest(store.get('req-2'), 'session-a', [user('user-1'), { role: 'system' }]), true);
  assert.deepEqual(store.get('req-2').payload, payload());
});

test('completion, deletion, expiration and capacity bound request retention', () => {
  const store = new ChatRetryRequests(2);
  store.remember('a', payload(), 'user-a');
  store.remember('b', { ...payload(), session_id: 'session-b' }, 'user-b');
  store.remember('c', { ...payload(), session_id: 'session-c' }, 'user-c');
  assert.equal(store.get('a'), undefined);
  store.pruneSessions(new Set(['session-c']));
  assert.equal(store.get('b'), undefined);
  store.forget('c');
  assert.equal(store.get('c'), undefined);
  const expired = new ChatRetryRequests(2, 0);
  expired.remember('expired', payload(), 'user-1');
  assert.equal(expired.get('expired'), undefined);
  assert.equal(new ChatRetryRequests().get('historical-request'), undefined);
});

test('excludes team workflows and goal steering from whole-request retry', () => {
  const store = new ChatRetryRequests();
  for (const extra of [{ mode: 'team' }, { input_mode: 'steer' }, { enable_swarmflow: true }]) {
    store.remember('req', { ...payload(), ...extra }, 'user-1');
    assert.equal(canRetryRequest(store.get('req'), 'session-a', [user('user-1')]), false);
  }
});
