// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyProgressEvent,
  applyStatusEvent,
  canSubmitDiagnosis,
  msRangeToUnixNano,
  highlightKeysOf,
  laneHighlightKeys,
  mergeTtseResults,
  resolveStepIds,
  stepRefKey,
  summaryOf,
  upsertSummary,
} from '../node_modules/.cache/trajectory-diagnosis/diagnosisState.mjs';
import {
  axisTicks,
  collectWallClockSamples,
  compressedToWall,
  compressSharedTimeline,
  diagnosisSelectionRanges,
  clampSharedViewport,
  formatElapsed,
  sharedWallClockDomain,
  visibleAxisTicks,
  wallToCompressed,
} from '../node_modules/.cache/trajectory-diagnosis/diagnosisTimeSelection.mjs';
import { asRewardPayload } from '../node_modules/.cache/trajectory-diagnosis/diagnosisTypes.mjs';

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
    focus_ranges: [],
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

test('highlightKeysOf collects root-cause and focus refs, or a chosen subset', () => {
  const current = record({
    root_causes: [
      { id: 'rc-1', category: 'other', subject_id: 'main', refs: [ref('1')], summary: '', evidence: '', confidence: 0.5 },
      { id: 'rc-2', category: 'other', subject_id: 'main', refs: [ref('2')], summary: '', evidence: '', confidence: 0.5 },
    ],
    focus_ranges: [{ subject_id: 'main', refs: [ref('3')], start_unix_nano: '1', end_unix_nano: '2' }],
  });
  assert.deepEqual([...highlightKeysOf(current)].sort(), [ref('1'), ref('2'), ref('3')].map(stepRefKey).sort());
  assert.deepEqual([...highlightKeysOf(current, ['rc-2'])], [stepRefKey(ref('2'))]);
  assert.equal(highlightKeysOf(null).size, 0);
});

test('laneHighlightKeys stays empty until a root cause is focused', () => {
  const current = record({
    root_causes: [
      { id: 'rc-1', category: 'other', subject_id: 'main', refs: [ref('1')], summary: '', evidence: '', confidence: 0.5 },
      { id: 'rc-2', category: 'other', subject_id: 'main', refs: [ref('2')], summary: '', evidence: '', confidence: 0.5 },
    ],
    focus_ranges: [{ subject_id: 'main', refs: [ref('3')], start_unix_nano: '1', end_unix_nano: '2' }],
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

test('canSubmitDiagnosis follows scope, busy and running state', () => {
  const base = { busy: false, submitting: false, running: false, fullScope: false, hasSelection: false };
  assert.equal(canSubmitDiagnosis(base), false);
  assert.equal(canSubmitDiagnosis({ ...base, hasSelection: true }), true);
  assert.equal(canSubmitDiagnosis({ ...base, fullScope: true }), true);
  assert.equal(canSubmitDiagnosis({ ...base, fullScope: true, busy: true }), false);
  assert.equal(canSubmitDiagnosis({ ...base, hasSelection: true, busy: true }), true);
  assert.equal(canSubmitDiagnosis({ ...base, hasSelection: true, running: true }), false);
});

test('asRewardPayload accepts a full reward payload', () => {
  const payload = {
    schema_version: 'attribution.reward.v1',
    overall: { comment: '基本完成', total_score: 87 },
    categories: [{ name: '代码质量', comment: '尚可', score: 85 }],
    dimensions: [
      { category: '代码质量', requirement: '可读性', comment: '一般', score: 70, related_signal_ids: ['diag-1'] },
    ],
  };
  const reward = asRewardPayload(payload);
  assert.equal(reward.overall.total_score, 87);
  assert.equal(reward.categories.length, 1);
  assert.equal(reward.categories[0].name, '代码质量');
  assert.equal(reward.dimensions[0].related_signal_ids[0], 'diag-1');
});

test('asRewardPayload rejects malformed shapes', () => {
  assert.equal(asRewardPayload(undefined), null);
  assert.equal(asRewardPayload(null), null);
  assert.equal(asRewardPayload('x'), null);
  assert.equal(asRewardPayload({}), null);
  assert.equal(asRewardPayload({ overall: { comment: 'x' } }), null);
  assert.equal(asRewardPayload({ overall: { comment: 'x', total_score: '87' } }), null);
});

test('asRewardPayload drops malformed categories and dimensions', () => {
  const reward = asRewardPayload({
    schema_version: 'attribution.reward.v1',
    overall: { comment: 'c', total_score: 1 },
    categories: [{ name: 'ok' }, { broken: true }],
    dimensions: [{ category: 'a', requirement: 'r', comment: 'c', score: 1 }, { category: 'a' }],
  });
  assert.equal(reward.categories.length, 1);
  assert.equal(reward.dimensions.length, 1);
});
