import assert from 'node:assert/strict';
import test from 'node:test';

import {
  workflowAgentAvatarName,
  workflowAgentDisplayName,
  workflowAgentIdentity,
  workflowPhaseWorkers,
  isWorkflowRunEnded,
  orderRunsForTreeView,
  partitionWorkflowRunsByLiveness,
} from '../node_modules/.cache/workflow-agent-identity/workflowTypes.js';

test('third-party node resolves to its CLI brand identity', () => {
  assert.deepEqual(workflowAgentIdentity({ agent_type: 'codex', kind: 'agent' }), { cliAgent: 'codex' });
  assert.deepEqual(workflowAgentIdentity({ agent_type: 'claude' }), { cliAgent: 'claude' });
});

test('human node resolves to the human_agent role', () => {
  assert.deepEqual(workflowAgentIdentity({ kind: 'human' }), { role: 'human_agent' });
});

test('native node gets an empty identity (hashed illustration, no roster fallback)', () => {
  assert.deepEqual(workflowAgentIdentity({ kind: 'agent' }), {});
  assert.deepEqual(workflowAgentIdentity({}), {});
});

test('agent_type wins over kind', () => {
  assert.deepEqual(workflowAgentIdentity({ agent_type: 'codex', kind: 'human' }), { cliAgent: 'codex' });
});

test('expert and plain nodes both show and hash by the call label (roster mirrors the label too)', () => {
  assert.equal(workflowAgentDisplayName({ name: 'clip_3', display_name: '视频镜头师' }), 'clip_3');
  assert.equal(workflowAgentAvatarName({ name: 'clip_3', display_name: '视频镜头师' }), 'clip_3');
  assert.equal(workflowAgentDisplayName({ name: 'free' }), 'free');
  assert.equal(workflowAgentAvatarName({ name: 'free' }), 'free');
});

test('expert node keeps the native identity (no CLI brand, no human role)', () => {
  assert.deepEqual(workflowAgentIdentity({ agent_type: undefined, kind: 'agent', agent_template: 'clip' }), { agentTemplate: 'clip' });
});

test('expert node carries its template in the identity so the avatar can look up the package image', () => {
  assert.deepEqual(workflowAgentIdentity({ kind: 'agent', agent_template: 'clip' }), { agentTemplate: 'clip' });
  assert.deepEqual(workflowAgentIdentity({ kind: 'agent' }), {});
});


test('phase workers keep same-named one-shot nodes apart and collapse session turns', () => {
  const runs = [{
    id: 'wf_1',
    phases: [{
      id: 'assemble-3',
      agents: [
        { id: 'a', name: 'assemble' },
        { id: 'b', name: 'assemble' },
        { id: 'c', name: 'assemble' },
        { id: 's1', name: 'chat', node_type: 'agent_session', member_name: 'analyst' },
        { id: 's2', name: 'chat', node_type: 'agent_session', member_name: 'analyst' },
      ],
    }],
  }];
  const workers = workflowPhaseWorkers({ task_id: 'wf_1:assemble-3', workflow_run_id: 'wf_1' }, runs);
  assert.deepEqual(workers.map((w) => w.id), ['a', 'b', 'c', 's1']);
});

test('isWorkflowRunEnded marks exactly completed / failed / stopped as ended', () => {
  for (const status of ['completed', 'failed', 'stopped']) {
    assert.equal(isWorkflowRunEnded(status), true, `${status} should be ended`);
  }
  for (const status of ['planned', 'pending', 'running', 'paused', 'waiting_for_human']) {
    assert.equal(isWorkflowRunEnded(status), false, `${status} should be active`);
  }
});

test('partitionWorkflowRunsByLiveness pins active runs first and keeps section order', () => {
  const runs = [
    { id: 'a', status: 'completed' },
    { id: 'b', status: 'running' },
    { id: 'c', status: 'failed' },
    { id: 'd', status: 'waiting_for_human' },
    { id: 'e', status: 'paused' },
    { id: 'f', status: 'stopped' },
    { id: 'g', status: 'pending' },
  ];
  const { active, ended } = partitionWorkflowRunsByLiveness(runs);
  // Store order is newest-first; each section must preserve it so a status
  // flip moves a card between sections without reshuffling its neighbours.
  assert.deepEqual(active.map((run) => run.id), ['b', 'd', 'e', 'g']);
  assert.deepEqual(ended.map((run) => run.id), ['a', 'c', 'f']);
});

test('partitionWorkflowRunsByLiveness handles empty and single-section lists', () => {
  assert.deepEqual(partitionWorkflowRunsByLiveness([]), { active: [], ended: [] });
  const allEnded = [{ id: 'a', status: 'completed' }, { id: 'b', status: 'stopped' }];
  assert.deepEqual(partitionWorkflowRunsByLiveness(allEnded), { active: [], ended: allEnded });
  const allActive = [{ id: 'a', status: 'running' }, { id: 'b', status: 'planned' }];
  assert.deepEqual(partitionWorkflowRunsByLiveness(allActive), { active: allActive, ended: [] });
});

test('orderRunsForTreeView interleaves the divider only when both sections exist', () => {
  const ids = (items) => items.map((item) => (typeof item === 'string' ? item : item.id));
  // Mixed list: active section first, divider marker, ended section.
  const mixed = orderRunsForTreeView([
    { id: 'a', status: 'completed' },
    { id: 'b', status: 'running' },
    { id: 'c', status: 'failed' },
  ]);
  assert.deepEqual(ids(mixed.items), ['b', 'divider', 'a', 'c']);
  assert.equal(mixed.activeCount, 1);
  assert.equal(mixed.endedCount, 2);
  // Single-section lists: no divider marker.
  const allActive = orderRunsForTreeView([{ id: 'b', status: 'running' }, { id: 'd', status: 'paused' }]);
  assert.deepEqual(ids(allActive.items), ['b', 'd']);
  assert.equal(allActive.activeCount, 2);
  assert.equal(allActive.endedCount, 0);
  const allEnded = orderRunsForTreeView([{ id: 'a', status: 'failed' }]);
  assert.deepEqual(ids(allEnded.items), ['a']);
  assert.equal(allEnded.activeCount, 0);
  assert.equal(allEnded.endedCount, 1);
  assert.deepEqual(orderRunsForTreeView([]), { items: [], activeCount: 0, endedCount: 0 });
});
