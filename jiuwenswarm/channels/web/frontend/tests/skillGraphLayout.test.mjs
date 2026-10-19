import assert from 'node:assert/strict';
import test from 'node:test';

import {
  computeConnectedComponents,
  seedPositions,
  stepSkillGraphLayout,
} from '../node_modules/.cache/skill-graph-layout/components/SkillGraphPanel/skillGraphLayout.js';

function createNode(id) {
  return { id, x: 0, y: 0, vx: 0, vy: 0 };
}

function createNodes(ids) {
  return ids.map((id) => createNode(id));
}

function createEdges(entries) {
  return entries.map(([source, target, type = 'related']) => ({ source, target, type }));
}

function cloneGraph(nodes, edges) {
  return {
    nodes: nodes.map((node) => ({ ...node })),
    edges: edges.map((edge) => ({ ...edge })),
  };
}

function sortedComponentIds(components) {
  return components
    .map((component) => [...component.nodes].map((node) => node.id).sort())
    .sort((left, right) => left[0].localeCompare(right[0]));
}

function runSimulation(nodes, edges, components, ticks, componentStrength = 0) {
  for (let step = 0; step < ticks; step += 1) {
    stepSkillGraphLayout(nodes, edges, 900, 620, components, componentStrength);
  }
}

function componentCentroid(component) {
  const count = component.nodes.length;
  let x = 0;
  let y = 0;
  for (const node of component.nodes) {
    x += node.x;
    y += node.y;
  }
  return { x: x / count, y: y / count };
}

function maxCentroidDistance(components) {
  let maxDistance = 0;
  for (let i = 0; i < components.length; i += 1) {
    const left = componentCentroid(components[i]);
    for (let j = i + 1; j < components.length; j += 1) {
      const right = componentCentroid(components[j]);
      const dx = right.x - left.x;
      const dy = right.y - left.y;
      const distance = Math.hypot(dx, dy);
      if (distance > maxDistance) maxDistance = distance;
    }
  }
  return maxDistance;
}

function minInterComponentNodeDistance(components) {
  let minDistance = Number.POSITIVE_INFINITY;
  for (let i = 0; i < components.length; i += 1) {
    const leftNodes = components[i].nodes;
    for (let j = i + 1; j < components.length; j += 1) {
      for (const left of leftNodes) {
        for (const right of components[j].nodes) {
          const distance = Math.hypot(right.x - left.x, right.y - left.y);
          if (distance < minDistance) minDistance = distance;
        }
      }
    }
  }
  return minDistance;
}

test('detectConnectedComponents treats directed edges as undirected and keeps isolated nodes', () => {
  const nodes = createNodes(['a', 'b', 'c', 'd', 'e']);
  const edges = createEdges([
    ['b', 'a'],
    ['c', 'd'],
  ]);

  const components = computeConnectedComponents(nodes, edges);
  assert.deepEqual(sortedComponentIds(components), [
    ['a', 'b'],
    ['c', 'd'],
    ['e'],
  ]);
});

test('single connected graph keeps behavior unchanged with component analysis', () => {
  const baseline = createNodes(['a', 'b', 'c']);
  const edges = createEdges([
    ['a', 'b', 'can_feed'],
    ['b', 'c', 'related'],
  ]);
  seedPositions(baseline, 900, 620);
  const baselineClone = cloneGraph(baseline, edges);

  const componentsBaseline = computeConnectedComponents(baseline, baselineClone.edges);
  const componentsClone = computeConnectedComponents(baselineClone.nodes, baselineClone.edges);
  assert.equal(componentsBaseline.length, 1);
  assert.equal(componentsClone.length, 1);

  runSimulation(baseline, baselineClone.edges, componentsBaseline, 1, 0);
  runSimulation(baselineClone.nodes, baselineClone.edges, componentsClone, 1, 0.006);

  assert.deepEqual(
    baseline.map((node) => ({ x: node.x, y: node.y, vx: node.vx, vy: node.vy })),
    baselineClone.nodes.map((node) => ({ x: node.x, y: node.y, vx: node.vx, vy: node.vy })),
  );
});

test('adds component centering force when multiple components exist', () => {
  const fixture = {
    nodes: createNodes(['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']),
    edges: createEdges([
      ['a', 'b', 'can_feed'],
      ['c', 'd', 'related'],
      ['e', 'f', 'can_feed'],
      ['f', 'g', 'related'],
      ['g', 'h', 'can_feed'],
    ]),
  };

  const baseline = cloneGraph(fixture.nodes, fixture.edges);
  const improved = cloneGraph(fixture.nodes, fixture.edges);

  seedPositions(baseline.nodes, 900, 620);
  seedPositions(improved.nodes, 900, 620);

  const baselineComponents = computeConnectedComponents(baseline.nodes, baseline.edges);
  const improvedComponents = computeConnectedComponents(improved.nodes, improved.edges);
  assert.equal(baselineComponents.length, 3);
  assert.equal(improvedComponents.length, 3);

  runSimulation(baseline.nodes, baseline.edges, baselineComponents, 1500, 0);
  runSimulation(improved.nodes, improved.edges, improvedComponents, 1500, 0.006);

  const baselineMaxDistance = maxCentroidDistance(baselineComponents);
  const improvedMaxDistance = maxCentroidDistance(improvedComponents);
  const improvedMinNodeDistance = minInterComponentNodeDistance(improvedComponents);

  assert.ok(improvedMaxDistance <= baselineMaxDistance * 0.6);
  assert.ok(improvedMinNodeDistance > 2);
});
