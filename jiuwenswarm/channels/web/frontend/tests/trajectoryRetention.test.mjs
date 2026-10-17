// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

// Retention removes a session's oldest turns as whole pages and leaves
// checkpoints behind. These fixtures come from a real trajectory store
// (tests/unit_tests/observability/retention_fixture_builder.py): each session
// exported before retention, and again after it. Replaying both must render
// every remaining turn exactly as it rendered before.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { readTrajectoryArchive } from '../node_modules/.cache/trajectory-retention/trajectoryArchive.mjs';
import { groupTrajectorySubjects } from '../node_modules/.cache/trajectory-retention/trajectorySubjects.mjs';
import { unresolvedAttributesByRecordId } from '../node_modules/.cache/trajectory-retention/trajectorySequences.mjs';
import { buildTeamMemberLanes } from '../node_modules/.cache/trajectory-retention/teamTrajectoryLanes.mjs';
import {
  createTrajectoryV2Reducer,
  projectOtelTrajectory,
} from '../node_modules/.cache/trajectory-retention/otel-trajectory-projector.mjs';

function fixture(name) {
  return new Blob([readFileSync(new URL(`./fixtures/${name}`, import.meta.url))]);
}

function recordIdentity(record) {
  const span = record.resourceSpans[0].scopeSpans[0].spans[0];
  return `${span.traceId}:${span.spanId}`;
}

/** Replay one archive the way the trajectory panel projects a session. */
async function replay(name, { teamMode, withCheckpoints = true }) {
  const archive = await readTrajectoryArchive(fixture(name));
  const checkpoints = archive.checkpoints;
  const reducer = createTrajectoryV2Reducer();
  if (withCheckpoints) reducer.seed(checkpoints.v2Seeds);
  const groups = groupTrajectorySubjects(
    archive.view.records,
    archive.view.rawRecords,
    archive.view.lifecycleByRecordId,
    archive.header.session_id,
    { teamMode, ...(withCheckpoints ? { retiredSubjects: checkpoints.retiredSubjects } : {}) },
  );
  const snapshots = new Map(groups.groups.map(group => [
    group.subject.id,
    projectOtelTrajectory(group.records, {
      ...(withCheckpoints ? { checkpoint: checkpoints.projections.get(group.subject.id) } : {}),
      lifecycleByRecordId: group.lifecycleByRecordId,
      sessionCumulativeUsageByRequestIdentity: archive.sessionCumulativeUsageByRequestIdentity,
      unresolvedAttributesByRecordId: unresolvedAttributesByRecordId(group.rawRecords),
      v2Reducer: reducer,
    }),
  ]));
  const recordIds = new Set(archive.view.records.map(recordIdentity));
  return { archive, groups, snapshots, recordIds };
}

function turnRecordIds(turn) {
  return turn.groups.flatMap(group => group.cells.map(cell => recordIdentity(cell.traceDetail)));
}

/**
 * The turns of a projection that retention kept, as they rendered before.
 *
 * A turn either survives whole or goes whole: a turn with some records kept
 * and others removed is a page retention split, which must never happen.
 */
function keptTurns(snapshot, keptRecordIds) {
  return snapshot.turns.filter((turn) => {
    const ids = turnRecordIds(turn);
    const kept = ids.filter(id => keptRecordIds.has(id));
    assert.ok(kept.length === 0 || kept.length === ids.length, 'retention split a turn page');
    return kept.length > 0;
  });
}

