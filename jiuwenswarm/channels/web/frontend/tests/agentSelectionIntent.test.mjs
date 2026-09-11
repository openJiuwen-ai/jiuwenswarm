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

test('restores the mounted Agent from session equipment without overriding an explicit local intent', () => {
  const sessionId = 'agent-equipment-restore';
  const store = useSessionStore.getState();
  store.ensureRuntime(sessionId);
  try {
    store.restoreSessionEquipment(sessionId, {
      agent_template_name: 'sales-data-analyst',
      plugin_names: [],
      mcp: [],
    });
    assert.deepEqual(
      useSessionStore.getState().getRuntime(sessionId)?.agentSelectionIntent,
      { kind: 'select', id: 'sales-data-analyst' },
    );

    store.setAgentSelectionIntent(sessionId, { kind: 'clear' });
    store.restoreSessionEquipment(sessionId, {
      agent_template_name: 'another-agent',
      plugin_names: [],
      mcp: [],
    });
    assert.deepEqual(
      useSessionStore.getState().getRuntime(sessionId)?.agentSelectionIntent,
      { kind: 'clear' },
    );

    store.setAgentSelectionIntent(sessionId, { kind: 'select', id: 'local-agent' });
    store.restoreSessionEquipment(sessionId, { plugin_names: [], mcp: [] });
    assert.deepEqual(
      useSessionStore.getState().getRuntime(sessionId)?.agentSelectionIntent,
      { kind: 'select', id: 'local-agent' },
    );

    store.setAgentSelectionIntent(sessionId, { kind: 'keep' });
    store.restoreSessionEquipment(sessionId, {
      agent_template_name: '',
      plugin_names: [],
      mcp: [],
    });
    assert.deepEqual(
      useSessionStore.getState().getRuntime(sessionId)?.agentSelectionIntent,
      { kind: 'keep' },
    );
  } finally {
    useSessionStore.getState().removeRuntime(sessionId);
  }
});
