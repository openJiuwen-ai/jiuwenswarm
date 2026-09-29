import test from 'node:test';
import assert from 'node:assert/strict';

import {
  getSubagentStatusLabelKey,
  getSubagentStatusTone,
} from '../node_modules/.cache/subagent-status/subagentStatusPresentation.mjs';

test('status presentation keeps normal completion distinct from neutral closure', () => {
  assert.equal(getSubagentStatusTone('running', null), 'running');
  assert.equal(getSubagentStatusTone('idle', null), 'waiting');
  assert.equal(getSubagentStatusTone('idle', null, 'failed'), 'danger');
  assert.equal(getSubagentStatusTone('idle', null, 'cancelled'), 'neutral');
  assert.equal(getSubagentStatusTone('closed', 'completed'), 'success');
  assert.equal(getSubagentStatusTone('closed', 'failed'), 'danger');
  assert.equal(getSubagentStatusTone('closed', 'evicted'), 'danger');
  assert.equal(getSubagentStatusTone('closed', 'cancelled'), 'success');
  assert.equal(getSubagentStatusTone('closed', 'parent_ended'), 'success');
  assert.equal(getSubagentStatusTone('closed', 'manual'), 'success');
  assert.equal(getSubagentStatusTone('closed', null), 'success');
});

test('status presentation uses localized labels for every terminal reason', () => {
  assert.equal(getSubagentStatusLabelKey('running', null), 'subagent.running');
  assert.equal(getSubagentStatusLabelKey('idle', null), 'subagent.idle');
  assert.equal(getSubagentStatusLabelKey('idle', null, 'failed'), 'subagent.failed');
  assert.equal(getSubagentStatusLabelKey('idle', null, 'cancelled'), 'subagent.cancelled');
  assert.equal(getSubagentStatusLabelKey('closed', null, 'failed'), 'subagent.failed');
  assert.equal(getSubagentStatusLabelKey('closed', 'completed'), 'subagent.closed');
  assert.equal(getSubagentStatusLabelKey('closed', 'failed'), 'subagent.failed');
  assert.equal(getSubagentStatusLabelKey('closed', 'evicted'), 'subagent.failed');
  assert.equal(getSubagentStatusLabelKey('closed', 'cancelled'), 'subagent.closed');
  assert.equal(getSubagentStatusLabelKey('closed', 'parent_ended'), 'subagent.closed');
  assert.equal(getSubagentStatusLabelKey('closed', 'manual'), 'subagent.closed');
  assert.equal(getSubagentStatusLabelKey('closed', null), 'subagent.closed');
});
