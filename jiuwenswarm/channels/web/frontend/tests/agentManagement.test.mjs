import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isPreviewableFile,
  normalizeAgentSource,
  normalizeAgentTemplateDetail,
  normalizeAgentTemplateListItem,
  normalizeAgentFileContent,
  normalizeAgentFileTree,
  normalizeAgentGroupListItem,
} from '../node_modules/.cache/agent-management/adapter.js';

test('keeps Hub Expert Teams distinct from local groups', () => {
  const item = normalizeAgentGroupListItem({
    id: 'group-asset-id', name: 'runtime-group', source: 'hub', installed: false,
    capabilities: { canInstall: true, canPublish: false },
  }, 'en');
  assert.equal(item.source, 'hub');
  assert.equal(item.name, 'runtime-group');
  assert.equal(item.capabilities.canInstall, true);
  assert.equal(item.capabilities.canPublish, false);
});
import {
  buildDefinitionSelectionPayload,
  buildDefinitionSelectionPayloadForMode,
  isAgentGroupSelected,
  resolveSelectedSkillsForRequest,
} from '../node_modules/.cache/agent-management/port.js';
import { isAgentUploadFilename } from '../node_modules/.cache/agent-management/upload.js';
import {
  agentManagementReducer,
  createInitialAgentManagementState,
  initialAgentManagementState,
} from '../node_modules/.cache/agent-management/state.js';
import { resolveAgentTagPayload } from '../node_modules/.cache/agent-management/tagOptions.js';
import {
  isAgentGroupAgentCompatibilityLoading,
  isMcpSelectable,
  isTeamSkillOption,
  isSkillVisibleInSourceTab,
  sortInstalledFirst,
  sortAgentGroupOptions,
  sortMcpOptions,
} from '../node_modules/.cache/agent-management/selection.js';
import {
  buildCatalogViewModel,
  buildGroupCatalogViewModel,
  findFirstPreviewableFile,
  mergeAgentDetailWithCatalog,
} from '../node_modules/.cache/agent-management/viewModel.js';
import {
  advancePendingInstallQueue,
  createPendingInstallQueue,
  enqueuePendingInstall,
} from '../node_modules/.cache/agent-management/pendingInstallQueue.js';

test('pending Expert installs keep target identity while connector flows run serially', () => {
  const first = {
    id: 'expert-a', mode: 'catalog', pendingConnectors: ['connector-a'],
  };
  const second = {
    id: 'expert-b', mode: 'group-picker', pendingConnectors: ['connector-b'],
  };
  const queued = enqueuePendingInstall(
    enqueuePendingInstall(createPendingInstallQueue(), first),
    second,
  );

  assert.deepEqual(queued.active, first);
  assert.deepEqual(queued.waiting, [second]);

  const afterFirst = advancePendingInstallQueue(queued);
  assert.deepEqual(afterFirst.finished, first);
  assert.deepEqual(afterFirst.queue.active, second);
  assert.deepEqual(afterFirst.queue.waiting, []);

  const afterSecond = advancePendingInstallQueue(afterFirst.queue);
  assert.deepEqual(afterSecond.finished, second);
  assert.equal(afterSecond.queue.active, null);
  assert.deepEqual(afterSecond.queue.waiting, []);
});

test('selection pickers share installed-first and label sorting', () => {
  const items = [
    { id: 'expert-z', displayName: 'Zulu', installed: true },
    { id: 'expert-a', displayName: 'alpha', installed: true },
    { id: 'skill-z', name: 'Zulu', installed: false },
    { id: 'skill-a', name: 'Alpha', installed: false },
  ];

  assert.deepEqual(sortInstalledFirst(items).map((item) => item.id), [
    'expert-a',
    'expert-z',
    'skill-a',
    'skill-z',
  ]);
});

