import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildAgentGroupCreatePayload,
  buildAgentGroupSelectionPayloadForMode,
  isAgentGroupAgentSelectable,
  resolveAgentGroupSelectionId,
} from '../node_modules/.cache/agent-group-management/port.js';
import {
  buildGroupCatalogViewModel,
  mergeAgentGroupDetailWithCatalog,
} from '../node_modules/.cache/agent-group-management/viewModel.js';
import {
  normalizeAgentGroupDetail,
  normalizeAgentGroupListItem,
} from '../node_modules/.cache/agent-group-management/adapter.js';
import { isAgentGroupUploadFilename } from '../node_modules/.cache/agent-group-management/upload.js';

const rawGroup = {
  id: 'review-team',
  name: 'review-team',
  displayName: { zh: '评审团队', en: 'Review Team' },
  displayDescription: { zh: '方案评审', en: 'Review plans' },
  category: 'engineering',
  source: 'builtin',
  installed: true,
  memberCount: 2,
  members: [
    { id: 'leader', agentTemplateId: 'architect', displayName: { zh: '架构师' }, role: 'leader' },
    { id: 'qa', agentTemplateId: 'qa', displayName: { zh: '质量专家' }, role: 'member' },
  ],
  skills: [{ id: 'review', displayName: { zh: '评审技能' } }],
  tags: [{ id: 'review', zh: '评审', en: 'Review' }],
  capabilities: { canUse: true, canInstall: false, canUninstall: true, canPreviewFiles: true },
};

test('normalizes AgentGroup list/detail fields and safe avatar fallbacks', () => {
  const item = normalizeAgentGroupListItem(rawGroup, 'zh');
  assert.equal(item.displayName, '评审团队');
  assert.equal(item.members[0].role, 'leader');
  assert.equal(item.memberCount, 2);
  assert.equal(item.avatarUrl, null);
  const detail = normalizeAgentGroupDetail(
    {
      ...rawGroup,
      version: '1.0.0',
      updatedAt: '2026-09-01T00:00:00Z',
      details: '# 详情',
      persona: '先分析再汇总',
      leaderId: 'leader',
      quickInputs: [{ zh: '评审方案' }],
    },
    'zh',
  );
  assert.equal(detail.quickInputs[0], '评审方案');
  assert.equal(detail.version, '1.0.0');
});

test('builds a group create payload without unsupported Agent fields', () => {
  const payload = buildAgentGroupCreatePayload({
    id: 'delivery-team',
    name: '交付团队',
    description: '负责交付评审',
    persona: '先独立分析',
    category: 'engineering',
    tagIds: ['product-development'],
    customTags: ['交付'],
    leaderId: 'architect',
    memberIds: ['qa', 'qa', 'architect'],
    skillRefs: ['review', 'review'],
    suggestedPrompts: ['评审方案', '  '],
  });
  assert.deepEqual(payload.memberIds, ['qa']);
  assert.deepEqual(payload.skills, ['review']);
  assert.deepEqual(payload.quickInputs, ['评审方案']);
  assert.equal(payload.tags[0].zh, '产品研发');
  for (const forbidden of ['model', 'mcps', 'pluginNames', 'apiKey', 'apiBase', 'path']) {
    assert.equal(Object.hasOwn(payload, forbidden), false, forbidden);
  }
});

test('uses the installed Hub runtime identity for group selection', () => {
  const hubAgent = {
    id: 'hub-asset-123',
    runtimePackageName: 'sales-data-analyst',
    source: 'hub',
    installed: true,
    teamCompatible: { leader: true, member: true },
  };
  assert.equal(resolveAgentGroupSelectionId(hubAgent), 'sales-data-analyst');
  assert.equal(isAgentGroupAgentSelectable(hubAgent, 'leader'), true);
  assert.equal(
    isAgentGroupAgentSelectable({ ...hubAgent, installed: false }, 'member'),
    false,
  );
  assert.equal(
    isAgentGroupAgentSelectable({ ...hubAgent, teamCompatible: undefined }, 'member'),
    false,
  );
});

test('only an unbound Team selection creates the first group payload', () => {
  assert.deepEqual(buildAgentGroupSelectionPayloadForMode('team', { kind: 'select', id: 'review-team' }, null), {
    agent_group_name: 'review-team',
  });
  assert.deepEqual(
    buildAgentGroupSelectionPayloadForMode('team', { kind: 'select', id: 'review-team' }, null, false),
    {},
  );
  assert.deepEqual(
    buildAgentGroupSelectionPayloadForMode('team', { kind: 'select', id: 'review-team' }, 'review-team'),
    {},
  );
  assert.deepEqual(buildAgentGroupSelectionPayloadForMode('team', { kind: 'clear' }, null), {});
  assert.deepEqual(buildAgentGroupSelectionPayloadForMode('agent', { kind: 'select', id: 'review-team' }, null), {});
});

test('group view model separates catalog and mine scopes and searches tags', () => {
  const local = normalizeAgentGroupListItem({ ...rawGroup, id: 'local-team', source: 'local', installed: false }, 'zh');
  const item = normalizeAgentGroupListItem(rawGroup, 'zh');
  assert.equal(
    buildGroupCatalogViewModel([item, local], {
      scope: 'catalog',
      category: 'ProductDevelopment',
      query: '',
      page: 1,
      pageSize: 15,
    }).totalItems,
    1,
  );
  assert.equal(
    buildGroupCatalogViewModel([item, local], {
      scope: 'mine',
      category: '',
      query: 'local-team',
      page: 1,
      pageSize: 15,
    }).totalItems,
    1,
  );
});

test('detail merge preserves detail data and authoritative list capability state', () => {
  const item = normalizeAgentGroupListItem(rawGroup, 'zh');
  const detail = normalizeAgentGroupDetail({ ...rawGroup, installed: false, details: '详情' }, 'zh');
  const merged = mergeAgentGroupDetailWithCatalog(detail, item);
  assert.equal(merged.installed, true);
  assert.equal(merged.details, '详情');
});

test('group upload accepts zip, tar and tar.gz only', () => {
  assert.equal(isAgentGroupUploadFilename('team.zip'), true);
  assert.equal(isAgentGroupUploadFilename('team.tar'), true);
  assert.equal(isAgentGroupUploadFilename('team.tar.gz'), true);
  assert.equal(isAgentGroupUploadFilename('team.json'), false);
});
