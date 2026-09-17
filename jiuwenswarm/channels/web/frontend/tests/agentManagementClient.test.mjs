import test from 'node:test';
import assert from 'node:assert/strict';
import { build } from 'esbuild';
await build({
  entryPoints: ['src/features/agentManagement/client.ts', 'src/services/webClient.ts'],
  bundle: true,
  splitting: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outdir: 'node_modules/.cache/agent-management-client',
  define: { 'import.meta.env': '{}' },
});
const { createLiveAgentManagementClient } =
  await import('../node_modules/.cache/agent-management-client/features/agentManagement/client.js');
const { webClient } = await import('../node_modules/.cache/agent-management-client/services/webClient.js');
const templates = [
  {
    id: 'cached',
    name: 'cached',
    display_name: 'Cached expert',
    description: 'A cached expert',
    source: 'hub',
    installed: false,
    tags: [],
  },
];
const cache = { state: 'stale', refreshing: true, complete: false };
test('cached catalog with missing tags renders without remote per-card detail requests', async () => {
  const calls = [];
  webClient.request = async (method) => {
    calls.push(method);
    if (method === 'agent_templates.list') return { templates, cache };
    throw new Error('Remote detail is unavailable');
  };
  const items = await createLiveAgentManagementClient().listCatalog({ filter: 'builtin+hub' });
  assert.equal(items.length, 1);
  assert.equal(items[0].id, 'cached');
  assert.deepEqual(items.cache, cache);
  assert.deepEqual(calls, ['agent_templates.list']);
});
test('optional tag enrichment retains cached cards when detail requests fail', async () => {
  webClient.request = async (method) => {
    if (method === 'agent_templates.list') return { templates, cache };
    throw new Error('Remote detail is unavailable');
  };
  const items = await createLiveAgentManagementClient().listCatalog({ enrichTags: true });
  assert.equal(items[0].id, 'cached');
  assert.deepEqual(items[0].tags, []);
  assert.deepEqual(items.cache, cache);
});
