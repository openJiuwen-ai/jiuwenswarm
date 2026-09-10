// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  deriveTrajectoryTimeline,
  panTrajectoryTimelineViewport,
  resolveTimelineMode,
  trajectoryTimelineFocusIndexes,
  zoomTrajectoryTimelineViewport,
} from '../node_modules/.cache/trajectory-timeline/timeline.mjs';

function cell(index, kind, extra = {}) {
  return { index, kind, text: `${kind}-${index}`, timeSeconds: null, ...extra };
}

function turn(number, cells) {
  return { turn: number, groups: [{ cells }] };
}

function tokenTimeline(turns) {
  return deriveTrajectoryTimeline(turns, 'tokens');
}

test('token blocks lay input and output head to tail across the full spend', () => {
  const model = tokenTimeline([
    turn(1, [
      cell(0, 'user'),
      cell(1, 'message', { input: 1_000, cacheRead: 400, output: 200 }),
      cell(2, 'tool'),
      cell(3, 'message', { input: 2_000, cacheRead: 1_500, output: 100 }),
    ]),
  ]);

  assert.deepEqual(
    model.spans.map(span => [span.segment, span.lane, span.start, span.end]),
    [
      ['input', 0, 0, 600],
      ['output', 1, 600, 800],
      ['input', 0, 800, 1_300],
      ['output', 1, 1_300, 1_400],
    ],
  );
  assert.equal(model.start, 0);
  assert.equal(model.end, 1_400);
});

test('input blocks measure cache-missed input and keep cache writes billed', () => {
  const model = tokenTimeline([
    turn(1, [cell(0, 'message', { input: 900, cacheRead: 300, cacheWrite: 200, output: 50 })]),
  ]);

  const input = model.spans.find(span => span.segment === 'input');
  assert.equal(input.end - input.start, 600);
});

test('input blocks split on the share written into the cache', () => {
  const model = tokenTimeline([
    turn(1, [
      cell(0, 'message', { input: 900, cacheRead: 300, cacheWrite: 150, output: 10 }),
      cell(1, 'message', { input: 900, cacheRead: 300, output: 10 }),
      // Everything new went into the cache, so the provider reports no plain miss.
      cell(2, 'message', { input: 900, cacheRead: 300, cacheWrite: 600, output: 10 }),
    ]),
  ]);

  assert.deepEqual(
    model.spans.filter(span => span.segment === 'input').map(span => span.splitFraction),
    [0.25, undefined, 1],
  );
});

test('an input block covers the records its request consumed, not the request row', () => {
  const model = tokenTimeline([
    turn(1, [
      cell(0, 'system'),
      cell(1, 'user'),
      cell(2, 'message', { input: 500, output: 40 }),
      cell(3, 'tool'),
      cell(4, 'subtool'),
      cell(5, 'message', { input: 800, output: 60 }),
    ]),
  ]);

  const inputs = model.spans.filter(span => span.segment === 'input');
  assert.deepEqual(inputs.map(span => span.coveredIndexes), [[0, 1], [3, 4]]);
  assert.deepEqual(inputs.map(span => span.index), [2, 5]);
  const outputs = model.spans.filter(span => span.segment === 'output');
  assert.deepEqual(outputs.map(span => span.coveredIndexes), [[2], [5]]);
});

test('an input block without preceding records falls back to its own request', () => {
  const model = tokenTimeline([
    turn(1, [cell(0, 'message', { input: 400, output: 10 })]),
  ]);

  assert.deepEqual(model.spans[0].coveredIndexes, [0]);
});

test('requests without usage take no room and hand their records to the next request', () => {
  const model = tokenTimeline([
    turn(1, [
      cell(0, 'user'),
      cell(1, 'message'),
      cell(2, 'tool'),
      cell(3, 'message', { input: 300, output: 20 }),
    ]),
  ]);

  assert.equal(model.spans.length, 2);
  assert.deepEqual(model.spans[0].coveredIndexes, [2]);
});

test('request-only records and trailing tool results never own a block', () => {
  const model = tokenTimeline([
    turn(1, [
      cell(0, 'context', { requestOnly: true }),
      cell(1, 'message', { input: 100, output: 10 }),
      cell(2, 'tool'),
    ]),
  ]);

  assert.deepEqual(model.spans.map(span => span.coveredIndexes), [[1], [1]]);
  assert.equal(model.end, 110);
});

