import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildSkillSourceDisplayNameMap,
  resolveSkillSourceDisplayName,
  shouldShowSkillTrustBadge,
} from '../node_modules/.cache/skill-approval-presentation/components/ChatPanel/skillApprovalPresentation.js';

test('registered source display name is used without changing its source id', () => {
  const displayNames = buildSkillSourceDisplayNameMap([
    { source_id: 'customer-source', display_name: 'Customer Skill Center' },
  ]);

  assert.equal(
    resolveSkillSourceDisplayName('customer-source', displayNames),
    'Customer Skill Center',
  );
  assert.equal(resolveSkillSourceDisplayName('unknown-source', displayNames), 'unknown-source');
});

test('blank source display name falls back to the stable source id', () => {
  const displayNames = buildSkillSourceDisplayNameMap([
    { source_id: 'customer-source', display_name: '   ' },
  ]);

  assert.equal(resolveSkillSourceDisplayName('customer-source', displayNames), 'customer-source');
});

test('only builtin skills display a trust badge', () => {
  assert.equal(shouldShowSkillTrustBadge('builtin'), true);
  assert.equal(shouldShowSkillTrustBadge('other'), false);
});