test('connector selection only allows connected items and orders by availability', () => {
  const items = [
    { id: 'not-installed', name: 'Alpha', installed: false, connectionState: 'disconnected' },
    { id: 'pending-connection', name: 'Bravo', installed: true, connectionState: 'disconnected' },
    { id: 'connecting', name: 'Charlie', installed: true, connectionState: 'connecting' },
    { id: 'available-z', name: 'Zulu', installed: true, connectionState: 'connected' },
    { id: 'available-a', name: 'Alpha', installed: true, connectionState: 'connected' },
  ];

  assert.deepEqual(sortMcpOptions(items).map((item) => item.id), [
    'available-a',
    'available-z',
    'pending-connection',
    'connecting',
    'not-installed',
  ]);
  assert.equal(isMcpSelectable(items[3]), true);
  assert.equal(isMcpSelectable(items[0]), false);
  assert.equal(isMcpSelectable(items[1]), false);
  assert.equal(isMcpSelectable(items[2]), false);
});

test('market Expert options still loading team compatibility sort after ready options', () => {
  const items = [
    {
      id: 'uninstalled',
      displayName: 'Uninstalled',
      source: 'hub',
      installed: false,
      teamCompatible: undefined,
    },
    {
      id: 'loading',
      displayName: 'Loading',
      source: 'hub',
      installed: true,
      teamCompatible: undefined,
    },
    {
      id: 'ready',
      displayName: 'Ready',
      source: 'hub',
      installed: true,
      teamCompatible: { leader: true, member: true },
    },
  ];

  assert.deepEqual(sortAgentGroupOptions(items, 'loading').map((item) => item.id), [
    'ready',
    'loading',
    'uninstalled',
  ]);
  assert.equal(isAgentGroupAgentCompatibilityLoading(items[1], 'loading'), true);
  assert.equal(isAgentGroupAgentCompatibilityLoading(items[1], 'success'), false);
  assert.equal(isAgentGroupAgentCompatibilityLoading(items[0], 'loading'), false);
  assert.equal(
    isAgentGroupAgentCompatibilityLoading(
      { id: 'builtin', displayName: 'Built-in', source: 'builtin', installed: true, teamCompatible: undefined },
      'loading',
    ),
    false,
  );
});

test('skill source tabs keep marketplace and local visibility semantics', () => {
  const marketplace = { source: 'hub', installed: false };
  const installedMarketplace = { source: 'hub', installed: true };
  const local = { source: 'local', installed: false };

  assert.equal(isSkillVisibleInSourceTab(marketplace, 'market'), true);
  assert.equal(isSkillVisibleInSourceTab(marketplace, 'local'), false);
  assert.equal(isSkillVisibleInSourceTab(installedMarketplace, 'market'), true);
  assert.equal(isSkillVisibleInSourceTab(installedMarketplace, 'local'), true);
  assert.equal(isSkillVisibleInSourceTab(local, 'market'), false);
  assert.equal(isSkillVisibleInSourceTab(local, 'local'), true);
});

test('team skill filtering accepts marketplace plugin type and installed kind contracts', () => {
  assert.equal(isTeamSkillOption({ pluginType: 'swarmskill' }, 'market'), true);
  assert.equal(isTeamSkillOption({ pluginType: 'swarmskill' }, 'local'), false);
  assert.equal(isTeamSkillOption({ kind: 'team-skill' }, 'market'), true);
  assert.equal(isTeamSkillOption({ skillType: 'swarm_skill' }, 'market'), true);
  assert.equal(isTeamSkillOption({ kind: 'swarm-skill' }, 'local'), true);
  assert.equal(isTeamSkillOption({ kind: 'team-skill' }, 'local'), true);
  assert.equal(isTeamSkillOption({ skillType: 'swarm_skill' }, 'local'), true);
  assert.equal(isTeamSkillOption({ pluginType: 'skill', kind: 'skill', skillType: 'skill' }, 'market'), false);
});

test('Expert Team plaza includes Hub groups alongside built-in groups', () => {
  const items = [
    { id: 'built-in', source: 'builtin', category: '', tags: [], name: 'built-in', displayName: 'Built-in', description: '' },
    { id: 'remote', source: 'hub', category: '', tags: [], name: 'remote', displayName: 'Remote', description: '' },
    { id: 'mine', source: 'local', category: '', tags: [], name: 'mine', displayName: 'Mine', description: '' },
  ];
  const view = buildGroupCatalogViewModel(items, { scope: 'catalog', category: '', query: '', page: 1, pageSize: 10 });
  assert.deepEqual(view.items.map(item => item.id), ['built-in', 'remote']);
});

