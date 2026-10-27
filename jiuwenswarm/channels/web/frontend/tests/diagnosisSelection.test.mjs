import assert from 'node:assert/strict';
import test from 'node:test';

import {
  EMPTY_DIAGNOSIS_SELECTION,
  buildDiagnosisScope,
  hasSelection,
  isAgentIncluded,
  laneRangeOf,
  selectOnAxis,
  selectOnLane,
  DIAGNOSIS_NODE_LIMIT,
  selectedNodeCount,
  selectionCoversInterval,
  selectionRanges,
  toggleAgent,
  toggleSelectAllOnLane,
  isAgentFullySelected,
  unionWallRange,
} from '../node_modules/.cache/trajectory-diagnosis/diagnosisSelection.mjs';

const SUBJECTS = [
  { subjectId: 'main', subjectLabel: 'Leader' },
  { subjectId: 'a', subjectLabel: 'Agent A' },
  { subjectId: 'b', subjectLabel: 'Agent B' },
];
const window1 = { startMs: 1_000, endMs: 2_000 };
const window2 = { startMs: 3_000, endMs: 4_000 };

test('nothing is picked at first', () => {
  assert.equal(EMPTY_DIAGNOSIS_SELECTION.mode, 'none');
  assert.deepEqual(selectionRanges(EMPTY_DIAGNOSIS_SELECTION, SUBJECTS), []);
  assert.equal(hasSelection(EMPTY_DIAGNOSIS_SELECTION, SUBJECTS), false);
  for (const subject of SUBJECTS) assert.equal(isAgentIncluded(EMPTY_DIAGNOSIS_SELECTION, subject.subjectId), false);
  assert.equal(toggleAgent(EMPTY_DIAGNOSIS_SELECTION, 'a'), EMPTY_DIAGNOSIS_SELECTION);
});

test('a window on the axis applies to every agent', () => {
  const state = selectOnAxis(EMPTY_DIAGNOSIS_SELECTION, window1);
  assert.equal(state.mode, 'axis');
  assert.deepEqual(
    selectionRanges(state, SUBJECTS).map(item => [item.subjectId, item.startMs, item.endMs]),
    [['main', 1_000, 2_000], ['a', 1_000, 2_000], ['b', 1_000, 2_000]],
  );
  for (const subject of SUBJECTS) assert.deepEqual(laneRangeOf(state, subject.subjectId), window1);
});

test('unticked agents leave the axis pick and come back when ticked again', () => {
  let state = selectOnAxis(EMPTY_DIAGNOSIS_SELECTION, window1);
  state = toggleAgent(state, 'a');
  assert.equal(isAgentIncluded(state, 'a'), false);
  assert.equal(laneRangeOf(state, 'a'), null);
  assert.deepEqual(selectionRanges(state, SUBJECTS).map(item => item.subjectId), ['main', 'b']);
  state = toggleAgent(state, 'a');
  assert.equal(isAgentIncluded(state, 'a'), true);
  assert.equal(selectionRanges(state, SUBJECTS).length, 3);
});

test('unticking every agent leaves nothing to run', () => {
  let state = selectOnAxis(EMPTY_DIAGNOSIS_SELECTION, window1);
  for (const subject of SUBJECTS) state = toggleAgent(state, subject.subjectId);
  assert.equal(hasSelection(state, SUBJECTS), false);
});

test('ticks survive clearing and redrawing the axis window', () => {
  let state = selectOnAxis(EMPTY_DIAGNOSIS_SELECTION, window1);
  state = toggleAgent(state, 'b');
  state = selectOnAxis(state, null);
  assert.equal(state.mode, 'none');
  state = selectOnAxis(state, window2);
  assert.deepEqual(selectionRanges(state, SUBJECTS).map(item => item.subjectId), ['main', 'a']);
});

test('drawing on a lane drops the axis window and keeps one window per agent', () => {
  let state = selectOnAxis(EMPTY_DIAGNOSIS_SELECTION, window1);
  state = selectOnLane(state, 'a', window2);
  assert.equal(state.mode, 'lanes');
  assert.equal(state.axisRange, null);
  assert.deepEqual(selectionRanges(state, SUBJECTS).map(item => [item.subjectId, item.startMs]), [['a', 3_000]]);
  state = selectOnLane(state, 'b', window1);
  assert.deepEqual(
    selectionRanges(state, SUBJECTS).map(item => [item.subjectId, item.startMs]),
    [['a', 3_000], ['b', 1_000]],
  );
  assert.equal(laneRangeOf(state, 'main'), null);
});

test('redrawing a lane replaces only that agent window', () => {
  let state = selectOnLane(EMPTY_DIAGNOSIS_SELECTION, 'a', window1);
  state = selectOnLane(state, 'b', window1);
  state = selectOnLane(state, 'a', window2);
  assert.deepEqual(laneRangeOf(state, 'a'), window2);
  assert.deepEqual(laneRangeOf(state, 'b'), window1);
});

