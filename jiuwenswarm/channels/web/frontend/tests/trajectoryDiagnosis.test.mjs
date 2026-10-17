// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyProgressEvent,
  applyStatusEvent,
  canSubmitDiagnosis,
  msRangeToUnixNano,
  highlightKeysOf,
  mergeTtseResults,
  resolveStepIds,
  stepRefKey,
  summaryOf,
  upsertSummary,
} from '../node_modules/.cache/trajectory-diagnosis/diagnosisState.mjs';

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

test('canSubmitDiagnosis follows scope, busy and running state', () => {
  const base = { busy: false, submitting: false, running: false, fullScope: false, hasSelection: false };
  assert.equal(canSubmitDiagnosis(base), false);
  assert.equal(canSubmitDiagnosis({ ...base, hasSelection: true }), true);
  assert.equal(canSubmitDiagnosis({ ...base, fullScope: true }), true);
  assert.equal(canSubmitDiagnosis({ ...base, fullScope: true, busy: true }), false);
  assert.equal(canSubmitDiagnosis({ ...base, hasSelection: true, busy: true }), true);
  assert.equal(canSubmitDiagnosis({ ...base, hasSelection: true, running: true }), false);
  assert.equal(canSubmitDiagnosis({ ...base, hasSelection: true, submitting: true }), false);
});
