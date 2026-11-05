import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildScheduledTeamLaneSections,
  reviewerIdentity,
  scheduledMemberDisplayName,
} from '../node_modules/.cache/trajectory-lanes/scheduledTrajectoryLanes.mjs';

function group(id, label = id, kind = 'team_member') {
  return {
    subject: {
      id,
      displayName: label,
      kind,
      parentId: null,
      sessionId: 'session',
    },
    records: [],
    rawRecords: [],
    lifecycleByRecordId: new Map([['record', 'completed']]),
    traceCount: 1,
    firstObservedTimeUnixNano: '100',
    label,
  };
}

test('reviewer identity parses round and retry without changing the subject id', () => {
  const id = 'team-reviewer:sess:team:t9-doc-align:verifier_1:r3:a1';
  assert.deepEqual(reviewerIdentity(id), {
    taskId: 't9-doc-align',
    reviewer: 'verifier_1',
    round: 3,
    attempt: 1,
  });
  assert.equal(reviewerIdentity('team-member:sess:team:writer'), null);
  assert.equal(reviewerIdentity('team-reviewer:verifier_1:r1'), null);
});

test('task assignee resolves to the same member label shown on its lane', () => {
  const sections = buildScheduledTeamLaneSections([
    group('team-member:sess:team:py-algo-dev', 'Python 算法工程师'),
  ]);

  assert.equal(
    scheduledMemberDisplayName('py-algo-dev', sections.members),
    'Python 算法工程师',
  );
});

test('scheduled lanes group by task and round while preserving physical ids', () => {
  const leaderId = 'team-member:sess:team:leader';
  const reviewerBase = 'team-reviewer:sess:team:t9-doc-align:verifier_1:r3';
  const reviewerRetry = `${reviewerBase}:a1`;
  const reviewerR2 = 'team-reviewer:sess:team:t9-doc-align:verifier_1:r2';
  const sections = buildScheduledTeamLaneSections([
    group(leaderId, 'Leader', 'team_leader'),
    group(reviewerRetry, 'verifier_1 R3 retry 1'),
    group(reviewerR2, 'verifier_1 R2'),
    group(reviewerBase, 'verifier_1 R3'),
  ], {
    tasks: [{
      taskId: 't9-doc-align',
      phase: 'completed',
      assignee: 'doc-writer',
      events: [],
      reviews: [{
        key: 't9-doc-align:3:verifier_1',
        taskId: 't9-doc-align',
        reviewer: 'verifier_1',
        round: 3,
        decision: { kind: 'score', value: 0.92 },
      }],
    }],
    anomalies: [],
    controlRecordCount: 0,
    knownEventCount: 0,
    reworkCount: 0,
    completedTaskCount: 1,
  });

  assert.deepEqual(sections.members.map(lane => lane.subjectId), [leaderId]);
  assert.equal(sections.reviewRunCount, 3);
  assert.deepEqual(sections.tasks[0].rounds.map(round => round.round), [2, 3]);
  assert.deepEqual(
    sections.tasks[0].rounds[1].lanes.map(lane => lane.subjectId),
    [reviewerBase, reviewerRetry],
  );
  assert.deepEqual(
    sections.tasks[0].rounds[1].lanes.map(lane => lane.label),
    ['R3 verifier_1', 'R3 verifier_1 retry 1'],
  );
  assert.deepEqual(
    sections.tasks[0].rounds[1].lanes.map(lane => lane.decision),
    [{ kind: 'score', value: 0.92 }, { kind: 'score', value: 0.92 }],
  );
});

test('review submission order outranks lexical reviewer order', () => {
  const sections = buildScheduledTeamLaneSections([
    group('team-reviewer:sess:team:t1:alpha:r1', 'alpha'),
    group('team-reviewer:sess:team:t1:zeta:r1', 'zeta'),
  ], {
    tasks: [{
      taskId: 't1',
      phase: 'in_review',
      events: [{
        eventId: 'e1',
        recordId: 'r1',
        kind: 'submitted',
        spanName: 'event.task.submitted.for.review',
        timestampMs: 1,
        taskId: 't1',
        reviewRound: 1,
        reviewers: ['zeta', 'alpha'],
      }],
      reviews: [],
    }],
    anomalies: [],
    controlRecordCount: 0,
    knownEventCount: 1,
    reworkCount: 0,
    completedTaskCount: 0,
  });
  assert.deepEqual(
    sections.tasks[0].rounds[0].lanes.map(lane => lane.reviewer),
    ['zeta', 'alpha'],
  );
});

test('review task labels preserve ordinals when earlier scheduling tasks are cancelled', () => {
  const taskIds = [
    'spec-model',
    'cli-impl',
    'test-suite',
    'docs-readme',
    'qa-audit',
    'cli-impl-v2',
    'test-suite-v2',
    'docs-readme-v2',
    'qa-audit-v2',
  ];
  const sections = buildScheduledTeamLaneSections([
    group('team-reviewer:sess:team:spec-model:verifier_1:r2', 'verifier'),
    group('team-reviewer:sess:team:cli-impl-v2:verifier_1:r1', 'verifier'),
    group('team-reviewer:sess:team:test-suite-v2:verifier_1:r1', 'verifier'),
    group('team-reviewer:sess:team:docs-readme-v2:verifier_1:r1', 'verifier'),
    group('team-reviewer:sess:team:qa-audit-v2:verifier_1:r1', 'verifier'),
  ], {
    tasks: taskIds.map((taskId, index) => ({
      taskId,
      phase: index > 0 && index < 5 ? 'cancelled' : 'completed',
      events: [],
      reviews: [],
    })),
    anomalies: [],
    controlRecordCount: 0,
    knownEventCount: 0,
    reworkCount: 0,
    completedTaskCount: 5,
  });

  assert.deepEqual(
    sections.tasks.map(task => [task.taskId, task.taskIndex]),
    [
      ['spec-model', 1],
      ['cli-impl-v2', 6],
      ['test-suite-v2', 7],
      ['docs-readme-v2', 8],
      ['qa-audit-v2', 9],
    ],
  );
});

test('scheduled grouping preserves every physical lane exactly once', () => {
  const groups = [
    group('team-member:sess:team:writer', 'Writer'),
    group('team-reviewer:sess:team:t1:verifier_1:r1', 'verifier_1 R1'),
    group('team-reviewer:malformed', 'Malformed reviewer'),
  ];
  const sections = buildScheduledTeamLaneSections(groups, {
    tasks: [{
      taskId: 't1',
      phase: 'in_review',
      events: [],
      reviews: [],
    }],
    anomalies: [],
    controlRecordCount: 0,
    knownEventCount: 0,
    reworkCount: 0,
    completedTaskCount: 0,
  });
  const scheduledIds = [
    ...sections.members.map(lane => lane.subjectId),
    ...sections.tasks.flatMap(task => task.rounds.flatMap(round => (
      round.lanes.map(lane => lane.subjectId)
    ))),
    ...sections.ungroupedReviewers.map(lane => lane.subjectId),
  ].sort();

  assert.deepEqual(
    scheduledIds,
    groups.map(candidate => candidate.subject.id).sort(),
  );
});
