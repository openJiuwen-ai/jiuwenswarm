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
  outExtension: { '.js': '.mjs' },
  banner: { js: 'const window = globalThis.window ?? (globalThis.window = {});' },
  define: { 'import.meta.env': '{}' },
});
globalThis.window = Object.assign(globalThis.window ?? {}, {
  location: globalThis.window?.location ?? { search: '' },
});
const { createLiveAgentManagementClient, retryWhilePodStarting } =
  await import('../node_modules/.cache/agent-management-client/features/agentManagement/client.mjs');
const { webClient } = await import('../node_modules/.cache/agent-management-client/services/webClient.mjs');
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
      // 后端 mcp.list summary 不下发 installed 字段（registry.py _build_summary），
      // "哪些算我的"由 filter 拆分表达：local = 已连接的预置 + 全部自定义。
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
              connected: false,
            },
            {
              id: 'preset-connector',
              name: 'preset-connector',
              package_name: 'preset-connector',
              display_name: 'Preset Connector',
              description: 'A disconnected preset connector',
              category: 'search',
              integration_type: 'stdio-mcp',
              connection_state: 'disconnected',
              has_bundled_skills: false,
              source: 'built_in',
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
  assert.deepEqual(mcps.map((mcp) => mcp.id), ['market-connector', 'preset-connector', 'custom-connector']);
  // installed 只表达安装态：预置包随应用分发，connectorApi 对非 hub 归一为 installed=true；
  // 抽屉"我的/广场"tab 归属不依赖 installed，由 AgentEditor 的 isMine 用 connectionState 判断
  assert.equal(mcps[0].installed, true);
  assert.equal(mcps[0].hubAssetId, 'hub-connector-1');
  assert.equal(mcps[0].connectionState, 'connected');
  assert.equal(mcps[1].installed, true);
  assert.equal(mcps[1].connectionState, 'disconnected');
  assert.equal(mcps[2].installed, true);
  assert.equal(mcps[2].connectionState, 'disconnected');
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

test('personal catalog keeps templates', async () => {
  const previous = globalThis.window.__JIUWENSWARM_EDITION__;
  globalThis.window.__JIUWENSWARM_EDITION__ = 'personal';
  try {
    webClient.request = async (method) => {
      assert.equal(method, 'agent_templates.list');
      return {
        templates: [{ id: 'builtin', source: 'builtin', installed: false, tags: [] }],
      };
    };
    const items = await createLiveAgentManagementClient().listCatalog({ filter: 'builtin+hub' });
    assert.equal(items.length, 1);
    assert.equal(items[0].id, 'builtin');
  } finally {
    if (previous === undefined) delete globalThis.window.__JIUWENSWARM_EDITION__;
    else globalThis.window.__JIUWENSWARM_EDITION__ = previous;
  }
});

test('enterprise catalog reads templates the same way as personal', async () => {
  const previous = globalThis.window.__JIUWENSWARM_EDITION__;
  globalThis.window.__JIUWENSWARM_EDITION__ = 'enterprise';
  const calls = [];
  try {
    webClient.request = async (method) => {
      calls.push(method);
      if (method === 'agent_templates.list') {
        return {
          templates: [{ id: 'contract', source: 'hub', installed: true, tags: [] }],
          items: [{ id: 'ignored', display_name: 'Nope' }],
        };
      }
      if (method === 'agent_templates.show') {
        return {
          template: { id: 'contract', source: 'hub', installed: true, skills: [], tags: [] },
        };
      }
      if (method === 'agent_templates.file.list') {
        return {
          tree: [{ name: 'manifest.json', path: 'manifest.json', type: 'file', size: 4 }],
        };
      }
      throw new Error(method);
    };
    const client = createLiveAgentManagementClient();
    const items = await client.listCatalog({ filter: 'builtin+hub' });
    assert.equal(items.length, 1);
    assert.equal(items[0].id, 'contract');
    assert.equal(items[0].installed, true);
    const detail = await client.getDefinition('contract');
    assert.equal(detail.id, 'contract');
    const files = await client.getDefinitionFiles('contract');
    assert.equal(files.length, 1);
    assert.equal(files[0].relativePath, 'manifest.json');
    assert.deepEqual(calls, ['agent_templates.list', 'agent_templates.show', 'agent_templates.file.list']);
  } finally {
    if (previous === undefined) delete globalThis.window.__JIUWENSWARM_EDITION__;
    else globalThis.window.__JIUWENSWARM_EDITION__ = previous;
  }
});

test('enterprise list retries pod cold start and stops other errors immediately', async () => {
  const previous = globalThis.window.__JIUWENSWARM_EDITION__;
  globalThis.window.__JIUWENSWARM_EDITION__ = 'enterprise';
  try {
    let calls = 0;
    webClient.request = async () => {
      calls += 1;
      const error = new Error('catalog down');
      error.code = 'OTHER';
      throw error;
    };
    await assert.rejects(() => createLiveAgentManagementClient().listCatalog({ filter: 'builtin+hub' }));
    assert.equal(calls, 1);

    let now = 0;
    let attempts = 0;
    const waits = [];
    const ready = await retryWhilePodStarting(async () => {
      attempts += 1;
      if (attempts < 3) {
        const error = new Error('Pod 冷启动中');
        error.code = 'NO_POD_AVAILABLE';
        throw error;
      }
      return 'ready';
    }, {
      onWaiting: () => waits.push(now),
      now: () => now,
      sleep: async (ms) => {
        now += ms;
      },
    });
    assert.equal(ready, 'ready');
    assert.equal(attempts, 3);
    assert.deepEqual(waits, [0, 2000]);

    now = 0;
    await assert.rejects(
      () => retryWhilePodStarting(async () => {
        const error = new Error('NO_POD_AVAILABLE');
        throw error;
      }, {
        now: () => now,
        sleep: async (ms) => {
          now += ms;
        },
      }),
      (error) => error.code === 'pod_start_timeout',
    );
    assert.equal(now, 60000);
  } finally {
    if (previous === undefined) delete globalThis.window.__JIUWENSWARM_EDITION__;
    else globalThis.window.__JIUWENSWARM_EDITION__ = previous;
  }
});
