import assert from 'node:assert/strict';
import test from 'node:test';

import { effectivePermissionProfile, permissionOptionsForMode } from '../node_modules/.cache/permission-profiles/config/permissionProfiles.js';

test('single agent exposes automatic approval', () => {
  assert.deepEqual(permissionOptionsForMode('agent'), ['default', 'automatic', 'full_access']);
  assert.equal(effectivePermissionProfile('automatic', 'agent'), 'automatic');
});

test('team and auto harness hide automatic without changing persisted profile', () => {
  assert.deepEqual(permissionOptionsForMode('team'), ['default', 'full_access']);
  assert.deepEqual(permissionOptionsForMode('auto_harness'), ['default', 'full_access']);
  assert.equal(effectivePermissionProfile('automatic', 'team'), 'default');
  assert.equal(effectivePermissionProfile('automatic', 'auto_harness'), 'default');
  assert.equal(effectivePermissionProfile('full_access', 'team'), 'full_access');
});
