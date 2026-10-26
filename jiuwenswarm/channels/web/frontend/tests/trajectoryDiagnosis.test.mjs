// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyProgressEvent,
  applyStatusEvent,
  canSubmitDiagnosis,
  msRangeToUnixNano,
  highlightKeysOf,
  isTtseCategory,
  laneHighlightKeys,
  mergeTtseResults,
  resolveStepIds,
  stepRefKey,
  summaryOf,
  ttseRootCauses,
  upsertSummary,
} from '../node_modules/.cache/trajectory-diagnosis/diagnosisState.mjs';
import { asRewardPayload } from '../node_modules/.cache/trajectory-diagnosis/diagnosisTypes.mjs';
import {
  axisTicks,
  collectWallClockSamples,
  compressedToWall,
  compressSharedTimeline,
  diagnosisSelectionRanges,
  clampSharedViewport,
  formatAxisClock,
  formatElapsed,
  mainAgentLastNode,
  sharedWallClockDomain,
  visibleAxisTicks,
  wallToCompressed,
} from '../node_modules/.cache/trajectory-diagnosis/diagnosisTimeSelection.mjs';

const TRACE = 'a'.repeat(32);
const ref = (spanId, subject = 'main') => ({ trace_id: TRACE, span_id: spanId.padEnd(16, '0'), subject_id: subject });

function record(overrides = {}) {
  return {
    diagnosis_id: 'dg_0123456789abcdef',
    ordinal: 1,
    status: 'running',
    created_at: '2026-10-15T00:00:00Z',
    scope_kind: 'full',
    evaluate_artifact: false,
    artifact_eval_applied: false,
    session_id: 's1',
    seq: 0,
    stages: [],
    root_causes: [],
    ttse_trigger: {},
    ...overrides,
  };
}

function progress(seq, kind, extra = {}) {
  return { session_id: 's1', diagnosis_id: 'dg_0123456789abcdef', seq, stage_id: 'evidence', kind, ...extra };
}

test('stepRefKey ignores the subject so it matches record ids', () => {
  assert.equal(stepRefKey(ref('1')), `${TRACE}:${'1'.padEnd(16, '0')}`);
});

test('progress events build a stage in order', () => {
  let current = record();
  for (const event of [
    progress(1, 'stage_started', { title: 'Collect' }),
    progress(2, 'output_delta', { text_delta: 'ab' }),
    progress(3, 'output_delta', { text_delta: 'cd' }),
    progress(4, 'stage_finished', { status: 'completed', summary: 'done', elapsed_ms: 12 }),
  ]) {
    const outcome = applyProgressEvent(current, event);
    assert.equal(outcome.refetch, false);
    current = outcome.record;
  }
  assert.equal(current.seq, 4);
  assert.deepEqual(current.stages, [
    { stage_id: 'evidence', title: 'Collect', status: 'completed', output: 'abcd', summary: 'done', elapsed_ms: 12 },
  ]);
});

test('root cause events fill the record while it runs and replace what a signal held', () => {
  const cause = (id, signalId) => ({
    id,
    category: 'other',
    subject_id: 'main',
    refs: [ref('1')],
    summary: id,
    evidence: '',
    signal: { id: signalId, source: 'keyword_tool' },
  });
  let current = record({ seq: 1 });
  const events = [
    progress(2, 'root_causes', { stage_id: 'attribute', signal_id: 'sig-1', root_causes: [cause('a', 'sig-1')] }),
    progress(3, 'root_causes', { stage_id: 'attribute', signal_id: 'sig-2', root_causes: [cause('b', 'sig-2')] }),
    progress(4, 'root_causes', {
      stage_id: 'attribute',
      signal_id: 'sig-1',
      root_causes: [cause('a2', 'sig-1'), cause('a3', 'sig-1')],
    }),
  ];
  for (const event of events) {
    const outcome = applyProgressEvent(current, event);
    assert.equal(outcome.refetch, false);
    current = outcome.record;
  }
  assert.equal(current.seq, 4);
  assert.deepEqual(current.root_causes.map(item => item.id), ['b', 'a2', 'a3']);
  // No stage is invented for an update that is not about a stage.
  assert.deepEqual(current.stages, []);
});

test('an empty root cause update clears the causes of its signal', () => {
  const kept = { id: 'k', category: 'other', subject_id: 'main', refs: [], summary: 'k', evidence: '' };
  const gone = { ...kept, id: 'g', signal: { id: 'sig-1', source: 'reward' } };
  const base = record({ seq: 3, root_causes: [kept, gone] });
  const outcome = applyProgressEvent(
    base,
    progress(4, 'root_causes', { stage_id: 'attribute', signal_id: 'sig-1', root_causes: [] }),
  );
  assert.deepEqual(outcome.record.root_causes.map(item => item.id), ['k']);
});

