// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  countTurns,
  unrecordedTurnRanges,
} from '../node_modules/.cache/trajectory-turn-gaps/trajectoryTurnGaps.mjs';

test('a session recorded from its first turn has no unrecorded turns', () => {
  assert.deepEqual(unrecordedTurnRanges([1, 2, 3], 0), []);
  assert.deepEqual(unrecordedTurnRanges([], 0), []);
});

test('turns before the switch was turned on are reported as one leading range', () => {
  const ranges = unrecordedTurnRanges([7, 8], 0);
  assert.deepEqual(ranges, [{ first: 1, last: 6 }]);
  assert.equal(countTurns(ranges), 6);
});

test('turning the switch off and on again leaves a range between recorded turns', () => {
  assert.deepEqual(
    unrecordedTurnRanges([1, 2, 5, 9], 0),
    [{ first: 3, last: 4 }, { first: 6, last: 8 }],
  );
});

test('turns retention removed are accounted for, not unrecorded', () => {
  assert.deepEqual(unrecordedTurnRanges([5, 6], 4), []);
  assert.deepEqual(unrecordedTurnRanges([7], 4), [{ first: 5, last: 6 }]);
});

test('work between turns, repeated numbers and order do not create gaps', () => {
  assert.deepEqual(unrecordedTurnRanges([null, 3, 2, 3, null, 1], 0), []);
  assert.deepEqual(unrecordedTurnRanges([null], 0), []);
});
