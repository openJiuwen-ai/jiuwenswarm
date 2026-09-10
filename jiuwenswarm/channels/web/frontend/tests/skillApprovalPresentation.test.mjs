import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildSkillSourceDisplayNameMap,
  buildSkillSourceTypeMap,
  resolveSkillTrustBadge,
  resolveSkillSourceDisplayName,
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

test('enterprise prebuilt skill displays the enterprise prebuilt badge', () => {
  const sourceTypes = buildSkillSourceTypeMap([
    { name: 'enterprise-probe', source_type: 'prebuilt' },
  ]);

  assert.equal(resolveSkillTrustBadge('builtin', sourceTypes.get('enterprise-probe')), 'prebuilt');
});

test('repository builtin skill keeps the builtin badge', () => {
  assert.equal(resolveSkillTrustBadge('builtin', undefined), 'builtin');
});

test('untrusted skill does not display a trust badge', () => {
  assert.equal(resolveSkillTrustBadge('other', 'user'), null);
});

test('blank installation identity is ignored when building source types', () => {
  const sourceTypes = buildSkillSourceTypeMap([
    { name: ' enterprise-probe ', source_type: ' prebuilt ' },
    { name: ' ', source_type: 'prebuilt' },
  ]);

  assert.equal(sourceTypes.get('enterprise-probe'), 'prebuilt');
  assert.equal(sourceTypes.size, 1);
});