test('Expert Team plaza filters installed state before pagination', () => {
  const items = [
    { id: 'installed', source: 'hub', installed: true, category: '', tags: [], name: 'installed', displayName: 'Installed', description: '' },
    { id: 'pending', source: 'hub', installed: false, category: '', tags: [], name: 'pending', displayName: 'Pending', description: '' },
  ];
  const options = { scope: 'catalog', category: '', query: '', page: 2, pageSize: 1 };
  const installed = buildGroupCatalogViewModel(items, { ...options, installation: 'installed' });
  const uninstalled = buildGroupCatalogViewModel(items, { ...options, installation: 'uninstalled' });
  assert.deepEqual(installed.items.map(item => item.id), ['installed']);
  assert.deepEqual(uninstalled.items.map(item => item.id), ['pending']);
  assert.equal(installed.totalItems, 1);
  assert.equal(installed.page, 1);
});

test('My Expert Teams filters install state before pagination', () => {
  const items = [
    { id: 'local-installed', source: 'local', installed: true, category: '', tags: [], name: 'local-installed', displayName: 'Local installed', description: '' },
    { id: 'local-pending', source: 'local', installed: false, category: '', tags: [], name: 'local-pending', displayName: 'Local pending', description: '' },
    { id: 'hub-installed', source: 'hub', installed: true, category: '', tags: [], name: 'hub-installed', displayName: 'Hub installed', description: '' },
  ];
  const options = { scope: 'mine', category: '', query: '', page: 2, pageSize: 1 };
  const installed = buildGroupCatalogViewModel(items, { ...options, installation: 'installed' });
  const uninstalled = buildGroupCatalogViewModel(items, { ...options, installation: 'uninstalled' });
  assert.deepEqual(installed.items.map(item => item.id), ['hub-installed']);
  assert.equal(installed.totalItems, 2);
  assert.deepEqual(uninstalled.items.map(item => item.id), ['local-pending']);
  assert.equal(uninstalled.totalItems, 1);
  assert.equal(uninstalled.page, 1);
});

test('normalizes interface source variants and bilingual display fields', () => {
  assert.equal(normalizeAgentSource('built-in'), 'builtin');
  assert.equal(normalizeAgentSource('builtin-in'), 'builtin');
  assert.equal(normalizeAgentSource('local'), 'local');

  const item = normalizeAgentTemplateListItem(
    {
      id: 'python-code-reviewer',
      displayName: { zh: 'Python 代码检视专家', en: 'Python Code Reviewer' },
      displayDescription: { zh: '检查 Python 代码', en: 'Reviews Python code' },
      category: 'Engineering',
      source: 'built-in',
      installed: true,
      enabled: true,
    },
    'en',
  );

  assert.deepEqual(item, {
    id: 'python-code-reviewer',
    runtimePackageName: 'python-code-reviewer',
    displayName: 'Python Code Reviewer',
    description: 'Reviews Python code',
    category: 'Engineering',
    source: 'builtin',
    installed: true,
    connectionState: 'disconnected',
    enabled: true,
    tags: [],
    avatarUrl: null,
  });
  assert.equal(item.enabled, true);
});

test('keeps Hub asset identity separate from the expert runtime package name', () => {
  const item = normalizeAgentTemplateListItem(
    {
      id: '8b52a9c0-hub-asset',
      packageName: 'sales-data-analyst',
      displayName: { zh: '销售数据分析专家', en: 'Sales Data Analyst' },
      displayDescription: { zh: '分析销售数据', en: 'Analyzes sales data' },
      source: 'hub',
      installed: false,
      version: '1.2.0',
    },
    'zh',
  );

  assert.equal(item.id, '8b52a9c0-hub-asset');
  assert.equal(item.hubAssetId, '8b52a9c0-hub-asset');
  assert.equal(item.runtimePackageName, 'sales-data-analyst');
  assert.equal(item.source, 'hub');
  assert.equal(item.version, '1.2.0');
});

