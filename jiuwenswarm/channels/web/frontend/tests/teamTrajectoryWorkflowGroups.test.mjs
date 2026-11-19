// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  attachWorkflowGroupsToHostLanes,
  groupTeamMemberLanesByWorkflowRun,
  memberNameOfSubjectId,
  parseEngineSwarmflowMemberId,
  shortWorkflowRunId,
  slugifyRunId,
  swarmflowIdentitiesOfMembers,
  swarmflowLaneFullName,
  swarmflowWorkerMemberName,
  workflowRunLaneStatus,
} from '../node_modules/.cache/trajectory-workflow-groups/teamTrajectoryWorkflowGroups.mjs';

function lane(subjectId, label) {
  return {
    subjectId,
    label,
    kind: 'team_member',
    status: 'completed',
    traceCount: 1,
    recordCount: 1,
  };
}

test('slugifyRunId mirrors agent-core slug rules', () => {
  assert.equal(slugifyRunId('wf_7e02a12cabdf'), 'wf-7e02a12cabdf');
  assert.equal(slugifyRunId('WF_7E02A12CABDF'), 'wf-7e02a12cabdf');
  assert.equal(slugifyRunId('run--with__separators!'), 'run-with-separators');
  assert.equal(slugifyRunId('--trimmed--'), 'trimmed');
});

test('memberNameOfSubjectId returns the member segment or null', () => {
  assert.equal(
    memberNameOfSubjectId('team-member:sess:team:wf-7e02a12cabdf-reviewer-0'),
    'wf-7e02a12cabdf-reviewer-0',
  );
  assert.equal(memberNameOfSubjectId('team-member:sess:team:engineer'), 'engineer');
  assert.equal(memberNameOfSubjectId('main'), null);
  assert.equal(memberNameOfSubjectId('subagent-a'), null);
  assert.equal(memberNameOfSubjectId('team-member:sess:team:'), null);
});

test('groupTeamMemberLanesByWorkflowRun groups workers by known run slug', () => {
  const runs = [
    { id: 'wf_7e02a12cabdf', name: '代码评审', status: 'running' },
    { id: 'wf_a1b2c3d4e5f6', name: '市场调研', status: 'completed' },
  ];
  const lanes = [
    lane('team-member:sess:t:team-leader', 'leader'),
    lane('team-member:sess:t:wf-7e02a12cabdf-sess-review-ocr-offline-py-0', 'review:ocr_offline.py'),
    lane('team-member:sess:t:wf-a1b2c3d4e5f6-reviewer-f171d940748a', 'reviewer'),
    lane('team-member:sess:t:wf-7e02a12cabdf-sess-review-workflow-py-1', 'review:workflow.py'),
    lane('main', 'Main'),
  ];
  const { ungrouped, groups } = groupTeamMemberLanesByWorkflowRun(lanes, runs);
  assert.deepEqual(ungrouped.map(l => l.label), ['leader', 'Main']);
  assert.deepEqual(groups.map(g => g.runId), ['wf_7e02a12cabdf', 'wf_a1b2c3d4e5f6']);
  assert.deepEqual(groups[0].lanes.map(l => l.label), ['review:ocr_offline.py', 'review:workflow.py']);
  assert.deepEqual(groups[1].lanes.map(l => l.label), ['reviewer']);
  assert.equal(groups[0].name, '代码评审');
  assert.equal(groups[0].status, 'running');
  assert.equal(groups[1].name, '市场调研');
  assert.equal(groups[1].status, 'completed');
});

test('groupTeamMemberLanesByWorkflowRun falls back to the untracked run pattern', () => {
  const lanes = [
    lane('team-member:sess:t:wf-0123456789ab-worker-0', 'worker'),
    lane('team-member:sess:t:team-leader', 'leader'),
  ];
  const { ungrouped, groups } = groupTeamMemberLanesByWorkflowRun(lanes, []);
  assert.deepEqual(ungrouped.map(l => l.label), ['leader']);
  assert.deepEqual(groups.map(g => g.runId), ['wf_0123456789ab']);
  assert.equal(groups[0].name, 'wf_01234');
  assert.equal(groups[0].shortId, 'wf_01234');
  assert.equal(groups[0].status, 'idle');
  assert.equal(groups[0].lanes.length, 1);
});

