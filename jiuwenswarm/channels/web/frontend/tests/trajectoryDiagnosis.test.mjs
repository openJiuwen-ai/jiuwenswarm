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
  collectTurnWallRanges,
  collectTurnWallStarts,
  collectUserInteractionWallRanges,
  collectWallClockSamples,
  compressedToWall,
  compressSharedTimeline,
  diagnosisSelectionRanges,
  clampSharedViewport,
  formatAxisClock,
  formatElapsed,
  formatUserTurnClock,
  isTeamHarnessNoiseCell,
  placeUserTurnAxisEdges,
  USER_TURN_GAP_FRACTION,
  userTurnDisplayGaps,
  userTurnGapHalfAxisMs,
  clipIntervalAgainstGaps,
  mainAgentLastNode,
  otelSpanWallInterval,
  rawOtelSpanInterval,
  rawOtelSpanIntervals,
  resolveSelectionWallMs,
  trajectoryRecordCount,
  trajectoryRecordIntervals,
  sharedWallClockDomain,
  visibleAxisTicks,
  wallToCompressed,
} from '../node_modules/.cache/trajectory-diagnosis/diagnosisTimeSelection.mjs';

function msToNano(ms) {
  return String(BigInt(ms) * 1_000_000n);
}

function rawOtelRecord({ startMs, endMs, empty = false } = {}) {
  if (empty) return { otlp: null };
  const span = { startTimeUnixNano: msToNano(startMs) };
  if (endMs !== undefined) span.endTimeUnixNano = msToNano(endMs);
  return {
    otlp: {
      resourceSpans: [{ scopeSpans: [{ spans: [span] }] }],
    },
  };
}

const TRACE = 'a'.repeat(32);

test('summary progress carries full text and all hierarchy before diagnosis completes', () => {
  const before = record({ seq: 1 });
  const tree = { trajectory_summary: 'key action '.repeat(100), turns: { main: [{ turn_id: 't1', segments: [] }] },
    agent_summaries: {}, problems: [], warnings: [], status: 'completed' };
  const result = applyProgressEvent(before, { diagnosis_id: before.diagnosis_id, seq: 2,
    stage_id: 'trajectory_summary', kind: 'trajectory_summary', summary_tree: tree });
  assert.equal(result.refetch, false);
  assert.equal(result.record.summary_tree, tree);
  assert.equal(result.record.trajectory_summary, tree.trajectory_summary);
  assert.ok(result.record.trajectory_summary.length > 500);
});
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

test('a reward event fills the evaluation while the run is still going', () => {
  const reward = {
    schema_version: 'attribution.reward.v1',
    overall: { comment: 'ok', total_score: 80 },
    categories: [],
    dimensions: [],
  };
  const outcome = applyProgressEvent(
    record({ seq: 1 }),
    progress(2, 'reward', { stage_id: 'diagnose', reward }),
  );
  assert.equal(outcome.refetch, false);
  assert.equal(outcome.record.status, 'running');
  assert.deepEqual(outcome.record.reward, reward);
  assert.deepEqual(outcome.record.stages, []);
});

test('whole-table events replace the failure causes and evolution items', () => {
  const cause = id => ({ id, dimension: 'd', summary: id, evidence: [ref('1')], suggestion: [] });
  const judge = id => ({ id, category: 'skill', summary: id, evidence: [ref('2')], suggestion: ['x'], score: 0.5, target: 's' });
  let current = record({ seq: 1, reward_failure_causes: [], evolution_judge: [] });
  let outcome = applyProgressEvent(
    current,
    progress(2, 'reward_failure_causes', { stage_id: 'attribute', reward_failure_causes: [cause('rfc-1')] }),
  );
  assert.equal(outcome.refetch, false);
  current = outcome.record;
  outcome = applyProgressEvent(
    current,
    progress(3, 'evolution_judge', { stage_id: 'attribute', evolution_judge: [judge('evo-1')] }),
  );
  current = outcome.record;
  assert.deepEqual(current.reward_failure_causes.map(item => item.id), ['rfc-1']);
  assert.deepEqual(current.evolution_judge.map(item => item.id), ['evo-1']);
  current = applyProgressEvent(
    current,
    progress(4, 'reward_failure_causes', { stage_id: 'attribute', reward_failure_causes: [cause('rfc-2')] }),
  ).record;
  assert.deepEqual(current.reward_failure_causes.map(item => item.id), ['rfc-2']);
  assert.deepEqual(current.stages, []);
});

