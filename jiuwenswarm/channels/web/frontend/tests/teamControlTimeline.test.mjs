import assert from 'node:assert/strict';
import test from 'node:test';

import { buildTeamControlModel } from '../node_modules/.cache/trajectory-schedule/teamControlTimeline.mjs';

const TRACE_ID = '11111111111111111111111111111111';

function attribute(key, value) {
  return { key, value: { stringValue: value } };
}

function record(spanId, name, startMs, input, extraAttributes = []) {
  return {
    record_id: `${TRACE_ID}:${spanId}`,
    lifecycle: 'final',
    observed_time_unix_nano: `${BigInt(startMs) * 1_000_000n}`,
    otlp: {
      resourceSpans: [{
        scopeSpans: [{
          spans: [{
            traceId: TRACE_ID,
            spanId,
            name,
            startTimeUnixNano: `${BigInt(startMs) * 1_000_000n}`,
            endTimeUnixNano: `${BigInt(startMs + 1) * 1_000_000n}`,
            attributes: [
              attribute('openjiuwen.span.input', JSON.stringify(input)),
              ...extraAttributes,
            ],
          }],
        }],
      }],
    },
  };
}

test('main control records build task phases and review rounds', () => {
  const model = buildTeamControlModel([
    record('0000000000000001', 'task.t1-spec.created', 1_000, {
      task_id: 't1-spec',
      status: 'pending',
    }),
    record('0000000000000002', 'event.task.started', 2_000, {
      task_id: 't1-spec',
      member_name: 'spec-architect',
    }),
    record('0000000000000003', 'event.task.submitted.for.review', 3_000, {
      task_id: 't1-spec',
      member_name: 'spec-architect',
      reviewer: ['verifier_1', 'challenger_1'],
      review_round: 1,
    }),
    record('0000000000000004', 'event.task.review.vote', 4_000, {
      task_id: 't1-spec',
      reviewer: 'verifier_1',
      decision: 'fail',
      review_round: 1,
    }),
    record('0000000000000005', 'event.task.revision.requested', 5_000, {
      task_id: 't1-spec',
      feedback: 'fix the contract',
    }),
    record('0000000000000006', 'event.task.verified', 7_000, {
      task_id: 't1-spec',
    }),
    record('0000000000000007', 'event.anomaly.detected', 8_000, {
      member_name: 'spec-architect',
      detector: 'model_error',
      severity: 'medium',
      summary: 'model connection failed',
    }),
    record('0000000000000008', 'team.completed', 9_000, {}),
  ]);

  assert.equal(model.controlRecordCount, 8);
  assert.equal(model.knownEventCount, 8);
  assert.equal(model.tasks.length, 1);
  assert.equal(model.completedTaskCount, 1);
  assert.equal(model.reworkCount, 1);
  assert.equal(model.teamCompletedAtMs, 9_000);
  assert.equal(model.anomalies.length, 1);
  assert.equal(model.tasks[0].phase, 'completed');
  assert.equal(model.tasks[0].assignee, 'spec-architect');
  assert.deepEqual(model.tasks[0].reviews.map(review => [
    review.round,
    review.reviewer,
    review.decision.kind,
  ]), [
    [1, 'challenger_1', 'pending'],
    [1, 'verifier_1', 'fail'],
  ]);
});

test('task root snapshots never overwrite event-derived state', () => {
  const model = buildTeamControlModel([
    record('0000000000000011', 'task.t2-cli.created', 1_000, { status: 'pending' }),
    record('0000000000000012', 'event.task.started', 2_000, { task_id: 't2-cli' }),
    record('0000000000000013', 'event.task.verified', 3_000, { task_id: 't2-cli' }),
    record('0000000000000014', 'task.t2-cli', 4_000, {
      task_id: 't2-cli',
      status: 'blocked',
    }),
  ]);
  assert.equal(model.tasks[0].phase, 'completed');
});

test('task assignee is read from the semantic convention attribute', () => {
  const model = buildTeamControlModel([
    record(
      '0000000000000015',
      'event.task.started',
      1_000,
      { task_id: 't3-docs' },
      [attribute('agentteam.task.assignee', 'docs-agent')],
    ),
  ]);

  assert.equal(model.tasks[0].assignee, 'docs-agent');
});

test('non-main records do not create control tasks', () => {
  const execution = record('0000000000000021', 'llm.call', 1_000, {});
  execution.otlp.resourceSpans[0].scopeSpans[0].spans[0].attributes.push(
    attribute('openjiuwen.execution.subject.id', 'team-member:test:member'),
    attribute('openjiuwen.execution.subject.kind', 'team_member'),
  );
  const model = buildTeamControlModel([execution]);
  assert.equal(model.controlRecordCount, 0);
  assert.deepEqual(model.tasks, []);
});
