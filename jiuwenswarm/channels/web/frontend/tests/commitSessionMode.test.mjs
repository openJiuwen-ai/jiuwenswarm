import assert from 'node:assert/strict';
import test from 'node:test';

import {
  commitSessionMode,
  sessionModeErrorCode,
} from '../node_modules/.cache/session-mode-commit/features/sessionMode/commitSessionMode.js';
import {
  allowsInPlaceModeSwitch,
  canSwitchSessionModeInPlace,
} from '../node_modules/.cache/session-mode-commit/features/sessionMode/inPlaceModeSwitch.js';

const INPUT = { sessionId: 'web_1', mode: 'team.work.normal', expectedMode: 'agent.work.normal' };

function codedError(code, message = code) {
  return Object.assign(new Error(message), { code });
}

/** A request double that answers each call from `replies` in order and records it. */
function scriptedRequest(replies) {
  const calls = [];
  const request = async (method, params) => {
    calls.push({ method, params });
    const reply = replies.shift();
    assert.ok(reply, `unexpected ${method} call`);
    if (reply.error) throw reply.error;
    return reply.payload;
  };
  return { request, calls };
}

test('mode.set commits the target with the expected mode', async () => {
  const { request, calls } = scriptedRequest([{ payload: { mode: 'team.work.normal', changed: true } }]);
  const outcome = await commitSessionMode(request, INPUT);
  assert.deepEqual(outcome, { status: 'committed', mode: 'team.work.normal', changed: true });
  assert.deepEqual(calls, [{
    method: 'mode.set',
    params: { session_id: 'web_1', mode: 'team.work.normal', expected_mode: 'agent.work.normal' },
  }]);
});

test('a conflict adopts the mode the server reads back', async () => {
  const { request, calls } = scriptedRequest([
    { error: codedError('MODE_CONFLICT') },
    { payload: { mode: 'agent.code.normal' } },
  ]);
  const outcome = await commitSessionMode(request, INPUT);
  assert.deepEqual(outcome, { status: 'not_applied', mode: 'agent.code.normal' });
  assert.deepEqual(calls.map((call) => call.method), ['mode.set', 'mode.get']);
  assert.deepEqual(calls[1].params, { session_id: 'web_1' });
});

test('a busy Session commits nothing and is not retried', async () => {
  const { request, calls } = scriptedRequest([{ error: codedError('SESSION_BUSY') }]);
  assert.deepEqual(await commitSessionMode(request, INPUT), { status: 'busy' });
  assert.equal(calls.length, 1);
});

test('a lost reply retries once; an already-applied target settles as committed', async () => {
  const { request, calls } = scriptedRequest([
    { error: codedError('REQUEST_TIMEOUT') },
    { payload: { mode: 'team.work.normal', changed: false } },
  ]);
  const outcome = await commitSessionMode(request, INPUT);
  assert.deepEqual(outcome, { status: 'committed', mode: 'team.work.normal', changed: false });
  assert.deepEqual(calls.map((call) => call.method), ['mode.set', 'mode.set']);
});

test('a busy retry after a lost reply reads the mode back; still the old mode is busy', async () => {
  const { request, calls } = scriptedRequest([
    { error: codedError('WS_DISCONNECTED') },
    { error: codedError('SESSION_BUSY') },
    { payload: { mode: 'agent.work.normal' } },
  ]);
  assert.deepEqual(await commitSessionMode(request, INPUT), { status: 'busy' });
  assert.deepEqual(calls.map((call) => call.method), ['mode.set', 'mode.set', 'mode.get']);
});

test('a busy retry after a lost reply settles as committed when the first attempt landed', async () => {
  // The first mode.set committed, then a prompt took the Session before the retry.
  const { request } = scriptedRequest([
    { error: codedError('REQUEST_TIMEOUT') },
    { error: codedError('SESSION_BUSY') },
    { payload: { mode: 'team.work.normal' } },
  ]);
  assert.deepEqual(
    await commitSessionMode(request, INPUT),
    { status: 'committed', mode: 'team.work.normal', changed: false },
  );
});

test('a busy retry whose read-back also fails is uncertain', async () => {
  const readError = codedError('REQUEST_TIMEOUT');
  const { request } = scriptedRequest([
    { error: codedError('WS_DISCONNECTED') },
    { error: codedError('SESSION_BUSY') },
    { error: readError },
  ]);
  assert.deepEqual(await commitSessionMode(request, INPUT), { status: 'uncertain', error: readError });
});

