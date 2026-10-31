import assert from 'node:assert/strict';
import test from 'node:test';

import {
  useSessionStore,
  useTeamSelectorStore,
  applyTeamSnapshotToSession,
} from '../node_modules/.cache/team-slice-apply/teamSliceApply.mjs';

const SESSION_ID = 'a10-owner-session';

/**
 * Owner-team setup mirroring the real bug scene: the selector knows an owner
 * team for the session, so applyTeamSnapshotToSession writes the projection
 * under the bare sessionId (the main conversation's member panel).
 */
function seedOwnerTeam() {
  useSessionStore.getState().ensureRuntime(SESSION_ID);
  // Zustand stores persist across tests in the same process; start from a
  // known baseline.
  useSessionStore.getState().setTeamMembers(SESSION_ID, []);
  useSessionStore.getState().setTeamDepartedMembers(SESSION_ID, []);
  useTeamSelectorStore.setState({
    runtimes: {
      [SESSION_ID]: {
        teams: [{ team_id: 'owner-team', team_name: 'owner-team', is_owner: true }],
        selectedTeamId: 'owner-team',
        defaultTeamId: 'owner-team',
        loading: false,
        error: null,
        loaded: true,
      },
    },
  });
}

function mirroredWorker(runId) {
  return {
    id: `mirror-${runId}`,
    member_id: `wf_${runId}`,
    status: 'completed',
    timestamp: 1,
    name: '工作流执行员',
    mode: 'worker',
    role: 'worker',
    workflow_run_id: `wf_${runId}`,
  };
}

function currentMembers() {
  return useSessionStore.getState().runtimes[SESSION_ID].teamMembers;
}

test('snapshot replacement keeps mirrored workers that are not in the roster', () => {
  seedOwnerTeam();
  // Live projection built from team.member.spawned events: real member + two
  // mirrored workers (one running, one already finished — neither re-announces).
  useSessionStore.getState().setTeamMembers(SESSION_ID, [
    mirroredWorker('a'),
    { id: 'm-real', member_id: 'researcher', status: 'ready', timestamp: 1 },
    mirroredWorker('b'),
  ]);

  // The DB roster only knows the real member — mirrored workers never persist.
  applyTeamSnapshotToSession(SESSION_ID, 'owner-team', {
    members: [{ member_id: 'researcher', status: 'ready', name: '研究员' }],
    tasks: [],
  });

  const members = currentMembers();
  // Mirrored members go to the front (same insert-front semantics as
  // addTeamMember); run-group order follows first appearance.
  assert.deepEqual(members.map((m) => m.member_id), ['wf_a', 'wf_b', 'researcher']);
  // The mirrored members pass through untouched (id/status/workflow_run_id).
  assert.equal(members[0].workflow_run_id, 'wf_a');
  assert.equal(members[0].id, 'mirror-a');
  assert.equal(members[1].workflow_run_id, 'wf_b');
  assert.equal(members[1].id, 'mirror-b');
});

test('a truly departed member (no workflow_run_id, absent from snapshot) is removed', () => {
  seedOwnerTeam();
  useSessionStore.getState().setTeamMembers(SESSION_ID, [
    { id: 'm-real', member_id: 'researcher', status: 'ready', timestamp: 1 },
    { id: 'm-departed', member_id: 'writer', status: 'shut_down', timestamp: 1 },
    { id: 'm-stale', member_id: 'reviewer', status: 'ready', timestamp: 1 },
  ]);

  // Snapshot dropped writer (departed) and reviewer (gone entirely).
  applyTeamSnapshotToSession(SESSION_ID, 'owner-team', {
    members: [{ member_id: 'researcher', status: 'ready' }],
    tasks: [],
  });

  assert.deepEqual(currentMembers().map((m) => m.member_id), ['researcher']);
});

test('a member_id present in both sides resolves to the snapshot version', () => {
  seedOwnerTeam();
  useSessionStore.getState().setTeamMembers(SESSION_ID, [
    {
      id: 'm-dup',
      member_id: 'researcher',
      status: 'stale-status',
      timestamp: 1,
      workflow_run_id: 'wf_x',
    },
  ]);

  applyTeamSnapshotToSession(SESSION_ID, 'owner-team', {
    members: [{ member_id: 'researcher', status: 'ready', name: '研究员' }],
    tasks: [],
  });

  const members = currentMembers();
  assert.deepEqual(members.map((m) => m.member_id), ['researcher']);
  assert.equal(members[0].status, 'ready');
  assert.equal(members[0].name, '研究员');
  // Snapshot wins the conflict entirely — no mirrored fields leaked in.
  assert.equal(members[0].workflow_run_id, undefined);
});

test('an empty snapshot still writes back and keeps mirrored workers', () => {
  seedOwnerTeam();
  useSessionStore.getState().setTeamMembers(SESSION_ID, [
    mirroredWorker('a'),
    { id: 'm-real', member_id: 'researcher', status: 'ready', timestamp: 1 },
  ]);

  // Empty roster (monitor down / DB empty) must still clear real members —
  // this is the full-replacement contract — while mirrored workers survive.
  applyTeamSnapshotToSession(SESSION_ID, 'owner-team', {
    members: [],
    tasks: [],
  });

  assert.deepEqual(currentMembers().map((m) => m.member_id), ['wf_a']);
});

test('member mapping passes through workflow_run_id / agent_template / display_name', () => {
  seedOwnerTeam();

  applyTeamSnapshotToSession(SESSION_ID, 'owner-team', {
    members: [
      {
        member_id: 'wf_f1c3',
        status: 'completed',
        workflow_run_id: 'wf_f1c3',
        agent_template: 'research-specialist',
        display_name: '研究专家',
      },
    ],
    tasks: [],
  });

  const member = currentMembers()[0];
  assert.equal(member.workflow_run_id, 'wf_f1c3');
  assert.equal(member.agent_template, 'research-specialist');
  assert.equal(member.display_name, '研究专家');
});