test('groupTeamMemberLanesByWorkflowRun does not claim a longer run id through a shorter slug', () => {
  const runs = [{ id: 'wf_7e02a12cabdf', name: 'first', status: 'completed' }];
  const lanes = [
    lane('team-member:sess:t:wf-7e02a12cabdf2-reviewer-1', 'other'),
  ];
  const { ungrouped, groups } = groupTeamMemberLanesByWorkflowRun(lanes, runs);
  assert.deepEqual(ungrouped.map(l => l.label), ['other']);
  assert.deepEqual(groups, []);
});

test('groupTeamMemberLanesByWorkflowRun falls back to the short id for an empty run name', () => {
  const runs = [{ id: 'wf_7e02a12cabdf', name: '', status: 'completed' }];
  const lanes = [lane('team-member:sess:t:wf-7e02a12cabdf-worker-0', 'worker')];
  const { groups } = groupTeamMemberLanesByWorkflowRun(lanes, runs);
  assert.equal(groups[0].name, 'wf_7e02a');
  assert.equal(groups[0].status, 'completed');
});

test('groupTeamMemberLanesByWorkflowRun ignores malformed run entries', () => {
  const runs = [{ id: '', name: 'empty' }, null, { id: 'wf_7e02a12cabdf' }];
  const lanes = [lane('team-member:sess:t:wf-7e02a12cabdf-worker-0', 'worker')];
  const { groups } = groupTeamMemberLanesByWorkflowRun(lanes, runs);
  assert.deepEqual(groups.map(g => g.runId), ['wf_7e02a12cabdf']);
  assert.equal(groups[0].lanes.length, 1);
});

test('workflowRunLaneStatus maps run statuses onto lane statuses', () => {
  assert.equal(workflowRunLaneStatus('running'), 'running');
  assert.equal(workflowRunLaneStatus('waiting_for_human'), 'running');
  assert.equal(workflowRunLaneStatus('paused'), 'running');
  assert.equal(workflowRunLaneStatus('failed'), 'error');
  assert.equal(workflowRunLaneStatus('completed'), 'completed');
  assert.equal(workflowRunLaneStatus('stopped'), 'completed');
  assert.equal(workflowRunLaneStatus('weird-status'), 'idle');
  assert.equal(workflowRunLaneStatus(undefined), 'idle');
});

test('shortWorkflowRunId keeps ids within eight characters', () => {
  assert.equal(shortWorkflowRunId('wf_7e02a12cabdf'), 'wf_7e02a');
  assert.equal(shortWorkflowRunId('short'), 'short');
});

test('attachWorkflowGroupsToHostLanes binds each group to one lane per member', () => {
  // Interleaved task runs of one member share a subjectId; the group must
  // attach to the lane it matched, not render in every lane with that id.
  const aliceT1 = lane('team-member:sess:t:alice', 'alice');
  const bobT2 = lane('team-member:sess:t:bob', 'bob');
  const aliceT3 = lane('team-member:sess:t:alice', 'alice');
  const aliceGroup = { runId: 'wf_aaa', name: '调研', shortId: 'wf_aaa', status: 'completed', executor: 'alice', lanes: [] };
  const bobGroup = { runId: 'wf_bbb', name: '评审', shortId: 'wf_bbb', status: 'running', executor: 'bob', lanes: [] };
  const plan = attachWorkflowGroupsToHostLanes([aliceGroup, bobGroup], [aliceT1, bobT2, aliceT3]);
  assert.deepEqual(plan.attached.get(aliceT1), [aliceGroup]);
  assert.equal(plan.attached.get(aliceT3), undefined);
  assert.deepEqual(plan.attached.get(bobT2), [bobGroup]);
  assert.deepEqual(plan.trailing, []);
});