test('projects detail capabilities without leaking raw package fields', () => {
  const detail = normalizeAgentTemplateDetail(
    {
      id: 'content-creator',
      displayName: { zh: '内容创作专家', en: 'Content Creation Expert' },
      displayDescription: { zh: '内容能力', en: 'Content capability' },
      source: 'local',
      avatar: 'avatars/avatar.png',
      version: '1.0.0',
      details: '# 内容创作专家',
      tags: [{ id: 'copywriting', zh: '文案创作', en: 'Copywriting' }],
      skills: [{ id: 'content-methodology', displayName: { zh: '内容方法', en: 'Content Methodology' } }],
      tools: [],
      rails: [],
      mcps: [],
      quickInputs: [{ zh: '帮我写标题', en: 'Write titles' }],
    },
    'zh',
  );

  assert.equal(detail.displayName, '内容创作专家');
  assert.equal(detail.tags[0].id, 'copywriting');
  assert.equal(detail.skills[0].id, 'content-methodology');
  assert.deepEqual(detail.suggestedPrompts, ['帮我写标题']);
  assert.equal(detail.version, '1.0.0');
  assert.equal('api_key' in detail, false);
});

test('detail merges authoritative install state from list when show omits it', () => {
  const detail = normalizeAgentTemplateDetail({ id: 'python-code-reviewer', displayName: { zh: 'Python' } }, 'zh');
  const merged = mergeAgentDetailWithCatalog(detail, {
    id: 'python-code-reviewer',
    runtimePackageName: 'python-code-reviewer',
    displayName: 'Python 代码检视专家',
    description: '检查 Python 代码',
    category: 'Engineering',
    source: 'builtin',
    installed: true,
    connectionState: 'connected',
    tags: [],
    avatarUrl: null,
  });

  assert.equal(merged.installed, true);
  assert.equal(merged.source, 'builtin');
});

test('preserves an explicitly disabled template for selection guards', () => {
  const item = normalizeAgentTemplateListItem(
    {
      id: 'disabled-agent',
      displayName: { zh: '不可用专家' },
      installed: true,
      enabled: false,
    },
    'zh',
  );

  assert.equal(item.enabled, false);
});

test('normalizes package file tree and keeps preview policy extension-based', () => {
  assert.equal(isPreviewableFile('README.md'), true);
  assert.equal(isPreviewableFile('manifest.JSON'), true);
  assert.equal(isPreviewableFile('tools/runtime.py'), true);
  assert.equal(isPreviewableFile('docs/guide.pdf'), true);
  assert.equal(isPreviewableFile('runtime.bin'), false);

  const tree = normalizeAgentFileTree([
    {
      path: 'persona/',
      type: 'dir',
      children: [
        { path: 'persona/hidden.md', type: 'file', visible: false, size: 12 },
        { path: 'persona/agent.md', type: 'file', size: 12 },
      ],
    },
    { path: 'manifest.json', type: 'file', size: 42 },
    { path: 'runtime.bin', type: 'file', size: 1024, previewable: false },
  ]);

  assert.equal(tree[0].kind, 'directory');
  assert.equal(tree[0].children[0].visible, false);
  assert.equal(tree[0].children[1].previewable, true);
  assert.equal(tree[1].previewable, true);
  assert.equal(tree[2].relativePath, 'runtime.bin');
  assert.equal(tree[2].previewable, false);
});

test('normalizes binary preview URLs without inventing text content', () => {
  const file = normalizeAgentFileContent({
    path: 'docs/guide.pdf',
    content: null,
    download_url: '/file-api/download?token=pdf',
  });
  assert.equal(file.content, null);
  assert.equal(file.downloadUrl, '/file-api/download?token=pdf');
});

test('initial file selection skips hidden previewable files', () => {
  const files = normalizeAgentFileTree([
    {
      path: 'assets/',
      type: 'dir',
      children: [{ path: 'assets/hidden.md', type: 'file', visible: false }],
    },
    {
      path: 'persona/',
      type: 'dir',
      children: [{ path: 'persona/SKILL.md', type: 'file' }],
    },
  ]);

  assert.equal(findFirstPreviewableFile(files), 'persona/SKILL.md');
});

test('selection payload preserves keep, clear and select semantics', () => {
  assert.deepEqual(buildDefinitionSelectionPayload({ kind: 'keep' }), {});
  assert.deepEqual(buildDefinitionSelectionPayload({ kind: 'clear' }), { agent_template_name: '' });
  assert.deepEqual(buildDefinitionSelectionPayload({ kind: 'select', id: 'content-creator' }), {
    agent_template_name: 'content-creator',
  });
});

