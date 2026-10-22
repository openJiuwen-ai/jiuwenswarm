import assert from 'node:assert/strict';
import test from 'node:test';

import { shouldPollSkillGraph } from '../node_modules/.cache/skill-graph-polling/components/SkillGraphPanel/skillGraphPolling.js';

test('stops polling when the panel is inactive', () => {
  assert.equal(shouldPollSkillGraph({ isActive: false, updating: true, whenUpdating: true }), false);
  assert.equal(shouldPollSkillGraph({ isActive: false, updating: false, whenUpdating: false }), false);
  assert.equal(shouldPollSkillGraph({ isActive: false, updating: true, whenUpdating: false }), false);
});

test('progress-log poll runs only while updating and active', () => {
  assert.equal(shouldPollSkillGraph({ isActive: true, updating: true, whenUpdating: true }), true);
  assert.equal(shouldPollSkillGraph({ isActive: true, updating: false, whenUpdating: true }), false);
});

test('passive status poll runs only while idle and active', () => {
  assert.equal(shouldPollSkillGraph({ isActive: true, updating: false, whenUpdating: false }), true);
  assert.equal(shouldPollSkillGraph({ isActive: true, updating: true, whenUpdating: false }), false);
});