function assertRemainingViewUnchanged(before, after, { teamMode }) {
  let removedTurns = 0;
  for (const group of after.groups.groups) {
    const prior = before.groups.byId.get(group.subject.id);
    assert.ok(prior, `subject ${group.subject.id} appeared only after retention`);
    assert.deepEqual(group.subject, prior.subject);
    assert.equal(group.label, prior.label);
    const snapshot = after.snapshots.get(group.subject.id);
    const priorSnapshot = before.snapshots.get(group.subject.id);
    const expectedTurns = keptTurns(priorSnapshot, after.recordIds);
    removedTurns += priorSnapshot.turns.length - expectedTurns.length;
    assert.deepEqual(snapshot.turns, expectedTurns, `turns of ${group.subject.id} changed`);
    assert.deepEqual(
      snapshot.requests,
      priorSnapshot.requests.filter(request => after.recordIds.has(recordIdentity(request.traceDetail))),
      `requests of ${group.subject.id} changed`,
    );
    // Retention may take diagnostics away with the events they were about,
    // but never adds one: every diagnostic left was already there.
    const priorDiagnostics = (priorSnapshot.diagnostics ?? []).map(item => JSON.stringify(item));
    for (const item of snapshot.diagnostics ?? []) {
      assert.ok(
        priorDiagnostics.includes(JSON.stringify(item)),
        `retention added ${item.code} to ${group.subject.id}`,
      );
    }
  }
  assert.ok(removedTurns > 0, 'the fixture removed no turn');
  const keptOrder = before.groups.groups
    .map(group => group.subject.id)
    .filter(subjectId => after.groups.byId.has(subjectId));
  assert.deepEqual(after.groups.groups.map(group => group.subject.id), keptOrder);
  if (teamMode) {
    const labels = lanes => lanes.map(lane => [lane.subjectId, lane.label, lane.kind]);
    assert.deepEqual(
      labels(buildTeamMemberLanes(after.groups.groups)),
      labels(buildTeamMemberLanes(before.groups.groups)).filter(([subjectId]) => after.groups.byId.has(subjectId)),
    );
  }
  for (const [identity, usage] of after.archive.sessionCumulativeUsageByRequestIdentity) {
    assert.deepEqual(usage, before.archive.sessionCumulativeUsageByRequestIdentity.get(identity));
  }
  assert.ok(after.archive.sessionCumulativeUsageByRequestIdentity.size > 0);
}

test('the well-formed fixtures render without any diagnostic', async () => {
  for (const [name, teamMode] of [
    ['trajectory-retention-single.before.jsonl', false],
    ['trajectory-retention-team.before.jsonl', true],
  ]) {
    const replayed = await replay(name, { teamMode });
    for (const snapshot of replayed.snapshots.values()) assert.equal(snapshot.diagnostics, undefined);
  }
});

test('a single Agent renders its remaining turns unchanged after each retention pass', async () => {
  const before = await replay('trajectory-retention-single.before.jsonl', { teamMode: false });
  for (const name of [
    'trajectory-retention-single.after-1.jsonl',
    'trajectory-retention-single.after-2.jsonl',
  ]) {
    const after = await replay(name, { teamMode: false });
    assert.ok(after.archive.checkpoints.projections.size > 0);
    assertRemainingViewUnchanged(before, after, { teamMode: false });
  }
});

test('a retired subagent keeps the tab ordinal of the subagent sharing its name', async () => {
  const before = await replay('trajectory-retention-single.before.jsonl', { teamMode: false });
  const after = await replay('trajectory-retention-single.after-1.jsonl', { teamMode: false });
  assert.equal(before.groups.byId.get('subagent:researcher-2').label, 'Researcher 2');
  assert.equal(after.groups.byId.has('subagent:researcher-1'), false);
  assert.equal(after.groups.byId.get('subagent:researcher-2').label, 'Researcher 2');
});

test('Team members render their remaining turns unchanged beside work outside every turn', async () => {
  const before = await replay('trajectory-retention-team.before.jsonl', { teamMode: true });
  const after = await replay('trajectory-retention-team.after.jsonl', { teamMode: true });
  assertRemainingViewUnchanged(before, after, { teamMode: true });
  for (const subjectId of ['member:alice', 'member:bob']) {
    const turns = after.snapshots.get(subjectId).turns;
    assert.deepEqual(turns.map(turn => turn.turn), [null, 3]);
  }
});