test('the token projection is empty without reported usage', () => {
  assert.equal(
    tokenTimeline([turn(1, [cell(0, 'user'), cell(1, 'message'), cell(2, 'tool')])]),
    null,
  );
});

test('output blocks split on the reasoning share, up to an all-reasoning block', () => {
  const model = tokenTimeline([
    turn(1, [
      cell(0, 'message', { input: 10, output: 400, think: 100 }),
      cell(1, 'message', { input: 10, output: 400, think: 0 }),
      cell(2, 'message', { input: 10, output: 400, think: 400 }),
    ]),
  ]);

  assert.deepEqual(
    model.spans.filter(span => span.segment === 'output').map(span => span.splitFraction),
    [0.25, undefined, 1],
  );
});

test('turn boundaries land on the cumulative spend where each turn opens', () => {
  const model = tokenTimeline([
    turn(1, [cell(0, 'message', { input: 500, output: 100 })]),
    turn(2, [cell(1, 'message', { input: 200, output: 50 })]),
    turn(3, [cell(2, 'user')]),
  ]);

  assert.deepEqual(model.turnBoundaries, [
    { turn: 1, time: 0 },
    { turn: 2, time: 600 },
  ]);
});

test('focusing an input block selects every record that request consumed', () => {
  const turns = [
    turn(1, [
      cell(0, 'user'),
      cell(1, 'message', { input: 500, output: 100 }),
      cell(2, 'tool'),
      cell(3, 'message', { input: 200, output: 50 }),
    ]),
  ];

  assert.deepEqual(
    [...trajectoryTimelineFocusIndexes(turns, { start: 10, end: 490 }, 'tokens')],
    [0],
  );
  assert.deepEqual(
    [...trajectoryTimelineFocusIndexes(turns, { start: 520, end: 580 }, 'tokens')],
    [1],
  );
  assert.deepEqual(
    [...trajectoryTimelineFocusIndexes(turns, { start: 610, end: 790 }, 'tokens')],
    [2],
  );
});

test('token cost wins over the recorded-duration and complete-time switches', () => {
  assert.equal(
    resolveTimelineMode({ tokenView: true, actualDuration: true, actualTime: true }),
    'tokens',
  );
  assert.equal(
    resolveTimelineMode({ tokenView: false, actualDuration: false, actualTime: false }),
    'sequence',
  );
  assert.equal(
    resolveTimelineMode({ tokenView: false, actualDuration: true, actualTime: false }),
    'duration',
  );
  assert.equal(
    resolveTimelineMode({ tokenView: false, actualDuration: true, actualTime: true }),
    'actual',
  );
  assert.equal(
    resolveTimelineMode({ tokenView: false, actualDuration: false, actualTime: true }),
    'time',
  );
});

test('zoom keeps the pointed timeline time anchored and respects the minimum width', () => {
  const next = zoomTrajectoryTimelineViewport(
    { start: 0, end: 1_000 },
    null,
    0.75,
    -500,
    100,
  );

  assert.ok(next !== null);
  assert.ok(next.end - next.start >= 100);
  assert.ok(Math.abs(next.start + 0.75 * (next.end - next.start) - 750) < 0.000_001);
});

test('zooming back to the complete domain clears the viewport', () => {
  assert.equal(
    zoomTrajectoryTimelineViewport(
      { start: 0, end: 1_000 },
      { start: 250, end: 750 },
      0.5,
      10_000,
      100,
    ),
    null,
  );
});

test('horizontal scrolling pans a zoomed viewport and clamps both edges', () => {
  const fullRange = { start: 0, end: 1_000 };

  assert.deepEqual(
    panTrajectoryTimelineViewport(fullRange, { start: 200, end: 600 }, 0.25),
    { start: 300, end: 700 },
  );
  assert.deepEqual(
    panTrajectoryTimelineViewport(fullRange, { start: 200, end: 600 }, -10),
    { start: 0, end: 400 },
  );
  assert.deepEqual(
    panTrajectoryTimelineViewport(fullRange, { start: 200, end: 600 }, 10),
    { start: 600, end: 1_000 },
  );
  assert.equal(panTrajectoryTimelineViewport(fullRange, null, 0.25), null);
});
