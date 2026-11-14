import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

await build({
  entryPoints: ['src/features/rsi/rsiTreeLayout.ts'],
  outfile: 'node_modules/.cache/rsi-tree-layout/rsiTreeLayout.mjs',
  bundle: true,
  platform: 'node',
  format: 'esm',
});
const { layoutTree } = await import('../node_modules/.cache/rsi-tree-layout/rsiTreeLayout.mjs');

test('Harness tree spacing ignores extra scores on both roots and descendants', () => {
  const nodes = [
    { node_id: 'ROOT', parent_id: null, iteration: 0, type: 'ROOT', adopted: true },
    { node_id: 'H-01', parent_id: 'ROOT', iteration: 1, type: 'REJECTED', adopted: false },
  ].map((node) => ({
    ...node,
    score: 0.7,
    changes: [],
    extra: { best_reference_score: 0.9, latest_evaluated_score: 0.7 },
  }));
  const baseline = nodes.map((node) => ({ ...node, extra: {} }));
  const geometry = (tree) => tree.nodes.map(({ node, x, y, width, height }) => ({
    id: node.node_id, x, y, width, height,
  }));
  const expected = geometry(layoutTree([], baseline, new Set(), false, new Set(), 'HARNESS'));

  for (const expanded of [new Set(), new Set(['ROOT', 'H-01'])]) {
    assert.deepEqual(geometry(layoutTree([], nodes, new Set(), false, expanded, 'HARNESS')), expected);
  }
  const artifact = layoutTree([], nodes, new Set(), false, new Set(), 'ARTIFACT');
  assert(artifact.nodes.every((node, index) => node.height > expected[index].height));
});