test('the remaining turns would not render unchanged without their checkpoints', async () => {
  const before = await replay('trajectory-retention-single.before.jsonl', { teamMode: false });
  const after = await replay('trajectory-retention-single.after-2.jsonl', {
    teamMode: false,
    withCheckpoints: false,
  });
  assert.throws(() => assertRemainingViewUnchanged(before, after, { teamMode: false }));
  const teamBefore = await replay('trajectory-retention-team.before.jsonl', { teamMode: true });
  const teamAfter = await replay('trajectory-retention-team.after.jsonl', {
    teamMode: true,
    withCheckpoints: false,
  });
  assert.throws(() => assertRemainingViewUnchanged(teamBefore, teamAfter, { teamMode: true }));
});

test('edge-case records render their remaining turns unchanged after each retention pass', async () => {
  const before = await replay('trajectory-retention-quirks.before.jsonl', { teamMode: false });
  // The edge cases are really there: malformed records reach the view only
  // as raw records, and the malformed commits are diagnosed.
  assert.ok(before.archive.view.rawRecords.length > before.archive.view.records.length);
  assert.ok(before.archive.view.invalidRecordSeen);
  assert.ok((before.snapshots.get('subagent:broken').diagnostics ?? []).length > 0);
  for (const pass of [1, 2, 3, 4]) {
    const after = await replay(`trajectory-retention-quirks.after-${pass}.jsonl`, { teamMode: false });
    assertRemainingViewUnchanged(before, after, { teamMode: false });
  }
});

test('conflicting and malformed turn numbers keep every remaining turn number', async () => {
  const numbers = replayed => replayed.snapshots.get('main').turns.map(turn => turn.turn);
  const before = await replay('trajectory-retention-quirks.before.jsonl', { teamMode: false });
  // Turn 1 ends on 3, turn 2 states 3 too, turn 3 a padded hexadecimal 4,
  // turn 5 a padded 5; turns 4 and 6 state nothing valid and follow them.
  assert.deepEqual(numbers(before), [3, 3, 4, 5, 6, 7]);
  const after = await replay('trajectory-retention-quirks.after-4.jsonl', { teamMode: false });
  assert.deepEqual(numbers(after), [5, 7]);
});

test('a subagent keeps the name its earliest record gave it once that record is retired', async () => {
  const labels = replayed => Object.fromEntries(replayed.groups.groups.map(group => [group.subject.id, group.label]));
  const before = await replay('trajectory-retention-quirks.before.jsonl', { teamMode: false });
  // helper-x called itself Helper, later Assistant; helper-y is first seen
  // through a record the view only lists, so it sorts first.
  assert.equal(labels(before)['subagent:helper-y'], 'Helper 1');
  assert.equal(labels(before)['subagent:helper-x'], 'Helper 2');
  // Pass 1 retires the record that named helper-x Helper, pass 3 the record
  // that listed helper-y first; pass 4 retires helper-x altogether.
  for (const pass of [1, 3]) {
    const after = await replay(`trajectory-retention-quirks.after-${pass}.jsonl`, { teamMode: false });
    assert.deepEqual(labels(after), labels(before));
  }
  const after = await replay('trajectory-retention-quirks.after-4.jsonl', { teamMode: false });
  assert.equal(after.groups.byId.has('subagent:helper-x'), false);
  assert.equal(labels(after)['subagent:helper-y'], 'Helper 1');
});

test('cumulative usage adds up the usage each request shows, whatever shape its tokens take', async () => {
  const before = await replay('trajectory-retention-quirks.before.jsonl', { teamMode: false });
  for (const snapshot of before.snapshots.values()) {
    const running = {};
    const requests = [...snapshot.requests]
      .sort((left, right) => left.startedAt - right.startedAt || left.number - right.number);
    for (const request of requests) {
      for (const [key, value] of Object.entries(request.usage)) running[key] = (running[key] ?? 0) + value;
      assert.deepEqual({ ...request.cumulativeUsage }, { ...running });
    }
  }
});
