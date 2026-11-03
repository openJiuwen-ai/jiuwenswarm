import assert from 'node:assert/strict';
import test from 'node:test';
import {
  isDesignerGraphEditingLocked,
  isDesignerLeaderBusy,
} from '../node_modules/.cache/designer-graph-lock/designerGraphLock.js';

const promptGraph = {
  graph_id: 'g1',
  source: 'prompt',
  nodes: [{ id: 'n1' }, { id: 'n2' }],
};

test('a built graph stays locked until every node finishes', () => {
  assert.equal(
    isDesignerGraphEditingLocked({
      bootstrapInProgress: false,
      graph: promptGraph,
      nodeStates: { n1: { status: 'completed' }, n2: { status: 'pending' } },
      runStatus: 'paused',
    }),
    true,
  );
  assert.equal(
    isDesignerGraphEditingLocked({
      bootstrapInProgress: false,
      graph: promptGraph,
      nodeStates: { n1: { status: 'completed' }, n2: { status: 'completed' } },
      runStatus: 'completed',
    }),
    false,
  );
});

test('building and an active run lock the graph', () => {
  assert.equal(
    isDesignerGraphEditingLocked({
      bootstrapInProgress: true,
      graph: null,
      nodeStates: {},
      runStatus: null,
    }),
    true,
  );
  assert.equal(
    isDesignerGraphEditingLocked({
      bootstrapInProgress: false,
      graph: { graph_id: 'g2', source: 'manual', nodes: [{ id: 'n1' }] },
      nodeStates: { n1: { status: 'pending' } },
      runStatus: 'running',
    }),
    true,
  );
});

test('the graph stays locked while the leader answers the first prompt', () => {
  const emptyPromptGraph = { graph_id: 'g3', source: 'prompt', nodes: [] };
  assert.equal(
    isDesignerGraphEditingLocked({
      bootstrapInProgress: false,
      leaderBusy: true,
      graph: emptyPromptGraph,
      nodeStates: {},
      runStatus: null,
    }),
    true,
  );
  assert.equal(
    isDesignerGraphEditingLocked({
      bootstrapInProgress: false,
      leaderBusy: true,
      graph: { graph_id: 'g2', source: 'manual', nodes: [{ id: 'n1' }] },
      nodeStates: {},
      runStatus: null,
    }),
    true,
  );
  assert.equal(
    isDesignerGraphEditingLocked({
      bootstrapInProgress: false,
      leaderBusy: false,
      graph: emptyPromptGraph,
      nodeStates: {},
      runStatus: null,
    }),
    false,
  );
});

test('the leader stays busy while it waits for an answer or is paused', () => {
  const idle = { isProcessing: false, isPaused: false, pendingQuestions: [] };
  assert.equal(isDesignerLeaderBusy(undefined), false);
  assert.equal(isDesignerLeaderBusy(idle), false);
  assert.equal(isDesignerLeaderBusy({ ...idle, isProcessing: true }), true);
  assert.equal(isDesignerLeaderBusy({ ...idle, pendingQuestions: [{ request_id: 'q1' }] }), true);
  assert.equal(isDesignerLeaderBusy({ ...idle, isPaused: true }), true);
});

test('a failed built graph unlocks so it can be edited', () => {
  assert.equal(
    isDesignerGraphEditingLocked({
      bootstrapInProgress: false,
      graph: promptGraph,
      nodeStates: { n1: { status: 'completed' }, n2: { status: 'failed' } },
      runStatus: 'failed',
    }),
    false,
  );
});

test('a manual canvas stays editable before the first run', () => {
  assert.equal(
    isDesignerGraphEditingLocked({
      bootstrapInProgress: false,
      graph: { graph_id: 'g2', source: 'manual', nodes: [{ id: 'n1' }] },
      nodeStates: {},
      runStatus: null,
    }),
    false,
  );
});

test('nodes added after a completed run keep the graph editable', () => {
  const grown = { ...promptGraph, nodes: [...promptGraph.nodes, { id: 'n3' }] };
  const finished = { n1: { status: 'completed' }, n2: { status: 'completed' } };
  assert.equal(
    isDesignerGraphEditingLocked({
      bootstrapInProgress: false,
      graph: grown,
      nodeStates: finished,
      runStatus: 'completed',
    }),
    false,
  );
  assert.equal(
    isDesignerGraphEditingLocked({
      bootstrapInProgress: false,
      graph: grown,
      nodeStates: finished,
      runStatus: null,
    }),
    true,
  );
  assert.equal(
    isDesignerGraphEditingLocked({
      bootstrapInProgress: false,
      graph: grown,
      nodeStates: { ...finished, n3: { status: 'pending' } },
      runStatus: 'completed',
    }),
    false,
  );
});

test('a single-node rerun after a failed run keeps the graph editable', () => {
  const nodeStates = { n1: { status: 'completed' }, n2: { status: 'failed' } };
  assert.equal(
    isDesignerGraphEditingLocked({
      bootstrapInProgress: false,
      graph: promptGraph,
      nodeStates,
      runStatus: 'running',
    }),
    true,
  );
  assert.equal(
    isDesignerGraphEditingLocked({
      bootstrapInProgress: false,
      graph: promptGraph,
      nodeStates,
      runStatus: 'completed',
    }),
    false,
  );
  assert.equal(
    isDesignerGraphEditingLocked({
      bootstrapInProgress: false,
      graph: promptGraph,
      nodeStates: { n1: { status: 'completed' }, n2: { status: 'cancelled' } },
      runStatus: 'completed',
    }),
    false,
  );
});
