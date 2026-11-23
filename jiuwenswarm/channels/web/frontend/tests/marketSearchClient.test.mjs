import './fixtures/browserEnvironment.mjs';
import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

await build({
  entryPoints: [
    'src/features/agentManagement/client.ts',
    'src/features/agentManagement/groupClient.ts',
    'src/services/pluginPackagesApi.ts',
    'src/services/connectorApi.ts',
    'src/services/webClient.ts',
  ],
  bundle: true,
  splitting: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outdir: 'node_modules/.cache/market-search-client',
  define: { 'import.meta.env': '{}' },
});

const { createLiveAgentManagementClient } =
  await import('../node_modules/.cache/market-search-client/features/agentManagement/client.js');
const { createLiveAgentGroupManagementClient } =
  await import('../node_modules/.cache/market-search-client/features/agentManagement/groupClient.js');
const { pluginPackagesApi } =
  await import('../node_modules/.cache/market-search-client/services/pluginPackagesApi.js');
const { connectorApi } =
  await import('../node_modules/.cache/market-search-client/services/connectorApi.js');
const { webClient } =
  await import('../node_modules/.cache/market-search-client/services/webClient.js');

test('market catalog clients forward the debounced query to their list RPCs', async () => {
  const calls = [];
  webClient.request = async (method, params) => {
    calls.push([method, params]);
    if (method === 'agent_templates.list') return { templates: [] };
    if (method === 'agent_groups.list') return { agentGroups: [] };
    if (method === 'plugin_packages.list') return { packages: [] };
    if (method === 'mcp.list') return { items: [] };
    throw new Error(`Unexpected method: ${method}`);
  };

  await createLiveAgentManagementClient().listCatalog({ filter: 'builtin+hub', query: '销售' });
  await createLiveAgentGroupManagementClient().listGroups({ filter: 'builtin+hub', query: '销售' });
  await pluginPackagesApi.list('builtin+hub', '销售');
  await connectorApi.list('builtin', '销售');

  assert.deepEqual(calls, [
    ['agent_templates.list', { filter: 'builtin+hub', query: '销售' }],
    ['agent_groups.list', { filter: 'builtin+hub', query: '销售' }],
    ['plugin_packages.list', { filter: 'builtin+hub', query: '销售' }],
    ['mcp.list', { filter: 'builtin', query: '销售' }],
  ]);
});

test('expert and expert team detail requests use the same 90 second timeout', async () => {
  const calls = [];
  webClient.request = async (method, params, options) => {
    calls.push([method, params, options]);
    return {};
  };

  await assert.rejects(createLiveAgentManagementClient().getDefinition('expert-id'));
  await assert.rejects(createLiveAgentGroupManagementClient().getGroup('team-id'));

  assert.deepEqual(calls, [
    ['agent_templates.show', { id: 'expert-id' }, { timeoutMs: 90_000 }],
    ['agent_groups.show', { id: 'team-id' }, { timeoutMs: 90_000 }],
  ]);
});

test('connector list preserves the visible display name', async () => {
  webClient.request = async () => ({ items: [{
    name: 'hidden-6', display_name: '回归连接器',
    display_name_i18n: { zh: '回归连接器', en: 'Regression Connector' },
    source: 'customize', description: 'description-only', category: '',
  }] });
  const items = await connectorApi.list('local');
  assert.equal(items[0].displayName, '回归连接器');
});

test('connector catalogue RPCs forward optional source_id', async () => {
  const calls = [];
  webClient.request = async (method, params) => {
    calls.push([method, params]);
    if (method === 'mcp.list') return { items: [] };
    if (method === 'mcp.show') {
      return {
        item: {
          id: 'asset-1',
          name: 'pkg-1',
          display_name: 'Pkg',
          description: '',
          category: '',
          integration_type: 'stdio-mcp',
          connection_state: 'disconnected',
          has_bundled_skills: false,
          source: 'hub',
          source_id: 'corp-hub',
          installed: false,
          connected: false,
          bundled_skills: [],
          skills: [],
          tools: [],
        },
      };
    }
    if (method === 'mcp.install') {
      return { type: 'installed', item: { id: 'asset-1', name: 'pkg-1', installed: true } };
    }
    if (method === 'mcp.sources.providers') {
      return {
        type: 'list',
        providers: [
          {
            source_id: 'openjiuwen-hub',
            display_name: 'OpenJiuwen Hub',
            enabled: true,
            capabilities: ['search'],
          },
          {
            source_id: 'corp-hub',
            display_name: 'Corp Hub',
            enabled: true,
            capabilities: ['search'],
          },
        ],
      };
    }
    throw new Error(`Unexpected method: ${method}`);
  };

  await connectorApi.list('hub', '钉钉', { sourceId: 'corp-hub' });
  const detail = await connectorApi.show('asset-1', { sourceId: 'corp-hub' });
  await connectorApi.install('asset-1', { sourceId: 'corp-hub' });
  const providers = await connectorApi.listSourceProviders();

  assert.deepEqual(calls, [
    ['mcp.list', { filter: 'hub', query: '钉钉', source_id: 'corp-hub' }],
    ['mcp.show', { id: 'asset-1', source_id: 'corp-hub' }],
    ['mcp.install', { id: 'asset-1', source_id: 'corp-hub' }],
    ['mcp.sources.providers', {}],
  ]);
  assert.equal(detail.sourceId, 'corp-hub');
  assert.equal(providers.length, 2);
  assert.equal(providers[1].source_id, 'corp-hub');
});