test('Agent Group selection owns the Team skill slot across selection states', () => {
  assert.equal(isAgentGroupSelected('agent', { kind: 'select', id: 'group-1' }), false);
  assert.equal(isAgentGroupSelected('team', { kind: 'keep' }), false);
  assert.equal(isAgentGroupSelected('team', { kind: 'select', id: 'group-1' }), true);
  assert.equal(isAgentGroupSelected('team', { kind: 'keep' }, 'group-1'), true);
  assert.equal(isAgentGroupSelected('team', { kind: 'keep' }, null, 'group-1'), true);
  assert.equal(isAgentGroupSelected('team', { kind: 'select', id: '  ' }), false);
  assert.deepEqual(
    resolveSelectedSkillsForRequest('team', ['team-skill'], { kind: 'keep' }, 'group-1'),
    [],
  );
  assert.deepEqual(
    resolveSelectedSkillsForRequest('team', ['team-skill'], { kind: 'keep' }),
    ['team-skill'],
  );
});

test('Agent upload accepts only zip and tar archives', () => {
  assert.equal(isAgentUploadFilename('agent.ZIP'), true);
  assert.equal(isAgentUploadFilename('agent.tar'), true);
  assert.equal(isAgentUploadFilename('agent.tar.gz'), false);
  assert.equal(isAgentUploadFilename('agent.rar'), false);
});

test('selection payload is restricted to ordinary Agent mode', () => {
  assert.deepEqual(buildDefinitionSelectionPayloadForMode('agent', { kind: 'select', id: 'content-creator' }), {
    agent_template_name: 'content-creator',
  });
  assert.deepEqual(buildDefinitionSelectionPayloadForMode('team', { kind: 'select', id: 'content-creator' }), {});
  assert.deepEqual(buildDefinitionSelectionPayloadForMode('auto_harness', { kind: 'clear' }), {});
});

test('custom tags keep fixed and user-entered labels in create order', () => {
  assert.deepEqual(resolveAgentTagPayload(['product-development'], ['行业研究', '数据产品']), [
    { zh: '产品研发', en: 'Product Development' },
    { zh: '行业研究', en: '行业研究' },
    { zh: '数据产品', en: '数据产品' },
  ]);
});

test('catalog view model filters mine/search and returns the full filtered list', () => {
  const catalog = [
    {
      id: 'a',
      displayName: '甲',
      description: '市场',
      category: 'Design',
      source: 'local',
      installed: true,
      connectionState: 'connected',
      tags: [],
      avatarUrl: null,
    },
    {
      id: 'b',
      displayName: '乙',
      description: '工程',
      category: 'Engineering',
      source: 'builtin',
      installed: false,
      connectionState: 'disconnected',
      tags: [],
      avatarUrl: null,
    },
    {
      id: 'hub-c',
      displayName: '丙',
      description: '远端专家',
      category: 'Engineering',
      source: 'hub',
      installed: false,
      connectionState: 'disconnected',
      tags: [],
      avatarUrl: null,
    },
  ];
  // 2026-09-11 分页移除后 viewModel 不再收 page/pageSize、返回全量过滤结果（见
  // buildCatalogViewModel），断言只看 items/totalItems。
  const view = buildCatalogViewModel(catalog, {
    scope: 'mine',
    category: '',
    query: '市场',
  });

  assert.equal(view.totalItems, 1);
  assert.deepEqual(
    view.items.map((item) => item.id),
    ['a'],
  );

  const installedBuiltin = buildCatalogViewModel(
    [
      {
        id: 'builtin',
        displayName: '官方',
        description: '',
        category: '',
        source: 'builtin',
        installed: true,
        connectionState: 'connected',
        tags: [],
        avatarUrl: null,
      },
    ],
    { scope: 'mine', category: '', query: '' },
  );
  assert.deepEqual(
    installedBuiltin.items.map((item) => item.id),
    ['builtin'],
  );

  const productCatalog = buildCatalogViewModel(catalog, {
    scope: 'catalog',
    category: 'ProductDevelopment',
    query: '',
  });
  assert.deepEqual(
    productCatalog.items.map((item) => item.id),
    ['b', 'hub-c'],
  );
});

