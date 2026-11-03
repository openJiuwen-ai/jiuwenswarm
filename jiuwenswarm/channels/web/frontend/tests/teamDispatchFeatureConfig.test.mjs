import assert from 'node:assert/strict';
import test from 'node:test';

import {
  resolveRestoredTeamDispatchMode,
} from '../node_modules/.cache/team-dispatch-config/featureConfig.mjs';

test('restored dispatch mode prefers an explicit metadata value', () => {
  assert.equal(resolveRestoredTeamDispatchMode('scheduled', 'leader'), 'scheduled');
  assert.equal(resolveRestoredTeamDispatchMode('autonomous', 'framework'), 'autonomous');
});

test('a persisted bootstrap mode recovers scheduled dispatch after the lost-update bug', () => {
  assert.equal(resolveRestoredTeamDispatchMode(undefined, 'leader'), 'scheduled');
  assert.equal(resolveRestoredTeamDispatchMode(undefined, 'framework'), 'scheduled');
  assert.equal(resolveRestoredTeamDispatchMode('invalid', 'leader'), 'scheduled');
});

test('missing dispatch metadata without a bootstrap mode stays unspecified', () => {
  assert.equal(resolveRestoredTeamDispatchMode(undefined, undefined), undefined);
  assert.equal(resolveRestoredTeamDispatchMode('invalid', 'invalid'), undefined);
});
