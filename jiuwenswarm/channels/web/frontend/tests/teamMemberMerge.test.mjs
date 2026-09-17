import assert from 'node:assert/strict';
import test from 'node:test';

import { useSessionStore } from '../node_modules/.cache/team-member-merge/sessionStore.mjs';

const SESSION_ID = 'team-member-merge';

function seedRoster() {
  useSessionStore.getState().ensureRuntime(SESSION_ID);
  useSessionStore.getState().setTeamMembers(SESSION_ID, []);
  useSessionStore.getState().addTeamMember(SESSION_ID, {
    id: 'member-1',
    member_id: 'research-specialist',
    status: 'unstarted',
    timestamp: 1,
    name: '研究专家',
    mode: 'teammate',
  });
}

function currentMember() {
  return useSessionStore
    .getState()
    .runtimes[SESSION_ID].teamMembers.find((m) => m.member_id === 'research-specialist');
}

test('a later event without a name keeps the known display name', () => {
  seedRoster();

  // team.member.spawned 只带 member_id / status，不带 name。
  useSessionStore.getState().addTeamMember(SESSION_ID, {
    id: 'member-2',
    member_id: 'research-specialist',
    status: 'ready',
    timestamp: 2,
    name: undefined,
    mode: undefined,
  });

  const member = currentMember();
  assert.equal(member.name, '研究专家');
  assert.equal(member.mode, 'teammate');
  assert.equal(member.status, 'ready');
});

test('a non-empty name still wins over the known one', () => {
  seedRoster();

  useSessionStore.getState().addTeamMember(SESSION_ID, {
    id: 'member-2',
    member_id: 'research-specialist',
    status: '',
    timestamp: 2,
    name: '资深研究专家',
  });

  const member = currentMember();
  assert.equal(member.name, '资深研究专家');
  // 空 status 同样不覆盖已知值。
  assert.equal(member.status, 'unstarted');
});
