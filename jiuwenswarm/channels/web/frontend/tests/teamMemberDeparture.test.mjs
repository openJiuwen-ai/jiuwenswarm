import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isDepartedTeamMemberStatus,
  isReachableTeamMemberStatus,
  reconcileSnapshotRoster,
} from '../node_modules/.cache/team-member-departure/teamMemberDeparture.mjs';

const member = (member_id, status = 'ready') => ({ member_id, status });
const ids = (members) => members.map((item) => item.member_id);

test('only shut_down is departed; shutdown_requested is still on the roster but unreachable', () => {
  assert.equal(isDepartedTeamMemberStatus('shut_down'), true);
  assert.equal(isDepartedTeamMemberStatus(' SHUT_DOWN '), true);
  for (const status of ['shutdown_requested', 'dormant', 'ready', '', undefined]) {
    assert.equal(isDepartedTeamMemberStatus(status), false, String(status));
  }

  assert.equal(isReachableTeamMemberStatus('ready'), true);
  assert.equal(isReachableTeamMemberStatus('dormant'), true);
  assert.equal(isReachableTeamMemberStatus('shutdown_requested'), false);
  assert.equal(isReachableTeamMemberStatus('shut_down'), false);
});

test('a team whose teammates all departed still drops them from the roster', () => {
  const result = reconcileSnapshotRoster(
    [],
    new Set(['reviewer', 'tester']),
    [member('reviewer'), member('tester')],
    new Set(),
  );

  assert.deepEqual(ids(result.members), []);
  assert.deepEqual(result.reconciledMemberIds, ['reviewer', 'tester']);
});

test('departed members mixed into an old backend snapshot are left out', () => {
  const result = reconcileSnapshotRoster(
    [member('worker'), member('retired', 'shut_down')],
    new Set(['retired']),
    [member('worker'), member('retired')],
    new Set(),
  );

  assert.deepEqual(ids(result.members), ['worker']);
});

test('members missing from the snapshot are dropped unless they changed during the request', () => {
  const result = reconcileSnapshotRoster(
    [member('worker', 'ready')],
    new Set(),
    [member('worker', 'busy'), member('stale'), member('fresh', 'starting')],
    new Set(['fresh']),
  );

  // worker 以快照为准；stale 未出现在快照里也没有实时事件，移除；fresh 以实时状态为准保留。
  assert.deepEqual(result.members.map((item) => [item.member_id, item.status]), [
    ['worker', 'ready'],
    ['fresh', 'starting'],
  ]);
  assert.deepEqual(result.reconciledMemberIds, ['worker']);
});

test('nothing to reconcile returns null so the caller keeps its roster', () => {
  assert.equal(reconcileSnapshotRoster([], new Set(), [member('worker')], new Set()), null);
  assert.equal(
    reconcileSnapshotRoster([member('worker')], new Set(), [member('worker')], new Set(['worker'])),
    null,
  );
});
