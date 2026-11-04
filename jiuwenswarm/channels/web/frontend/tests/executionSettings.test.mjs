// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';
import { saveIndependentGitIdentity, restoredGitIdentity } from '../node_modules/.cache/execution-settings/executionSettings.mjs';

test('updates preserve environment and use the committed version', async () => {
  const calls = [];
  const request = async (method, params) => {
    calls.push({ method, params });
    return { version: 7, settings: { independent_git_identity: false, environment: { BUILD: 'debug', REMOVE: null } } };
  };
  await saveIndependentGitIdentity(request, 'session-a', true);
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1], {
    method: 'session.execution_settings',
    params: {
      session_id: 'session-a', mode: 'team', expected_version: 7,
      settings: { independent_git_identity: true, environment: { BUILD: 'debug', REMOVE: null } },
    },
  });
});

test('failed updates reject instead of pretending the switch was saved', async () => {
  const request = async (_method, params) => {
    if (params.settings) throw new Error('version conflict');
    return { version: 2, settings: { independent_git_identity: false } };
  };
  await assert.rejects(saveIndependentGitIdentity(request, 'session-a', true), /version conflict/);
});

test('restoration preserves unsent drafts and restores both persisted states', () => {
  assert.equal(restoredGitIdentity({ version: -1, settings: { independent_git_identity: false } }, true), true);
  assert.equal(restoredGitIdentity({ version: 0, settings: { independent_git_identity: false } }, true), false);
  assert.equal(restoredGitIdentity({ version: 1, settings: { independent_git_identity: true } }, false), true);
});
