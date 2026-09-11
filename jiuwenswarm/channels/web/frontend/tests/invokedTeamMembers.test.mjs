import assert from 'node:assert/strict';
import test from 'node:test';

import { filterInvokedTeamMembers } from '../node_modules/.cache/invoked-team-members/components/teamArea/invokedTeamMembers.js';

const members = [{ member_id: 'team_leader' }, { member_id: 'researcher' }, { member_id: 'writer' }, { member_id: 'designer' }];

test('keeps the legacy roster before runtime dispatch evidence arrives', () => {
  assert.deepEqual(filterInvokedTeamMembers(members, [], [], []), members);
});

test('keeps only the leader and members actually selected for this query', () => {
  const filtered = filterInvokedTeamMembers(members, [{ assignee: 'researcher' }], [{ member_id: 'writer' }], [], ['team_leader']);

  assert.deepEqual(filtered, [members[0], members[1], members[2]]);
});

test('execution events count as dispatch evidence without a task snapshot', () => {
  const filtered = filterInvokedTeamMembers(members, [], [], [{ member_id: 'designer' }]);
  assert.deepEqual(filtered, [members[0], members[3]]);
});
