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

test('builtin MCP listing failure does not hide local MCP options', async () => {
  webClient.request = async (method, params) => {
    if (method !== 'mcp.list') throw new Error(`Unexpected method: ${method}`);
    if (params.filter === 'builtin') throw new Error('Builtin MCP catalog unavailable');
    return {
      items: [
        {
          id: 'local-connector',
          name: 'local-connector',
          package_name: 'local-connector',
          display_name: 'Local Connector',
          description: 'A local connector',
          category: 'custom',
          integration_type: 'remote-mcp',
          connection_state: 'connected',
          has_bundled_skills: false,
          source: 'customize',
          installed: true,
          connected: true,
        },
      ],
    };
  };

  const mcps = await createLiveAgentManagementClient().listMcpOptions();
  assert.deepEqual(mcps.map((mcp) => mcp.id), ['local-connector']);
});

test('team skill market options use the SkillPanel type contract and Hub asset install', async () => {
  const calls = [];
  webClient.request = async (method, params) => {
    calls.push([method, params]);
    if (method === 'skills.list') {
      return {
        skills: [
          {
            name: 'installed-team',
            display_name: 'Installed Team',
            description: 'Installed team skill',
            source: 'teamskillshub',
            installed: true,
            kind: 'team-skill',
            skill_type: 'swarm_skill',
          },
        ],
      };
    }
    if (method === 'skills.swarmskillshub.recommend') {
      assert.equal(params.plugin_type, 'swarmskill');
      return {
        success: true,
        skills: [
          {
            asset_id: 'market-team-asset',
            name: 'market-team',
            display_name: 'Market Team',
            short_desc: 'Remote team skill',
            plugin_type: 'swarmskill',
          },
        ],
      };
    }
    if (method === 'skills.teamskillshub.install') {
      assert.equal(params.asset_id, 'market-team-asset');
      return { success: true };
    }
    throw new Error(`Unexpected method: ${method}`);
  };

  const client = createLiveAgentManagementClient();
  const skills = await client.listSkillOptions({ includeTeamMarketplace: true });
  const marketTeam = skills.find((skill) => skill.id === 'market-team');
  assert.ok(marketTeam);
  assert.equal(marketTeam.pluginType, 'swarmskill');
  assert.equal(marketTeam.hubAssetId, 'market-team-asset');
  assert.equal(marketTeam.installed, false);
  assert.equal(skills.find((skill) => skill.id === 'installed-team').skillType, 'swarm_skill');

  await client.installSkill(marketTeam);
  assert.deepEqual(calls.map(([method]) => method), [
    'skills.list',
    'skills.swarmskillshub.recommend',
    'skills.teamskillshub.install',
  ]);
});

test('team skill marketplace failure preserves the base skill list', async () => {
  webClient.request = async (method) => {
    if (method === 'skills.list') {
      return {
        skills: [{ name: 'local-review', display_name: 'Local Review', source: 'project', installed: true }],
      };
    }
    if (method === 'skills.swarmskillshub.recommend') {
      return { success: false, detail: 'Team Skills Hub unavailable' };
    }
    throw new Error(`Unexpected method: ${method}`);
  };

  const skills = await createLiveAgentManagementClient().listSkillOptions({ includeTeamMarketplace: true });
  assert.deepEqual(skills.map((skill) => skill.id), ['local-review']);
});

test('returns base skills before delayed Team Skills Hub enrichment and reports cache state', async () => {
  let resolveMarketplace;
  const marketplaceUpdates = [];
  webClient.request = async (method) => {
    if (method === 'skills.list') {
      return {
        skills: [{ name: 'local-review', display_name: 'Local Review', source: 'project', installed: true }],
      };
    }
    if (method === 'skills.swarmskillshub.recommend') {
      return new Promise((resolve) => {
        resolveMarketplace = resolve;
      });
    }
    throw new Error(`Unexpected method: ${method}`);
  };

  const client = createLiveAgentManagementClient();
  const skillsPromise = client.listSkillOptions({
    includeTeamMarketplace: true,
    onTeamMarketplaceLoaded: (skills, cache) => marketplaceUpdates.push({ skills, cache }),
  });
  let timeoutId;
  const timeout = new Promise((_, reject) => {
    timeoutId = setTimeout(() => reject(new Error('Base skill list was blocked by the marketplace request')), 100);
  });
  const skills = await Promise.race([skillsPromise, timeout]);
  clearTimeout(timeoutId);

  assert.deepEqual(skills.map((skill) => skill.id), ['local-review']);
  assert.equal(marketplaceUpdates.length, 0);

  await new Promise((resolve) => setTimeout(resolve, 0));
  resolveMarketplace({
    success: true,
    skills: [
      {
        asset_id: 'market-team-asset',
        name: 'market-team',
        display_name: 'Market Team',
        plugin_type: 'swarmskill',
      },
    ],
    cache: { state: 'stale', refreshing: true, complete: false },
  });
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(marketplaceUpdates.length, 1);
  assert.deepEqual(
    marketplaceUpdates[0].skills.map((skill) => skill.id),
    ['local-review', 'market-team'],
  );
  assert.deepEqual(marketplaceUpdates[0].cache, { state: 'stale', refreshing: true, complete: false });
});
