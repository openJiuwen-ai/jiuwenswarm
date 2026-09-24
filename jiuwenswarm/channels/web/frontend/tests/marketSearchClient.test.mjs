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
