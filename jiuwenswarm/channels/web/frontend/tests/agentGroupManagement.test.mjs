import test from 'node:test';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import {
  buildAgentGroupCreatePayload,
  buildAgentGroupSelectionPayloadForMode,
  dedupeAgentGroupOptions,
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
  assert.equal(payload.tags[0].id, 'product-development');
  assert.equal(payload.tags[1].id, '交付');
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
  assert.equal(
    isAgentGroupAgentSelectable(
      { ...hubAgent, source: 'local', installed: false, teamCompatible: undefined },
      'leader',
    ),
    false,
  );
  assert.equal(
    isAgentGroupAgentSelectable(
      { ...hubAgent, source: 'local', installed: true, teamCompatible: undefined },
      'member',
    ),
    true,
  );
});

test('deduplicates Team options by runtime identity and keeps the installed entry', () => {
  const uninstalled = {
    id: 'hub-asset-123',
    runtimePackageName: 'engineering-quality-mentor',
    source: 'hub',
    installed: false,
  };
  const installed = {
    id: 'engineering-quality-mentor',
    runtimePackageName: 'engineering-quality-mentor',
    source: 'local',
    installed: true,
  };
  assert.deepEqual(dedupeAgentGroupOptions([uninstalled, installed]), [installed]);
  assert.deepEqual(dedupeAgentGroupOptions([installed, uninstalled]), [installed]);
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

test('group view model separates catalog and mine scopes and excludes tags from mine search', () => {
  const local = normalizeAgentGroupListItem({
    ...rawGroup,
    id: 'local-team',
    name: 'local-team',
    displayName: { zh: '本地团队', en: 'Local Team' },
    tags: [{ id: 'local-only', zh: '本地专属', en: 'Local only' }],
    source: 'local',
    installed: false,
  }, 'zh');
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
      query: '本地团队',
      page: 1,
      pageSize: 15,
    }).totalItems,
    1,
  );
});

test('ordinary Expert Team search does not match an internal Hub asset id', () => {
  const result = buildGroupCatalogViewModel(
    [
      {
        id: '93e6e963-0896-4473-8655-09f611bcddc8',
        name: 'research-team',
        displayName: '研究专家团',
        description: '面向行业研究',
        category: 'research',
        source: 'hub',
        installed: false,
        memberCount: 0,
        members: [],
        skills: [],
        tags: [],
        avatarUrl: null,
        capabilities: { canUse: false, canInstall: true, canUninstall: false, canPreviewFiles: true, canEdit: false, canPublish: false },
      },
    ],
    { scope: 'catalog', category: '', query: '6', page: 1, pageSize: 20 },
  );

  assert.equal(result.totalItems, 0);
});

test('detail merge preserves detail data and authoritative list capability state', () => {
  const item = normalizeAgentGroupListItem(rawGroup, 'zh');
  const detail = normalizeAgentGroupDetail({ ...rawGroup, installed: false, details: '详情' }, 'zh');
  const merged = mergeAgentGroupDetailWithCatalog(detail, item);
  assert.equal(merged.installed, true);
  assert.equal(merged.details, '详情');
});

test('Expert Team capability and content come only from detail fields', () => {
  for (const locale of ['zh', 'en']) {
    const catalogItem = normalizeAgentGroupListItem(rawGroup, locale);
    for (const displayDescription of [undefined, {}, { zh: '', en: '' }, { zh: '详情介绍', en: 'Detail capability' }]) {
      const detail = normalizeAgentGroupDetail({
        ...rawGroup,
        displayDescription,
        details: 'Content details',
      }, locale);
      const merged = mergeAgentGroupDetailWithCatalog(detail, catalogItem);
      assert.equal(merged.description, displayDescription?.[locale] || '');
      assert.equal(merged.details, 'Content details');
    }
  }
});

test('group upload accepts zip, tar and tar.gz only', () => {
  assert.equal(isAgentGroupUploadFilename('team.zip'), true);
  assert.equal(isAgentGroupUploadFilename('team.tar'), true);
  assert.equal(isAgentGroupUploadFilename('team.tar.gz'), true);
  assert.equal(isAgentGroupUploadFilename('team.json'), false);
});

test('mine group search matches only the visible localized name (#4473)', () => {
  const item = normalizeAgentGroupListItem({
    ...rawGroup, name: 'hidden-package-s-6',
    displayName: { zh: '旅游规划专家团', en: 'Travel Team' },
    displayDescription: { zh: '生成可执行旅行方案' },
    category: 'hidden-category', tags: [{ zh: 'hidden-tag' }],
  }, 'zh');
  const search = query => buildGroupCatalogViewModel([item], {
    scope: 'mine', category: '', query, page: 1, pageSize: 12,
  }).items.length;
  for (const query of ['生成', 'hidden', 's', '6', 'travel']) assert.equal(search(query), 0, query);
  for (const query of ['旅游', ' 旅游 ']) assert.equal(search(query), 1, query);
});

test('catalog filtering keeps description matches while mine excludes them', () => {
  const item = normalizeAgentGroupListItem({ ...rawGroup, displayDescription: { zh: 'description-only' } }, 'zh');
  const options = { category: '', query: 'description-only', page: 1, pageSize: 12 };
  assert.equal(buildGroupCatalogViewModel([item], { ...options, scope: 'catalog' }).totalItems, 1);
  assert.equal(buildGroupCatalogViewModel([item], { ...options, scope: 'mine' }).totalItems, 0);
});

test('chat group picker submits runtime package names and keeps selection checked', () => {
  const source = readFileSync(new URL('../src/components/ChatPanel/InputArea.tsx', import.meta.url), 'utf8');
  const picker = source.slice(source.indexOf('filteredGroupOptions.map((item) => {'));
  const handler = picker.match(/onClick=\{\(\) => \{([\s\S]*?)\}\}/)[1];
  const selectedExpression = picker.match(/const isSelected = ([^;]+);/)[1];
  for (const item of [
    { id: 'hub-asset-uuid', name: 'presentation-generation' },
    { id: 'local-team', name: 'local-team' },
  ]) {
    let intent;
    new Function('item', 'agentGroupSelectionDisabled', 'activeSessionId', 'useSessionStore',
      'setAgentGroupSelectionIntent', 'setAgentSelectionIntent', 'clearSelectedSkills', 'setAttachMenuOpen', handler)(
      item, false, 'new', { getState: () => ({ setMode() {} }) },
      (_session, value) => { intent = value; }, () => {}, () => {}, () => {},
    );
    assert.deepEqual(buildAgentGroupSelectionPayloadForMode('team', intent), { agent_group_name: item.name });
    assert.equal(new Function('item', 'selectedGroupId', `return ${selectedExpression}`)(item, item.name), true);
  }
});

test('uninstalling an agent group asks for confirmation before calling the API', () => {
  const source = readFileSync(new URL('../src/components/AgentManagementPanel/index.tsx', import.meta.url), 'utf8');
  const handler = source.slice(source.indexOf('const handleUninstallGroup'), source.indexOf('const handleReconnect'));
  const confirmAt = handler.indexOf('window.confirm(');
  assert.notEqual(confirmAt, -1);
  assert.ok(confirmAt < handler.indexOf('groupClient.uninstallGroup('));
  for (const locale of ['zh', 'en']) {
    const messages = JSON.parse(readFileSync(new URL(`../src/i18n/locales/${locale}.json`, import.meta.url), 'utf8'));
    assert.match(messages.agentManagement.confirm.uninstallGroupMessage, /\{\{name\}\}/);
  }
});
