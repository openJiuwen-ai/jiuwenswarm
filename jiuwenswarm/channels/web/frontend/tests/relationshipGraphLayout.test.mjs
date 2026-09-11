import assert from 'node:assert/strict';
import test from 'node:test';

import {
  COMPONENT_CENTER_ATTRACTION_STRENGTH,
  computeRelationshipGraphComponents,
  seedRelationshipGraphPositions,
  stepRelationshipGraphLayout,
} from '../node_modules/.cache/relationship-graph-layout/components/RelationshipGraph/relationshipGraphLayout.js';

function nodes(ids) {
  return ids.map(id => ({ id, x: 0, y: 0, vx: 0, vy: 0 }));
}

test('detects directed relations as undirected components and keeps isolated experts', () => {
  const graphNodes = nodes(['a', 'b', 'c', 'd', 'isolated']);
  const components = computeRelationshipGraphComponents(graphNodes, [
    { source: 'b', target: 'a', type: 'can_feed' },
    { source: 'c', target: 'd', type: 'complements' },
  ]);
  const ids = components.map(component => component.nodes.map(node => node.id).sort()).sort((left, right) => left[0].localeCompare(right[0]));
  assert.deepEqual(ids, [['a', 'b'], ['c', 'd'], ['isolated']]);
});

test('force layout stays inside its elliptical boundary for a larger expert graph', () => {
  const graphNodes = nodes(Array.from({ length: 80 }, (_, index) => `expert-${index}`));
  const edges = graphNodes.slice(1).map((node, index) => ({ source: graphNodes[index].id, target: node.id, type: 'can_feed' }));
  seedRelationshipGraphPositions(graphNodes, 900, 620);
  const components = computeRelationshipGraphComponents(graphNodes, edges);
  for (let step = 0; step < 250; step += 1) {
    stepRelationshipGraphLayout(graphNodes, edges, 900, 620, components, COMPONENT_CENTER_ATTRACTION_STRENGTH);
  }
  assert.ok(graphNodes.every(node => Math.hypot(node.x / 900, node.y / 620) <= 1.000001));
});