test('two lost replies fall back to mode.get', async () => {
  const { request, calls } = scriptedRequest([
    { error: codedError('REQUEST_TIMEOUT') },
    { error: codedError('AGENT_SERVER_TIMEOUT') },
    { payload: { mode: 'team.work.normal' } },
  ]);
  const outcome = await commitSessionMode(request, INPUT);
  assert.deepEqual(outcome, { status: 'committed', mode: 'team.work.normal', changed: false });
  assert.deepEqual(calls.map((call) => call.method), ['mode.set', 'mode.set', 'mode.get']);
});

test('an unreadable outcome is reported as uncertain', async () => {
  const readError = codedError('REQUEST_TIMEOUT');
  const { request } = scriptedRequest([
    { error: codedError('REQUEST_TIMEOUT') },
    { error: codedError('REQUEST_TIMEOUT') },
    { error: readError },
  ]);
  assert.deepEqual(await commitSessionMode(request, INPUT), { status: 'uncertain', error: readError });
});

test('a definite refusal fails without retry or read-back', async () => {
  for (const code of ['BAD_REQUEST', 'NOT_FOUND', 'WS_NOT_READY', 'INTERNAL_ERROR']) {
    const error = codedError(code);
    const { request, calls } = scriptedRequest([{ error }]);
    assert.deepEqual(await commitSessionMode(request, INPUT), { status: 'failed', code, error });
    assert.equal(calls.length, 1);
  }
});

test('a failure without a code is uncertain: retry once, then read back', async () => {
  // The Gateway reports a dropped AgentServer link as ok:false with only an error text.
  const { request, calls } = scriptedRequest([
    { error: new Error('AgentServer WebSocket connection closed') },
    { error: new Error('AgentServer WebSocket connection closed') },
    { payload: { mode: 'team.work.normal' } },
  ]);
  const outcome = await commitSessionMode(request, INPUT);
  assert.deepEqual(outcome, { status: 'committed', mode: 'team.work.normal', changed: false });
  assert.deepEqual(calls.map((call) => call.method), ['mode.set', 'mode.set', 'mode.get']);
});

test('a read-back that differs only in plan state settles as committed', async () => {
  const { request } = scriptedRequest([
    { error: codedError('REQUEST_TIMEOUT') },
    { error: codedError('REQUEST_TIMEOUT') },
    { payload: { mode: 'team.work.plan' } },
  ]);
  assert.deepEqual(
    await commitSessionMode(request, INPUT),
    { status: 'committed', mode: 'team.work.plan', changed: false },
  );
});

test('a plain Agent/Team mode from an AgentOS-routed Gateway settles by route', async () => {
  const committed = scriptedRequest([{ payload: { mode: 'team', changed: true } }]);
  assert.deepEqual(
    await commitSessionMode(committed.request, INPUT),
    { status: 'committed', mode: 'team', changed: true },
  );
  const conflict = scriptedRequest([
    { error: codedError('MODE_CONFLICT') },
    { payload: { mode: 'agent' } },
  ]);
  assert.deepEqual(await commitSessionMode(conflict.request, INPUT), { status: 'not_applied', mode: 'agent' });
});

test('error codes are read from error.code only', () => {
  assert.equal(sessionModeErrorCode(codedError('MODE_CONFLICT')), 'MODE_CONFLICT');
  assert.equal(sessionModeErrorCode(new Error('MODE_CONFLICT')), undefined);
  assert.equal(sessionModeErrorCode(null), undefined);
});

test('in-place switching needs the personal edition and the WebSocket transport', () => {
  const personal = { enterprise: false, transport: 'websocket' };
  assert.equal(allowsInPlaceModeSwitch(personal), true);
  assert.equal(allowsInPlaceModeSwitch({ ...personal, enterprise: true }), false);
  assert.equal(allowsInPlaceModeSwitch({ ...personal, transport: 'http' }), false);
});

test('only Agent and Team switch in place', () => {
  const personal = { enterprise: false, transport: 'websocket' };
  assert.equal(canSwitchSessionModeInPlace(personal, 'agent', 'team'), true);
  assert.equal(canSwitchSessionModeInPlace(personal, 'team', 'agent'), true);
  assert.equal(canSwitchSessionModeInPlace(personal, 'agent', 'agent'), false);
  assert.equal(canSwitchSessionModeInPlace(personal, 'auto_harness', 'agent'), false);
  assert.equal(canSwitchSessionModeInPlace(personal, undefined, 'team'), false);
  assert.equal(canSwitchSessionModeInPlace({ ...personal, enterprise: true }, 'agent', 'team'), false);
});
