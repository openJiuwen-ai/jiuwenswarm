import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isPreviewableFile,
  normalizeAgentSource,
  normalizeAgentTemplateDetail,
  normalizeAgentTemplateListItem,
  normalizeAgentFileTree,
} from '../node_modules/.cache/agent-management/adapter.js';
import {
  buildDefinitionSelectionPayload,
  buildDefinitionSelectionPayloadForMode,
} from '../node_modules/.cache/agent-management/port.js';
import { isAgentUploadFilename } from '../node_modules/.cache/agent-management/upload.js';
import {
  agentManagementReducer,
  createInitialAgentManagementState,
  initialAgentManagementState,
} from '../node_modules/.cache/agent-management/state.js';
import { resolveAgentTagPayload } from '../node_modules/.cache/agent-management/tagOptions.js';
import {
  buildCatalogViewModel,
  findFirstPreviewableFile,
  mergeAgentDetailWithCatalog,
} from '../node_modules/.cache/agent-management/viewModel.js';

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
  ]);

  assert.equal(tree[0].kind, 'directory');
  assert.equal(tree[0].children[0].visible, false);
  assert.equal(tree[0].children[1].previewable, true);
  assert.equal(tree[1].previewable, true);
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

test('catalog view model filters mine/search and clamps pages deterministically', () => {
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
  const view = buildCatalogViewModel(catalog, {
    scope: 'mine',
    category: '',
    query: '市场',
    page: 99,
    pageSize: 1,
  });

  assert.equal(view.totalItems, 1);
  assert.equal(view.page, 1);
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
    { scope: 'mine', category: '', query: '', page: 1, pageSize: 6 },
  );
  assert.deepEqual(
    installedBuiltin.items.map((item) => item.id),
    ['builtin'],
  );

  const productCatalog = buildCatalogViewModel(catalog, {
    scope: 'catalog',
    category: 'ProductDevelopment',
    query: '',
    page: 1,
    pageSize: 12,
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
