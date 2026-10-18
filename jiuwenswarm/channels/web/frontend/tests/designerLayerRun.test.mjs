import assert from 'node:assert/strict';
import test from 'node:test';

import {
  computeNextLayer,
  computeRootLayer,
  derivePrimaryAction,
  initialNodeStates,
} from '../node_modules/.cache/designer-layer-run/designerLayerRun.mjs';

const graph = {
  schema_version: 'designer-execution-graph.v1',
  graph_id: 'g1',
  project_id: 'p1',
  title: 't',
  nodes: [
    { id: 'a', type: 'text', label: 'A' },
    { id: 'b', type: 'image', label: 'B' },
    { id: 'c', type: 'image', label: 'C' },
    { id: 'd', type: 'video', label: 'D' },
  ],
  edges: [
    { id: 'e1', source: 'a', target: 'b' },
    { id: 'e2', source: 'a', target: 'c' },
    { id: 'e3', source: 'b', target: 'd' },
    { id: 'e4', source: 'c', target: 'd' },
  ],
};

test('root layer is nodes without incoming edges', () => {
  assert.deepEqual(computeRootLayer(graph), ['a']);
});

test('next layer is ready direct downstream of previous layer', () => {
  const states = initialNodeStates(graph);
  states.a = { status: 'completed' };
  assert.deepEqual(computeNextLayer(graph, ['a'], states), ['b', 'c']);

  states.b = { status: 'completed' };
  states.c = { status: 'completed' };
  assert.deepEqual(computeNextLayer(graph, ['b', 'c'], states), ['d']);
});

test('derivePrimaryAction switches execute → continue → done', () => {
  const pending = initialNodeStates(graph);
  assert.equal(
    derivePrimaryAction({
      graph,
      nodeStates: pending,
      currentLayerNodeIds: [],
      isRunning: false,
    }),
    'execute',
  );

  const afterRoot = {
    ...pending,
    a: { status: 'completed' },
  };
  assert.equal(
    derivePrimaryAction({
      graph,
      nodeStates: afterRoot,
      currentLayerNodeIds: ['a'],
      isRunning: false,
    }),
    'continue',
  );

  const failedLayer = {
    ...afterRoot,
    b: { status: 'failed' },
    c: { status: 'failed' },
  };
  assert.equal(
    derivePrimaryAction({
      graph,
      nodeStates: failedLayer,
      currentLayerNodeIds: ['b', 'c'],
      isRunning: false,
    }),
    'retry_failed',
  );
});