test('events at or below the record seq are dropped', () => {
  const base = record({ seq: 5 });
  const outcome = applyProgressEvent(base, progress(5, 'output_delta', { text_delta: 'x' }));
  assert.equal(outcome.record, base);
  assert.equal(outcome.refetch, false);
});

test('a seq gap asks for a refetch and leaves the record alone', () => {
  const base = record({ seq: 2 });
  const outcome = applyProgressEvent(base, progress(5, 'output_delta', { text_delta: 'x' }));
  assert.equal(outcome.record, base);
  assert.equal(outcome.refetch, true);
});

test('events of another diagnosis are ignored', () => {
  const base = record();
  const outcome = applyProgressEvent(base, { ...progress(1, 'stage_started'), diagnosis_id: 'dg_other' });
  assert.equal(outcome.record, base);
  assert.equal(outcome.refetch, false);
});

test('a terminal status event refetches to load evidence and suggestions', () => {
  const base = record({ seq: 4 });
  const done = summaryOf({ ...base, status: 'completed', headline: 'ok' });
  const outcome = applyStatusEvent(base, {
    session_id: 's1',
    diagnosis_id: base.diagnosis_id,
    seq: 5,
    status: 'completed',
    summary: done,
  });
  assert.equal(outcome.record.status, 'completed');
  assert.equal(outcome.record.seq, 5);
  assert.equal(outcome.refetch, true);
});

test('upsertSummary keeps newest ordinal first and replaces by id', () => {
  const first = summaryOf(record({ diagnosis_id: 'dg_aaaaaaaaaaaaaaaa', ordinal: 1 }));
  const second = summaryOf(record({ diagnosis_id: 'dg_bbbbbbbbbbbbbbbb', ordinal: 2 }));
  const list = upsertSummary(upsertSummary([], first), second);
  assert.deepEqual(list.map(item => item.ordinal), [2, 1]);
  const updated = upsertSummary(list, { ...first, status: 'completed' });
  assert.equal(updated.length, 2);
  assert.equal(updated.find(item => item.ordinal === 1).status, 'completed');
});

test('highlightKeysOf collects root-cause refs, or a chosen subset', () => {
  const current = record({
    root_causes: [
      { id: 'rc-1', category: 'other', subject_id: 'main', refs: [ref('1')], summary: '', evidence: '', confidence: 0.5 },
      { id: 'rc-2', category: 'other', subject_id: 'main', refs: [ref('2')], summary: '', evidence: '', confidence: 0.5 },
    ],
  });
  assert.deepEqual([...highlightKeysOf(current)].sort(), [ref('1'), ref('2')].map(stepRefKey).sort());
  assert.deepEqual([...highlightKeysOf(current, ['rc-2'])], [stepRefKey(ref('2'))]);
  assert.equal(highlightKeysOf(null).size, 0);
});

test('laneHighlightKeys stays empty until a root cause is focused', () => {
  const current = record({
    root_causes: [
      { id: 'rc-1', category: 'other', subject_id: 'main', refs: [ref('1')], summary: '', evidence: '', confidence: 0.5 },
      { id: 'rc-2', category: 'other', subject_id: 'main', refs: [ref('2')], summary: '', evidence: '', confidence: 0.5 },
    ],
  });
  assert.equal(laneHighlightKeys(current, null).size, 0);
  assert.equal(laneHighlightKeys(current, []).size, 0);
  assert.equal(laneHighlightKeys(null, ['rc-1']).size, 0);
  assert.deepEqual([...laneHighlightKeys(current, ['rc-2'])], [stepRefKey(ref('2'))]);
});

test('resolveStepIds separates steps that are no longer available', () => {
  const known = new Set([stepRefKey(ref('1'))]);
  const { found, missing } = resolveStepIds([ref('1'), ref('9')], known);
  assert.equal(found.length, 1);
  assert.equal(missing.length, 1);
});

test('mergeTtseResults only touches ttse_trigger', () => {
  const merged = mergeTtseResults(record(), { 'tt-1': { result: 'triggered', triggered_at: 'x' } });
  assert.equal(merged.ttse_trigger['tt-1'].result, 'triggered');
  assert.equal(merged.status, 'running');
});

test('ttseRootCauses keeps only the experience categories', () => {
  const cause = (id, category) => ({ id, category, subject_id: 'main', refs: [], summary: id, evidence: '' });
  const found = ttseRootCauses(
    record({
      root_causes: [cause('a', 'skill_defect'), cause('b', 'ttse_partial'), cause('c', 'tool_or_env')],
    }),
  );
  assert.deepEqual(found.map(item => item.id), ['b']);
  assert.equal(isTtseCategory('ttse_success'), true);
  assert.equal(isTtseCategory('ttse_fail'), true);
  assert.equal(isTtseCategory('other'), false);
});

