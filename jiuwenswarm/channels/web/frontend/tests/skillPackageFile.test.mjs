import assert from 'node:assert/strict';
import test from 'node:test';

import { isSkillPackageFile } from '../node_modules/.cache/skill-package-file/utils/skillPackageFile.js';

test('is_skill_package true shows as skill even for plain zip name', () => {
  assert.equal(
    isSkillPackageFile({ name: 'demo.zip', is_skill_package: true }),
    true
  );
});

test('plain zip without flag is not skill package', () => {
  assert.equal(isSkillPackageFile({ name: 'artifact.zip' }), false);
  assert.equal(isSkillPackageFile({ name: 'artifact.zip', is_skill_package: false }), false);
});

test('skill extensions are skill packages without flag', () => {
  assert.equal(isSkillPackageFile({ name: 'demo.skill' }), true);
  assert.equal(isSkillPackageFile({ name: 'demo.skill.zip' }), true);
  assert.equal(isSkillPackageFile({ path: 'C:\\out\\pack.skill' }), true);
});
