import assert from 'node:assert/strict';
import test from 'node:test';
import { useRsiStore } from '../node_modules/.cache/rsi-stage/rsiStore.mjs';

test('full H0 updates the baseline without consuming an optimization iteration', () => {
  const tid = 'harness-baseline';
  const root = { node_id: 'ROOT', parent_id: null, iteration: 0, type: 'ROOT', score: null };
  useRsiStore.setState({
    list: [{ task_id: tid, base: null }],
    detail: { [tid]: { tree: { nodes: [root], depth: 0, iteration: 0 }, pendingTreeNodes: [] } },
  });
  for (const baseline of [0, 0.6]) {
    useRsiStore.getState().applyProgress({ task_id: tid, iteration: 0, total_iterations: 5, score: baseline, baseline });
    useRsiStore.getState().applyTreeDelta({ task_id: tid, nodes: [{ ...root, score: baseline }] });
    const state = useRsiStore.getState();
    assert.equal(state.detail[tid].liveProgress.baseline, baseline);
    assert.equal(state.list[0].base, baseline);
    assert.equal(state.detail[tid].liveProgress.iteration, 0);
    assert.equal(state.detail[tid].liveProgress.total, 5);
    assert.equal(state.detail[tid].tree.nodes.length, 1);
    assert.equal(state.detail[tid].tree.nodes[0].score, baseline);
    assert.equal(state.detail[tid].tree.iteration, 0);
  }
  useRsiStore.getState().applyProgress({ task_id: tid, iteration: 1, total_iterations: 5, score: 0.8, baseline: 0.6 });
  assert.equal(useRsiStore.getState().detail[tid].liveProgress.baseline, 0.6);
});

test('program progress counts completed candidates despite out-of-order completion', () => {
  const extra = { program: {} };
  const root = { node_id: 'ROOT', parent_id: null, iteration: 0, type: 'ROOT', extra };
  useRsiStore.setState({ detail: { p: { tree: { nodes: [root], depth: 0, iteration: 0 }, pendingTreeNodes: [] } } });
  const push = (node) => useRsiStore.getState().applyTreeDelta({ task_id: 'p', nodes: [{ ...node, extra }] });
  push({ node_id: 'second', parent_id: 'ROOT', iteration: 2, type: 'ADOPTED' });
  assert.equal(useRsiStore.getState().detail.p.tree.iteration, 1);
  push({ node_id: 'first', parent_id: 'ROOT', iteration: 1, type: 'PROVISIONAL' });
  assert.equal(useRsiStore.getState().detail.p.tree.iteration, 1);
  push({ node_id: 'first', parent_id: 'ROOT', iteration: 1, type: 'REJECTED' });
  assert.equal(useRsiStore.getState().detail.p.tree.iteration, 2);
});

test('stages update one node; completed rounds and branch depth remain distinct', () => {
  const root = { node_id: 'ROOT', parent_id: null, iteration: 0, type: 'ROOT' };
  useRsiStore.setState({ detail: { t: { tree: { nodes: [root], depth: 0, iteration: 0 }, pendingTreeNodes: [] } } });
  const push = (node) => useRsiStore.getState().applyTreeDelta({ task_id: 't', nodes: [node] });
  const tree = () => useRsiStore.getState().detail.t.tree;
  const first = { node_id: 'n1', parent_id: 'ROOT', iteration: 1, type: 'PROVISIONAL' };
  push({ ...first, description: '正在调研文献' });
  push({ ...first, description: '正在执行实验' });
  assert.equal(tree().nodes.length, 2);
  assert.equal(tree().nodes[1].description, '正在执行实验');
  assert.equal(tree().iteration, 0);
  push({ ...first, type: 'REJECTED' });
  assert.equal(tree().iteration, 1);
  push({ node_id: 'n2', parent_id: 'ROOT', iteration: 2, type: 'PROVISIONAL' });
  assert.equal(tree().iteration, 1);
  assert.equal(tree().depth, 1);
  push({ node_id: 'n2', parent_id: 'ROOT', iteration: 2, type: 'ADOPTED' });
  assert.equal(tree().iteration, 2);
  assert.equal(tree().depth, 1);
});