test('clearing the last lane window returns to nothing picked', () => {
  let state = selectOnLane(EMPTY_DIAGNOSIS_SELECTION, 'a', window1);
  state = selectOnLane(state, 'a', null);
  assert.equal(state.mode, 'none');
  assert.equal(hasSelection(state, SUBJECTS), false);
});

test('drawing on the axis after lane windows clears every lane window', () => {
  let state = selectOnLane(EMPTY_DIAGNOSIS_SELECTION, 'a', window1);
  state = selectOnLane(state, 'b', window2);
  state = selectOnAxis(state, window2);
  assert.equal(state.mode, 'axis');
  assert.equal(state.laneRanges.size, 0);
  assert.equal(selectionRanges(state, SUBJECTS).length, 3);
});

test('with lane windows a tick shows whether the agent has one', () => {
  let state = selectOnLane(EMPTY_DIAGNOSIS_SELECTION, 'a', window1);
  assert.equal(isAgentIncluded(state, 'a'), true);
  assert.equal(isAgentIncluded(state, 'b'), false);
  // A window cannot be made by ticking.
  assert.equal(toggleAgent(state, 'b'), state);
  state = toggleAgent(state, 'a');
  assert.equal(state.mode, 'none');
});

test('invalid windows pick nothing', () => {
  assert.equal(selectOnAxis(EMPTY_DIAGNOSIS_SELECTION, { startMs: 5, endMs: 1 }).mode, 'none');
  assert.equal(selectOnAxis(EMPTY_DIAGNOSIS_SELECTION, { startMs: Number.NaN, endMs: 1 }).mode, 'none');
  assert.equal(selectOnLane(EMPTY_DIAGNOSIS_SELECTION, 'a', { startMs: 5, endMs: 1 }).mode, 'none');
});

test('agents unknown to the page are not sent', () => {
  const state = selectOnLane(EMPTY_DIAGNOSIS_SELECTION, 'gone', window1);
  assert.deepEqual(selectionRanges(state, SUBJECTS), []);
});

test('the whole trajectory needs no pick', () => {
  assert.deepEqual(buildDiagnosisScope(EMPTY_DIAGNOSIS_SELECTION, SUBJECTS, true), { kind: 'full' });
});

test('a pick becomes one request range per agent in Unix nanoseconds', () => {
  const state = selectOnLane(selectOnLane(EMPTY_DIAGNOSIS_SELECTION, 'b', window2), 'a', window1);
  const scope = buildDiagnosisScope(state, SUBJECTS, false);
  assert.equal(scope.kind, 'selection');
  assert.deepEqual(scope.ranges.map(range => range.subject_id), ['a', 'b']);
  assert.equal(scope.ranges[0].start_unix_nano, '1000000000');
  // The end widens to the whole last millisecond.
  assert.equal(scope.ranges[0].end_unix_nano, '2000999999');
});

test('a pick covers the main agent interval only while that agent is included', () => {
  const lastTurn = { startMs: 1_500, endMs: 1_800 };
  const axis = selectOnAxis(EMPTY_DIAGNOSIS_SELECTION, window1);
  assert.equal(selectionCoversInterval(axis, 'main', lastTurn, false), true);
  assert.equal(selectionCoversInterval(toggleAgent(axis, 'main'), 'main', lastTurn, false), false);
  assert.equal(selectionCoversInterval(axis, 'main', { startMs: 2_500, endMs: 2_600 }, false), false);
  assert.equal(selectionCoversInterval(axis, 'main', { startMs: 100, endMs: 1_500 }, false), false);
  assert.equal(selectionCoversInterval(axis, 'main', { startMs: 1_500, endMs: 2_500 }, false), false);
  assert.equal(selectionCoversInterval(axis, 'main', { startMs: 1_000, endMs: 1_000 }, false), true);
  const lanes = selectOnLane(EMPTY_DIAGNOSIS_SELECTION, 'a', window1);
  assert.equal(selectionCoversInterval(lanes, 'main', lastTurn, false), false);
  assert.equal(selectionCoversInterval(EMPTY_DIAGNOSIS_SELECTION, 'main', lastTurn, true), true);
  assert.equal(selectionCoversInterval(axis, 'main', { startMs: 5, endMs: 1 }, false), false);
});

