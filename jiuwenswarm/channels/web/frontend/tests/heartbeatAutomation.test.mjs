import assert from 'node:assert/strict';
import test from 'node:test';

import { refreshHeartbeatListAtRunStart } from '../node_modules/.cache/heartbeat-automation/utils/heartbeatAutomation.js';

test('heartbeat run start refreshes the matching session immediately', () => {
  const refreshedRunIds = new Set();
  const refreshedSessions = [];

  const refreshed = refreshHeartbeatListAtRunStart(
    refreshedRunIds,
    'run-1',
    'session-1',
    (sessionId) => refreshedSessions.push(sessionId),
  );

  assert.equal(refreshed, true);
  assert.deepEqual(refreshedSessions, ['session-1']);
  assert.deepEqual([...refreshedRunIds], ['run-1']);
});

test('duplicate processing frames refresh a heartbeat run only once', () => {
  const refreshedRunIds = new Set();
  const refreshedSessions = [];
  const dispatchRefresh = (sessionId) => refreshedSessions.push(sessionId);

  assert.equal(refreshHeartbeatListAtRunStart(refreshedRunIds, 'run-1', 'session-1', dispatchRefresh), true);
  assert.equal(refreshHeartbeatListAtRunStart(refreshedRunIds, 'run-1', 'session-1', dispatchRefresh), false);
  assert.equal(refreshHeartbeatListAtRunStart(refreshedRunIds, 'run-2', 'session-1', dispatchRefresh), true);

  assert.deepEqual(refreshedSessions, ['session-1', 'session-1']);
  assert.deepEqual([...refreshedRunIds], ['run-1', 'run-2']);
});
