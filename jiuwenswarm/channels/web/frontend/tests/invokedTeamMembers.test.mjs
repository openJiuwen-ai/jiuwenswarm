import assert from 'node:assert/strict';
import test from 'node:test';

import {
  filterInvokedTeamMembers,
  latestUserTurnStartedAtMs,
  timestampToEpochMs,
} from '../node_modules/.cache/invoked-team-members/components/teamArea/invokedTeamMembers.js';

const members = [{ member_id: 'team_leader' }, { member_id: 'researcher' }, { member_id: 'writer' }, { member_id: 'designer' }];
const turnStartedAt = 1_789_091_100_000;

test('shows only the leader before current-turn dispatch evidence arrives', () => {
  assert.deepEqual(filterInvokedTeamMembers(members, [], [], [], ['team_leader'], turnStartedAt), [members[0]]);
});

test('keeps only the leader and members actually selected for this query', () => {
  const filtered = filterInvokedTeamMembers(
    members,
    [{ assignee: 'researcher', timestamp: turnStartedAt + 100 }],
    [{ member_id: 'writer', timestamp: turnStartedAt + 200 }],
    [],
    ['team_leader'],
    turnStartedAt,
  );

  assert.deepEqual(filtered, [members[0], members[1], members[2]]);
});

test('execution events count as dispatch evidence without a task snapshot', () => {
  const filtered = filterInvokedTeamMembers(members, [], [], [{ member_id: 'designer', timestamp: turnStartedAt + 500 }], [], turnStartedAt);
  assert.deepEqual(filtered, [members[0], members[3]]);
});

test('does not leak members invoked by an earlier query in the same session', () => {
  const filtered = filterInvokedTeamMembers(
    members,
    [{ assignee: 'researcher', timestamp: turnStartedAt - 500 }],
    [{ member_id: 'writer', timestamp: turnStartedAt - 100 }],
    [{ member_id: 'designer', timestamp: turnStartedAt + 100 }],
    ['team_leader'],
    turnStartedAt,
  );

  assert.deepEqual(filtered, [members[0], members[3]]);
});

test('derives the current turn boundary from the latest user message', () => {
  assert.equal(
    latestUserTurnStartedAtMs([
      { role: 'user', timestamp: '2026-09-11T01:00:00.000Z' },
      { role: 'assistant', timestamp: '2026-09-11T01:00:02.000Z' },
      { role: 'user', timestamp: '2026-09-11T01:05:00.000Z' },
    ]),
    Date.parse('2026-09-11T01:05:00.000Z'),
  );
});

test('normalizes second-based runtime timestamps before turn comparison', () => {
  assert.equal(timestampToEpochMs(1_789_091_100), 1_789_091_100_000);
});
