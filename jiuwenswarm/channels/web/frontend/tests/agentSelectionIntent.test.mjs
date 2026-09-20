import assert from 'node:assert/strict';
import test from 'node:test';

import { useSessionStore } from '../node_modules/.cache/agent-selection/sessionStore.mjs';

test('stale send completion cannot consume a newer Agent clear intent', () => {
  const sessionId = 'agent-selection-race';
  const store = useSessionStore.getState();
  store.ensureRuntime(sessionId);
  try {
    store.setAgentSelectionIntent(sessionId, { kind: 'select', id: 'expert-a' });
    store.clearAgentSelectionIntent(sessionId, { kind: 'select', id: 'expert-a' });
    assert.deepEqual(
      useSessionStore.getState().getRuntime(sessionId)?.agentSelectionIntent,
      { kind: 'select', id: 'expert-a' },
    );

    store.setAgentSelectionIntent(sessionId, { kind: 'clear' });
    store.clearAgentSelectionIntent(sessionId, { kind: 'select', id: 'expert-a' });
    assert.deepEqual(
      useSessionStore.getState().getRuntime(sessionId)?.agentSelectionIntent,
      { kind: 'clear' },
    );

    store.clearAgentSelectionIntent(sessionId, { kind: 'clear' });
    assert.deepEqual(
      useSessionStore.getState().getRuntime(sessionId)?.agentSelectionIntent,
      { kind: 'keep' },
    );
  } finally {
    useSessionStore.getState().removeRuntime(sessionId);
  }
});

test('an AgentGroup selection can be locked optimistically while awaiting binding', () => {
  const sessionId = 'agent-group-binding-pending';
  const store = useSessionStore.getState();
  store.ensureRuntime(sessionId);
  try {
    store.setAgentGroupSelectionIntent(sessionId, { kind: 'select', id: 'group-a' });
    store.setAgentGroupBindingPending(sessionId, 'group-a');

    let runtime = useSessionStore.getState().getRuntime(sessionId);
    assert.equal(runtime?.agentGroupBinding, null);
    assert.equal(runtime?.agentGroupBindingPending, 'group-a');
    assert.deepEqual(runtime?.agentGroupSelectionIntent, { kind: 'select', id: 'group-a' });

    store.setAgentGroupBinding(sessionId, 'group-a');
    runtime = useSessionStore.getState().getRuntime(sessionId);
    assert.equal(runtime?.agentGroupBinding, 'group-a');
    assert.equal(runtime?.agentGroupBindingPending, null);

    store.setAgentGroupBindingPending(sessionId, 'group-b');
    store.setCurrentSession({ session_id: sessionId, mode: 'team', agent_group_name: null });
    runtime = useSessionStore.getState().getRuntime(sessionId);
    assert.equal(runtime?.agentGroupBindingPending, 'group-b');

    store.setCurrentSession({ session_id: sessionId, mode: 'team', agent_group_name: 'group-b' });
    runtime = useSessionStore.getState().getRuntime(sessionId);
    assert.equal(runtime?.agentGroupBindingPending, null);

    store.setAgentGroupBindingPending(sessionId, 'group-c');
    store.setAgentGroupBinding(sessionId, null);
    runtime = useSessionStore.getState().getRuntime(sessionId);
    assert.equal(runtime?.agentGroupBinding, null);
    assert.equal(runtime?.agentGroupBindingPending, null);
  } finally {
    useSessionStore.getState().removeRuntime(sessionId);
  }
});