test('highlightKeysOf covers failure causes, evolution items and legacy root causes', () => {
  const current = record({
    reward_failure_causes: [{ id: 'rfc-1', dimension: 'd', summary: '', evidence: [ref('1')], suggestion: [] }],
    evolution_judge: [{ id: 'evo-1', category: 'skill', summary: '', evidence: [ref('2')], suggestion: [], score: 0.5 }],
    root_causes: [{ id: 'rc-1', category: 'other', subject_id: 'main', refs: [ref('3')], summary: '', evidence: '' }],
  });
  assert.deepEqual(
    [...highlightKeysOf(current)].sort(),
    [ref('1'), ref('2'), ref('3')].map(stepRefKey).sort(),
  );
  assert.deepEqual([...highlightKeysOf(current, ['evo-1'])], [stepRefKey(ref('2'))]);
});

test('highlightKeysOf covers every cause of the focused problem', () => {
  const signal = { id: 'sig-1', source: 'reward', text: '要求\n终稿被拆开。' };
  const current = record({
    reward_failure_causes: [
      { id: 'rfc-1', dimension: '篇幅', summary: '派发放宽', evidence: [ref('1')], suggestion: [], signal },
      { id: 'rfc-2', dimension: '篇幅', summary: '提纲铺开', evidence: [ref('2', 'member')], suggestion: [], signal },
      { id: 'rfc-3', dimension: '其他', summary: '另一问题', evidence: [ref('3')], suggestion: [] },
    ],
  });
  assert.deepEqual(
    [...highlightKeysOf(current, ['rfc-1'])].sort(),
    [stepRefKey(ref('1')), stepRefKey(ref('2', 'member'))].sort(),
  );
});

