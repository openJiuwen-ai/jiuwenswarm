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
