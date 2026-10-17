import assert from 'node:assert/strict';
import test from 'node:test';

import {
  SESSION_CREATE_METADATA_POLL_ATTEMPTS,
  SESSION_CREATE_METADATA_POLL_INTERVAL_MS,
  SESSION_CREATE_TIMEOUT_MS,
  createConversationSession,
  isAlreadyExistsError,
  isRequestTimeoutError,
  parsePersistSessionCommand,
  resolveCreatedSessionId,
} from '../node_modules/.cache/create-conversation-session/multi-session/state/createConversationSession.js';

const fastRetryOptions = {
  metadataPollAttempts: 3,
  metadataPollIntervalMs: 0,
  sleep: async () => {},
};

test('session.create constants and response ID normalization', () => {
  assert.equal(SESSION_CREATE_TIMEOUT_MS, 60_000);
  assert.equal(SESSION_CREATE_METADATA_POLL_ATTEMPTS, 5);
  assert.equal(SESSION_CREATE_METADATA_POLL_INTERVAL_MS, 500);
  assert.equal(resolveCreatedSessionId({ session_id: 'web_a' }), 'web_a');
  assert.equal(resolveCreatedSessionId({ sessionId: 'web_b' }), 'web_b');
});

test('error helpers read error.code', () => {
  assert.equal(isRequestTimeoutError({ code: 'REQUEST_TIMEOUT' }), true);
  assert.equal(isAlreadyExistsError({ code: 'ALREADY_EXISTS' }), true);
});

test('parses Persist Session only from an exact leading command', () => {
  assert.deepEqual(parsePersistSessionCommand('/persist build the login flow'), {
    content: 'build the login flow',
    persistSession: true,
  });
  assert.deepEqual(parsePersistSessionCommand('/persist + build the login flow'), {
    content: '+ build the login flow',
    persistSession: true,
  });
  assert.deepEqual(parsePersistSessionCommand('/persistent data'), {
    content: '/persistent data',
    persistSession: false,
  });
  assert.deepEqual(parsePersistSessionCommand('explain /persist usage'), {
    content: 'explain /persist usage',
    persistSession: false,
  });
  assert.deepEqual(parsePersistSessionCommand('/persist'), {
    content: '',
    persistSession: true,
  });
});

test('uses the AgentServer-returned ID', async () => {
  const calls = [];
  const request = async (method, params, options) => {
    calls.push({ method, params, options });
    return { sessionId: 'web_real', projectId: 'default', workMode: 'work' };
  };
  const created = await createConversationSession(request, {
    create_token: 'stable-token',
    mode: 'agent',
    persist_session: true,
  });
  assert.equal(created.session_id, 'web_real');
  assert.equal(created.persist_session, false);
  assert.equal(calls[0].params.session_id, undefined);
  assert.equal(calls[0].params.persist_session, true);
  assert.equal(calls[0].options.timeoutMs, SESSION_CREATE_TIMEOUT_MS);
});

test('response-loss retry reuses the same create_token', async () => {
  const calls = [];
  const request = async (method, params) => {
    calls.push({ method, params: { ...params } });
    if (calls.length === 1) {
      const error = new Error('timeout');
      error.code = 'REQUEST_TIMEOUT';
      throw error;
    }
    return { session_id: 'web_retry', persist_session: true };
  };
  const created = await createConversationSession(
    request,
    { create_token: 'retry-token', mode: 'agent' },
    fastRetryOptions,
  );
  assert.equal(created.session_id, 'web_retry');
  assert.equal(created.persist_session, true);
  assert.deepEqual(calls.map((call) => call.params.create_token), [
    'retry-token',
    'retry-token',
  ]);
  assert.deepEqual(calls.map((call) => call.params.persist_session), [
    undefined,
    undefined,
  ]);
});

test('requires create_token and does not swallow non-timeout errors', async () => {
  await assert.rejects(
    () => createConversationSession(async () => ({}), { mode: 'agent' }),
    /requires create_token/,
  );
  await assert.rejects(
    () =>
      createConversationSession(async () => {
        const error = new Error('bad request');
        error.code = 'BAD_REQUEST';
        throw error;
      }, { create_token: 'x' }),
    (error) => error.code === 'BAD_REQUEST',
  );
});
