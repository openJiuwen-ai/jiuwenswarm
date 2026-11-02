import assert from 'node:assert/strict';
import test from 'node:test';

import {
  heartbeatStatusVariant,
  heartbeatStatusLabelKey,
  heartbeatRunNowMessageKey,
  heartbeatCancelMessageKey,
  heartbeatLastRunStatusLabelKey,
  canHeartbeatRunNow,
} from '../node_modules/.cache/heartbeat-status-text/components/HeartbeatPanel/heartbeatStatusText.js';

test('heartbeatStatusVariant maps every backend status to a display variant', () => {
  assert.equal(heartbeatStatusVariant('scheduled'), 'scheduled');
  assert.equal(heartbeatStatusVariant('running'), 'running');
  assert.equal(heartbeatStatusVariant('disabled'), 'paused');
  assert.equal(heartbeatStatusVariant('completed'), 'completed');
  assert.equal(heartbeatStatusVariant('expired'), 'expired');
});

test('heartbeatStatusLabelKey namespaces under heartbeat.status.*', () => {
  assert.equal(heartbeatStatusLabelKey('running'), 'heartbeat.status.running');
  assert.equal(heartbeatStatusLabelKey('disabled'), 'heartbeat.status.paused');
});

test('heartbeatRunNowMessageKey: accepted without queue', () => {
  assert.equal(heartbeatRunNowMessageKey(true), 'heartbeat.toast.runNowAccepted');
});

test('heartbeatRunNowMessageKey: accepted and queued', () => {
  assert.equal(heartbeatRunNowMessageKey(true, undefined, true), 'heartbeat.toast.runNowQueued');
});

test('heartbeatRunNowMessageKey: rejected with a known reason', () => {
  assert.equal(heartbeatRunNowMessageKey(false, 'session_busy'), 'heartbeat.toast.runNowRejected.session_busy');
  assert.equal(
    heartbeatRunNowMessageKey(false, 'replacement_cancel_failed'),
    'heartbeat.toast.runNowRejected.replacement_cancel_failed',
  );
});

test('heartbeatRunNowMessageKey: rejected with an unrecognized reason falls back to unknown', () => {
  assert.equal(heartbeatRunNowMessageKey(false, 'something_new_from_backend'), 'heartbeat.toast.runNowRejected.unknown');
  assert.equal(heartbeatRunNowMessageKey(false, undefined), 'heartbeat.toast.runNowRejected.unknown');
});

test('heartbeatCancelMessageKey maps known cancel_status values', () => {
  assert.equal(heartbeatCancelMessageKey('idle'), 'heartbeat.toast.cancel.idle');
  assert.equal(heartbeatCancelMessageKey('cancelled'), 'heartbeat.toast.cancel.cancelled');
  assert.equal(heartbeatCancelMessageKey('not_found'), 'heartbeat.toast.cancel.not_found');
  assert.equal(heartbeatCancelMessageKey('failed'), 'heartbeat.toast.cancel.failed');
});

test('heartbeatCancelMessageKey falls back to failed for unknown values', () => {
  assert.equal(heartbeatCancelMessageKey('bogus'), 'heartbeat.toast.cancel.failed');
});

test('heartbeatLastRunStatusLabelKey returns null for null input, key otherwise', () => {
  assert.equal(heartbeatLastRunStatusLabelKey(null), null);
  assert.equal(heartbeatLastRunStatusLabelKey('failed'), 'heartbeat.runState.failed');
  assert.equal(heartbeatLastRunStatusLabelKey('skipped'), 'heartbeat.runState.skipped');
});

test('canHeartbeatRunNow: only enabled+scheduled allows run_now', () => {
  assert.equal(canHeartbeatRunNow(true, 'scheduled'), true);
});

test('canHeartbeatRunNow: disabled statuses are all rejected', () => {
  assert.equal(canHeartbeatRunNow(true, 'running'), false);
  assert.equal(canHeartbeatRunNow(true, 'completed'), false);
  assert.equal(canHeartbeatRunNow(true, 'expired'), false);
  assert.equal(canHeartbeatRunNow(true, 'disabled'), false);
});

test('canHeartbeatRunNow: enabled=false always rejected even if status is scheduled', () => {
  assert.equal(canHeartbeatRunNow(false, 'scheduled'), false);
});
