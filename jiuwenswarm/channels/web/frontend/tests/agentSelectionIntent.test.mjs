import './fixtures/browserEnvironment.mjs';
import assert from 'node:assert/strict';
import test from 'node:test';

const { useSessionStore } = await import('../node_modules/.cache/agent-selection/sessionStore.mjs');

test('an existing Session does not inherit another window\'s Team mode preference', () => {
  const previousStorage = globalThis.localStorage;
  globalThis.localStorage = window.localStorage;
  const previousMode = localStorage.getItem('jiuwenclaw_mode');
  const store = useSessionStore.getState();
  try {
    localStorage.setItem('jiuwenclaw_mode', 'team');
    store.removeRuntime('new');
    assert.equal(store.ensureRuntime('new').mode, 'team');
    assert.equal(store.ensureRuntime('existing-agent').mode, 'agent');
    store.addEnabledPlugin('existing-plugin-session', 'test-plugin');
    assert.equal(store.getRuntime('existing-plugin-session').mode, 'agent');
    store.setCurrentSession({ session_id: 'existing-team', mode: 'team.work.normal' });
    assert.equal(store.getRuntime('existing-team').mode, 'team');
  } finally {
    for (const id of ['new', 'existing-agent', 'existing-plugin-session', 'existing-team']) {
      store.removeRuntime(id);
    }
    store.setCurrentSession(null);
    if (previousMode === null) localStorage.removeItem('jiuwenclaw_mode');
    else localStorage.setItem('jiuwenclaw_mode', previousMode);
    globalThis.localStorage = previousStorage;
  }
});

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

test('an AgentGroup display identity survives catalog removal for the session', () => {
  const sessionId = 'agent-group-display-identity';
  const store = useSessionStore.getState();
  store.ensureRuntime(sessionId);
  try {
    const identity = {
      id: 'hub-asset-123',
      name: 'group-a',
      displayName: '中文专家团',
      avatarUrl: null,
    };
    store.setMode(sessionId, 'team');
    store.setAgentGroupBinding(sessionId, 'group-a');
    store.setTeamGroupIdentity(sessionId, identity);

    assert.deepEqual(
      useSessionStore.getState().getRuntime(sessionId)?.teamGroupIdentity,
      identity,
    );

    // Refreshing the local catalog after uninstall must not erase the
    // identity that is already attached to this historical session.
    store.setAgentGroupBinding(sessionId, 'group-a');
    assert.deepEqual(
      useSessionStore.getState().getRuntime(sessionId)?.teamGroupIdentity,
      identity,
    );
  } finally {
    useSessionStore.getState().removeRuntime(sessionId);
  }
});

test('an AgentGroup display identity is restored from session metadata after restart', () => {
  const sessionId = 'agent-group-persisted-display-identity';
  const store = useSessionStore.getState();
  const session = {
    session_id: sessionId,
    mode: 'team',
    agent_group_name: 'group-a',
    team_group_identity: {
      id: 'hub-asset-123',
      name: 'group-a',
      display_name: '中文专家团',
      display_name_i18n: { zh: '中文专家团', en: 'Expert Team' },
      avatar: 'https://example.test/group.png',
    },
  };
  try {
    store.setCurrentSession(session);
    assert.deepEqual(useSessionStore.getState().getRuntime(sessionId)?.teamGroupIdentity, {
      id: 'hub-asset-123',
      name: 'group-a',
      displayName: '中文专家团',
      displayNameI18n: { zh: '中文专家团', en: 'Expert Team' },
      avatarUrl: 'https://example.test/group.png',
    });

    // Simulate a browser/app restart: all frontend runtime state is gone, but
    // the backend metadata snapshot remains after the package is uninstalled.
    store.removeRuntime(sessionId);
    store.setCurrentSession(session);
    assert.equal(
      useSessionStore.getState().getRuntime(sessionId)?.teamGroupIdentity?.displayName,
      '中文专家团',
    );
  } finally {
    useSessionStore.getState().setCurrentSession(null);
    useSessionStore.getState().removeRuntime(sessionId);
  }
});
