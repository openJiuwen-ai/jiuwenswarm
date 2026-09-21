// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildTeamMemberLanes,
  defaultTeamMemberSubjectId,
  laneStatusOf,
} from '../node_modules/.cache/trajectory-lanes/teamTrajectoryLanes.mjs';

const TRACE_ID = '11111111111111111111111111111111';

function attribute(key, value) {
  return { key, value: { stringValue: value } };
}

function otlpRecord(spanId, name, startTimeUnixNano, subject) {
  const attributes = subject === undefined ? [] : [
    attribute('openjiuwen.execution.subject.id', subject.id),
    attribute('openjiuwen.execution.subject.display_name', subject.displayName),
    attribute('openjiuwen.execution.subject.kind', subject.kind),
    ...(subject.parentId === null
      ? []
      : [attribute('openjiuwen.execution.subject.parent_id', subject.parentId)]),
    attribute('openjiuwen.execution.subject.session_id', subject.sessionId),
  ];
  return {
    resourceSpans: [{
      scopeSpans: [{
        spans: [{
          traceId: TRACE_ID,
          spanId,
          name,
          startTimeUnixNano,
          endTimeUnixNano: `${BigInt(startTimeUnixNano) + 10n}`,
          attributes,
        }],
      }],
    }],
  };
}

const leader = {
  id: 'team-member:sess:research:team-leader',
  displayName: 'Team Leader',
  kind: 'team_leader',
  parentId: null,
  sessionId: 'sess',
};
const teammate = {
  id: 'team-member:sess:research:engineer',
  displayName: 'Engineer',
  kind: 'team_member',
  parentId: 'team-member:sess:research:team-leader',
  sessionId: 'sess',
};

function groupFor(subject, records, lifecycle = new Map()) {
  return {
    subject,
    records,
    rawRecords: [],
    lifecycleByRecordId: lifecycle,
    traceCount: records.length,
    firstObservedTimeUnixNano: '100',
    label: subject.displayName,
  };
}

test('buildTeamMemberLanes orders lanes and derives status from lifecycle', () => {
  const records = [
    otlpRecord('0000000000000001', 'leader request', '100', leader),
    otlpRecord('0000000000000002', 'engineer request', '200', teammate),
  ];
  const lanes = buildTeamMemberLanes([
    groupFor(leader, [records[0]], new Map([['rec-1', 'completed']])),
    groupFor(teammate, [records[1]], new Map([['rec-2', 'running']])),
  ]);
  assert.deepEqual(lanes.map(lane => lane.label), ['Team Leader', 'Engineer']);
  assert.deepEqual(lanes.map(lane => lane.status), ['completed', 'running']);
  assert.equal(lanes[0].traceCount, 1);
  assert.equal(lanes[1].recordCount, 1);
});

test('laneStatusOf returns idle for an empty group', () => {
  assert.equal(laneStatusOf(groupFor(leader, [])), 'idle');
});

test('laneStatusOf error wins over completed', () => {
  const group = groupFor(leader, [1, 2], new Map([
    ['rec-a', 'completed'],
    ['rec-b', 'error'],
  ]));
  assert.equal(laneStatusOf(group), 'error');
});

test('laneStatusOf running wins over error', () => {
  const group = groupFor(leader, [1, 2], new Map([
    ['rec-a', 'error'],
    ['rec-b', 'running'],
  ]));
  assert.equal(laneStatusOf(group), 'running');
});

test('defaultTeamMemberSubjectId picks the first group with records', () => {
  const empty = groupFor(leader, []);
  const withRecords = groupFor(teammate, [otlpRecord('0000000000000003', 'r', '300', teammate)]);
  assert.equal(defaultTeamMemberSubjectId([empty, withRecords]), teammate.id);
  assert.equal(defaultTeamMemberSubjectId([empty]), leader.id);
  assert.equal(defaultTeamMemberSubjectId([]), null);
});