test('msRangeToUnixNano widens the end to the whole last millisecond', () => {
  const range = msRangeToUnixNano(1760511592139, 1760511592140.4);
  assert.equal(range.start_unix_nano, '1760511592139000000');
  assert.equal(range.end_unix_nano, '1760511592141999999');
});

test('msRangeToUnixNano rejects reversed or non-finite hulls', () => {
  assert.equal(msRangeToUnixNano(5, 4), null);
  assert.equal(msRangeToUnixNano(Number.NaN, 4), null);
  assert.equal(msRangeToUnixNano(-1, 4), null);
});

test('sharedWallClockDomain spans the earliest start and latest end', () => {
  const domain = sharedWallClockDomain([
    { startedAt: 5_000, timeSeconds: 2 },
    { startedAt: 1_000, timeSeconds: 1 },
    { startedAt: 8_000, timeSeconds: null },
    { startedAt: 9_000, timeSeconds: 1, requestOnly: true },
  ], 10_000);
  assert.deepEqual(domain, { startMs: 1_000, endMs: 8_000 });
  assert.equal(sharedWallClockDomain([{ startedAt: null, timeSeconds: 1 }], 0), null);
  assert.deepEqual(
    sharedWallClockDomain([{ startedAt: 8_000, timeSeconds: null, status: 'running' }], 10_000),
    { startMs: 8_000, endMs: 10_000 },
  );
});

test('compressSharedTimeline drops idle gaps shared by every agent', () => {
  const timeline = compressSharedTimeline([
    { startedAt: 0, timeSeconds: 10 },
    { startedAt: 1_000_000, timeSeconds: 5 },
  ], 2_000_000);
  assert.equal(timeline.durationMs, 15_000);
  assert.equal(wallToCompressed(timeline, 0), 0);
  assert.equal(wallToCompressed(timeline, 10_000), 10_000);
  assert.equal(wallToCompressed(timeline, 1_000_000), 10_000);
  assert.equal(wallToCompressed(timeline, 1_005_000), 15_000);
  assert.equal(compressedToWall(timeline, 10_000), 1_000_000);
  assert.equal(compressedToWall(timeline, 12_000), 1_002_000);
  const overlap = compressSharedTimeline([
    { startedAt: 0, timeSeconds: 10 },
    { startedAt: 8_000, timeSeconds: 4 },
  ], 0);
  assert.equal(overlap.durationMs, 12_000);
  assert.equal(overlap.segments.length, 1);
});

test('a zero-width domain stays selectable', () => {
  assert.deepEqual(
    sharedWallClockDomain([{ startedAt: 4_000, timeSeconds: 0 }], 0),
    { startMs: 4_000, endMs: 4_001 },
  );
});

test('mainAgentLastNode is the last assistant reply of the main agent', () => {
  const turns = [
    { groups: [{ cells: [{ kind: 'message', startedAt: 1_000, timeSeconds: 1 }, { requestOnly: true, startedAt: 9_000 }] }] },
    { groups: [{ cells: [{ kind: 'message', startedAt: 4_000, timeSeconds: 2 }, { startedAt: 500 }] }] },
  ];
  assert.deepEqual(mainAgentLastNode([
    { subjectId: 'member', kind: 'team_member', turns },
    { subjectId: 'main', kind: 'main_agent', turns: [{ groups: [{ cells: [{ startedAt: null }] }] }] },
    { subjectId: 'leader', kind: 'team_leader', turns },
  ]), null);
  assert.deepEqual(mainAgentLastNode([
    { subjectId: 'leader', kind: 'team_leader', turns },
    { subjectId: 'member', kind: 'team_member', turns: [{ groups: [{ cells: [{ startedAt: 8_000 }] }] }] },
  ]), { subjectId: 'leader', startMs: 4_000, endMs: 6_000 });
});

test('mainAgentLastNode ignores a user cell whose commit replay time outlasts the reply start', () => {
  // The user row starts 6ms after the reply's inference began (commit replay
  // time), yet the reply interval [10_000, 14_000] is the delivery node.
  const turns = [
    { groups: [{ cells: [
      { kind: 'message', startedAt: 10_000, timeSeconds: 4 },
      { kind: 'user', startedAt: 10_006 },
    ] }] },
  ];
  assert.deepEqual(mainAgentLastNode([
    { subjectId: 'main', kind: 'main_agent', turns },
  ]), { subjectId: 'main', startMs: 10_000, endMs: 14_000 });
});

