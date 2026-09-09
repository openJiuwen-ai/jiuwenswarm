import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createTrajectorySubjectViewCache,
  groupTrajectorySubjects,
  MAIN_TRAJECTORY_SUBJECT_ID,
  UNASSIGNED_TRAJECTORY_SUBJECT_ID,
} from '../node_modules/.cache/trajectory-window/trajectorySubjects.mjs';
import {
  parseTrajectoryArchive,
  trajectoryArchiveView,
} from '../node_modules/.cache/trajectory-window/trajectoryArchive.mjs';

const TRACE_ID = '11111111111111111111111111111111';

function attribute(key, value) {
  return { key, value: { stringValue: value } };
}

function record(spanId, name, startTimeUnixNano, subject) {
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

function detail(otlp, ingestSeq) {
  const span = otlp.resourceSpans[0].scopeSpans[0].spans[0];
  return {
    ingest_seq: ingestSeq,
    record_id: `${span.traceId}:${span.spanId}`,
    record_revision: 1,
    lifecycle: 'completed',
    operation: 'upsert',
    observed_time_unix_nano: span.startTimeUnixNano,
    trace_id: span.traceId,
    span_id: span.spanId,
    otlp,
    raw_valid: true,
  };
}

const main = {
  id: 'main',
  displayName: 'Main Agent',
  kind: 'main_agent',
  parentId: null,
  sessionId: 'session-main',
};
const subagentOne = {
  id: 'subagent:one',
  displayName: 'Researcher',
  kind: 'subagent',
  parentId: 'main',
  sessionId: 'session-sub-one',
};
const subagentTwo = {
  id: 'subagent:two',
  displayName: 'Researcher',
  kind: 'subagent',
  parentId: 'main',
  sessionId: 'session-sub-two',
};

test('explicit subject identity separates repeated same-name subagent invocations', () => {
  const records = [
    record('0000000000000001', 'main context', '100', main),
    record('0000000000000002', 'subagent one request', '200', subagentOne),
    record('0000000000000003', 'subagent two request', '300', subagentTwo),
  ];
  const rawRecords = records.map(detail);
  const lifecycle = new Map(rawRecords.map(item => [item.record_id, 'completed']));
  const result = groupTrajectorySubjects(records, rawRecords, lifecycle);

  assert.deepEqual(result.groups.map(group => group.subject.id), [
    MAIN_TRAJECTORY_SUBJECT_ID,
    subagentOne.id,
    subagentTwo.id,
  ]);
  assert.deepEqual(result.groups.map(group => group.label), [
    'Main Agent',
    'Researcher 1',
    'Researcher 2',
  ]);
  assert.deepEqual(
    result.byId.get(subagentOne.id).records.map(item => item.resourceSpans[0].scopeSpans[0].spans[0].name),
    ['subagent one request'],
  );
  assert.equal(result.byId.get(subagentOne.id).subject.parentId, 'main');
  assert.equal(result.byId.get(subagentOne.id).subject.sessionId, 'session-sub-one');
  assert.equal(result.byId.get(subagentOne.id).lifecycleByRecordId.size, 1);
});

test('an incremental first subagent record creates its subject group immediately', () => {
  const mainRecord = record('0000000000000010', 'main request', '100', main);
  const firstWindow = groupTrajectorySubjects(
    [mainRecord],
    [detail(mainRecord, 1)],
    new Map(),
  );
  assert.deepEqual(firstWindow.groups.map(group => group.subject.id), [
    MAIN_TRAJECTORY_SUBJECT_ID,
  ]);

  const subagentRecord = record(
    '0000000000000011',
    'first live subagent request',
    '200',
    subagentOne,
  );
  const nextWindow = groupTrajectorySubjects(
    [mainRecord, subagentRecord],
    [detail(mainRecord, 1), detail(subagentRecord, 2)],
    new Map(),
  );
  assert.deepEqual(nextWindow.groups.map(group => group.subject.id), [
    MAIN_TRAJECTORY_SUBJECT_ID,
    subagentOne.id,
  ]);
  assert.equal(nextWindow.byId.get(subagentOne.id).records.length, 1);
});

test('the first schema-v2 subagent event creates a tab without misusing the owner session as execution session', () => {
  const subagentEvent = record('0000000000000019', 'context.window.commit', '400');
  const span = subagentEvent.resourceSpans[0].scopeSpans[0].spans[0];
  span.attributes.push(
    attribute('openjiuwen.trajectory.schema_version', '2'),
    attribute('openjiuwen.trajectory.subject_id', 'subagent:v2-first'),
    attribute('openjiuwen.trajectory.session_id', 'session-main'),
  );

  const result = groupTrajectorySubjects(
    [subagentEvent],
    [detail(subagentEvent, 1)],
    new Map(),
    'session-main',
  );
  const group = result.byId.get('subagent:v2-first');

  assert.ok(group);
  assert.equal(group.subject.kind, 'subagent');
  assert.equal(group.subject.parentId, MAIN_TRAJECTORY_SUBJECT_ID);
  assert.equal(group.subject.sessionId, null);
  assert.equal(group.records.length, 1);
});

test('schema-v2 team leader events remain in the leader lane', () => {
  const leader = {
    id: 'team-member:session-main:demo:leader',
    displayName: 'Leader',
    kind: 'team_leader',
    parentId: null,
    sessionId: 'session-main',
  };
  const compacted = record('000000000000001a', 'compaction.completed', '500', leader);
  const span = compacted.resourceSpans[0].scopeSpans[0].spans[0];
  span.attributes.push(
    attribute('openjiuwen.trajectory.schema_version', '2'),
    attribute('openjiuwen.trajectory.subject_id', leader.id),
    attribute('openjiuwen.trajectory.session_id', 'session-main'),
  );

  const result = groupTrajectorySubjects(
    [compacted],
    [detail(compacted, 1)],
    new Map(),
    'session-main',
    { teamMode: true },
  );
  const group = result.byId.get(leader.id);

  assert.ok(group);
  assert.equal(group.subject.kind, 'team_leader');
  assert.equal(group.subject.displayName, 'Leader');
  assert.equal(group.records.length, 1);
});

test('subject view cache reuses every unchanged group and projection', () => {
  const records = [
    record('0000000000000020', 'main request', '100', main),
    record('0000000000000021', 'subagent one request', '200', subagentOne),
    record('0000000000000022', 'subagent two request', '300', subagentTwo),
  ];
  const rawRecords = records.map(detail);
  const lifecycle = new Map(rawRecords.map(item => [item.record_id, 'completed']));
  const cache = createTrajectorySubjectViewCache();
  let projections = 0;
  const project = (group) => ({ subjectId: group.subject.id, call: ++projections });
  const first = cache.update(
    groupTrajectorySubjects(records, rawRecords, lifecycle),
    project,
  );
  const second = cache.update(
    groupTrajectorySubjects([...records], [...rawRecords], new Map(lifecycle)),
    project,
  );

  assert.equal(projections, 3);
  for (const group of first.groups.groups) {
    assert.equal(second.groups.byId.get(group.subject.id), group);
    assert.equal(second.snapshots.get(group.subject.id), first.snapshots.get(group.subject.id));
  }
});

test('one changed subagent invalidates only its own projection cache entry', () => {
  const mainRecord = record('0000000000000030', 'main request', '100', main);
  const subOneRecord = record('0000000000000031', 'subagent one request', '200', subagentOne);
  const subTwoRecord = record('0000000000000032', 'subagent two request', '300', subagentTwo);
  const records = [mainRecord, subOneRecord, subTwoRecord];
  const rawRecords = records.map(detail);
  const lifecycle = new Map(rawRecords.map(item => [item.record_id, 'completed']));
  const cache = createTrajectorySubjectViewCache();
  let projections = 0;
  const project = (group) => ({ subjectId: group.subject.id, call: ++projections });
  const first = cache.update(groupTrajectorySubjects(records, rawRecords, lifecycle), project);
  const changedSubOne = record(
    '0000000000000033',
    'subagent one terminal record',
    '400',
    subagentOne,
  );
  const nextRecords = [...records, changedSubOne];
  const nextRawRecords = [...rawRecords, detail(changedSubOne, 4)];
  const second = cache.update(
    groupTrajectorySubjects(nextRecords, nextRawRecords, lifecycle),
    project,
  );

  assert.equal(projections, 4);
  assert.equal(
    second.snapshots.get(MAIN_TRAJECTORY_SUBJECT_ID),
    first.snapshots.get(MAIN_TRAJECTORY_SUBJECT_ID),
  );
  assert.notEqual(
    second.snapshots.get(subagentOne.id),
    first.snapshots.get(subagentOne.id),
  );
  assert.equal(
    second.snapshots.get(subagentTwo.id),
    first.snapshots.get(subagentTwo.id),
  );
});

test('adding a same-name subagent relabels its peer without reprojecting that peer', () => {
  const mainRecord = record('0000000000000040', 'main request', '100', main);
  const subOneRecord = record('0000000000000041', 'subagent one request', '200', subagentOne);
  const cache = createTrajectorySubjectViewCache();
  let projections = 0;
  const project = (group) => ({ subjectId: group.subject.id, call: ++projections });
  const firstRecords = [mainRecord, subOneRecord];
  const firstRaw = firstRecords.map(detail);
  const first = cache.update(
    groupTrajectorySubjects(firstRecords, firstRaw, new Map()),
    project,
  );
  const subTwoRecord = record('0000000000000042', 'subagent two request', '300', subagentTwo);
  const secondRecords = [...firstRecords, subTwoRecord];
  const secondRaw = [...firstRaw, detail(subTwoRecord, 3)];
  const second = cache.update(
    groupTrajectorySubjects(secondRecords, secondRaw, new Map()),
    project,
  );

  assert.equal(projections, 3);
  assert.equal(first.groups.byId.get(subagentOne.id).label, 'Researcher');
  assert.equal(second.groups.byId.get(subagentOne.id).label, 'Researcher 1');
  assert.equal(
    second.snapshots.get(subagentOne.id),
    first.snapshots.get(subagentOne.id),
  );
});

test('legacy records stay in Main but malformed explicit subjects never leak into Main', () => {
  const legacy = record('0000000000000004', 'legacy main', '100');
  const malformed = record('0000000000000005', 'malformed', '200', {
    ...subagentOne,
    parentId: null,
  });
  const records = [legacy, malformed];
  const result = groupTrajectorySubjects(records, records.map(detail), new Map());

  assert.deepEqual(
    result.byId.get(MAIN_TRAJECTORY_SUBJECT_ID).records.map(item => item.resourceSpans[0].scopeSpans[0].spans[0].name),
    ['legacy main'],
  );
  assert.deepEqual(
    result.byId.get(UNASSIGNED_TRAJECTORY_SUBJECT_ID).records.map(item => item.resourceSpans[0].scopeSpans[0].spans[0].name),
    ['malformed'],
  );
});

test('a subagent execution session owned by another chat is excluded', () => {
  const ownerSession = 'session-current';
  const current = record('0000000000000008', 'current subagent', '100', {
    ...subagentOne,
    sessionId: `${ownerSession}_sub_general_current`,
  });
  const leaked = record('0000000000000009', 'old subagent', '200', {
    ...subagentTwo,
    sessionId: 'session-previous_sub_general_old',
  });

  const result = groupTrajectorySubjects(
    [current, leaked],
    [detail(current, 1), detail(leaked, 2)],
    new Map(),
    ownerSession,
  );

  assert.ok(result.byId.has(subagentOne.id));
  assert.equal(result.byId.has(subagentTwo.id), false);
});

test('Archive v1 replay produces the same execution-subject groups as live records', () => {
  const records = [
    record('0000000000000006', 'main request', '100', main),
    record('0000000000000007', 'subagent request', '200', subagentOne),
  ];
  const archiveRecords = records.map((otlp, index) => {
    const span = otlp.resourceSpans[0].scopeSpans[0].spans[0];
    return {
      record_id: `${span.traceId}:${span.spanId}`,
      record_revision: 1,
      lifecycle: 'final',
      operation: 'upsert',
      change_seq: String(index + 1),
      observed_time_unix_nano: span.startTimeUnixNano,
      trace_id: span.traceId,
      span_id: span.spanId,
      raw_json_base64: Buffer.from(JSON.stringify(otlp)).toString('base64'),
      raw_valid: true,
      otlp,
    };
  });
  const archive = parseTrajectoryArchive(JSON.stringify({
    format: 'openjiuwen.trajectory.archive',
    archive_version: 1,
    session_id: 'session-main',
    store_epoch: 'epoch-1',
    revision: '2',
    exported_at: '2026-08-21T00:00:00Z',
    records: archiveRecords,
  }));
  const replay = trajectoryArchiveView(archive);
  const live = groupTrajectorySubjects(
    records,
    records.map(detail),
    new Map(records.map(item => {
      const span = item.resourceSpans[0].scopeSpans[0].spans[0];
      return [`${span.traceId}:${span.spanId}`, 'completed'];
    })),
  );
  const archived = groupTrajectorySubjects(
    replay.records,
    replay.rawRecords,
    replay.lifecycleByRecordId,
  );

  assert.deepEqual(
    archived.groups.map(group => [group.subject.id, group.records.length]),
    live.groups.map(group => [group.subject.id, group.records.length]),
  );
});

test('team leader and teammate subjects group into parallel lanes', () => {
  const teamLeader = {
    id: 'team-member:sess:research:team-leader',
    displayName: 'Team Leader',
    kind: 'team_leader',
    parentId: null,
    sessionId: 'sess',
  };
  const engineer = {
    id: 'team-member:sess:research:engineer',
    displayName: 'Engineer',
    kind: 'team_member',
    parentId: 'team-member:sess:research:team-leader',
    sessionId: 'sess',
  };
  const records = [
    record('0000000000000040', 'leader plan request', '100', teamLeader),
    record('0000000000000041', 'engineer tool call', '200', engineer),
  ];
  const rawRecords = records.map(detail);
  const lifecycle = new Map(rawRecords.map(item => [item.record_id, 'completed']));
  const result = groupTrajectorySubjects(records, rawRecords, lifecycle, 'sess', { teamMode: true });

  assert.deepEqual(result.groups.map(group => group.subject.id), [
    teamLeader.id,
    engineer.id,
  ]);
  assert.deepEqual(result.groups.map(group => group.subject.kind), [
    'team_leader',
    'team_member',
  ]);
  assert.deepEqual(result.groups.map(group => group.label), [
    'Team Leader',
    'Engineer',
  ]);
  assert.equal(result.byId.get(engineer.id).subject.parentId, teamLeader.id);
  assert.equal(result.byId.get(engineer.id).records.length, 1);
  assert.equal(result.byId.get(teamLeader.id).records.length, 1);
});

test('team leader sorts before teammates regardless of observation order', () => {
  const leader = {
    id: 'team-member:sess:proj:team-leader',
    displayName: 'Team Leader',
    kind: 'team_leader',
    parentId: null,
    sessionId: 'sess',
  };
  const member = {
    id: 'team-member:sess:proj:member',
    displayName: 'Member',
    kind: 'team_member',
    parentId: 'team-member:sess:proj:team-leader',
    sessionId: 'sess',
  };
  // Member observed first, leader later.
  const records = [
    record('0000000000000050', 'member request', '100', member),
    record('0000000000000051', 'leader request', '200', leader),
  ];
  const result = groupTrajectorySubjects(
    records,
    records.map(detail),
    new Map(),
    'sess',
    { teamMode: true },
  );
  assert.deepEqual(result.groups.map(group => group.subject.id), [
    leader.id,
    member.id,
  ]);
});
