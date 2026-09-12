import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  isExpertTeamCandidateInstalled,
  filterExpertGraph,
  normalizeExpertGraph,
  normalizeExpertTeamCandidates,
  normalizeExpertTeamMaterialization,
} from '../node_modules/.cache/expert-graph/features/expertGraph.js';
import { buildBetaExpertCallChain, normalizeBetaExpertCatalog } from '../node_modules/.cache/expert-graph/features/betaExpertCatalog.js';

const expertGraphCss = readFileSync(new URL('../src/components/BetaExpertManagementPanel/betaExpertManagement.css', import.meta.url), 'utf8');

function cssRule(selector, source = expertGraphCss) {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return source.match(new RegExp(`${escapedSelector}\\s*\\{([^}]+)\\}`))?.[1] || '';
}

test('expert list cannot expand the bounded graph canvas row', () => {
  const desktopLayout = cssRule('.beta-expert-graph__visual-layout');
  const controls = cssRule('.beta-expert-graph__graph-controls');
  const nodeList = cssRule('.beta-expert-graph__node-list');
  const responsiveLayout = expertGraphCss.match(/@media \(max-width: 1050px\)\s*\{([\s\S]*?)@media \(max-width: 900px\)/)?.[1] || '';

  assert.match(desktopLayout, /height:\s*clamp\(540px,\s*calc\(100vh - 320px\),\s*820px\)/);
  assert.match(desktopLayout, /grid-template-rows:\s*minmax\(0,\s*1fr\)/);
  assert.match(desktopLayout, /overflow:\s*hidden/);
  assert.match(controls, /min-height:\s*0/);
  assert.match(nodeList, /min-height:\s*0/);
  assert.match(nodeList, /flex:\s*1/);
  assert.match(nodeList, /overflow-y:\s*auto/);
  assert.match(cssRule('.beta-expert-graph__visual-layout', responsiveLayout), /grid-template-rows:\s*540px auto/);
});

test('normalizeExpertGraph preserves original skill names and canonical contract', () => {
  const graph = normalizeExpertGraph({
    success: true,
    graph: {
      schemaVersion: '1.0',
      graphId: 'graph-1',
      createdAt: '2026-09-10T10:00:00Z',
      sourceHash: 'sha256:abc',
      nodes: [
        {
          id: 'data',
          name: '数据洞察看板师',
          skills: [{ name: 'excel-analysis' }, { name: 'canvas-design' }],
          inputs: [{ id: 'sheet', mediaType: 'text/csv' }],
          outputs: [{ id: 'insight', mediaType: 'application/json' }],
        },
        { id: 'campaign', name: '营销策划专家', reusable: true },
      ],
      edges: [{ id: 'feed', source: 'data', target: 'campaign', type: 'can_feed', confidence: 0.92 }],
    },
  });

  assert.equal(graph?.graphId, 'graph-1');
  assert.deepEqual(graph?.nodes[0].skills, ['excel-analysis', 'canvas-design']);
  assert.equal(graph?.nodes[0].inputs[0].mediaType, 'text/csv');
  assert.equal(graph?.edges[0].confidence, 0.92);
});

test('normalizeExpertGraph accepts snake case aliases and removes dangling edges', () => {
  const graph = normalizeExpertGraph({
    expert_graph: {
      graph_id: 'graph-2',
      schema_version: '1',
      nodes: [{ id: 'a', skills: ['skill-a'], reusable: false }],
      edges: [{ source_id: 'a', target_id: 'missing', type: 'can_feed' }],
    },
  });

  assert.equal(graph?.graphId, 'graph-2');
  assert.equal(graph?.nodes[0].reusable, false);
  assert.deepEqual(graph?.edges, []);
});

test('filterExpertGraph applies confidence and keeps matching experts with their direct neighbors', () => {
  const graph = normalizeExpertGraph({
    graph: {
      nodes: [
        { id: 'a', name: '数据洞察', skills: ['spreadsheet'] },
        { id: 'b', name: '营销策划' },
        { id: 'c', name: '旅行手帐' },
      ],
      edges: [
        { source: 'a', target: 'b', type: 'can_feed', confidence: 0.92 },
        { source: 'b', target: 'c', type: 'can_feed', confidence: 0.4 },
      ],
    },
  });
  assert.ok(graph);
  const visible = filterExpertGraph(graph, 'spreadsheet', 0.5);
  assert.deepEqual(
    visible.nodes.map(node => node.id),
    ['a', 'b'],
  );
  assert.deepEqual(
    visible.edges.map(edge => `${edge.source}:${edge.target}`),
    ['a:b'],
  );
});

test('normalizeExpertTeamCandidates handles workflow objects and score bounds', () => {
  const candidates = normalizeExpertTeamCandidates({
    candidates: [
      {
        id: 'growth-team',
        name: '数据增长内容团',
        memberIds: ['data', 'campaign'],
        workflow: [{ description: '先分析数据' }, { expertName: '营销策划专家', dependsOn: ['data'] }, { from: 'data', to: 'campaign' }],
        score: 120,
        quickPrompts: ['分析销售数据并做推广方案'],
      },
    ],
  });

  assert.equal(candidates.length, 1);
  assert.equal(candidates[0].score, 100);
  assert.deepEqual(candidates[0].workflow, ['先分析数据', '营销策划专家 接收上一步交付并继续处理', 'data → campaign']);
});

test('normalizeExpertTeamCandidates prefers manager routing policy, member profiles, and route examples', () => {
  const [candidate] = normalizeExpertTeamCandidates({
    candidates: [
      {
        id: 'content-team',
        memberIds: ['researcher', 'writer', 'designer'],
        leaderId: 'researcher',
        routingPolicy: {
          mode: 'leader_selected',
          minSelected: 1,
          maxSelected: 2,
          selection: 'minimal_sufficient',
          allowSingleMember: true,
          allowParallel: true,
          allowSerial: false,
        },
        memberProfiles: [
          { expertId: 'researcher', displayName: '研究员', role: 'lead', capabilities: ['调研'] },
          { expertId: 'writer', displayName: '写作专家', capabilities: ['写作'] },
        ],
        routeExamples: [
          {
            id: 'research-write',
            type: 'serial',
            title: '研究后写作',
            intent: '产出有依据的文章',
            selectedMemberIds: ['researcher', 'writer'],
            steps: ['先调研', '再写作'],
            relationEdgeIds: ['researcher-writer'],
          },
        ],
      },
    ],
  });

  assert.deepEqual(candidate.routingPolicy, {
    mode: 'leader_selected',
    minSelected: 1,
    maxSelected: 2,
    selection: 'minimal_sufficient',
    allowSingleMember: true,
    allowParallel: true,
    allowSerial: false,
  });
  assert.deepEqual(
    candidate.memberProfiles.map(profile => profile.name),
    ['研究员', '写作专家'],
  );
  assert.deepEqual(candidate.routeExamples[0], {
    id: 'research-write',
    type: 'serial',
    title: '研究后写作',
    intent: '产出有依据的文章',
    query: '',
    memberIds: ['researcher', 'writer'],
    steps: ['先调研', '再写作'],
    relationEdgeIds: ['researcher-writer'],
    summary: '',
  });
});

test('normalizers return empty values for unavailable RPC payloads', () => {
  assert.equal(normalizeExpertGraph({ success: false }), null);
  assert.deepEqual(normalizeExpertTeamCandidates({ success: false }), []);
});

test('normalizeExpertTeamMaterialization accepts the materialize RPC contract', () => {
  const result = normalizeExpertTeamMaterialization({
    expert: { id: 'growth-team', name: '数据增长内容团' },
    source_refreshed: true,
    package_name: 'growth-team',
    warnings: [],
  });

  assert.deepEqual(result, {
    expertId: 'growth-team',
    expertName: '数据增长内容团',
    sourceRefreshed: true,
    packageName: 'growth-team',
    warnings: [],
    graph: null,
    candidates: [],
    hasFreshWorkspace: false,
  });
});

test('materialization refreshes the workspace and preserves installed candidate state', () => {
  const result = normalizeExpertTeamMaterialization({
    expert: { id: 'growth-team', name: '数据增长内容团' },
    graph: {
      graphId: 'graph-2',
      nodes: [{ id: 'data', name: '数据洞察看板师' }],
      edges: [],
    },
    candidates: [
      {
        id: 'growth-team',
        name: '数据增长内容团',
        memberIds: ['data'],
        status: 'installed',
      },
      {
        id: 'next-team',
        name: '下一个专家团',
        memberIds: ['data'],
        status: 'candidate',
      },
    ],
  });

  assert.equal(result.hasFreshWorkspace, true);
  assert.equal(result.graph?.graphId, 'graph-2');
  assert.equal(result.candidates.length, 2);
  assert.equal(isExpertTeamCandidateInstalled(result.candidates[0]), true);
  assert.equal(isExpertTeamCandidateInstalled(result.candidates[1]), false);
});

test('team detail uses workflow and member names instead of an empty Skill chain', () => {
  const [team] = normalizeBetaExpertCatalog({
    experts: [
      {
        id: 'growth-team',
        name: '数据增长内容团',
        type: 'team',
        available: true,
        skills: [],
        members: [
          { id: 'leader', name: '增长团主理人', role: 'lead' },
          { id: 'data', name: '数据洞察看板师', role: 'member' },
          { id: 'campaign', name: '营销策划专家', role: 'member' },
        ],
        metadata: {
          workflow: ['分析经营数据并输出洞察', '根据洞察生成营销方案'],
        },
      },
    ],
  });

  assert.equal(team.type, 'team');
  assert.deepEqual(buildBetaExpertCallChain(team), [
    { name: '数据洞察看板师', description: '分析经营数据并输出洞察' },
    { name: '营销策划专家', description: '根据洞察生成营销方案' },
  ]);
});

test('single expert call chain preserves original Skill names and empty chains remain empty', () => {
  const [expert, emptyTeam] = normalizeBetaExpertCatalog({
    experts: [
      {
        id: 'data-expert',
        type: 'agent',
        available: true,
        skills: [{ name: 'excel-analysis', description: '分析表格' }],
      },
      { id: 'empty-team', type: 'team', available: true, skills: [], members: [] },
    ],
  });

  assert.deepEqual(buildBetaExpertCallChain(expert), [{ name: 'excel-analysis', description: '分析表格' }]);
  assert.deepEqual(buildBetaExpertCallChain(emptyTeam), []);
});