test('mainAgentLastNode breaks equal reply starts by their end', () => {
  const turns = [
    { groups: [{ cells: [
      { kind: 'message', startedAt: 10_000, timeSeconds: 2 },
      { kind: 'message', startedAt: 10_000, timeSeconds: 4 },
    ] }] },
  ];
  assert.deepEqual(mainAgentLastNode([
    { subjectId: 'main', kind: 'main_agent', turns },
  ]), { subjectId: 'main', startMs: 10_000, endMs: 14_000 });
});

test('collectWallClockSamples keeps every cell in order', () => {
  const samples = collectWallClockSamples([
    { groups: [{ cells: [{ startedAt: 1 }] }, { cells: [{ startedAt: 2 }] }] },
  ]);
  assert.deepEqual(samples.map(sample => sample.startedAt), [1, 2]);
});

test('diagnosisSelectionRanges repeats one window for each checked subject', () => {
  const subjects = [
    { subjectId: 'main', subjectLabel: 'Main' },
    { subjectId: 'writer', subjectLabel: 'Writer' },
  ];
  const ranges = diagnosisSelectionRanges({
    range: { startMs: 10, endMs: 40 },
    subjects,
    excludedSubjectIds: new Set(['writer']),
  });
  assert.deepEqual(ranges, [
    { subjectId: 'main', subjectLabel: 'Main', startMs: 10, endMs: 40 },
  ]);
  assert.deepEqual(diagnosisSelectionRanges({
    range: null,
    subjects,
    excludedSubjectIds: new Set(),
  }), []);
  assert.deepEqual(diagnosisSelectionRanges({
    range: { startMs: 10, endMs: 40 },
    subjects,
    excludedSubjectIds: new Set(['main', 'writer']),
  }), []);
});

test('clampSharedViewport keeps a zoom window inside the domain', () => {
  const window = { start: 1_000, end: 4_000 };
  assert.equal(clampSharedViewport(window, 0, 10_000), window);
  assert.equal(clampSharedViewport({ start: 0, end: 10_000 }, 0, 10_000), null);
  assert.deepEqual(clampSharedViewport({ start: -500, end: 1_500 }, 0, 10_000), { start: 0, end: 2_000 });
  assert.equal(clampSharedViewport(null, 0, 10_000), null);
});

test('visibleAxisTicks label the visible window from the axis origin', () => {
  const ticks = visibleAxisTicks(120_000, 180_000);
  assert.equal(ticks[0].label, '2:00');
  assert.equal(ticks[0].fraction, 0);
  assert.equal(ticks[ticks.length - 1].label, '3:00');
  assert.equal(ticks[ticks.length - 1].fraction, 1);
});

test('formatElapsed and axis ticks run from 0:00 to the end', () => {
  assert.equal(formatElapsed(0), '0:00');
  assert.equal(formatElapsed(5_000), '0:05');
  assert.equal(formatElapsed(65_000), '1:05');
  assert.equal(formatElapsed(3_661_000), '1:01:01');
  const ticks = axisTicks(90_000);
  assert.equal(ticks[0].label, '0:00');
  assert.equal(ticks[ticks.length - 1].label, '1:30');
  assert.equal(ticks[ticks.length - 1].fraction, 1);
});

test('formatAxisClock shows local wall-clock time', () => {
  const stamp = new Date(2026, 9, 23, 14, 5, 9).getTime();
  assert.equal(formatAxisClock(stamp), '14:05:09');
  assert.equal(formatAxisClock(Number.NaN), '');
});

test('canSubmitDiagnosis follows scope, busy and running state', () => {
  const base = { busy: false, submitting: false, running: false, fullScope: false, hasSelection: false };
  assert.equal(canSubmitDiagnosis(base), false);
  assert.equal(canSubmitDiagnosis({ ...base, hasSelection: true }), true);
  assert.equal(canSubmitDiagnosis({ ...base, fullScope: true }), true);
  assert.equal(canSubmitDiagnosis({ ...base, fullScope: true, busy: true }), false);
  assert.equal(canSubmitDiagnosis({ ...base, hasSelection: true, busy: true }), true);
  assert.equal(canSubmitDiagnosis({ ...base, hasSelection: true, running: true }), false);
  assert.equal(canSubmitDiagnosis({ ...base, hasSelection: true, submitting: true }), false);
  assert.equal(canSubmitDiagnosis({ ...base, hasSelection: true, nodeLimitExceeded: true }), false);
  assert.equal(canSubmitDiagnosis({ ...base, fullScope: true, nodeLimitExceeded: true }), false);
});