test('attachWorkflowGroupsToHostLanes trails groups with no matching lane', () => {
  const leader = lane('team-member:sess:t:team-leader', 'leader');
  const group = { runId: 'wf_ccc', name: '归档', shortId: 'wf_ccc', status: 'idle', executor: 'gone-member', lanes: [] };
  const anonymous = { ...group, runId: 'wf_ddd', executor: undefined };
  const plan = attachWorkflowGroupsToHostLanes([group, anonymous], [leader]);
  assert.equal(plan.attached.size, 0);
  assert.deepEqual(plan.trailing, [group, anonymous]);
});

test('parseEngineSwarmflowMemberId splits the wf_<run>:<call key> id', () => {
  assert.deepEqual(parseEngineSwarmflowMemberId('wf_16515e37c7f4:[["call", 0]]'), {
    runId: 'wf_16515e37c7f4',
    callKey: '[["call", 0]]',
  });
  assert.deepEqual(
    parseEngineSwarmflowMemberId('wf_f4742f4f0e47:[["par", 0, 1], ["call", 0]]'),
    { runId: 'wf_f4742f4f0e47', callKey: '[["par", 0, 1], ["call", 0]]' },
  );
  assert.equal(parseEngineSwarmflowMemberId('team-leader'), null);
  assert.equal(parseEngineSwarmflowMemberId('wf_16515e37c7f4:'), null);
  assert.equal(parseEngineSwarmflowMemberId('wf_'), null);
});

test('swarmflowIdentitiesOfMembers keeps only engine call-key members', () => {
  assert.deepEqual(
    swarmflowIdentitiesOfMembers([
      { member_id: 'wf_16515e37c7f4:[["call", 0]]', name: '算子1-写诗' },
      { member_id: 'team-leader', name: '团队领导' },
      { member_id: 'wf_f4742f4f0e47:[["par", 0, 0], ["call", 0]]' },
    ]),
    [
      { memberId: 'wf_16515e37c7f4:[["call", 0]]', name: '算子1-写诗' },
      { memberId: 'wf_f4742f4f0e47:[["par", 0, 0], ["call", 0]]', name: '' },
    ],
  );
});

test('swarmflowWorkerMemberName mirrors the backend mint', () => {
  // 'eb2ad63f42da' is the real ident of the second parallel branch's first
  // call: the label '算子2-户外' slugs down to '2'.
  assert.equal(
    swarmflowWorkerMemberName('wf_f4742f4f0e47', '算子2-户外', 'eb2ad63f42da'),
    'wf-f4742f4f0e47-2-eb2ad63f42da',
  );
  assert.equal(
    swarmflowWorkerMemberName('wf_x', '', 'abc123def456'),
    'wf-x-worker-abc123def456',
  );
});

test('swarmflowLaneFullName prefers the engine id, falls back to the worker name', () => {
  const lane = {
    subjectId: 'team-member:sess:t:wf-f4742f4f0e47-2-eb2ad63f42da',
    label: '算子2-户外',
    kind: 'team_member',
    status: 'completed',
    traceCount: 1,
    recordCount: 1,
  };
  const index = new Map([
    ['wf-f4742f4f0e47-2-eb2ad63f42da', 'wf_f4742f4f0e47:[["par", 0, 1], ["call", 0]]'],
  ]);
  assert.equal(
    swarmflowLaneFullName(lane, index),
    'wf_f4742f4f0e47:[["par", 0, 1], ["call", 0]]',
  );
  assert.equal(swarmflowLaneFullName(lane, new Map()), 'wf-f4742f4f0e47-2-eb2ad63f42da');
  const leaderLane = { ...lane, subjectId: 'team-member:sess:t:team-leader' };
  assert.equal(swarmflowLaneFullName(leaderLane, index), null);
});
