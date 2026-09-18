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

test('skill and connector picker adapters retain marketplace/install state', async () => {
  const calls = [];
  webClient.request = async (method, params) => {
    calls.push([method, params]);
    if (method === 'skills.list') {
      return {
        skills: [
          {
            name: 'team-review',
            display_name: 'Team Review',
            description: 'Review with a team',
            source: 'team-market',
            marketplace: 'team-market',
            kind: 'team-skill',
            skill_type: 'swarm_skill',
            installed: false,
          },
          {
            name: 'local-review',
            display_name: 'Local Review',
            description: 'Review locally',
            source: 'project',
            installed: true,
          },
          { name: 'bundled-mcp-skill', source: 'mcp', installed: true },
        ],
      };
    }
    if (method === 'mcp.list') {
      if (params.filter === 'builtin') {
        return {
          items: [
            {
              id: 'hub-connector-1',
              name: 'market-connector',
              package_name: 'market-connector',
              display_name: 'Market Connector',
              description: 'A marketplace connector',
              category: 'search',
              integration_type: 'stdio-mcp',
              connection_state: 'disconnected',
              has_bundled_skills: false,
              source: 'hub',
              installed: false,
              connected: false,
            },
          ],
        };
      }
      return {
        items: [
          {
            id: 'market-connector',
            name: 'market-connector',
            package_name: 'market-connector',
            display_name: 'Market Connector',
            description: 'A connected marketplace connector',
            category: 'search',
            integration_type: 'stdio-mcp',
            connection_state: 'connected',
            has_bundled_skills: false,
            source: 'built_in',
            installed: true,
            connected: true,
          },
          {
            id: 'custom-connector',
            name: 'custom-connector',
            display_name: 'Custom Connector',
            description: 'A local connector',
            category: 'custom',
            integration_type: 'remote-mcp',
            connection_state: 'disconnected',
            has_bundled_skills: false,
            source: 'customize',
            installed: true,
            connected: false,
          },
        ],
      };
    }
    throw new Error(`Unexpected method: ${method}`);
  };

  const client = createLiveAgentManagementClient();
  const skills = await client.listSkillOptions();
  assert.deepEqual(skills.map((skill) => skill.id), ['team-review', 'local-review']);
  assert.equal(skills[0].kind, 'team-skill');
  assert.equal(skills[0].installSpec, 'team-review@team-market');
  assert.equal(skills[1].installed, true);

  const mcps = await client.listMcpOptions();
  assert.deepEqual(mcps.map((mcp) => mcp.id), ['market-connector', 'custom-connector']);
  assert.equal(mcps[0].installed, true);
  assert.equal(mcps[0].hubAssetId, 'hub-connector-1');
  assert.equal(mcps[1].connectionState, 'disconnected');
  assert.deepEqual(calls.map(([method]) => method), ['skills.list', 'mcp.list', 'mcp.list']);
});
