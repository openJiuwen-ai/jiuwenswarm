// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  groupTrajectorySubjects,
} from '../node_modules/.cache/trajectory-window/trajectorySubjects.mjs';

function attribute(key, value) {
  return { key, value: { stringValue: value } };
}

function teamRecord(spanId, name, startNs, subject) {
  return {
    resourceSpans: [{
      scopeSpans: [{
        spans: [{
          traceId: 'a'.repeat(32),
          spanId,
          name,
          startTimeUnixNano: startNs,
          endTimeUnixNano: `${BigInt(startNs) + 10n}`,
          attributes: [
            attribute('openjiuwen.execution.subject.id', subject.id),
            attribute('openjiuwen.execution.subject.display_name', subject.displayName),
            attribute('openjiuwen.execution.subject.kind', subject.kind),
            attribute('openjiuwen.execution.subject.parent_id', subject.parentId),
            attribute('openjiuwen.execution.subject.session_id', subject.sessionId),
          ],
        }],
      }],
    }],
  };
}

const leader = {
  id: 'team-member:sess:research:team-leader',
  displayName: 'Team Leader',
  kind: 'team_leader',
  parentId: '',
  sessionId: 'sess',
};
const engineer = {
  id: 'team-member:sess:research:engineer',
  displayName: 'Engineer',
  kind: 'team_member',
  parentId: 'team-member:sess:research:team-leader',
  sessionId: 'sess',
};

test('agent-core team subject records group into leader + teammate lanes', () => {
  const records = [
    teamRecord('1'.repeat(16), 'leader llm.call', '100', leader),
    teamRecord('2'.repeat(16), 'engineer llm.call', '200', engineer),
    teamRecord('3'.repeat(16), 'engineer tool.exec', '300', engineer),
  ];
  const result = groupTrajectorySubjects(records, [], new Map(), 'sess', { teamMode: true });

  assert.deepEqual(result.groups.map(group => group.subject.kind), [
    'team_leader',
    'team_member',
  ]);
  assert.equal(result.groups[0].records.length, 1);
  assert.equal(result.groups[1].records.length, 2);
  assert.equal(result.groups[0].subject.displayName, 'Team Leader');
  assert.equal(result.groups[1].subject.displayName, 'Engineer');
});