test('summaryOf carries the trajectory summary', () => {
  const summary = summaryOf(record({ trajectory_summary: 'what happened' }));
  assert.equal(summary.trajectory_summary, 'what happened');
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

test('collectTurnWallStarts keeps the earliest timed cell per numbered turn', () => {
  assert.deepEqual(collectTurnWallStarts([
    {
      turn: 1,
      groups: [{ cells: [
        { requestOnly: true, startedAt: 0 },
        { startedAt: 2_000, timeSeconds: 1 },
        { startedAt: 1_000, timeSeconds: 0.5 },
      ] }],
    },
    { turn: null, groups: [{ cells: [{ startedAt: 50 }] }] },
    { turn: 2, groups: [{ cells: [{ startedAt: null }, { startedAt: -1 }] }] },
    { turn: 3, groups: [{ cells: [{ startedAt: 9_000 }] }] },
  ]), [
    { turn: 1, wallMs: 1_000 },
    { turn: 3, wallMs: 9_000 },
  ]);
});

test('collectTurnWallRanges keeps earliest start and latest end per numbered turn', () => {
  assert.deepEqual(collectTurnWallRanges([
    {
      turn: 1,
      groups: [{ cells: [
        { startedAt: 1_000, timeSeconds: 0.5 },
        { startedAt: 2_000, timeSeconds: 1 },
      ] }],
    },
    { turn: 2, groups: [{ cells: [{ startedAt: 9_000, timeSeconds: 2 }] }] },
  ]), [
    { turn: 1, startWallMs: 1_000, endWallMs: 3_000 },
    { turn: 2, startWallMs: 9_000, endWallMs: 11_000 },
  ]);
});

test('collectUserInteractionWallRanges merges model turns until the next user prompt', () => {
  assert.deepEqual(collectUserInteractionWallRanges([
    {
      turn: 1,
      groups: [{ cells: [
        { kind: 'user', startedAt: 1_000, timeSeconds: 0 },
        { kind: 'message', startedAt: 1_100, timeSeconds: 1 },
      ] }],
    },
    {
      turn: 2,
      groups: [{ cells: [
        { kind: 'message', startedAt: 3_000, timeSeconds: 2 },
        { kind: 'tool', startedAt: 5_000, timeSeconds: 1 },
      ] }],
    },
    {
      turn: 3,
      groups: [{ cells: [
        { kind: 'user', messageSource: { kind: 'trajectory_ask_user' }, startedAt: 7_000, timeSeconds: 0 },
        { kind: 'message', startedAt: 7_500, timeSeconds: 1 },
      ] }],
    },
    {
      turn: 4,
      groups: [{ cells: [
        {
          kind: 'user',
          text: '<team-inbound from="member">done</team-inbound>',
          startedAt: 9_000,
          timeSeconds: 0,
        },
        { kind: 'message', startedAt: 9_500, timeSeconds: 1 },
      ] }],
    },
    {
      turn: 5,
      groups: [{ cells: [
        { kind: 'user', startedAt: 20_000, timeSeconds: 0 },
        { kind: 'message', startedAt: 21_000, timeSeconds: 1 },
      ] }],
    },
  ]), [
    { userTurn: 1, startWallMs: 1_000, endWallMs: 10_500 },
    { userTurn: 2, startWallMs: 20_000, endWallMs: 22_000 },
  ]);
});

test('collectUserInteractionWallRanges ignores team-clock and team-event user rows', () => {
  assert.deepEqual(collectUserInteractionWallRanges([
    {
      turn: 4,
      groups: [{ cells: [
        { kind: 'user', text: 'please plan the release', startedAt: 1_000, timeSeconds: 0 },
        { kind: 'message', startedAt: 1_100, timeSeconds: 1 },
      ] }],
    },
    {
      turn: 5,
      groups: [{ cells: [
        { kind: 'user', text: '<team-clock at="17:40"/>', startedAt: 3_000, timeSeconds: 0 },
        { kind: 'message', startedAt: 3_100, timeSeconds: 1 },
      ] }],
    },
    {
      turn: 6,
      groups: [{ cells: [
        { kind: 'user', inputDetail: '<team-event kind="task-board"/>', startedAt: 5_000, timeSeconds: 0 },
        { kind: 'tool', startedAt: 5_100, timeSeconds: 1 },
      ] }],
    },
  ]), [
    { userTurn: 1, startWallMs: 1_000, endWallMs: 6_100 },
  ]);
});

test('isTeamHarnessNoiseCell recognizes team envelopes and harness_internal origin', () => {
  assert.equal(isTeamHarnessNoiseCell({ kind: 'user', text: '<team-clock at="17:40"/>' }), true);
  assert.equal(isTeamHarnessNoiseCell({ kind: 'user', inputDetail: '<team-inbound/>' }), true);
  assert.equal(
    isTeamHarnessNoiseCell({ kind: 'message', messageSource: { origin: 'harness_internal' } }),
    true,
  );
  assert.equal(
    isTeamHarnessNoiseCell({
      kind: 'user',
      text: '<team-clock now="2026-11-05 15:59:11"/>\n<team-event kind="task-board">刚完成</team-event>',
      messageSource: { origin: 'external_user', sourceKind: 'query' },
    }),
    true,
  );
  assert.equal(
    isTeamHarnessNoiseCell({
      kind: 'user',
      text: '<team-clock now="2026-11-05 16:16:43"/> 你收到一条消息\n{"origin_kind": "external_user_authored", "content": "身高186cm"}',
      messageSource: { origin: 'external_user' },
    }),
    false,
  );
  assert.equal(isTeamHarnessNoiseCell({ kind: 'user', text: 'please continue' }), false);
  assert.equal(isTeamHarnessNoiseCell({ kind: 'tool', startedAt: 1_000, timeSeconds: 1 }), false);
});

test('a page send opens a user_turn and a team-leader user row does not', () => {
  assert.deepEqual(collectUserInteractionWallRanges([
    {
      turn: 1,
      groups: [{ cells: [
        {
          kind: 'user',
          text: '<team-clock now="2026-11-05 15:45:58"/> 你收到一条消息\n{"origin_kind": "external_user_authored"}',
          messageSource: { origin: 'external_user' },
          startedAt: 1_000,
          timeSeconds: 0,
        },
        { kind: 'message', startedAt: 1_100, timeSeconds: 1 },
      ] }],
    },
    {
      turn: 2,
      groups: [{ cells: [
        {
          kind: 'user',
          text: '<team-clock now="2026-11-05 15:59:11"/>\n<team-event kind="task-board">刚完成</team-event>',
          messageSource: { origin: 'external_user', sourceKind: 'query' },
          startedAt: 3_000,
          timeSeconds: 0,
        },
        { kind: 'message', startedAt: 3_100, timeSeconds: 1 },
      ] }],
    },
    {
      turn: 4,
      groups: [{ cells: [
        {
          kind: 'user',
          text: '<team-clock now="2026-11-05 16:16:43"/> 你收到一条消息\n{"origin_kind": "external_user_authored"}',
          messageSource: { origin: 'external_user' },
          startedAt: 20_000,
          timeSeconds: 0,
        },
        { kind: 'message', startedAt: 21_000, timeSeconds: 1 },
      ] }],
    },
  ]), [
    { userTurn: 1, startWallMs: 1_000, endWallMs: 4_100 },
    { userTurn: 2, startWallMs: 20_000, endWallMs: 22_000 },
  ]);
});

test('team harness noise does not keep overnight idle on the shared axis', () => {
  const dayOne = { kind: 'message', startedAt: 0, timeSeconds: 10 };
  const overnightClock = {
    kind: 'user',
    text: '<team-clock at="02:00"/>',
    startedAt: 50_000,
    timeSeconds: 400,
  };
  const dayTwo = { kind: 'message', startedAt: 1_000_000, timeSeconds: 5 };
  const withNoise = compressSharedTimeline([dayOne, overnightClock, dayTwo], 2_000_000);
  assert.ok(withNoise);
  assert.equal(withNoise.durationMs, 10_000 + 400_000 + 5_000);
  const filtered = [dayOne, overnightClock, dayTwo].filter(sample => !isTeamHarnessNoiseCell(sample));
  const withoutNoise = compressSharedTimeline(filtered, 2_000_000);
  assert.ok(withoutNoise);
  assert.equal(withoutNoise.durationMs, 15_000);
  assert.equal(wallToCompressed(withoutNoise, 1_000_000), 10_000);
});

test('resolveSelectionWallMs keeps turn-end wall time and snaps picks in the gap', () => {
  const turns = [
    { turn: 1, startAxisMs: 0, endAxisMs: 10_000, startWallMs: 1_000, endWallMs: 11_000 },
    { turn: 2, startAxisMs: 10_000, endAxisMs: 15_000, startWallMs: 1_010_000, endWallMs: 1_015_000 },
  ];
  const toWall = (axisMs) => (axisMs >= 10_000 ? 1_010_000 : axisMs + 1_000);
  assert.equal(resolveSelectionWallMs({
    axisMs: 10_000,
    role: 'end',
    turns,
    toWall,
    gapHalfAxisMs: 100,
  }), 11_000);
  assert.equal(resolveSelectionWallMs({
    axisMs: 10_000,
    role: 'start',
    turns,
    toWall,
    gapHalfAxisMs: 100,
  }), 1_010_000);
  assert.equal(resolveSelectionWallMs({
    axisMs: 10_050,
    role: 'start',
    turns,
    toWall,
    gapHalfAxisMs: 100,
  }), 1_010_000);
  assert.equal(resolveSelectionWallMs({
    axisMs: 9_950,
    role: 'end',
    turns,
    toWall,
    gapHalfAxisMs: 100,
  }), 11_000);
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

test('otelSpanWallInterval reads start/end nanos and collapses a missing end', () => {
  assert.deepEqual(
    otelSpanWallInterval({
      startTimeUnixNano: msToNano(1_000),
      endTimeUnixNano: msToNano(1_250),
    }),
    { startMs: 1_000, endMs: 1_250 },
  );
  assert.deepEqual(
    otelSpanWallInterval({ startTimeUnixNano: msToNano(500) }),
    { startMs: 500, endMs: 500 },
  );
  assert.equal(otelSpanWallInterval({ startTimeUnixNano: 'not-a-number' }), null);
  assert.equal(otelSpanWallInterval({ startTimeUnixNano: msToNano(-1) }), null);
});

test('trajectoryRecordCount keeps user, assistant and tool rows', () => {
  const turns = [{
    groups: [{
      cells: [
        { kind: 'system', startedAt: 1_000 },
        { kind: 'context', startedAt: 1_000 },
        { kind: 'user', startedAt: 1_000 },
        { kind: 'context', startedAt: 1_000 },
        { kind: 'message', startedAt: 2_000, timeSeconds: 1 },
        { kind: 'tool', startedAt: 3_000, timeSeconds: 1 },
        { kind: 'message', startedAt: 4_000, timeSeconds: 1 },
        { kind: 'tool', startedAt: 5_000, timeSeconds: 1 },
        { kind: 'message', startedAt: 6_000, requestOnly: true },
        { kind: 'message', startedAt: 7_000, timeSeconds: 1 },
        { kind: 'tool', startedAt: 8_000, timeSeconds: 1 },
        { kind: 'message', startedAt: 9_000, timeSeconds: 1 },
      ],
    }],
  }];
  assert.equal(trajectoryRecordCount(turns), 8);
  assert.equal(trajectoryRecordIntervals(turns).length, 8);
  assert.deepEqual(
    trajectoryRecordIntervals([{
      groups: [{
        cells: [
          { kind: 'context', startedAt: 1_000 },
          { kind: 'user', startedAt: 1_000 },
          { kind: 'context', startedAt: 1_000 },
        ],
      }],
    }]),
    [{ startMs: 1_000, endMs: 1_000 }],
  );
});

test('rawOtelSpanIntervals keep every timed raw record and skip unusable ones', () => {
  assert.equal(rawOtelSpanInterval(rawOtelRecord({ empty: true })), null);
  assert.equal(rawOtelSpanInterval({ otlp: { resourceSpans: [] } }), null);
  assert.deepEqual(
    rawOtelSpanIntervals([
      rawOtelRecord({ startMs: 1_000, endMs: 1_100 }),
      rawOtelRecord({ empty: true }),
      rawOtelRecord({ startMs: 2_000 }),
      { otlp: { resourceSpans: [{ scopeSpans: [{ spans: [] }] }] } },
    ]),
    [
      { startMs: 1_000, endMs: 1_100 },
      { startMs: 2_000, endMs: 2_000 },
    ],
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
  const stamp = new Date(2026, 9, 23, 14, 5, 9, 250).getTime();
  assert.equal(formatAxisClock(stamp), '14:05:09');
  assert.equal(formatAxisClock(Number.NaN), '');
});

test('userTurnGapHalfAxisMs is half of the visible-width fraction', () => {
  assert.equal(USER_TURN_GAP_FRACTION, 0.02);
  assert.equal(userTurnGapHalfAxisMs(10_000), 100);
  assert.equal(userTurnGapHalfAxisMs(0), 0);
  assert.equal(userTurnGapHalfAxisMs(Number.NaN), 0);
});

test('placeUserTurnAxisEdges forces a 2% gap between compressed user_turn junctions', () => {
  const edges = placeUserTurnAxisEdges([
    {
      turn: 1,
      startAxisMs: 0,
      endAxisMs: 4_000,
      startWallMs: Date.UTC(2026, 9, 31, 10, 0, 0),
      endWallMs: Date.UTC(2026, 9, 31, 18, 8, 0),
    },
    {
      turn: 2,
      startAxisMs: 4_000,
      endAxisMs: 10_000,
      startWallMs: Date.UTC(2026, 10, 5, 11, 27, 0),
      endWallMs: Date.UTC(2026, 10, 5, 11, 54, 0),
    },
  ], 10_000);
  const firstEnd = edges.find(edge => edge.turn === 1 && edge.edge === 'end');
  const secondStart = edges.find(edge => edge.turn === 2 && edge.edge === 'start');
  assert.ok(firstEnd);
  assert.ok(secondStart);
  assert.equal(firstEnd.displayAxisMs, 3_900);
  assert.equal(secondStart.displayAxisMs, 4_100);
  assert.equal(secondStart.displayAxisMs - firstEnd.displayAxisMs, 200);
});

test('clipIntervalAgainstGaps splits spans that cross a forced user_turn blank', () => {
  const edges = placeUserTurnAxisEdges([
    {
      turn: 1,
      startAxisMs: 0,
      endAxisMs: 4_000,
      startWallMs: 1,
      endWallMs: 2,
    },
    {
      turn: 2,
      startAxisMs: 4_000,
      endAxisMs: 10_000,
      startWallMs: 3,
      endWallMs: 4,
    },
  ], 10_000);
  const gaps = userTurnDisplayGaps(edges);
  assert.deepEqual(gaps, [{ startMs: 3_900, endMs: 4_100 }]);
  // Main-Agent long model/tool bars typically look like this crossing span.
  assert.deepEqual(
    clipIntervalAgainstGaps(3_000, 5_000, gaps),
    [{ startMs: 3_000, endMs: 3_900 }, { startMs: 4_100, endMs: 5_000 }],
  );
  assert.deepEqual(
    clipIntervalAgainstGaps(3_950, 4_050, gaps),
    [{ startMs: 3_800, endMs: 3_900 }],
  );
  assert.deepEqual(clipIntervalAgainstGaps(0, 3_000, gaps), [{ startMs: 0, endMs: 3_000 }]);
  assert.deepEqual(
    clipIntervalAgainstGaps(1_000, 1_000, gaps),
    [{ startMs: 1_000, endMs: 1_000 }],
  );
  assert.deepEqual(clipIntervalAgainstGaps(3_900, 3_900, gaps), [{ startMs: 3_900, endMs: 3_900 }]);
  // Compressed junction: the next user_turn starts on the midpoint and must
  // stay visible on that turn's leading edge.
  assert.deepEqual(clipIntervalAgainstGaps(4_000, 4_000, gaps), [{ startMs: 4_100, endMs: 4_100 }]);
  assert.deepEqual(clipIntervalAgainstGaps(3_950, 3_950, gaps), [{ startMs: 3_900, endMs: 3_900 }]);
  assert.deepEqual(
    clipIntervalAgainstGaps(4_000, 4_050, gaps),
    [{ startMs: 4_100, endMs: 4_150 }],
  );
  assert.deepEqual(clipIntervalAgainstGaps(Number.NaN, 1_000, gaps), []);
});

test('clipIntervalAgainstGaps pins a swallowed span when the shoulder has no room', () => {
  const gaps = [
    { startMs: 3_900, endMs: 4_100 },
    { startMs: 4_100, endMs: 4_300 },
  ];
  assert.deepEqual(
    clipIntervalAgainstGaps(4_000, 4_050, gaps),
    [{ startMs: 4_100, endMs: 4_100 }],
  );
});

test('formatUserTurnClock puts month-day above the clock', () => {
  const stamp = new Date(2026, 9, 31, 18, 8, 0).getTime();
  assert.equal(formatUserTurnClock(stamp), '10-31\n18:08:00');
  assert.equal(formatUserTurnClock(Number.NaN), '');
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
