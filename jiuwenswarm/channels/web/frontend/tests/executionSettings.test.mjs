// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';
import { restoredGitIdentity } from '../node_modules/.cache/execution-settings/executionSettings.mjs';

test('restores both committed states from session metadata', () => {
  assert.equal(restoredGitIdentity({ execution_settings: { independent_git_identity: true } }), true);
  assert.equal(restoredGitIdentity({ execution_settings: { independent_git_identity: false, environment: {} } }), false);
});

test('leaves the draft alone when nothing was committed', () => {
  assert.equal(restoredGitIdentity(null), undefined);
  assert.equal(restoredGitIdentity({}), undefined);
  assert.equal(restoredGitIdentity({ execution_settings: { independent_git_identity: 'true' } }), undefined);
});
