import assert from 'node:assert/strict';
import test from 'node:test';

import {
  mayProcessingTrueFrameRelight,
  maySnapshotRelightProcessing,
} from '../node_modules/.cache/interrupt-settle/interruptSettleGuard.mjs';
import { useChatStore } from '../node_modules/.cache/interrupt-settle/chatStore.mjs';

// bug1112 regression: after a successful pause/cancel ack, a lagging
// session.get_metadata snapshot (status=processing, is_processing=true) must
// not relight foreground processing. run2 evidence: ack(has_active_task=false)
// settled the turn, the metadata poll 80ms later still said processing, and
// the writeback loop kept the stop button stuck for 11 minutes.

test('snapshot relight guard: interrupt-settled window blocks busy snapshot, others pass', () => {
  // Normal case: no settle window — a busy snapshot may relight (page refresh).
  assert.equal(maySnapshotRelightProcessing(true, false, false), true);
  // Idle snapshots always pass through (settling to idle is always allowed).
  assert.equal(maySnapshotRelightProcessing(false, true, false), true);
  // bug1112 core: settle window open — busy snapshot must not relight.
  assert.equal(maySnapshotRelightProcessing(true, true, false), false);
  // Reconnect snapshot (allowDowngrade) restores a running turn legitimately.
  assert.equal(maySnapshotRelightProcessing(true, true, true), true);
});

test('synthetic processing(true) guard: frames without event_type are blocked while settled', () => {
  // Genuine wire frames carry event_type — always allowed.
  assert.equal(mayProcessingTrueFrameRelight(true, true), true);
  assert.equal(mayProcessingTrueFrameRelight(true, false), true);
  // webClient.resumeExecution synthesizes {session_id, is_processing} with no
  // event_type; inside the settle window it is a stale-snapshot echo.
  assert.equal(mayProcessingTrueFrameRelight(false, true), false);
  assert.equal(mayProcessingTrueFrameRelight(false, false), true);
});

test('run2 frame sequence: pause ack settles, stale snapshot and synthetic frame stay idle', () => {
  const sessionId = 'bug1112-run2';
  const store = useChatStore.getState();
  store.ensureRuntime(sessionId);
  try {
    // Round running (processing_status true wire frame).
    store.setProcessing(sessionId, true);
    assert.equal(store.getRuntime(sessionId).foregroundIsProcessing, true);

    // chat.interrupt_result {intent: pause, success: true, has_active_task: false}
    // → handler: setPaused(false) + setProcessing(false) + markInterruptSettled.
    store.setPaused(sessionId, false);
    store.setProcessing(sessionId, false);
    store.markInterruptSettled(sessionId);
    assert.equal(store.getRuntime(sessionId).foregroundIsProcessing, false);

    // session.get_metadata answers status=processing, is_processing=true —
    // App.tsx writeback consults the guard and must skip setProcessing(true).
    const interruptSettled = store.getRuntime(sessionId).interruptSettledEpoch != null;
    assert.equal(interruptSettled, true);
    assert.equal(maySnapshotRelightProcessing(true, interruptSettled, false), false);
    // The writeback is skipped, so the store stays idle.
    assert.equal(store.getRuntime(sessionId).foregroundIsProcessing, false);

    // webClient synthetic processing_status(true) (no event_type) — blocked too.
    assert.equal(
      mayProcessingTrueFrameRelight(false, store.getRuntime(sessionId).interruptSettledEpoch != null),
      false,
    );
    assert.equal(store.getRuntime(sessionId).foregroundIsProcessing, false);

    // A genuine new round: user sends (clearInterruptSettled) then a real
    // processing_status(true) wire frame arrives — relight allowed again.
    store.clearInterruptSettled(sessionId);
    assert.equal(store.getRuntime(sessionId).interruptSettledEpoch, null);
    assert.equal(maySnapshotRelightProcessing(true, false, false), true);
    store.setProcessing(sessionId, true);
    assert.equal(store.getRuntime(sessionId).foregroundIsProcessing, true);
  } finally {
    useChatStore.getState().removeRuntime(sessionId);
  }
});

test('wire processing_status(true) ends the settle window even without a user send', () => {
  const sessionId = 'bug1112-wire-true';
  const store = useChatStore.getState();
  store.ensureRuntime(sessionId);
  try {
    store.markInterruptSettled(sessionId);
    assert.notEqual(store.getRuntime(sessionId).interruptSettledEpoch, null);
    // handler clears the marker when a true wire frame arrives
    store.clearInterruptSettled(sessionId);
    assert.equal(store.getRuntime(sessionId).interruptSettledEpoch, null);
  } finally {
    useChatStore.getState().removeRuntime(sessionId);
  }
});
