// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  RAW_INSPECTOR_DEFAULT_HEIGHT,
  RAW_INSPECTOR_MIN_HEIGHT,
  clampRawInspectorHeight,
  rawInspectorHeightBounds,
  rawInspectorKeyboardHeight,
  shouldInsetTrajectoryForFloatingTasks,
} from '../node_modules/.cache/trajectory-layout/trajectoryLayout.js';

test('raw inspector height is clamped to its fixed minimum and container-relative maximum', () => {
  assert.deepEqual(rawInspectorHeightBounds(600), { min: 120, max: 360 });
  assert.equal(clampRawInspectorHeight(40, 600), RAW_INSPECTOR_MIN_HEIGHT);
  assert.equal(clampRawInspectorHeight(500, 600), 360);
  assert.equal(clampRawInspectorHeight(RAW_INSPECTOR_DEFAULT_HEIGHT, 600), 220);
  assert.deepEqual(rawInspectorHeightBounds(100), { min: 120, max: 120 });
});

test('raw inspector separator supports keyboard resizing and ignores unrelated keys', () => {
  assert.equal(rawInspectorKeyboardHeight(220, 'ArrowUp', 600), 236);
  assert.equal(rawInspectorKeyboardHeight(220, 'ArrowDown', 600), 204);
  assert.equal(rawInspectorKeyboardHeight(220, 'Home', 600), 120);
  assert.equal(rawInspectorKeyboardHeight(220, 'End', 600), 360);
  assert.equal(rawInspectorKeyboardHeight(220, 'Escape', 600), null);
  assert.equal(rawInspectorKeyboardHeight(355, 'ArrowUp', 600), 360);
  assert.equal(rawInspectorKeyboardHeight(125, 'ArrowDown', 600), 120);
});

test('Agent and Team trajectories reserve space for a visible collapsed floating task panel', () => {
  assert.equal(
    shouldInsetTrajectoryForFloatingTasks('agent', 'trajectory', true, false, false),
    true,
  );
  assert.equal(
    shouldInsetTrajectoryForFloatingTasks('agent', 'trajectory', true, true, false),
    false,
  );
  assert.equal(
    shouldInsetTrajectoryForFloatingTasks('agent', 'trajectory', true, false, true),
    false,
  );
  assert.equal(
    shouldInsetTrajectoryForFloatingTasks('agent', 'chat', true, false, false),
    false,
  );
  assert.equal(
    shouldInsetTrajectoryForFloatingTasks('team', 'trajectory', true, false, false),
    true,
  );
  assert.equal(
    shouldInsetTrajectoryForFloatingTasks('auto_harness', 'trajectory', true, false, false),
    false,
  );
});
