import assert from 'node:assert/strict';
import test from 'node:test';

import { resolvePendingPreviousSession } from '../node_modules/.cache/new-conversation-previous-session/newConversationPreviousSession.mjs';

const resolve = (overrides = {}) => resolvePendingPreviousSession({
  currentSessionId: 'new',
  currentMode: 'agent',
  pending: null,
  newConversationId: 'new',
  ...overrides,
});

test('captures a real Session as the pending previous Session', () => {
  assert.deepEqual(resolve({ currentSessionId: 'A' }), {
    sessionId: 'A',
    mode: 'agent',
  });
});

test('repeated new clicks preserve the real pending previous Session', () => {
  const pending = { sessionId: 'A', mode: 'agent' };
  assert.equal(resolve({ pending }), pending);
});

test('a clean draft has no previous Session', () => {
  assert.equal(resolve(), null);
});

test('deleting the current Session clears the pending previous Session', () => {
  assert.equal(resolve({
    currentSessionId: 'B',
    pending: { sessionId: 'A', mode: 'agent' },
    clear: true,
  }), null);
});

test('a newly created B replaces A as previous when B remains alive', () => {
  assert.deepEqual(resolve({
    currentSessionId: 'B',
    pending: { sessionId: 'A', mode: 'agent' },
  }), {
    sessionId: 'B',
    mode: 'agent',
  });
});
