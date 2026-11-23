import assert from 'node:assert/strict';
import test from 'node:test';

import {
  beginPromptSubmit,
  endPromptSubmit,
  isPromptSubmitInFlight,
  promptWaitsForModeSwitch,
  queueActionAfterModeCommit,
  queueDrainsInMode,
} from '../node_modules/.cache/session-mode-commit/features/sessionMode/modeSwitchGate.js';

test('a prompt waits only for a Session whose mode switch is pending', () => {
  assert.equal(promptWaitsForModeSwitch('web_1', true), true);
  assert.equal(promptWaitsForModeSwitch('web_1', false), false);
  assert.equal(promptWaitsForModeSwitch('web_1', undefined), false);
  assert.equal(promptWaitsForModeSwitch('', true), false);
  assert.equal(promptWaitsForModeSwitch(null, true), false);
});

test('the prompt queue drains in Agent and Team mode only', () => {
  assert.equal(queueDrainsInMode('agent'), true);
  assert.equal(queueDrainsInMode('team'), true);
  assert.equal(queueDrainsInMode('auto_harness'), false);
  assert.equal(queueDrainsInMode(undefined), false);
});

test('a committed switch drains the queue; every other outcome pauses a non-empty queue', () => {
  assert.equal(queueActionAfterModeCommit('committed', 2), 'drain');
  assert.equal(queueActionAfterModeCommit('committed', 0), 'drain');
  for (const status of ['not_applied', 'busy', 'uncertain', 'failed']) {
    assert.equal(queueActionAfterModeCommit(status, 1), 'pause', status);
    assert.equal(queueActionAfterModeCommit(status, 0), 'none', status);
  }
});

test('the submit marker is counted per Session until every submit has ended', () => {
  assert.equal(isPromptSubmitInFlight('web_a'), false);
  beginPromptSubmit('web_a');
  beginPromptSubmit('web_a');
  assert.equal(isPromptSubmitInFlight('web_a'), true);
  assert.equal(isPromptSubmitInFlight('web_b'), false);
  endPromptSubmit('web_a');
  assert.equal(isPromptSubmitInFlight('web_a'), true);
  endPromptSubmit('web_a');
  assert.equal(isPromptSubmitInFlight('web_a'), false);
  // An unmatched end never leaves a negative count behind.
  endPromptSubmit('web_a');
  beginPromptSubmit('web_a');
  assert.equal(isPromptSubmitInFlight('web_a'), true);
  endPromptSubmit('web_a');
  assert.equal(isPromptSubmitInFlight('web_a'), false);
  beginPromptSubmit(null);
  assert.equal(isPromptSubmitInFlight(null), false);
});
