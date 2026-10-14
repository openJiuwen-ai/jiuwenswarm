import assert from 'node:assert/strict';
import test from 'node:test';

import { workflowAgentIdentity } from '../node_modules/.cache/workflow-agent-identity/workflowTypes.js';

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