test('a pick counts an OTel span only when start and end are both covered', () => {
  const lanes = [
    {
      subjectId: 'main',
      intervals: [
        { startMs: 1_100, endMs: 1_200 },
        { startMs: 1_500, endMs: 2_500 },
        { startMs: 100, endMs: 1_500 },
        { startMs: 5, endMs: 1 },
      ],
    },
    { subjectId: 'a', intervals: [{ startMs: 1_100, endMs: 1_200 }] },
  ];
  const axis = selectOnAxis(EMPTY_DIAGNOSIS_SELECTION, window1);
  assert.equal(selectedNodeCount(EMPTY_DIAGNOSIS_SELECTION, lanes, false), 0);
  assert.equal(selectedNodeCount(axis, lanes, false), 2);
  assert.equal(selectedNodeCount(toggleAgent(axis, 'a'), lanes, false), 1);
  assert.equal(selectedNodeCount(EMPTY_DIAGNOSIS_SELECTION, lanes, true), 4);
  assert.equal(
    selectionCoversInterval(axis, 'main', { startMs: 1_500, endMs: 2_500 }, false),
    false,
  );
  assert.equal(
    selectionCoversInterval(axis, 'main', { startMs: 1_100, endMs: 1_200 }, false),
    true,
  );
  const many = [{ subjectId: 'main', intervals: Array.from({ length: DIAGNOSIS_NODE_LIMIT + 1 }, () => ({
    startMs: 1_100,
    endMs: 1_200,
  })) }];
  assert.equal(selectedNodeCount(axis, many, false) > DIAGNOSIS_NODE_LIMIT, true);
  const exactly = [{ subjectId: 'main', intervals: many[0].intervals.slice(0, DIAGNOSIS_NODE_LIMIT) }];
  assert.equal(selectedNodeCount(axis, exactly, false) > DIAGNOSIS_NODE_LIMIT, false);
});

test('full-scope selected count matches the header span total from raw intervals', () => {
  const lanes = [
    {
      subjectId: 'main',
      intervals: [
        { startMs: 1_000, endMs: 1_100 },
        { startMs: 2_000, endMs: 2_500 },
        { startMs: 3_000, endMs: 3_000 },
      ],
    },
    { subjectId: 'a', intervals: [{ startMs: 1_500, endMs: 1_600 }] },
    { subjectId: 'b', intervals: [] },
  ];
  const headerSpans = lanes.reduce((sum, lane) => sum + lane.intervals.length, 0);
  assert.equal(headerSpans, 4);
  assert.equal(selectedNodeCount(EMPTY_DIAGNOSIS_SELECTION, lanes, true), headerSpans);
  assert.equal(
    selectedNodeCount(selectOnAxis(EMPTY_DIAGNOSIS_SELECTION, window1), lanes, true),
    headerSpans,
  );
});

test('without a pick no scope is built unless the whole trajectory is chosen', () => {
  assert.equal(buildDiagnosisScope(EMPTY_DIAGNOSIS_SELECTION, SUBJECTS, false), null);
  const unknownOnly = selectOnLane(EMPTY_DIAGNOSIS_SELECTION, 'gone', window1);
  assert.equal(buildDiagnosisScope(unknownOnly, SUBJECTS, false), null);
});

test('unionWallRange spans every interval of one agent', () => {
  assert.equal(unionWallRange([]), null);
  assert.equal(unionWallRange([{ startMs: 5, endMs: 1 }]), null);
  assert.deepEqual(
    unionWallRange([
      { startMs: 3_000, endMs: 3_500 },
      { startMs: 1_000, endMs: 1_200 },
      { startMs: 2_000, endMs: 4_000 },
    ]),
    { startMs: 1_000, endMs: 4_000 },
  );
});

test('select-all puts one agent full span into lane mode', () => {
  const full = unionWallRange([window1, window2]);
  assert.deepEqual(full, { startMs: 1_000, endMs: 4_000 });
  let state = selectOnAxis(EMPTY_DIAGNOSIS_SELECTION, window1);
  state = selectOnLane(state, 'a', full);
  assert.equal(state.mode, 'lanes');
  assert.deepEqual(laneRangeOf(state, 'a'), full);
  assert.equal(laneRangeOf(state, 'main'), null);
  assert.deepEqual(selectionRanges(state, SUBJECTS).map(item => item.subjectId), ['a']);
  assert.equal(selectedNodeCount(state, [{
    subjectId: 'a',
    intervals: [window1, window2, { startMs: 1_500, endMs: 3_500 }],
  }], false), 3);
});

test('select-all toggles the same agent full span off on a second click', () => {
  const intervals = [window1, window2];
  let state = toggleSelectAllOnLane(EMPTY_DIAGNOSIS_SELECTION, 'a', intervals);
  assert.equal(isAgentFullySelected(state, 'a', intervals), true);
  assert.deepEqual(laneRangeOf(state, 'a'), { startMs: 1_000, endMs: 4_000 });
  state = toggleSelectAllOnLane(state, 'a', intervals);
  assert.equal(isAgentFullySelected(state, 'a', intervals), false);
  assert.equal(state.mode, 'none');
  assert.equal(laneRangeOf(state, 'a'), null);
});
