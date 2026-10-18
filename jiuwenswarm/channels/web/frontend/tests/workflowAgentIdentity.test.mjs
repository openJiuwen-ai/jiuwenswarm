import assert from 'node:assert/strict';
import test from 'node:test';

import {
  workflowAgentAvatarName,
  workflowAgentDisplayName,
  workflowAgentIdentity,
  workflowPhaseWorkers,
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