test('canonical reducer keeps file selection and content status separate from source DTOs', () => {
  const loading = agentManagementReducer(initialAgentManagementState, {
    type: 'file.loading',
    relativePath: 'README.md',
  });
  assert.equal(loading.fileStatus, 'loading');
  assert.equal(loading.selectedFilePath, 'README.md');

  const ready = agentManagementReducer(loading, {
    type: 'file.loaded',
    content: { relativePath: 'README.md', content: '# ready' },
  });
  assert.equal(ready.fileStatus, 'success');
  assert.equal(ready.fileContent.content, '# ready');

  const reset = agentManagementReducer(
    { ...ready, filesStatus: 'success', files: [{ relativePath: 'README.md', kind: 'file', previewable: true }] },
    { type: 'detail.loading' },
  );
  assert.equal(reset.detailStatus, 'loading');
  assert.equal(reset.filesStatus, 'idle');
  assert.equal(reset.selectedFilePath, null);
});

test('agent catalog starts empty and reports the current request failure', () => {
  const cachedAgent = {
    id: 'cached-agent',
    runtimePackageName: 'cached-agent',
    displayName: '缓存专家',
    description: '立即显示',
    category: 'Efficiency',
    source: 'hub',
    installed: false,
    connectionState: 'disconnected',
    tags: [],
    avatarUrl: null,
  };
  const initial = createInitialAgentManagementState([cachedAgent]);

  assert.equal(initial.catalogStatus, 'idle');
  assert.deepEqual(initial.catalog, []);

  const loading = agentManagementReducer(initial, { type: 'catalog.loading' });
  const failed = agentManagementReducer(loading, { type: 'catalog.error', message: 'Hub timeout' });
  assert.equal(failed.catalogStatus, 'error');
  assert.deepEqual(failed.catalog, []);
  assert.equal(failed.catalogError, 'Hub timeout');
});

test('catalog compatibility state keeps failures separate from cached catalog status', () => {
  const cachedAgent = {
    id: 'cached-agent',
    runtimePackageName: 'cached-agent',
    displayName: '缓存专家',
    description: '',
    category: 'Efficiency',
    source: 'hub',
    installed: true,
    connectionState: 'connected',
    tags: [],
    avatarUrl: null,
  };
  const cached = {
    ...initialAgentManagementState,
    catalog: [cachedAgent],
    catalogStatus: 'success',
  };

  const loading = agentManagementReducer(cached, { type: 'catalog.compatibility.loading' });
  const failed = agentManagementReducer(loading, {
    type: 'catalog.compatibility.error',
    message: 'Compatibility timeout',
  });
  assert.equal(failed.catalogStatus, 'success');
  assert.equal(failed.catalogCompatibilityStatus, 'error');
  assert.equal(failed.catalogCompatibilityError, 'Compatibility timeout');
  assert.deepEqual(failed.catalog, [cachedAgent]);

  const ready = agentManagementReducer(loading, { type: 'catalog.compatibility.loaded' });
  assert.equal(ready.catalogCompatibilityStatus, 'success');
  assert.equal(ready.catalogCompatibilityError, null);
});

test('agent detail does not display summary data when the full detail request fails', () => {
  const fallback = {
    id: 'cached-agent',
    runtimePackageName: 'cached-agent',
    displayName: '缓存专家',
    description: '摘要说明',
    category: 'Efficiency',
    source: 'hub',
    installed: false,
    connectionState: 'disconnected',
    tags: [],
    avatarUrl: null,
    prompt: '',
    details: '摘要说明',
    skills: [],
    tools: [],
    rails: [],
    mcps: [],
    suggestedPrompts: [],
    pendingConnectors: [],
  };

  const loading = agentManagementReducer(initialAgentManagementState, { type: 'detail.loading', fallback });
  const failed = agentManagementReducer(loading, { type: 'detail.error', message: 'Hub timeout' });
  assert.equal(failed.detailStatus, 'error');
  assert.equal(failed.detail, null);
  assert.equal(failed.detailError, 'Hub timeout');
});
