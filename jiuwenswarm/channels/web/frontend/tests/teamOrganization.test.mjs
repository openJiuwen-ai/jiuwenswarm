import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
import { loadCallback } from './loadProductionCallback.mjs';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import {
  setTeamOrganizationUiEnabled,
  isTeamOrganizationUiEnabled,
  selectedExpertTeamId,
  expertTeamId,
  sendExpertMessage,
  organizationOwnerStopTarget,
  isOrganizationProcessing,
  useOrganizationProcessing,
  clearOrganizationRequests,
  startExpertRequest,
  finishExpertRequest,
  reconcileOrganizationRequests,
  organizationProcessingRevision,
  organizationOwnerStateRevision,
  useOrganizationConversationKey,
  useOrganizationEvents,
  useChatStore,
  useSessionStore,
  useTeamSelectorStore,
  applyTeamSnapshotToSession,
  webClient,
  OrgInfoPanel,
  TeamSelector,
  resolveOrganizationTeamIdentity,
  resolveOrganizationTeamLabel,
  useTeamOrganizationUiEnabled,
  useSettingsConfig,
  SettingsServicesProvider,
  parseHistoryJsonFileToPreviewMessages,
  parseOrganizationHistoryPanelRecords,
  setOrganizationOwnerStopped,
  isOrganizationOwnerStopped,
  expertProcessingSnapshot,
  MemberToolEventProjector,
} from '../node_modules/.cache/team-organization/organization.mjs';

test('child member tools populate isolated process panels without duplicating Owner or Leader events', async () => {
  seed();
  const originalRuntimes = useSessionStore.getState().runtimes;
  useSessionStore.setState({ runtimes: { session: originalRuntimes.session } });
  setTeamOrganizationUiEnabled(true);
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const originals = { on: webClient.on, getState: webClient.getState, onStateChange: webClient.onStateChange };
  const callbacks = new Map();
  webClient.on = (event, callback) => { callbacks.set(event, callback); return () => callbacks.delete(event); };
  webClient.getState = () => 'disconnected';
  webClient.onStateChange = () => () => {};
  const root = createRoot(document.getElementById('root'));
  function Probe() { useOrganizationEvents(); return null; }
  const payload = (team, member, id) => ({
    session_id: 'session', team_id: team, member_name: member, role: 'teammate',
    source: 'org_expert_background', request_id: 'round', timestamp: 1_700_000_000,
    tool_call: { id, name: 'claim_task', arguments: { task_id: 'task' } },
  });
  const emit = (event, value) => callbacks.get(event)({ payload: value });
  try {
    await act(async () => root.render(React.createElement(Probe)));
    await act(async () => {
      emit('chat.tool_call', payload('expert', 'solution_architect', 'shared-id'));
      emit('chat.tool_call', payload('expert', 'solution_architect', 'shared-id'));
      emit('chat.tool_call', payload('other-child', 'researcher', 'shared-id'));
      emit('chat.tool_call', payload('expert', 'reviewer', 'review-id'));
      for (const team of ['expert', 'other-child']) {
        emit('chat.tool_result', {
          ...payload(team, undefined, 'shared-id'), role: undefined, tool_call: undefined,
          tool_name: 'claim_task', tool_call_id: 'shared-id', result: 'claimed', success: true,
        });
      }
      emit('chat.tool_call', { ...payload('expert', 'team_leader', 'leader-id'), role: 'leader' });
      emit('chat.tool_call', { ...payload('owner', 'owner-member', 'owner-id'), source: 'org_root_background' });
      emit('chat.tool_result', {
        ...payload('unselected', undefined, 'shared-id'), tool_call: undefined,
        tool_name: 'claim_task', tool_call_id: 'shared-id', result: 'unknown member',
      });
    });
    const runtimes = useSessionStore.getState().runtimes;
    const events = runtimes['session::expert'].teamMemberExecutionEvents;
    assert.equal(events.length, 3);
    assert.deepEqual(events.map(event => [event.member_id, event.kind]).sort(), [
      ['solution_architect', 'tool_call'], ['reviewer', 'tool_call'], ['solution_architect', 'tool_result'],
    ].sort());
    assert.equal(events[0].timestamp, 1_700_000_000_000);
    const call = events.find(event => event.member_id === 'solution_architect' && event.kind === 'tool_call');
    const result = events.find(event => event.kind === 'tool_result');
    assert.equal(call.tool_call_id, result.tool_call_id);
    assert.equal(result.content, 'claimed');
    assert.deepEqual(runtimes['session::other-child'].teamMemberExecutionEvents.map(event => event.member_id),
      ['researcher', 'researcher']);
    assert.equal(runtimes.session.teamMemberExecutionEvents.length, 0);
    assert.equal(runtimes['session::unselected'], undefined);
    assert.ok(useChatStore.getState().runtimes['session::expert'].toolExecutions.get('leader-id'));
    await act(async () => setTeamOrganizationUiEnabled(false));
    assert.equal(callbacks.has('chat.tool_call'), false);
  } finally {
    await act(async () => root.unmount());
    useSessionStore.setState({ runtimes: originalRuntimes });
    setTeamOrganizationUiEnabled(false);
    Object.assign(webClient, originals);
    dom.window.close();
    delete globalThis.window;
    delete globalThis.document;
  }
});

test('history restores member tools by Team and preserves legacy behavior when organization UI is disabled', () => {
  seed();
  const records = ['owner', 'expert', 'another-child'].flatMap((team, index) => {
    const common = {
      session_id: 'session', mode: 'team', team_id: team, member_name: 'same-member', role: 'teammate',
      source: team === 'owner' ? 'org_root_background' : 'org_expert_background', timestamp: 1_700_000_000_000 + index,
    };
    return [
      { ...common, event_type: 'chat.tool_call', tool_call: { id: 'same-call', name: 'view_task', arguments: {} } },
      { ...common, event_type: 'chat.tool_result', tool_call_id: 'same-call', tool_name: 'view_task', result: team },
    ];
  });
  setTeamOrganizationUiEnabled(true);
  try {
    const state = parseOrganizationHistoryPanelRecords(records, 'session');
    assert.equal(state.executionEvents.length, 2);
    assert.equal(state.executionEvents[1].content, 'owner');
    assert.equal(state.organizationExecutionEvents.expert.length, 2);
    assert.equal(state.organizationExecutionEvents.expert[1].content, 'expert');
    assert.equal(state.organizationExecutionEvents['another-child'][1].content, 'another-child');
    setTeamOrganizationUiEnabled(false);
    const legacy = parseOrganizationHistoryPanelRecords(records, 'session');
    assert.equal(legacy.executionEvents.length, 6);
    assert.equal(legacy.organizationExecutionEvents, undefined);
  } finally {
    setTeamOrganizationUiEnabled(false);
  }
});

test('stopping Owner makes it idle while child execution and reconnect snapshots remain independent', () => {
  seed();
  const chat = useChatStore.getState();
  startExpertRequest('session::expert', 'child-round', 'org_expert_background');
  assert.equal(chat.getRuntime('session').isProcessing, true);
  const oldRevision = organizationProcessingRevision();
  setOrganizationOwnerStopped('session', true);
  assert.equal(chat.getRuntime('session').isProcessing, false);
  assert.equal(chat.getRuntime('session::expert').isProcessing, true);
  startExpertRequest('session', 'late-owner-round', 'org_root_delivery');
  // A late child event cannot relight the stopped Owner's Stop button.
  startExpertRequest('session::expert', 'child-finalize', 'org_expert_background');
  assert.equal(chat.getRuntime('session').isProcessing, false);
  reconcileOrganizationRequests('session', 'owner', [], oldRevision, [], false);
  assert.equal(isOrganizationOwnerStopped('session'), true);
  reconcileOrganizationRequests('session', 'owner', [
    { team_id: 'expert', request_id: 'child-round', source: 'org_expert_background' },
  ], organizationProcessingRevision(), [], true);
  assert.equal(chat.getRuntime('session').isProcessing, false);
  assert.equal(chat.getRuntime('session::expert').isProcessing, true);
  setOrganizationOwnerStopped('session', false);
  assert.equal(isOrganizationProcessing('session'), true);
  clearOrganizationRequests('session');
  assert.equal(isOrganizationOwnerStopped('session'), false);
});

test('Owner history roster excludes expert and Summary members even when those Teams are no longer listed', () => {
  seed();
  // Uninstalled/stopped child Teams may be missing from the current selector roster.
  useTeamSelectorStore.setState({ runtimes: {
    session: { teams: [{ team_id: 'owner', is_owner: true }], selectedTeamId: 'owner' },
  } });
  const memberRecord = (team, member, source) => ({
    session_id: 'session', mode: 'team', event_type: 'chat.tool_call', team_id: team, source,
    role: 'teammate', member_name: member, timestamp: 1_700_000_000_000,
    tool_call: { id: `call-${member}`, name: 'view_task', arguments: {} },
  });
  const records = [
    memberRecord('owner', 'owner-member', 'org_root_background'),
    memberRecord('removed-expert', 'solution_architect', 'org_expert_background'),
    memberRecord('removed-summary', 'source-integrator', 'org_summary_background'),
    memberRecord('removed-summary', 'delivery-drafter', 'org_summary_background'),
  ];
  setTeamOrganizationUiEnabled(true);
  try {
    const state = parseOrganizationHistoryPanelRecords(records, 'session');
    assert.deepEqual(state.members.map(member => member.member_id), ['owner-member']);
    assert.equal(state.organizationExecutionEvents['removed-expert'].length, 1);
    assert.equal(state.organizationExecutionEvents['removed-summary'].length, 2);
    setTeamOrganizationUiEnabled(false);
    assert.equal(parseOrganizationHistoryPanelRecords(records, 'session').members.length, 4);
  } finally {
    setTeamOrganizationUiEnabled(false);
  }
});

test('child shutdown does not remove a same-named Owner member during history restore', () => {
  seed();
  const ownerRecord = {
    session_id: 'session', mode: 'team', event_type: 'chat.tool_call', team_id: 'owner',
    source: 'org_root_background', role: 'teammate', member_name: 'shared-member', timestamp: 1_700_000_000_000,
    tool_call: { id: 'owner-call', name: 'view_task', arguments: {} },
  };
  const shutdown = {
    session_id: 'session', mode: 'team', event_type: 'team.member', team_id: 'expert',
    source: 'org_expert_background', timestamp: 1_700_000_001_000,
    event: { type: 'team.member.shutdown', member_id: 'shared-member', target_status: 'shut_down' },
  };
  setTeamOrganizationUiEnabled(true);
  try {
    const ownerOnly = parseOrganizationHistoryPanelRecords([ownerRecord], 'session');
    const mixed = parseOrganizationHistoryPanelRecords([ownerRecord, shutdown], 'session');
    assert.deepEqual(mixed.members, ownerOnly.members);
    assert.deepEqual(mixed.members.map(member => member.member_id), ['shared-member']);
    assert.deepEqual(mixed.executionEvents, ownerOnly.executionEvents);
    setTeamOrganizationUiEnabled(false);
    assert.deepEqual(parseOrganizationHistoryPanelRecords([ownerRecord, shutdown], 'session').members, []);
  } finally {
    setTeamOrganizationUiEnabled(false);
  }
});

function seed() {
  clearOrganizationRequests('session');
  setTeamOrganizationUiEnabled(false);
  useChatStore.getState().ensureRuntime('session');
  useSessionStore.getState().ensureRuntime('session');
  useSessionStore.getState().setTeamMembers('session', [{ member_id: 'owner-member', id: 'owner', timestamp: 1 }]);
  useTeamSelectorStore.setState({
    runtimes: {
      session: {
        selectedTeamId: 'expert',
        teams: [
          { team_id: 'owner', is_owner: true },
          { team_id: 'expert', is_owner: false },
        ],
      },
    },
  });
}


for (const scenario of [
  { name: 'expert team', mode: 'team', enabled: true, teamId: 'expert', intent: 'cancel' },
  { name: 'owner foreground', mode: 'team', enabled: true, teamId: 'owner', intent: 'cancel' },
  { name: 'owner team', mode: 'team', enabled: true, teamId: 'owner', intent: 'cancel' },
  { name: 'disabled organization UI', mode: 'team', enabled: false, teamId: 'expert', intent: 'pause' },
  { name: 'agent', mode: 'agent', enabled: false, teamId: 'owner', intent: 'cancel' },
]) {
  test(`composer Stop sends the supported intent for ${scenario.name}`, async () => {
    seed();
    setTeamOrganizationUiEnabled(scenario.enabled);
    useSessionStore.getState().setMode('session', scenario.mode);
    useTeamSelectorStore.setState((state) => ({
      runtimes: { session: { ...state.runtimes.session, selectedTeamId: scenario.teamId,
        teams: state.runtimes.session.teams.map((team) => ({ ...team, organization_id: 'org' })) } },
    }));
    if (scenario.name === 'owner team') startExpertRequest('session', 'background', 'org_root_background');
    if (scenario.name === 'owner foreground') useChatStore.getState().setProcessing('session', true);
    const calls = [];
    const pending = [];
    const originalRequest = webClient.request;
    const dom = new JSDOM('<div id="root"></div>');
    const previousWindow = globalThis.window;
    const previousDocument = globalThis.document;
    globalThis.window = dom.window;
    globalThis.document = dom.window.document;
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    webClient.request = async (method, params) => {
      calls.push({ method, params });
      // Match AgentServer: targeted expert cancellation only accepts cancel.
      assert.equal(params.target_team_id ? params.intent === 'cancel' : true, true);
      return { success: true };
    };
    const fail = (error) => assert.fail(error);
    const interrupt = loadCallback('hooks/useWebSocket.ts', 'interrupt', {
      selectedExpertTeamId,
      organizationOwnerStopTarget,
      setOrganizationOwnerStopped,
      isOrganizationOwnerStopped,
      useChatStore,
      closeActiveTeamLeaderMessages() {},
      sendExpertMessage,
      request: loadCallback('hooks/useWebSocket.ts', 'request', { webClient }),
      useSessionStore,
      getSessionWorkContext: () => ({}),
      resolveOutgoingMode: (_sessionId, mode) => mode,
      onErrorRef: { current: fail },
      t: (key) => key,
    });
    const controls = Object.fromEntries(['pause', 'cancel'].map((intent) => {
      const callback = loadCallback('hooks/useWebSocket.ts', intent, {
        interrupt,
        organizationOwnerStopTarget,
        useSubagentStore: { getState: () => ({ markRunningSubagentsCancelled() {} }) },
        onErrorRef: { current: fail },
        t: (key) => key,
      });
      return [intent, (sessionId) => {
        const task = callback(sessionId);
        pending.push(task);
        return task;
      }];
    }));
    const onCancel = loadCallback('App.tsx', 'handleCancel', {
      sessionIdRef: { current: 'session' },
      NEW_CONVERSATION_ID: 'new',
      useGoalStore: { getState: () => ({ runtimes: {} }) },
      useChatStore,
      mode: scenario.mode,
      organizationOwnerStopTarget,
      ...controls,
    });
    const onClick = loadCallback('components/ChatPanel/InputArea.tsx', 'handleSendButtonClick', {
      designLandingSubmitting: false,
      showStop: true,
      onCancel,
      handleSubmit: () => assert.fail('Stop must not send a new message'),
    });
    const root = createRoot(document.getElementById('root'));
    try {
      await act(async () => root.render(React.createElement('button', { onClick }, 'Stop')));
      await act(async () => {
        document.querySelector('button').click();
        await Promise.all(pending);
      });
      assert.equal(calls.length, 1);
      assert.equal(calls[0].method, 'chat.interrupt');
      assert.equal(calls[0].params.session_id, 'session');
      assert.equal(calls[0].params.intent, scenario.intent);
      assert.equal(calls[0].params.mode, scenario.mode);
      assert.equal(
        calls[0].params.target_team_id,
        scenario.enabled && scenario.mode === 'team' ? scenario.teamId : undefined,
      );
      if (scenario.enabled && scenario.teamId === 'owner') {
        assert.equal(calls[0].params.organization_owner_stop, true);
        assert.equal(calls[0].params.organization_stop, undefined);
      }
    } finally {
      await act(async () => root.unmount());
      webClient.request = originalRequest;
      dom.window.close();
      globalThis.window = previousWindow;
      globalThis.document = previousDocument;
      delete globalThis.IS_REACT_ACT_ENVIRONMENT;
      setTeamOrganizationUiEnabled(false);
    }
  });
}

for (const source of ['org_expert_background', 'org_summary_background', 'org_root_background', 'org_root_delivery']) {
  test(`background lifecycle stays busy across Team selection: ${source}`, async () => {
    seed();
    clearOrganizationRequests('session');
    useChatStore.getState().setProcessing('session', false);
    const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
    globalThis.window = dom.window;
    globalThis.document = dom.window.document;
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    const listeners = new Map();
    const original = webClient.on;
    webClient.on = (event, handler) => { listeners.set(event, handler); return () => listeners.delete(event); };
    const root = createRoot(document.getElementById('root'));
    function Probe() {
      useOrganizationEvents();
      const key = useOrganizationConversationKey('session');
      const teamBusy = useChatStore((state) => state.runtimes[key]?.isProcessing ?? false);
      const organizationBusy = useOrganizationProcessing('session');
      return React.createElement('output', null, `${key}:${key === 'session' ? organizationBusy || teamBusy : teamBusy}`);
    }
    const emit = (event, requestId, extra = {}) => listeners.get(event)({ payload: {
      session_id: 'session', team_id: 'expert', source, request_id: requestId, ...extra,
    } });
    const select = (team) => useTeamSelectorStore.setState((state) => ({ runtimes: {
      ...state.runtimes, session: { ...state.runtimes.session, selectedTeamId: team },
    } }));
    try {
      await act(async () => { setTeamOrganizationUiEnabled(true); root.render(React.createElement(Probe)); });
      await act(async () => emit('chat.processing_status', 'background-a', { is_processing: true }));
      assert.equal(document.querySelector('output').textContent, 'session::expert:true');
      await act(async () => select('owner'));
      assert.equal(document.querySelector('output').textContent, 'session:true');
      assert.equal(organizationOwnerStopTarget('session'), 'owner');
      assert.equal(useChatStore.getState().getRuntime('session').isProcessing, true);
      await act(async () => {
        emit('chat.processing_status', 'background-b', { is_processing: true });
        emit('org.turn_completed', 'background-a');
        emit('chat.processing_status', 'background-a', { is_processing: false });
      });
      assert.equal(isOrganizationProcessing('session'), true);
      await act(async () => select('expert'));
      assert.equal(document.querySelector('output').textContent, 'session::expert:true');
      // Every matching error releases its request, including member and legacy frames.
      await act(async () => emit('chat.error', 'background-b', { role: 'teammate', error: 'member failed' }));
      assert.equal(useChatStore.getState().getRuntime('session::expert').isProcessing, false);
      await act(async () => emit('chat.processing_status', 'background-b', { is_processing: false }));
      assert.equal(document.querySelector('output').textContent, 'session::expert:false');
      assert.equal(isOrganizationProcessing('session'), false);
      // Owner background work uses the Owner key and also settles correctly.
      await act(async () => {
        select('owner');
        emit('chat.processing_status', 'owner-round', { team_id: 'owner', source: 'org_root_background', is_processing: true });
      });
      assert.equal(document.querySelector('output').textContent, 'session:true');
      await act(async () => emit('chat.processing_status', 'owner-round', {
        team_id: 'owner', source: 'org_root_background', is_processing: false,
      }));
      assert.equal(document.querySelector('output').textContent, 'session:false');
    } finally {
      await act(async () => root.unmount());
      clearOrganizationRequests('session');
      setTeamOrganizationUiEnabled(false);
      webClient.on = original;
      dom.window.close();
      delete globalThis.window;
      delete globalThis.document;
    }
  });
}

test('authoritative Team list restores active rounds and rejects a snapshot racing a new send', async () => {
  seed();
  clearOrganizationRequests('session');
  setTeamOrganizationUiEnabled(true);
  const original = webClient.request;
  const teams = useTeamSelectorStore.getState().runtimes.session.teams;
  const runtimes = { ...useChatStore.getState().runtimes };
  delete runtimes['session::expert'];
  useChatStore.setState({ runtimes });
  try {
    webClient.request = async () => ({ teams, organization_requests: [
      { team_id: 'expert', request_id: 'recovered', source: 'org_expert_background' },
      { team_id: 'owner', request_id: 'root', source: 'org_root_delivery' },
    ] });
    await useTeamSelectorStore.getState().fetchTeams('session');
    assert.equal(isOrganizationProcessing('session'), true);
    assert.equal(useChatStore.getState().getRuntime('session::expert').isProcessing, true);
    let finish;
    webClient.request = () => new Promise((resolve) => { finish = resolve; });
    const pending = useTeamSelectorStore.getState().fetchTeams('session');
    startExpertRequest('session::expert', 'newer');
    finish({ teams, organization_requests: [] });
    await pending;
    assert.equal(useChatStore.getState().getRuntime('session::expert').isProcessing, true);
    webClient.request = async () => ({ teams, organization_requests: [], expert_requests: [] });
    await useTeamSelectorStore.getState().fetchTeams('session');
    assert.equal(isOrganizationProcessing('session'), false);
    assert.equal(useChatStore.getState().getRuntime('session::expert').isProcessing, false);
    // A direct question to an expert must not make an idle Owner look active.
    startExpertRequest('session::expert', 'direct');
    assert.equal(isOrganizationProcessing('session'), false);
  } finally {
    clearOrganizationRequests('session');
    setTeamOrganizationUiEnabled(false);
    webClient.request = original;
  }
});

test('organization composer preserves Send for a follow-up draft', () => {
  const source = ts.createSourceFile('InputArea.tsx', readFileSync(
    new URL('../src/components/ChatPanel/InputArea.tsx', import.meta.url), 'utf8',
  ), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let expression;
  let runningWork;
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(source) === 'showStop') expression = node.initializer.getText(source);
    if (ts.isVariableDeclaration(node) && node.name.getText(source) === 'hasRunningWork') {
      runningWork = node.initializer.getText(source);
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  assert.ok(expression);
  const bindings = {
    designLandingSubmitting: false, isProcessing: true, isPaused: false, isTeamMode: true,
    ownerOrganizationIsProcessing: false, organizationEnabled: true, hasDraft: true, hasPendingQuestion: false,
  };
  const evaluate = (overrides) => {
    const values = { ...bindings, ...overrides };
    const body = `const hasRunningWork = ${runningWork}; return ${expression};`;
    return new Function(...Object.keys(values), body)(...Object.values(values));
  };
  assert.equal(evaluate({}), false);
  assert.equal(evaluate({ hasPendingQuestion: true }), true);
  assert.equal(evaluate({ hasDraft: false }), true);
  assert.equal(evaluate({ isProcessing: false }), false);
  assert.equal(evaluate({ organizationEnabled: false }), false);
  assert.equal(evaluate({ isTeamMode: false }), false);
  assert.equal(evaluate({ isPaused: true, hasDraft: false }), false);
  assert.equal(evaluate({ isPaused: true, hasDraft: false, ownerOrganizationIsProcessing: true }), true);
});

test('missing/false config cannot select an expert, enabling makes selection explicit', () => {
  seed();
  assert.equal(isTeamOrganizationUiEnabled(), false);
  assert.equal(selectedExpertTeamId('session'), null);
  setTeamOrganizationUiEnabled('true');
  assert.equal(selectedExpertTeamId('session'), 'expert');
  setTeamOrganizationUiEnabled('false');
  assert.equal(selectedExpertTeamId('session'), null);
});

test('root background routing uses Team identity only when the organization UI is enabled', () => {
  seed();
  const expert = { team_id: 'expert', source: 'org_root_background' };
  assert.equal(expertTeamId(expert, 'session'), null);
  setTeamOrganizationUiEnabled(true);
  assert.equal(expertTeamId(expert, 'session'), 'expert');
  assert.equal(expertTeamId({ team_id: 'owner', source: 'org_root_delivery' }, 'session'), null);
  assert.equal(expertTeamId({ content: 'normal chat' }, 'session'), null);
  setTeamOrganizationUiEnabled(false);
  assert.equal(expertTeamId(expert, 'session'), null);
});

test('expert processing survives ACKs and selection changes, and settles only matching requests', async () => {
  seed();
  const chat = useChatStore.getState();
  chat.setProcessing('session', false);
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const listeners = new Map();
  const originalOn = webClient.on;
  const originalRequest = webClient.request;
  webClient.on = (event, handler) => {
    listeners.set(event, handler);
    return () => listeners.delete(event);
  };
  let sequence = 0;
  let fail = false;
  const calls = [];
  webClient.request = async (method, params, options) => {
    calls.push({ method, params });
    options.onRequestId(`direct-${++sequence}`);
    if (fail) throw new Error('send failed');
    return { accepted: true };
  };
  function Probe() { useOrganizationEvents(); return null; }
  const root = createRoot(document.getElementById('root'));
  const emit = (event, id, extra = {}) => listeners.get(event)({ payload: {
    session_id: 'session', team_id: 'expert', source: 'org_expert_direct', request_id: id, ...extra,
  } });
  const select = (teamId) => useTeamSelectorStore.setState((state) => ({
    runtimes: { ...state.runtimes, session: { ...state.runtimes.session, selectedTeamId: teamId } },
  }));
  const busy = (key = 'session::expert') => chat.getRuntime(key)?.isProcessing;
  try {
    await act(async () => {
      setTeamOrganizationUiEnabled(true);
      root.render(React.createElement(Probe));
    });
    assert.equal(await sendExpertMessage('first question', 'session'), true);
    assert.equal(busy(), true);
    assert.equal(busy('session'), false);
    emit('chat.processing_status', 'direct-1', { is_processing: true });
    select('owner');
    emit('chat.delta', 'direct-1', { content: 'partial reply' });
    emit('chat.processing_status', 'direct-1', { is_processing: false });
    assert.equal(busy(), false);
    assert.equal(busy('session'), false);
    assert.equal(chat.getRuntime('session::expert').messages.at(-1).isStreaming, false);
    assert.equal(calls[0].params.target_team_id, 'expert');

    // Two requests for the same expert can finish in either order.
    select('expert');
    await sendExpertMessage('second question', 'session');
    await sendExpertMessage('third question', 'session');
    emit('chat.processing_status', 'direct-3', { is_processing: false });
    assert.equal(busy(), true);
    emit('org.turn_completed', 'direct-1');
    emit('chat.error', 'direct-1', { error: 'late error' });
    assert.equal(busy(), true);
    emit('org.turn_completed', 'background-turn');
    assert.equal(busy(), true);
    emit('chat.error', 'direct-2', { error: 'execution failed' });
    assert.equal(busy(), false);
    assert.equal(chat.getRuntime('session::expert').messages.at(-1).content, 'execution failed');

    // Other teams and the owner keep their own running state.
    chat.setProcessing('session', true);
    chat.ensureTeamRuntime('session', 'other');
    emit('chat.processing_status', 'other-request', { team_id: 'other', is_processing: true });
    await sendExpertMessage('fourth question', 'session');
    emit('chat.processing_status', 'direct-4', { is_processing: false });
    assert.equal(busy(), false);
    assert.equal(busy('session::other'), true);
    assert.equal(busy('session'), true);
    emit('chat.processing_status', 'other-request', { team_id: 'other', is_processing: false });
    assert.equal(busy('session::other'), false);

    fail = true;
    await assert.rejects(sendExpertMessage('failed send', 'session'), /send failed/);
    assert.equal(busy(), false);
    assert.equal(busy('session'), true);
    chat.setProcessing('session', false);
  } finally {
    await act(async () => root.unmount());
    setTeamOrganizationUiEnabled(false);
    webClient.on = originalOn;
    webClient.request = originalRequest;
    dom.window.close();
    delete globalThis.window;
    delete globalThis.document;
  }
});

for (const { endedWhileOffline, organizationSnapshot } of [true, false].flatMap((endedWhileOffline) =>
  [true, false].map((organizationSnapshot) => ({ endedWhileOffline, organizationSnapshot })))) {
  test(`expert reconnect reconciles ACKed requests, ended offline=${endedWhileOffline}, organization snapshot=${organizationSnapshot}`, async () => {
    seed();
    const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
    globalThis.window = dom.window;
    globalThis.document = dom.window.document;
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    const originals = { on: webClient.on, request: webClient.request, getState: webClient.getState, onStateChange: webClient.onStateChange };
    const listeners = new Map();
    let state = 'ready';
    let stateChanged;
    let tick;
    let serverRequests = [];
    let sequence = 0;
    let delayedSnapshot;
    webClient.on = (event, handler) => { listeners.set(event, handler); return () => listeners.delete(event); };
    webClient.getState = () => state;
    webClient.onStateChange = (handler) => { stateChanged = handler; return () => { stateChanged = undefined; }; };
    dom.window.setInterval = (callback) => { tick = callback; return 1; };
    dom.window.clearInterval = () => {};
    webClient.request = async (method, params, options) => {
      if (method === 'team.list') {
        if (delayedSnapshot) return new Promise((resolve) => { delayedSnapshot.resolve = resolve; });
        return organizationSnapshot ? {
          expert_requests: [],
          teams: useTeamSelectorStore.getState().runtimes.session.teams,
          organization_requests: serverRequests.map((item) => ({ ...item, source: 'org_expert_background' })),
        } : { expert_requests: serverRequests };
      }
      options.onRequestId(`reconnect-${++sequence}`);
      return { accepted: true };
    };
    function Probe() { useOrganizationEvents(); return null; }
    const root = createRoot(document.getElementById('root'));
    const busy = () => useChatStore.getState().getRuntime('session::expert').isProcessing;
    const emit = (event, extra = {}) => listeners.get(event)({ payload: {
      session_id: 'session', team_id: 'expert', source: 'org_expert_direct', ...extra,
    } });
    try {
      await act(async () => { setTeamOrganizationUiEnabled(true); root.render(React.createElement(Probe)); });
      await sendExpertMessage('accepted before disconnect', 'session');
      assert.equal(busy(), true);
      state = 'reconnecting';
      stateChanged(state);
      assert.equal(busy(), false);
      serverRequests = endedWhileOffline ? [] : [{ team_id: 'expert', request_id: 'reconnect-1' }];
      await act(async () => { state = 'ready'; stateChanged(state); });
      assert.equal(busy(), !endedWhileOffline);
      assert.equal(isOrganizationProcessing('session'), organizationSnapshot && !endedWhileOffline);
      // If it was still running, a lost terminal is repaired by the periodic query.
      serverRequests = [];
      await act(async () => tick());
      assert.equal(busy(), false);
      await sendExpertMessage('new question after reconnect', 'session');
      emit('chat.processing_status', { request_id: 'reconnect-1', is_processing: false });
      assert.equal(busy(), true);
      emit('chat.interrupt_result', { success: true, cancelled_request_ids: ['reconnect-2'] });
      assert.equal(busy(), false);

      assert.equal(isOrganizationProcessing('session'), false);

      // A snapshot started before a new send must not clear the newer request.
      await sendExpertMessage('third question', 'session');
      delayedSnapshot = {};
      tick();
      await sendExpertMessage('fourth question', 'session');
      await act(async () => delayedSnapshot.resolve({ expert_requests: [] }));
      assert.equal(busy(), true);
      delayedSnapshot = undefined;
      await act(async () => tick());
      assert.equal(busy(), false);
      await sendExpertMessage('close session', 'session');
      listeners.get('session.deleted')({ payload: { session_id: 'session' } });
      assert.equal(busy(), false);
    } finally {
      await act(async () => root.unmount());
      setTeamOrganizationUiEnabled(false);
      Object.assign(webClient, originals);
      dom.window.close();
      delete globalThis.window;
      delete globalThis.document;
    }
  });
}

test('organization chunks group by gateway request ID and leave Owner messages untouched', async () => {
  seed();
  useChatStore.getState().ensureTeamRuntime('session', 'expert');
  useChatStore.getState().replaceHistoryMessages('session::expert', []);
  const ownerMessages = useChatStore.getState().getRuntime('session').messages;
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const listeners = new Map();
  const original = webClient.on;
  webClient.on = (name, handler) => {
    listeners.set(name, handler);
    return () => listeners.delete(name);
  };
  function Probe() { useOrganizationEvents(); return null; }
  const root = createRoot(document.getElementById('root'));
  const emit = (event, requestId, content) => listeners.get(event)({ payload: {
    session_id: 'session', team_id: 'expert', source: 'org_root_background',
    member_name: 'team_leader', request_id: requestId, content,
  } });
  try {
    await act(async () => {
      setTeamOrganizationUiEnabled(true);
      root.render(React.createElement(Probe));
    });
    await act(async () => {
      emit('chat.reasoning', 'round-a', '先核对');
      emit('chat.reasoning', 'round-a', '来源。');
      emit('chat.delta', 'round-a', '**处理');
      emit('chat.delta', 'round-b', '另一个请求');
      emit('chat.delta', 'round-a', '结论**：完成。');
      emit('chat.final', 'round-a', '**处理结论**：完成。');
      emit('chat.final', 'round-b', '另一个请求');
    });
    const messages = useChatStore.getState().getRuntime('session::expert').messages;
    assert.deepEqual(messages.map((message) => message.content), ['**处理结论**：完成。', '另一个请求']);
    assert.ok(messages.every((message) => message.isStreaming === false));
    const reasoning = useChatStore.getState().getRuntime('session::expert').reasoningSegments;
    assert.equal(reasoning.at(-1).text, '先核对来源。');
    assert.equal(reasoning.at(-1).closed, true);
    assert.deepEqual(useChatStore.getState().getRuntime('session').messages, ownerMessages);
    await act(async () => setTeamOrganizationUiEnabled(false));
    assert.equal(listeners.size, 0);
  } finally {
    await act(async () => root.unmount());
    setTeamOrganizationUiEnabled(false);
    webClient.on = original;
    dom.window.close();
    delete globalThis.window;
    delete globalThis.document;
  }
});

test('organization turn completion closes trailing Summary reasoning without ending Owner work', async () => {
  seed();
  const chat = useChatStore.getState();
  chat.ensureTeamRuntime('session', 'summary');
  chat.replaceHistoryMessages('session::summary', []);
  chat.setProcessing('session', true);
  chat.setThinking('session', true);
  chat.appendReasoning('session', 'Owner is still working');
  chat.setProcessing('session::summary', true);
  chat.setThinking('session::summary', true);
  const owner = chat.getRuntime('session');
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const listeners = new Map();
  const original = webClient.on;
  webClient.on = (name, handler) => {
    listeners.set(name, handler);
    return () => listeners.delete(name);
  };
  function Probe() { useOrganizationEvents(); return null; }
  const root = createRoot(document.getElementById('root'));
  const emit = (event, content = '', member = 'team_leader', requestId = 'summary-round') =>
    listeners.get(event)({ payload: {
      session_id: 'session', team_id: 'summary', source: 'org_summary_background',
      member_name: member, request_id: requestId, content,
    } });
  try {
    await act(async () => {
      setTeamOrganizationUiEnabled(true);
      root.render(React.createElement(Probe));
    });
    await act(async () => {
      emit('chat.final', 'report');
      emit('chat.delta', 'member draft', 'drafter');
      emit('chat.reasoning', 'retry submission without result_uri');
    });
    assert.equal(chat.getRuntime('session::summary').reasoningSegments.at(-1).closed, false);
    await act(async () => emit('org.turn_completed'));
    const summary = chat.getRuntime('session::summary');
    assert.equal(summary.reasoningSegments.at(-1).closed, true);
    assert.equal(summary.isProcessing, false);
    assert.equal(summary.isThinking, false);
    assert.deepEqual(summary.messages.map((message) => message.content), ['report', 'member draft']);
    assert.ok(summary.messages.every((message) => message.isStreaming === false));
    assert.deepEqual(chat.getRuntime('session'), owner);
    // A subsequent turn must still produce its own output normally.
    await act(async () => {
      emit('chat.reasoning', 'new turn');
      emit('chat.delta', 'new report', 'team_leader', 'next-round');
      emit('org.turn_completed', '', 'team_leader', 'next-round');
    });
    assert.equal(chat.getRuntime('session::summary').messages.at(-1).content, 'new report');
    assert.equal(chat.getRuntime('session::summary').messages.at(-1).isStreaming, false);
    await act(async () => {
      emit('chat.reasoning', 'failed turn');
      emit('chat.delta', 'partial report', 'team_leader', 'failed-round');
      emit('chat.error', '', 'team_leader', 'failed-round');
    });
    assert.equal(chat.getRuntime('session::summary').reasoningSegments.at(-1).closed, true);
    assert.ok(chat.getRuntime('session::summary').messages.every((message) => message.isStreaming !== true));
    assert.deepEqual(chat.getRuntime('session'), owner);
    await act(async () => setTeamOrganizationUiEnabled(false));
    assert.equal(listeners.size, 0);
  } finally {
    await act(async () => root.unmount());
    setTeamOrganizationUiEnabled(false);
    webClient.on = original;
    dom.window.close();
    delete globalThis.window;
    delete globalThis.document;
  }
});

async function withOrganizationPanel(request, run, { enabled = true } = {}) {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost', pretendToBeVisual: true });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const originalRequest = webClient.request;
  const originalOn = webClient.on;
  const listeners = new Map();
  const calls = [];
  webClient.request = async (method, params) => {
    calls.push({ method, params });
    return request(method, params);
  };
  webClient.on = (event, handler) => {
    listeners.set(event, handler);
    return () => listeners.delete(event);
  };
  useTeamSelectorStore.setState({ runtimes: {} });
  useChatStore.getState().setActiveSessionId('session');
  setTeamOrganizationUiEnabled(enabled);
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(React.createElement(OrgInfoPanel)));
    await run({ calls, listeners, root, dom });
  } finally {
    await act(async () => root.unmount());
    setTeamOrganizationUiEnabled(false);
    webClient.request = originalRequest;
    webClient.on = originalOn;
    dom.window.close();
    delete globalThis.window;
    delete globalThis.document;
  }
}

const ownerList = { teams: [{ team_id: 'owner', is_owner: true }], default_team_id: 'owner' };
const boundSnapshot = { organization: { organization_id: 'org', display_name: 'Bound organization' }, tasks: [] };

test('uninstalled organization experts remain visible and selectable for history with unavailable status', async () => {
  const teams = [
    { team_id: 'owner', is_owner: true, organization_id: 'org', state: 'paused' },
    { team_id: 'expert', is_owner: false, organization_id: 'org', state: 'configured', available: false,
      unavailable_reason: 'AGENT_GROUP_NOT_INSTALLED', display_name: 'Removed Expert' },
  ];
  await withOrganizationPanel(
    (method) => method === 'team.list' ? { teams, default_team_id: 'owner' }
      : method === 'org.snapshot' ? { organization: { ...boundSnapshot.organization, leaders: [{ team_id: 'expert', available: false }] }, tasks: [] }
      : method === 'agent_groups.list' ? { agentGroups: [] } : { members: [], tasks: [] },
    async ({ root }) => {
      assert.ok(document.querySelector('[data-testid="team-area-org-info-team-unavailable"]'));
      await act(async () => root.render(React.createElement(TeamSelector, { sessionId: 'session' })));
      await act(async () => document.querySelector('[data-testid="chat-panel-team-select-trigger"]').click());
      const option = document.querySelector('[data-testid="chat-panel-team-select-option"][data-team-id="expert"]');
      assert.ok(option.querySelector('[data-testid="chat-panel-team-select-option-unavailable"]'));
      assert.equal(option.disabled, false);
      await act(async () => option.click());
      assert.equal(useTeamSelectorStore.getState().runtimes.session.selectedTeamId, 'expert');
      assert.ok(document.querySelector('[data-testid="chat-panel-team-select-unavailable"]'));
    },
  );
});

test('composer rejects new input for an unavailable selected team without affecting Owner or disabled organization UI', () => {
  const source = ts.createSourceFile('InputArea.tsx', readFileSync(
    new URL('../src/components/ChatPanel/InputArea.tsx', import.meta.url), 'utf8',
  ), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let selector;
  let disabled;
  function visit(node) {
    if (ts.isVariableDeclaration(node)) {
      if (node.name.getText(source) === 'organizationTeamUnavailable') selector = node.initializer.arguments[0].getText(source);
      if (node.name.getText(source) === 'composerDisabled') disabled = node.initializer.getText(source);
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  assert.ok(selector && disabled);
  const evaluate = (enabled, teamId) => {
    const fn = new Function('organizationEnabled', 'activeSessionId', `return (${selector});`)(enabled, 'session');
    return fn({ runtimes: { session: { selectedTeamId: teamId, teams: [
      { team_id: 'owner', available: true }, { team_id: 'expert', available: false },
    ] } } });
  };
  assert.equal(evaluate(true, 'expert'), true);
  assert.equal(evaluate(true, 'owner'), false);
  assert.equal(evaluate(false, 'expert'), false);
  const bindings = { agentGroupUnavailable: false, organizationTeamUnavailable: true, isCompactRunning: false,
    hasPendingQuestion: false, hasRunningAgent: false, designLandingSubmitting: false };
  assert.equal(new Function(...Object.keys(bindings), `return ${disabled};`)(...Object.values(bindings)), true);
});

test('settings load and save immediately gate the Team selector; failed saves preserve the flag', async () => {
  let settings;
  let failSave = false;
  function Probe() {
    settings = useSettingsConfig();
    const enabled = useTeamOrganizationUiEnabled();
    return enabled ? React.createElement(TeamSelector, { sessionId: 'session' }) : null;
  }
  await withOrganizationPanel(
    (method) => {
      if (method === 'config.get') return {};
      if (method === 'config.save_all') {
        if (failSave) throw new Error('save failed');
        return { success: true };
      }
      return method === 'team.list' ? ownerList : boundSnapshot;
    },
    async ({ root }) => {
      await act(async () =>
        root.render(
          React.createElement(
            SettingsServicesProvider,
            {
              isConnected: true,
              connectionState: 'connected',
              request: webClient.request,
            },
            React.createElement(Probe),
          ),
        ),
      );
      assert.equal(isTeamOrganizationUiEnabled(), false);
      assert.equal(document.querySelector('[data-testid="chat-panel-team-select-trigger"]'), null);
      await act(async () => settings.save({ team_organization_ui_enabled: 'true' }, 'enable'));
      assert.ok(document.querySelector('[data-testid="chat-panel-team-select-trigger"]'));
      failSave = true;
      await act(async () =>
        assert.rejects(settings.save({ team_organization_ui_enabled: 'false' }, 'disable'), /save failed/),
      );
      assert.equal(isTeamOrganizationUiEnabled(), true);
      failSave = false;
      await act(async () => settings.save({ team_organization_ui_enabled: 'false' }, 'disable'));
      assert.equal(document.querySelector('[data-testid="chat-panel-team-select-trigger"]'), null);
    },
  );
});

test('TeamSelector belongs to the persistent toolbar, not an attachment portal', () => {
  const source = ts.createSourceFile(
    'InputArea.tsx',
    readFileSync(new URL('../src/components/ChatPanel/InputArea.tsx', import.meta.url), 'utf8'),
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  let found = 0;
  function visit(node) {
    if (ts.isJsxSelfClosingElement(node) && node.tagName.getText(source) === 'TeamSelector') {
      found += 1;
      for (let ancestor = node.parent; ancestor; ancestor = ancestor.parent) {
        assert.equal(ts.isCallExpression(ancestor) && ancestor.expression.getText(source) === 'createPortal', false);
      }
      assert.match(node.parent.parent.getText(source), /isTeamMode && organizationUiEnabled/);
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  assert.equal(found, 1);
});

test('TeamSelector remains available when Owner is idle and unmount preserves selection', async () => {
  let teams = ownerList;
  await withOrganizationPanel(
    (method) =>
      method === 'team.list'
        ? teams
        : method === 'org.snapshot'
          ? boundSnapshot
          : { team_id: 'owner', members: [], tasks: [] },
    async ({ root, dom }) => {
      let refresh;
      const interval = dom.window.setInterval;
      dom.window.setInterval = (callback) => {
        refresh = callback;
        return 1;
      };
      try {
        await act(async () =>
          root.render(React.createElement(TeamSelector, { sessionId: 'session', isProcessing: false })),
        );
        assert.ok(document.querySelector('[data-testid="chat-panel-team-select-trigger"]'));
        assert.equal(typeof refresh, 'function');
        teams = { ...ownerList, teams: [...ownerList.teams, { team_id: 'summary', is_owner: false }] };
        await act(async () => refresh());
        await act(async () => document.querySelector('[data-testid="chat-panel-team-select-trigger"]').click());
        assert.ok(document.querySelector('[data-team-id="summary"]'));
        await act(async () => document.querySelector('[data-team-id="summary"]').click());
        await act(async () => root.render(null));
        assert.equal(useTeamSelectorStore.getState().runtimes.session.selectedTeamId, 'summary');
      } finally {
        dom.window.setInterval = interval;
      }
    },
  );
});

test('organization panel loads its own Team list and never queries without a Team ID', async () => {
  await withOrganizationPanel(
    (method) => (method === 'team.list' ? ownerList : boundSnapshot),
    async ({ calls }) => {
      assert.deepEqual(
        calls.map((call) => call.method),
        ['team.list', 'org.snapshot', 'agent_groups.list'],
      );
      assert.deepEqual(calls[1].params, { session_id: 'session', team_id: 'owner' });
      assert.equal(
        document.querySelector('[data-testid="team-area-org-info-org-name"]').textContent,
        'Bound organization',
      );
      assert.equal(document.querySelector('[data-testid="team-area-org-info-empty"]'), null);
    },
  );
});

test('disabled organization panel issues no requests or subscriptions', async () => {
  await withOrganizationPanel(
    () => {
      throw new Error('unexpected request');
    },
    async ({ calls, listeners }) => {
      assert.equal(calls.length, 0);
      assert.equal(listeners.size, 0);
      assert.equal(document.querySelector('[data-testid="team-area-org-info-panel"]'), null);
    },
    { enabled: false },
  );
});

const groupIdentities = [
  { id: 'business-planning', name: 'business-planning', displayName: '商业策划专家团', avatarUrl: 'http://localhost/business.png' },
  { id: 'technology-insight', name: 'technology-insight', displayName: '技术洞察专家团', avatarUrl: 'http://localhost/technology.png' },
  { id: 'software-architecture-design', name: 'software-architecture-design', displayName: '代码架构设计专家团', avatarUrl: 'http://localhost/architecture.png' },
];
const organizationTeams = {
  teams: groupIdentities.map((group, index) => ({
    team_id: index === 0 ? 'owner' : `expert-${index}`,
    team_name: index === 0 ? 'owner' : `expert-${index}`,
    organization_id: 'org',
    agent_group_name: group.name,
    is_owner: index === 0,
    state: 'paused',
  })),
  default_team_id: 'owner',
};

test('restarted Teams display saved names instead of generated IDs, including legacy records', () => {
  const modern = { team_id: 'org-expert-971a1cd81c04', agent_group_name: 'technology-insight', display_name: 'tech' };
  const legacy = { team_id: 'org-expert-f03138fc21f4', display_name: '代码架构设计专家团' };
  assert.equal(resolveOrganizationTeamLabel(modern, groupIdentities, '-'), '技术洞察专家团');
  assert.equal(resolveOrganizationTeamLabel(legacy, groupIdentities, '-'), '代码架构设计专家团');
  assert.equal(resolveOrganizationTeamIdentity(legacy, groupIdentities).id, 'software-architecture-design');
  assert.equal(resolveOrganizationTeamLabel(legacy, [], '-'), '代码架构设计专家团');
  assert.equal(resolveOrganizationTeamLabel({ team_id: 'summary', display_name: 'Organization Summary Team' }, [], '-'), 'Organization Summary Team');
  assert.equal(resolveOrganizationTeamLabel({ team_id: 'unknown' }, [], '-'), 'unknown');
  assert.equal(resolveOrganizationTeamLabel(null, [], '-'), '-');
});

test('legacy names with ambiguous catalog matches do not invent a package identity', () => {
  const team = { team_id: 'org-expert-old', display_name: '重复名称' };
  const groups = groupIdentities.map((group) => ({ ...group, displayName: '重复名称' }));
  assert.equal(resolveOrganizationTeamIdentity(team, groups), null);
  assert.equal(resolveOrganizationTeamLabel(team, groups, '-'), '重复名称');
});

test('persisted expert identity and legacy name fallback coexist after restart', () => {
  const saved = {
    id: 'saved-tech', name: 'technology-insight', display_name: 'Saved technology team', avatar: null,
  };
  const team = { agent_group_name: saved.name, team_group_identity: saved };
  const identity = resolveOrganizationTeamIdentity(team, []);
  assert.equal(identity.name, saved.name);
  assert.equal(identity.displayName, saved.display_name);
  assert.equal(resolveOrganizationTeamLabel(team, [], '-'), saved.display_name);
  assert.equal(resolveOrganizationTeamIdentity(team, groupIdentities).id, groupIdentities[1].id);
  assert.equal(
    resolveOrganizationTeamIdentity({ ...team, agent_group_name: 'different-package' }, []),
    null,
  );
  const legacy = { display_name: groupIdentities[2].displayName };
  assert.equal(resolveOrganizationTeamIdentity(legacy, groupIdentities).id, groupIdentities[2].id);
});

test('historical database names render in both the organization panel and selector without a catalog', async () => {
  const teams = organizationTeams.teams.map((team, index) => ({
    ...team, agent_group_name: null, display_name: groupIdentities[index].displayName, state: 'configured',
  }));
  await withOrganizationPanel(
    (method, params) => method === 'team.list' ? { teams, default_team_id: 'owner' }
      : method === 'agent_groups.list' ? { agentGroups: [] }
      : method === 'org.snapshot' ? { organization: { ...boundSnapshot.organization, owner_team_id: 'owner', leaders: teams }, tasks: [] }
      : { team_id: params.team_id, members: [], tasks: [] },
    async ({ root }) => {
      for (const [index, team] of teams.entries()) {
        assert.equal(
          document.querySelector(`[data-testid="team-area-org-info-team-name"][data-variant="${team.team_id}"]`).textContent,
          groupIdentities[index].displayName,
        );
      }
      await act(async () => root.render(React.createElement(TeamSelector, { sessionId: 'session', groups: [] })));
      await act(async () => document.querySelector('[data-testid="chat-panel-team-select-trigger"]').click());
      for (const [index, team] of teams.entries()) {
        assert.match(document.querySelector(`[data-team-id="${team.team_id}"]`).textContent, new RegExp(groupIdentities[index].displayName));
      }
    },
  );
});

test('organization identity uses the selected Team package, preserving the session binding', () => {
  seed();
  useSessionStore.getState().setAgentGroupBinding('session', 'business-planning');
  for (const [index, team] of organizationTeams.teams.entries()) {
    assert.deepEqual(resolveOrganizationTeamIdentity(team, groupIdentities), {
      id: groupIdentities[index].id,
      name: groupIdentities[index].name,
      displayName: groupIdentities[index].displayName,
      avatarUrl: groupIdentities[index].avatarUrl,
    });
    assert.equal(useSessionStore.getState().runtimes.session.agentGroupBinding, 'business-planning');
  }
  assert.equal(resolveOrganizationTeamIdentity({ team_id: 'summary' }, groupIdentities), null);
});

test('Team selector shows all group logos and names, switches identity, and hides when disabled', async () => {
  await withOrganizationPanel(
    (method, params) => method === 'team.list' ? organizationTeams
      : method === 'org.snapshot' ? boundSnapshot
      : method === 'agent_groups.list' ? { agentGroups: [] }
      : { team_id: params.team_id, members: [], tasks: [] },
    async ({ root, calls }) => {
      await act(async () => root.render(React.createElement(TeamSelector, {
        sessionId: 'session', groups: groupIdentities,
      })));
      assert.match(document.querySelector('[data-testid="chat-panel-team-select-value"]').textContent, /商业策划专家团/);
      assert.equal(document.querySelector('[data-testid="chat-panel-team-select-avatar"]').getAttribute('src'), 'http://localhost/business.png');
      for (const [index, team] of organizationTeams.teams.entries()) {
        await act(async () => document.querySelector('[data-testid="chat-panel-team-select-trigger"]').click());
        const option = document.querySelector(`[data-team-id="${team.team_id}"]`);
        assert.match(option.textContent, new RegExp(groupIdentities[index].displayName));
        assert.equal(option.querySelector('img').getAttribute('src'), groupIdentities[index].avatarUrl);
        await act(async () => option.click());
        assert.match(document.querySelector('[data-testid="chat-panel-team-select-value"]').textContent, new RegExp(groupIdentities[index].displayName));
        assert.equal(document.querySelector('[data-testid="chat-panel-team-select-avatar"]').getAttribute('src'), groupIdentities[index].avatarUrl);
      }
      await act(async () => setTeamOrganizationUiEnabled(false));
      assert.equal(document.querySelector('[data-testid="chat-panel-team-select"]'), null);
      const count = calls.length;
      await act(async () => root.render(React.createElement(TeamSelector, { sessionId: 'another', groups: groupIdentities })));
      assert.equal(calls.length, count);
    },
  );
});

test('uninstalled Team labels recover from server facts with empty conversation caches', async () => {
  const restoredTeams = {
    ...organizationTeams,
    teams: organizationTeams.teams.map((team, index) => ({
      ...team,
      team_group_identity: {
        id: groupIdentities[index].id,
        name: groupIdentities[index].name,
        display_name: groupIdentities[index].displayName,
        avatar: groupIdentities[index].avatarUrl,
      },
    })),
  };
  await withOrganizationPanel(
    (method, params) => method === 'team.list' ? restoredTeams
      : method === 'org.snapshot' ? boundSnapshot
      : { team_id: params.team_id, members: [], tasks: [] },
    async ({ root }) => {
      for (const team of organizationTeams.teams) {
        useSessionStore.getState().removeRuntime(team.is_owner ? 'session' : `session::${team.team_id}`);
      }
      useSessionStore.getState().ensureRuntime('session');
      const ownerRuntime = useSessionStore.getState().runtimes.session;
      await act(async () => root.render(React.createElement(TeamSelector, { sessionId: 'session', groups: [] })));
      for (const [index, team] of organizationTeams.teams.entries()) {
        await act(async () => document.querySelector('[data-testid="chat-panel-team-select-trigger"]').click());
        const option = document.querySelector(`[data-team-id="${team.team_id}"]`);
        assert.match(option.textContent, new RegExp(groupIdentities[index].displayName));
        await act(async () => option.click());
        assert.match(document.querySelector('[data-testid="chat-panel-team-select-value"]').textContent,
          new RegExp(groupIdentities[index].displayName));
        if (!team.is_owner) {
          const runtime = useSessionStore.getState().runtimes[`session::${team.team_id}`];
          assert.equal(runtime.agentGroupBinding, null);
          assert.equal(runtime.teamGroupIdentity.displayName, groupIdentities[index].displayName);
        }
      }
      assert.equal(useSessionStore.getState().runtimes.session.teamGroupIdentity, ownerRuntime.teamGroupIdentity);
      assert.equal(resolveOrganizationTeamIdentity({
        ...restoredTeams.teams[1], team_group_identity: restoredTeams.teams[0].team_group_identity,
      }, []), null);
      assert.equal(resolveOrganizationTeamIdentity(organizationTeams.teams[1], [], groupIdentities[0]), null);
      assert.equal(resolveOrganizationTeamIdentity({ team_id: 'summary' }, [], groupIdentities[0]), null);
      for (const team of organizationTeams.teams) {
        useSessionStore.getState().setTeamGroupIdentity(team.is_owner ? 'session' : `session::${team.team_id}`, null);
      }
    },
  );
});

test('organization panel resolves Owner and team names from installed groups and removes statistics', async () => {
  await withOrganizationPanel(
    (method) => method === 'team.list' ? organizationTeams : method === 'agent_groups.list'
      ? { agentGroups: groupIdentities.map((group) => ({
          id: group.id, name: group.name, displayName: group.displayName, avatar: group.avatarUrl, installed: true,
        })) }
      : { organization: { ...boundSnapshot.organization, owner_team_id: 'owner', leaders: organizationTeams.teams },
          tasks: [{ task_id: 'root', status: 'COMPLETED' }], stats: { total: 4, open: 0, unclaimed: 1 } },
    async () => {
      const organization = document.querySelector('[data-testid="team-area-org-info-organization"]');
      assert.match(organization.textContent, /商业策划专家团/);
      const ownerRow = document.querySelector('[data-testid="team-area-org-info-owner-team"]');
      assert.match(ownerRow.textContent, /商业策划专家团/);
      assert.equal(ownerRow.querySelector('[title="owner"]').textContent, '商业策划专家团');
      for (const [index, team] of organizationTeams.teams.entries()) {
        assert.equal(document.querySelector(`[data-testid="team-area-org-info-team-name"][data-variant="${team.team_id}"]`).textContent, groupIdentities[index].displayName);
        assert.equal(document.querySelector(`[data-testid="team-area-org-info-team-avatar"][data-variant="${team.team_id}"]`).getAttribute('src'), groupIdentities[index].avatarUrl);
      }
      assert.equal(document.querySelector('[data-testid="team-area-org-info-stats"]'), null);
      assert.equal(document.querySelector('[data-testid="team-area-org-info-stat-card"]'), null);
      assert.ok(document.querySelector('[data-testid="team-area-org-info-task-row"]'));
      await act(async () => setTeamOrganizationUiEnabled(false));
      assert.equal(document.querySelector('[data-testid="team-area-org-info-panel"]'), null);
    },
  );
});

test('loading and RPC failure do not display unbound organization or zero statistics', async () => {
  let rejectSnapshot;
  await withOrganizationPanel(
    (method) =>
      method === 'team.list'
        ? ownerList
        : new Promise((_, reject) => {
            rejectSnapshot = reject;
          }),
    async () => {
      assert.ok(document.querySelector('[data-testid="team-area-org-info-status"]'));
      assert.equal(document.querySelector('[data-testid="team-area-org-info-empty"]'), null);
      assert.equal(document.querySelector('[data-testid="team-area-org-info-stats"]'), null);
      await act(async () => rejectSnapshot(new Error('snapshot unavailable')));
      assert.equal(
        document.querySelector('[data-testid="team-area-org-info-error"]').textContent,
        'snapshot unavailable',
      );
      assert.equal(document.querySelector('[data-testid="team-area-org-info-empty"]'), null);
    },
  );
});

test('an empty Team list is not treated as an unbound organization', async () => {
  await withOrganizationPanel(
    () => ({ teams: [], default_team_id: null }),
    async ({ calls }) => {
      assert.ok(calls.every((call) => call.method === 'team.list'));
      assert.equal(document.querySelector('[data-testid="team-area-org-info-status"]').dataset.variant, 'no-team');
      assert.equal(document.querySelector('[data-testid="team-area-org-info-empty"]'), null);
    },
  );
});

test('organization changes on the same Team refresh after related events, ignoring other sessions', async () => {
  let snapshot = { organization: null, tasks: [] };
  await withOrganizationPanel(
    (method) => (method === 'team.list' ? ownerList : snapshot),
    async ({ listeners, calls }) => {
      assert.ok(document.querySelector('[data-testid="team-area-org-info-empty"]'));
      snapshot = boundSnapshot;
      await act(async () => {
        listeners.get('chat.tool_result')({ payload: { session_id: 'other' } });
        await new Promise((resolve) => setTimeout(resolve, 220));
      });
      assert.equal(calls.length, 2);
      await act(async () => {
        listeners.get('chat.tool_result')({ payload: { session_id: 'session' } });
        await new Promise((resolve) => setTimeout(resolve, 220));
      });
      assert.equal(
        document.querySelector('[data-testid="team-area-org-info-org-name"]').textContent,
        'Bound organization',
      );
    },
  );
});

test('late snapshots cannot overwrite the newly selected Team', async () => {
  let resolveOwner;
  await withOrganizationPanel(
    (method, params) => {
      if (method === 'team.list') return ownerList;
      if (params.team_id === 'owner')
        return new Promise((resolve) => {
          resolveOwner = resolve;
        });
      return { organization: { organization_id: 'expert-org' } };
    },
    async () => {
      await act(async () =>
        useTeamSelectorStore.setState((state) => ({
          runtimes: { ...state.runtimes, session: { ...state.runtimes.session, selectedTeamId: 'expert' } },
        })),
      );
      await act(async () => resolveOwner(boundSnapshot));
      assert.equal(document.querySelector('[data-testid="team-area-org-info-org-name"]').textContent, 'expert-org');
    },
  );
});

test('periodic refresh is deduplicated while pending and timers are removed when disabled', async () => {
  const originalInterval = globalThis.setInterval;
  const originalClearInterval = globalThis.clearInterval;
  const timers = new Map();
  globalThis.setInterval = (callback) => {
    const id = Symbol();
    timers.set(id, callback);
    return id;
  };
  globalThis.clearInterval = (id) => timers.delete(id);
  let resolveSnapshot;
  try {
    await withOrganizationPanel(
      (method) =>
        method === 'team.list'
          ? ownerList
          : new Promise((resolve) => {
              resolveSnapshot = resolve;
            }),
      async ({ calls, listeners }) => {
        await act(async () => {
          timers.values().next().value();
          timers.values().next().value();
        });
        assert.equal(calls.filter((call) => call.method === 'org.snapshot').length, 1);
        await act(async () => resolveSnapshot(boundSnapshot));
        await act(async () => timers.values().next().value());
        assert.equal(calls.filter((call) => call.method === 'org.snapshot').length, 2);
        await act(async () => setTeamOrganizationUiEnabled(false));
        assert.equal(timers.size, 0);
        assert.equal(listeners.size, 0);
        await act(async () => resolveSnapshot(boundSnapshot));
      },
    );
  } finally {
    globalThis.setInterval = originalInterval;
    globalThis.clearInterval = originalClearInterval;
  }
});

test('expert snapshot never overwrites owner roster or task board', () => {
  seed();
  applyTeamSnapshotToSession('session', 'expert', {
    members: [{ member_id: 'expert-member', status: 'ready' }],
    tasks: [{ task_id: 'expert-task', status: 'OPEN' }],
  });
  assert.equal(useSessionStore.getState().runtimes.session.teamMembers[0].member_id, 'owner-member');
  assert.equal(useSessionStore.getState().runtimes['session::expert'].teamMembers[0].member_id, 'expert-member');
  assert.equal(useSessionStore.getState().runtimes['session::expert'].teamTasks[0].task_id, 'expert-task');
});

test('composer mentions follow the selected Team and discard old member chips', async () => {
  seed();
  // Exercise the actual composer selectors/effect without mounting unrelated media/settings UI.
  const source = ts.createSourceFile('InputArea.tsx', readFileSync(
    new URL('../src/components/ChatPanel/InputArea.tsx', import.meta.url), 'utf8',
  ), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const names = new Set(['conversationId', 'teamMembers', 'mentionableMembers', 'extractPlainText', 'composerConversationRef']);
  const statements = [];
  function visit(node) {
    if (ts.isVariableStatement(node) && node.declarationList.declarations.some((d) => names.has(d.name.getText(source)))) {
      statements.push(node.getText(source));
    }
    if (ts.isExpressionStatement(node) && ts.isCallExpression(node.expression)
      && node.expression.expression.getText(source) === 'useEffect'
      && node.expression.arguments[1]?.getText(source) === '[conversationId, extractPlainText]') {
      statements.push(node.getText(source));
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  assert.equal(statements.length, 6);
  const body = ts.transpileModule(statements.join('\n'), { compilerOptions: { target: ts.ScriptTarget.ES2020 } }).outputText;
  const probe = new Function('useOrganizationConversationKey', 'useSessionStore', 'useChatStore', 'React', 'editor', 'report', `
    const { useMemo, useRef, useEffect, useCallback } = React;
    return function Probe({ activeSessionId }) {
      const inputRef = useRef(editor);
      const setComposerSuggestion = (value) => report.closed.push(value);
      ${body}
      report.members = mentionableMembers;
      return null;
    };
  `);
  const dom = new JSDOM('<div id="root"></div><div id="editor"></div>', { url: 'http://localhost' });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.Node = dom.window.Node;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const editor = document.getElementById('editor');
  const report = { closed: [] };
  const Probe = probe(useOrganizationConversationKey, useSessionStore, useChatStore, React, editor, report);
  const root = createRoot(document.getElementById('root'));
  const originalRequest = webClient.request;
  webClient.request = async () => { throw new Error('snapshot unavailable'); };
  useChatStore.getState().setActiveSessionId('session');
  const ids = () => report.members.map((member) => member.id);
  try {
    await act(async () => root.render(React.createElement(Probe, { activeSessionId: 'session' })));
    assert.deepEqual(ids(), ['owner-member']); // Organization disabled.
    await act(async () => setTeamOrganizationUiEnabled(true));
    await act(async () => useTeamSelectorStore.getState().selectTeam('session', 'expert'));
    await act(async () => applyTeamSnapshotToSession('session', 'expert', { members: [] }));
    assert.deepEqual(ids(), []); // No fallback to the Owner while absent/failed/empty.
    await act(async () => applyTeamSnapshotToSession('session', 'expert', {
      members: [{ member_id: 'expert-member', name: 'Same name' }, { member_id: 'user' }],
    }));
    assert.deepEqual(ids(), ['expert-member']);
    editor.innerHTML = 'draft <span contenteditable="false" data-composer-token="member" data-value="expert-member">@Same name</span>'
      + '<span contenteditable="false" data-composer-token="role" data-value="reviewer">$reviewer</span>'
      + ' <span contenteditable="false" data-composer-token="member" data-composer-item-kind="asset" data-value="notes.txt">@notes.txt</span>';
    await act(async () => useTeamSelectorStore.getState().selectTeam('session', 'owner'));
    assert.deepEqual(ids(), ['owner-member']);
    assert.equal(editor.querySelector('[data-composer-token="member"]:not([data-composer-item-kind="asset"])'), null);
    assert.ok(editor.querySelector('[data-composer-item-kind="asset"]'));
    assert.ok(editor.querySelector('[data-composer-token="role"]'));
    assert.equal(useChatStore.getState().getRuntime('session').inputValue, 'draft $reviewer @notes.txt');
    assert.equal(report.closed.at(-1), null);
    await act(async () => useTeamSelectorStore.setState((state) => ({ runtimes: {
      ...state.runtimes, session: { ...state.runtimes.session,
        teams: [...state.runtimes.session.teams, { team_id: 'other', is_owner: false }],
      },
    } })));
    await act(async () => useTeamSelectorStore.getState().selectTeam('session', 'other'));
    await act(async () => applyTeamSnapshotToSession('session', 'other', {
      members: [{ member_id: 'other-member', name: 'Same name' }],
    }));
    assert.deepEqual(ids(), ['other-member']);
    await act(async () => useTeamSelectorStore.getState().selectTeam('session', 'expert'));
    assert.deepEqual(ids(), ['expert-member']); // Cached roster stays local to its Team.
    await act(async () => setTeamOrganizationUiEnabled(false));
    assert.deepEqual(ids(), ['owner-member']);
  } finally {
    await act(async () => root.unmount());
    webClient.request = originalRequest;
    setTeamOrganizationUiEnabled(false);
    dom.window.close();
    delete globalThis.window;
    delete globalThis.document;
    delete globalThis.Node;
  }
});

test('expert member mentions retain their IDs and selected Team in chat.send', async () => {
  seed();
  setTeamOrganizationUiEnabled(true);
  const originalRequest = webClient.request;
  const calls = [];
  webClient.request = async (method, params) => { calls.push({ method, params }); return {}; };
  try {
    await sendExpertMessage('@expert-member explain', 'session');
    assert.equal(calls[0].method, 'chat.send');
    assert.equal(calls[0].params.target_team_id, 'expert');
    assert.equal(calls[0].params.content, '@expert-member explain');
  } finally {
    webClient.request = originalRequest;
    setTeamOrganizationUiEnabled(false);
  }
});

test('history is routed to independent conversations without polluting owner', () => {
  seed();
  useChatStore.getState().replaceHistoryMessages('session', [
    { id: 'root', role: 'user', content: 'root', timestamp: '2026-10-07' },
    { id: 'expert', role: 'user', content: 'expert', timestamp: '2026-10-07', teamId: 'expert' },
  ]);
  assert.deepEqual(
    useChatStore.getState().runtimes.session.messages.map((m) => m.id),
    ['root'],
  );
  assert.deepEqual(
    useChatStore.getState().runtimes['session::expert'].messages.map((m) => m.id),
    ['expert'],
  );
});

test('refresh restores background expert and member replies into their own conversations', () => {
  seed();
  setTeamOrganizationUiEnabled(true);
  const records = [
    { id: 'owner', role: 'user', content: 'owner task', timestamp: 1792800000 },
    ...['leader', 'teammate'].map((role, index) => ({
      id: `background-${role}`, role, member_name: role === 'leader' ? 'team_leader' : 'analyst',
      content: `${role} reply`, event_type: 'chat.final', source: 'org_expert_background',
      team_id: 'expert', mode: 'team', timestamp: 1792800001 + index,
    })),
    { id: 'summary', role: 'leader', content: 'summary reply', event_type: 'chat.final',
      source: 'org_summary_background', team_id: 'summary', mode: 'team', timestamp: 1792800003 },
    { id: 'owner-member', role: 'teammate', content: 'hidden owner member', event_type: 'chat.final',
      source: 'org_root_background', team_id: 'owner', mode: 'team', timestamp: 1792800004 },
    { id: 'expert-tool', role: 'leader', event_type: 'chat.tool_call', tool_name: 'read_file',
      source: 'org_expert_background', team_id: 'expert', mode: 'team', timestamp: 1792800005 },
  ];
  // A browser reload has no in-memory expert conversation to fall back to.
  useChatStore.getState().removeRuntime('session::expert');
  useChatStore.getState().removeRuntime('session::summary');
  const messages = parseHistoryJsonFileToPreviewMessages(records, 'session');
  useChatStore.getState().replaceHistoryMessages('session', messages);
  assert.equal(useChatStore.getState().runtimes.session.messages.length, 1);
  const expert = useChatStore.getState().runtimes['session::expert'].messages;
  assert.equal(expert.length, 2);
  assert.match(expert[0].content, /leader reply/);
  assert.equal(expert[1].content, 'teammate reply');
  assert.match(expert[1].id, /^team-member-analyst@/);
  assert.equal(useChatStore.getState().runtimes['session::summary'].messages.length, 1);
  setTeamOrganizationUiEnabled(false);
  assert.deepEqual(parseHistoryJsonFileToPreviewMessages(records, 'session').map((message) => message.id), ['owner']);
});

test('disabled UI has no organization event subscriptions; disabling unsubscribes', async () => {
  seed();
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const subscriptions = [];
  const original = webClient.on;
  webClient.on = (name, handler) => {
    const entry = { name, handler };
    subscriptions.push(entry);
    return () => subscriptions.splice(subscriptions.indexOf(entry), 1);
  };
  function Probe() {
    useOrganizationEvents();
    return null;
  }
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(React.createElement(Probe)));
    assert.equal(subscriptions.length, 0);
    await act(async () => setTeamOrganizationUiEnabled(true));
    assert.ok(subscriptions.some((entry) => entry.name === 'chat.processing_status'));
    assert.ok(subscriptions.some((entry) => entry.name === 'org.turn_completed'));
    await act(async () => setTeamOrganizationUiEnabled(false));
    assert.equal(subscriptions.length, 0);
  } finally {
    await act(async () => root.unmount());
    webClient.on = original;
    dom.window.close();
    delete globalThis.window;
    delete globalThis.document;
  }
});


test('organization terminals handle cold rosters and settle Owner output without clearing foreground work', async () => {
  seed();
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const originalOn = webClient.on;
  const previousSession = useSessionStore.getState().currentSession;
  useSessionStore.setState({ currentSession: { session_id: 'session', team_name: 'owner' } });
  const listeners = new Map();
  webClient.on = (event, handler) => { listeners.set(event, handler); return () => listeners.delete(event); };
  const root = createRoot(document.getElementById('root'));
  function Probe() { useOrganizationEvents(); return null; }
  const chat = useChatStore.getState();
  const emit = (event, team, request, extra = {}) => listeners.get(event)({ payload: {
    session_id: 'session', team_id: team, request_id: request, source: 'org_root_background', ...extra,
  } });
  try {
    await act(async () => { setTeamOrganizationUiEnabled(true); root.render(React.createElement(Probe)); });
    for (const source of ['org_root_background', 'org_root_delivery', 'org_summary_background', 'org_custom']) {
      await act(async () => emit('chat.processing_status', 'expert', source, { source, is_processing: true }));
      await act(async () => emit('chat.error', 'expert', source, { source, role: 'leader', error: 'terminal' }));
      assert.equal(chat.getRuntime('session::expert').isProcessing, false);
      assert.equal(isOrganizationProcessing('session'), false);
    }
    for (const extra of [
      { source: undefined }, { source: 'legacy' }, { role: 'member' }, { role: 'team_leader' },
    ]) {
      await act(async () => {
        emit('chat.processing_status', 'expert', 'error-request', { is_processing: true });
        emit('chat.processing_status', 'expert', 'newer-request', { is_processing: true });
        emit('chat.error', 'expert', 'error-request', { ...extra, error: 'failed' });
      });
      assert.equal(isOrganizationProcessing('session'), true);
      await act(async () => emit('org.turn_completed', 'expert', 'newer-request'));
      assert.equal(isOrganizationProcessing('session'), false);
      assert.equal(chat.getRuntime('session::expert').isProcessing, false);
      await act(async () => {
        emit('chat.processing_status', 'expert', 'only-request', { is_processing: true });
        emit('chat.error', 'expert', 'only-request', { ...extra, error: 'failed' });
      });
      assert.equal(isOrganizationProcessing('session'), false);
      assert.equal(chat.getRuntime('session::expert').isThinking, false);
    }
    await act(async () => {
      emit('chat.processing_status', 'expert', 'cancelled', { is_processing: true });
      useTeamSelectorStore.setState({ runtimes: {} });
      emit('chat.interrupt_result', 'owner', 'stop', { source: 'org_stop', success: true,
        cancelled_requests: [{ team_id: 'expert', request_id: 'cancelled' }] });
    });
    assert.equal(chat.getRuntime('session::expert').isProcessing, false);
    assert.equal(isOrganizationProcessing('session'), false);
    for (const foreground of [false, true]) {
      await act(async () => {
        chat.setProcessing('session', foreground);
        emit('chat.processing_status', 'owner', 'owner-round', { is_processing: true });
        chat.setThinking('session', true);
        chat.addMessage('session', { id: `owner-${foreground}`, role: 'system', content: 'partial', isStreaming: true });
        emit('org.turn_completed', 'owner', 'owner-round');
      });
      assert.equal(chat.getRuntime('session').isProcessing, foreground);
      assert.equal(chat.getRuntime('session').isThinking, foreground);
      assert.equal(Boolean(chat.getRuntime('session').messages.find((m) => m.id === `owner-${foreground}`).isStreaming), foreground);
    }
    await act(async () => {
      emit('chat.processing_status', 'expert', 'independent-child', { is_processing: true });
      emit('chat.interrupt_result', 'owner', 'failed-stop', { source: 'org_owner_stop', success: false });
    });
    assert.equal(chat.getRuntime('session').isProcessing, true);
    await act(async () => emit('chat.interrupt_result', 'owner', 'owner-stop', {
      source: 'org_owner_stop', success: true,
    }));
    assert.equal(chat.getRuntime('session').isProcessing, false);
    assert.equal(chat.getRuntime('session::expert').isProcessing, true);
    await act(async () => emit('org.turn_completed', 'expert', 'independent-child'));
    assert.equal(chat.getRuntime('session').isProcessing, false);
    assert.equal(chat.getRuntime('session::expert').isProcessing, false);
  } finally {
    await act(async () => root.unmount());
    chat.setProcessing('session', false);
    chat.setThinking('session', false);
    chat.stopStreaming('session');
    setTeamOrganizationUiEnabled(false);
    webClient.on = originalOn;
    useSessionStore.setState({ currentSession: previousSession });
    dom.window.close();
    delete globalThis.window;
    delete globalThis.document;
  }
});


for (const roster of [undefined, [], [{ team_id: 'owner' }, { team_id: 'expert' }]]) {
  test(`organization snapshot falls back to session Owner identity: roster=${JSON.stringify(roster)}`, async () => {
    seed();
    const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
    globalThis.window = dom.window;
    globalThis.document = dom.window.document;
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    const originals = { on: webClient.on, request: webClient.request,
      getState: webClient.getState, onStateChange: webClient.onStateChange };
    const previousSession = useSessionStore.getState().currentSession;
    useSessionStore.setState({ currentSession: { session_id: 'session', team_name: 'owner' } });
    const listeners = new Map();
    let tick;
    webClient.on = (event, handler) => { listeners.set(event, handler); return () => listeners.delete(event); };
    webClient.getState = () => 'ready';
    webClient.onStateChange = () => () => {};
    webClient.request = async () => ({ teams: roster, organization_requests: [
      { team_id: 'owner', request_id: 'owner-snapshot', source: 'org_root_background' },
      { team_id: 'expert', request_id: 'expert-snapshot', source: 'org_expert_background' },
    ] });
    dom.window.setInterval = (callback) => { tick = callback; return 1; };
    dom.window.clearInterval = () => {};
    const root = createRoot(document.getElementById('root'));
    function Probe() {
      useOrganizationEvents();
      const key = useOrganizationConversationKey('session');
      const busy = useChatStore((state) => state.runtimes[key]?.isProcessing ?? false);
      return React.createElement('output', null, `${key}:${busy}`);
    }
    const emit = (team, request, source) => listeners.get('org.turn_completed')({ payload: {
      session_id: 'session', team_id: team, request_id: request, source,
    } });
    try {
      await act(async () => {
        setTeamOrganizationUiEnabled(true);
        startExpertRequest('session', 'owner-snapshot', 'org_root_background');
        root.render(React.createElement(Probe));
      });
      await act(async () => tick());
      assert.equal(document.querySelector('output').textContent, 'session::expert:true');
      assert.equal(useChatStore.getState().getRuntime('session::owner')?.isProcessing ?? false, false);
      await act(async () => useTeamSelectorStore.setState((state) => ({ runtimes: {
        ...state.runtimes, session: { ...state.runtimes.session, selectedTeamId: 'idle',
          teams: [...state.runtimes.session.teams, { team_id: 'idle' }] },
      } })));
      assert.equal(document.querySelector('output').textContent, 'session::idle:false');
      await act(async () => {
        emit('owner', 'owner-snapshot', 'org_root_background');
        emit('expert', 'expert-snapshot', 'org_expert_background');
      });
      assert.equal(isOrganizationProcessing('session'), false);
      assert.equal(useChatStore.getState().getRuntime('session::expert').isProcessing, false);
    } finally {
      await act(async () => root.unmount());
      clearOrganizationRequests('session');
      setTeamOrganizationUiEnabled(false);
      Object.assign(webClient, originals);
      useSessionStore.setState({ currentSession: previousSession });
      dom.window.close();
      delete globalThis.window;
      delete globalThis.document;
    }
  });
}


test('Owner store processing combines independent foreground and organization lifecycles', () => {
  seed();
  const chat = useChatStore.getState();
  chat.setProcessing('session', false);
  startExpertRequest('session', 'background', 'org_root_background');
  assert.equal(chat.getRuntime('session').isProcessing, true);
  assert.equal(chat.getRuntime('session').foregroundIsProcessing, false);
  chat.setProcessing('session', true);
  finishExpertRequest('session', 'background');
  assert.equal(chat.getRuntime('session').isProcessing, true);
  chat.setProcessing('session', false);
  assert.equal(chat.getRuntime('session').isProcessing, false);
  startExpertRequest('session::expert', 'child', 'org_expert_background');
  chat.setProcessing('session', true);
  chat.setProcessing('session', false);
  assert.equal(chat.getRuntime('session').isProcessing, true);
  finishExpertRequest('session::expert', 'child');
  assert.equal(chat.getRuntime('session').isProcessing, false);
  startExpertRequest('session::expert', 'direct');
  assert.equal(chat.getRuntime('session').isProcessing, false);
  clearOrganizationRequests('session');
});

for (const direct of [undefined, [], [{ team_id: 'expert', request_id: 'direct' }]]) {
  test(`mixed snapshots preserve direct requests according to their own snapshot: ${JSON.stringify(direct)}`, () => {
    seed();
    startExpertRequest('session::expert', 'direct');
    startExpertRequest('session::expert', 'background', 'org_expert_background');
    reconcileOrganizationRequests('session', 'owner', [], organizationProcessingRevision(), direct);
    const expectedBusy = direct === undefined || direct.length > 0;
    assert.equal(useChatStore.getState().getRuntime('session::expert').isProcessing, expectedBusy);
    assert.equal(useChatStore.getState().getRuntime('session').isProcessing, false);
    finishExpertRequest('session::expert', 'direct');
    assert.equal(useChatStore.getState().getRuntime('session::expert').isProcessing, false);
    clearOrganizationRequests('session');
  });
}

for (const roster of [undefined, [], [{ team_id: 'expert' }]]) {
  test(`store Team list uses persisted Owner identity with incomplete roster: ${JSON.stringify(roster)}`, async () => {
    seed();
    setTeamOrganizationUiEnabled(true);
    const originalRequest = webClient.request;
    const previousSession = useSessionStore.getState().currentSession;
    useSessionStore.setState({ currentSession: { session_id: 'session', team_name: 'owner' } });
    webClient.request = async () => ({ teams: roster, organization_requests: [
      { team_id: 'owner', request_id: 'root', source: 'org_root_background' },
    ], expert_requests: [{ team_id: 'expert', request_id: 'direct' }] });
    try {
      await useTeamSelectorStore.getState().fetchTeams('session');
      assert.equal(useChatStore.getState().getRuntime('session').isProcessing, true);
      assert.equal(useChatStore.getState().getRuntime('session::owner')?.isProcessing ?? false, false);
      assert.equal(useChatStore.getState().getRuntime('session::expert').isProcessing, true);
      finishExpertRequest('session', 'root');
      assert.equal(useChatStore.getState().getRuntime('session').isProcessing, false);
      assert.equal(useChatStore.getState().getRuntime('session::expert').isProcessing, true);
      finishExpertRequest('session::expert', 'direct');
      assert.equal(isOrganizationProcessing('session'), false);
    } finally {
      webClient.request = originalRequest;
      clearOrganizationRequests('session');
      setTeamOrganizationUiEnabled(false);
      useSessionStore.setState({ currentSession: previousSession });
    }
  });
}

test('explicit Owner pause remains pause while organization requests are running', async () => {
  seed();
  setTeamOrganizationUiEnabled(true);
  useSessionStore.getState().setMode('session', 'team');
  useTeamSelectorStore.setState((state) => ({ runtimes: {
    session: { ...state.runtimes.session, selectedTeamId: 'owner' },
  } }));
  startExpertRequest('session', 'background', 'org_root_background');
  const calls = [];
  const interrupt = loadCallback('hooks/useWebSocket.ts', 'interrupt', {
    selectedExpertTeamId, organizationOwnerStopTarget, sendExpertMessage, useSessionStore,
    getSessionWorkContext: () => ({}), resolveOutgoingMode: (_sid, mode) => mode,
    request: async (method, params) => { calls.push({ method, params }); return { success: true }; },
    onErrorRef: { current: (error) => assert.fail(error) }, t: (key) => key,
  });
  try {
    assert.equal(await interrupt('session', 'pause'), true);
    assert.equal(calls[0].params.intent, 'pause');
    assert.equal(calls[0].params.organization_stop, undefined);
    assert.equal(calls[0].params.target_team_id, undefined);
  } finally {
    clearOrganizationRequests('session');
    setTeamOrganizationUiEnabled(false);
  }
});


test('organization Hook synchronizes direct and background snapshots together', async () => {
  seed();
  setTeamOrganizationUiEnabled(true);
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const originals = { on: webClient.on, request: webClient.request,
    getState: webClient.getState, onStateChange: webClient.onStateChange };
  let tick;
  let expertRequests = [{ team_id: 'expert', request_id: 'direct' }];
  webClient.on = () => () => {};
  webClient.getState = () => 'ready';
  webClient.onStateChange = () => () => {};
  webClient.request = async () => ({ organization_requests: [], expert_requests: expertRequests });
  dom.window.setInterval = (callback) => { tick = callback; return 1; };
  dom.window.clearInterval = () => {};
  const root = createRoot(document.getElementById('root'));
  function Probe() { useOrganizationEvents(); return null; }
  try {
    startExpertRequest('session::expert', 'direct');
    await act(async () => root.render(React.createElement(Probe)));
    await act(async () => tick());
    assert.equal(useChatStore.getState().getRuntime('session::expert').isProcessing, true);
    assert.equal(useChatStore.getState().getRuntime('session').isProcessing, false);
    expertRequests = [];
    await act(async () => tick());
    assert.equal(useChatStore.getState().getRuntime('session::expert').isProcessing, false);
  } finally {
    await act(async () => root.unmount());
    clearOrganizationRequests('session');
    setTeamOrganizationUiEnabled(false);
    Object.assign(webClient, originals);
    dom.window.close();
    delete globalThis.window;
    delete globalThis.document;
  }
});

test('organization model change refreshes only its session Team list and unsubscribes on unmount', async () => {
  seed();
  setTeamOrganizationUiEnabled(true);
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const originals = { on: webClient.on, getState: webClient.getState, onStateChange: webClient.onStateChange };
  const originalFetch = useTeamSelectorStore.getState().fetchTeams;
  const callbacks = new Map();
  const calls = [];
  webClient.on = (event, callback) => { callbacks.set(event, callback); return () => callbacks.delete(event); };
  webClient.getState = () => 'disconnected';
  webClient.onStateChange = () => () => {};
  useTeamSelectorStore.setState({ fetchTeams: async (sessionId) => { calls.push(sessionId); return []; } });
  const root = createRoot(document.getElementById('root'));
  function Probe() { useOrganizationEvents(); return null; }
  try {
    await act(async () => root.render(React.createElement(Probe)));
    await act(async () => callbacks.get('team.model_changed')({
      payload: { session_id: 'session', team_id: 'expert', model_name: 'deepseek-flash' },
    }));
    assert.deepEqual(calls, ['session']);
  } finally {
    await act(async () => root.unmount());
    assert.equal(callbacks.has('team.model_changed'), false);
    useTeamSelectorStore.setState({ fetchTeams: originalFetch });
    setTeamOrganizationUiEnabled(false);
    Object.assign(webClient, originals);
    dom.window.close();
    delete globalThis.window;
    delete globalThis.document;
  }
});


test('idle organization cancel does not persist a new Owner stop', () => {
  seed();
  setTeamOrganizationUiEnabled(true);
  useTeamSelectorStore.setState(state => ({ runtimes: { session: {
    ...state.runtimes.session, selectedTeamId: 'owner',
    teams: state.runtimes.session.teams.map(team => ({ ...team, organization_id: 'org' })),
  } } }));
  const chat = useChatStore.getState();
  chat.setProcessing('session', false);
  assert.equal(organizationOwnerStopTarget('session'), null);
  chat.setProcessing('session', true);
  assert.equal(organizationOwnerStopTarget('session'), 'owner');
  chat.setProcessing('session', false);
  setTeamOrganizationUiEnabled(false);
});

for (const intent of ['resume', 'supplement']) {
  for (const success of [true, false]) {
    test(`Owner ${intent} clears its stopped flag only when admitted: ${success}`, async () => {
      seed();
      setOrganizationOwnerStopped('session', true);
      const interrupt = loadCallback('hooks/useWebSocket.ts', 'interrupt', {
        selectedExpertTeamId: () => null,
        organizationOwnerStopTarget,
        useSessionStore,
        getSessionWorkContext: () => ({}),
        resolveOutgoingMode: (_sessionId, mode) => mode,
        resolvePlanEntryPayload: () => ({}),
        request: async () => ({ success }),
        setOrganizationOwnerStopped,
        clearConnectionPresentationForUserAction() {},
        consumePlanEntryMark() {},
        onErrorRef: { current: error => assert.fail(error) },
        t: key => key,
      });
      assert.equal(await interrupt('session', intent), success);
      assert.equal(isOrganizationOwnerStopped('session'), !success);
      clearOrganizationRequests('session');
    });
  }
}

test('stopped Owner still receives terminal and board events while late deltas are suppressed', () => {
  seed();
  setOrganizationOwnerStopped('session', true);
  const resolve = loadCallback('hooks/useWebSocket.ts', 'resolveEventSessionId', {
    getPayloadSessionId: payload => payload.session_id,
    isOrganizationOwnerStopped,
    expertTeamId: () => null,
    ensureSessionRuntimes() {},
    useChatStore,
  });
  const payload = { session_id: 'session', source: 'org_root_background' };
  for (const event of ['chat.delta', 'chat.reasoning', 'chat.tool_call', 'chat.tool_result']) {
    assert.equal(resolve(payload, event), null);
  }
  for (const event of ['chat.final', 'chat.error', 'team.member', 'team.task', 'chat.processing_status']) {
    assert.equal(resolve(payload, event), 'session');
  }
  assert.ok(expertProcessingSnapshot().keys.includes('session'));
  reconcileOrganizationRequests('session', 'owner', [], organizationProcessingRevision(), [], false);
  assert.equal(isOrganizationOwnerStopped('session'), false);
  assert.equal(resolve(payload, 'chat.delta'), 'session');
  assert.equal(expertProcessingSnapshot().keys.includes('session'), false);
});

test('history before team.list isolates uninstalled child records without org source tags', () => {
  seed();
  const previousSession = useSessionStore.getState().currentSession;
  useSessionStore.setState({ currentSession: { session_id: 'session', team_name: 'owner' } });
  useTeamSelectorStore.setState({ runtimes: {} });
  setTeamOrganizationUiEnabled(true);
  const record = (team, member) => ({
    session_id: 'session', event_type: 'chat.tool_call', role: 'teammate', member_name: member,
    team_id: team, timestamp: 1_700_000_000_000,
    tool_call: { id: `call-${member}`, name: 'view_task', arguments: {} },
  });
  try {
    const state = parseOrganizationHistoryPanelRecords([
      record('owner', 'owner-member'),
      { ...record('', 'child-member'), event_payload: { team_id: 'removed-child' } },
    ], 'session');
    assert.deepEqual(state.members.map(member => member.member_id), ['owner-member']);
    assert.equal(state.organizationExecutionEvents['removed-child'].length, 1);
  } finally {
    useSessionStore.setState({ currentSession: previousSession });
    setTeamOrganizationUiEnabled(false);
  }
});


test('member tool process events always contain renderable strings for missing data', () => {
  seed();
  const projector = new MemberToolEventProjector();
  const key = 'session::missing-tool-data';
  const payload = { role: 'teammate', member_name: 'worker', request_id: 'round' };
  projector.toolCall(key, payload, { id: 'empty-call', name: 'noop', memberName: 'worker' });
  projector.toolResult(key, payload, { toolCallId: 'empty-call', toolName: 'noop' });
  const events = useSessionStore.getState().runtimes[key].teamMemberExecutionEvents;
  assert.equal(events.length, 2);
  assert.deepEqual(events.map(event => event.content.trim()), ['', '']);
});


test('child final-only output populates its member process panel and stays isolated by Team', async () => {
  seed();
  setTeamOrganizationUiEnabled(true);
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const originals = { on: webClient.on, getState: webClient.getState, onStateChange: webClient.onStateChange };
  const callbacks = new Map();
  webClient.on = (event, callback) => { callbacks.set(event, callback); return () => callbacks.delete(event); };
  webClient.getState = () => 'disconnected';
  webClient.onStateChange = () => () => {};
  const root = createRoot(document.getElementById('root'));
  function Probe() { useOrganizationEvents(); return null; }
  const emit = payload => callbacks.get('chat.final')({ payload });
  const payload = {
    session_id: 'session', team_id: 'expert', member_name: 'final-only-worker', role: 'teammate',
    source: 'org_expert_background', request_id: 'final-only-round', timestamp: 1_700_000_000,
    content: 'Completed analysis',
  };
  try {
    await act(async () => root.render(React.createElement(Probe)));
    await act(async () => {
      emit(payload);
      emit(payload);
      emit({ ...payload, team_id: 'summary-final-only', source: 'org_summary_background', content: 'Summary result' });
      emit({ ...payload, role: 'leader', member_name: 'team_leader' });
      emit({ ...payload, member_name: undefined });
      emit({ ...payload, content: '  ' });
      emit({ ...payload, team_id: 'owner', source: 'org_root_background' });
    });
    const events = useSessionStore.getState().runtimes['session::expert'].teamMemberExecutionEvents
      .filter(event => event.member_id === 'final-only-worker');
    assert.equal(events.length, 1);
    assert.equal(events[0].kind, 'final');
    assert.equal(events[0].content, 'Completed analysis');
    assert.equal(events[0].timestamp, 1_700_000_000_000);
    assert.ok(events[0].title);
    const summary = useSessionStore.getState().runtimes['session::summary-final-only'].teamMemberExecutionEvents;
    assert.equal(summary.length, 1);
    assert.equal(summary[0].content, 'Summary result');
    assert.equal(useSessionStore.getState().runtimes.session.teamMemberExecutionEvents
      .some(event => event.member_id === 'final-only-worker'), false);
  } finally {
    await act(async () => root.unmount());
    setTeamOrganizationUiEnabled(false);
    Object.assign(webClient, originals);
    dom.window.close();
    delete globalThis.window;
    delete globalThis.document;
  }
});


for (const outcome of ['success', 'failure', 'disconnect']) {
  test(`Owner stop is immediate before acknowledgement and restores on ${outcome}`, async () => {
    seed();
    clearOrganizationRequests('session');
    setTeamOrganizationUiEnabled(true);
    useTeamSelectorStore.setState((state) => ({ runtimes: {
      ...state.runtimes, session: { ...state.runtimes.session, selectedTeamId: 'owner' },
    } }));
    useSessionStore.getState().setMode('session', 'team');
    const chat = useChatStore.getState();
    chat.setProcessing('session', true);
    startExpertRequest('session::expert', 'child-running', 'org_expert_background');
    let complete;
    let fail;
    const response = new Promise((resolve, reject) => { complete = resolve; fail = reject; });
    const interrupt = loadCallback('hooks/useWebSocket.ts', 'interrupt', {
      selectedExpertTeamId: () => null,
      organizationOwnerStopTarget,
      useSessionStore,
      useChatStore,
      getSessionWorkContext: () => ({}),
      resolveOutgoingMode: (_sessionId, mode) => mode,
      setOrganizationOwnerStopped,
      isOrganizationOwnerStopped,
      closeActiveTeamLeaderMessages() {},
      request: async () => response,
      onErrorRef: { current() {} },
      t: key => key,
    });
    try {
      const pending = interrupt('session', 'cancel');
      assert.equal(isOrganizationOwnerStopped('session'), true);
      assert.equal(chat.getRuntime('session').isProcessing, false);
      assert.equal(chat.getRuntime('session').isThinking, false);
      assert.equal(chat.getRuntime('session::expert').isProcessing, true);
      startExpertRequest('session::expert', 'late-child-round', 'org_expert_background');
      assert.equal(chat.getRuntime('session').isProcessing, false);
      if (outcome === 'disconnect') fail(new Error('disconnected'));
      else complete({ success: outcome === 'success' });
      assert.equal(await pending, outcome === 'success');
      assert.equal(isOrganizationOwnerStopped('session'), outcome === 'success');
      assert.equal(chat.getRuntime('session').isProcessing, outcome !== 'success');
      assert.equal(chat.getRuntime('session::expert').isProcessing, true);
    } finally {
      chat.setProcessing('session', false);
      clearOrganizationRequests('session');
      setTeamOrganizationUiEnabled(false);
    }
  });
}


for (const ownerOnly of [true, false]) {
  test(`cancel preserves running subagents only for an Owner-only stop: ${ownerOnly}`, async () => {
    const cancelled = [];
    const cancel = loadCallback('hooks/useWebSocket.ts', 'cancel', {
      organizationOwnerStopTarget: () => ownerOnly ? 'owner' : null,
      interrupt: async () => true,
      useSubagentStore: { getState: () => ({ markRunningSubagentsCancelled: id => cancelled.push(id) }) },
      onErrorRef: { current: error => assert.fail(error) },
      t: key => key,
    });
    await cancel('session');
    assert.deepEqual(cancelled, ownerOnly ? [] : ['session']);
  });
}


test('pending Owner stop retains live background rounds for failure rollback', () => {
  seed();
  clearOrganizationRequests('session');
  useChatStore.getState().setProcessing('session', false);
  startExpertRequest('session', 'owner-background', 'org_root_background');
  setOrganizationOwnerStopped('session', true, true);
  assert.equal(useChatStore.getState().getRuntime('session').isProcessing, false);
  setOrganizationOwnerStopped('session', false);
  assert.equal(useChatStore.getState().getRuntime('session').isProcessing, true);
  setOrganizationOwnerStopped('session', true);
  setOrganizationOwnerStopped('session', false);
  assert.equal(useChatStore.getState().getRuntime('session').isProcessing, false);
  clearOrganizationRequests('session');
});


for (const stoppedBy of ['local', 'persisted', 'neither']) {
  test(`metadata refresh cannot relight a stopped Owner: ${stoppedBy}`, async () => {
    seed();
    clearOrganizationRequests('session');
    const chat = useChatStore.getState();
    chat.setProcessing('session', true);
    startExpertRequest('session::expert', 'child-running', 'org_expert_background');
    if (stoppedBy === 'local') setOrganizationOwnerStopped('session', true, true);
    let projected;
    const refresh = loadCallback('App.tsx', 'fetchSessionMetadata', {
      useChatStore,
      request: async () => ({ session_id: 'session', is_processing: true, status: 'processing',
        organization_owner_stopped: stoppedBy === 'persisted' }),
      sessionIdRef: { current: 'other-session' },
      upsertSessionMetadata: session => { projected = session; },
      useWorkspaceStore: { getState: () => ({ upsertSession() {} }) },
      setProcessing: chat.setProcessing,
      setThinking: chat.setThinking,
      isOrganizationOwnerStopped,
      setOrganizationOwnerStopped,
      organizationOwnerStateRevision,
    });
    try {
      assert.ok(await refresh('session'));
      assert.equal(chat.getRuntime('session').isProcessing, stoppedBy === 'neither');
      assert.equal(projected.is_processing, stoppedBy === 'neither');
      if (stoppedBy !== 'neither') assert.equal(projected.status, 'interrupted');
      assert.equal(chat.getRuntime('session::expert').isProcessing, true);
    } finally {
      chat.setProcessing('session', false);
      clearOrganizationRequests('session');
    }
  });
}


test('a snapshot received while Owner stop is pending cannot clear its local stop', () => {
  seed();
  clearOrganizationRequests('session');
  setOrganizationOwnerStopped('session', true, true);
  reconcileOrganizationRequests('session', 'owner', [
    { team_id: 'expert', request_id: 'child-running', source: 'org_expert_background' },
  ], organizationProcessingRevision(), [], false);
  assert.equal(isOrganizationOwnerStopped('session'), true);
  assert.equal(useChatStore.getState().getRuntime('session').isProcessing, false);
  setOrganizationOwnerStopped('session', false);
  assert.equal(useChatStore.getState().getRuntime('session').isProcessing, true);
  clearOrganizationRequests('session');
});

test('stopped Owner rejects late foreground deltas without an organization source tag', () => {
  seed();
  clearOrganizationRequests('session');
  setOrganizationOwnerStopped('session', true);
  const resolve = loadCallback('hooks/useWebSocket.ts', 'resolveEventSessionId', {
    getPayloadSessionId: payload => payload.session_id,
    isOrganizationOwnerStopped,
    expertTeamId: () => null,
    ensureSessionRuntimes() {},
    useChatStore,
  });
  try {
    for (const event of ['chat.delta', 'chat.reasoning', 'chat.tool_call', 'chat.tool_result']) {
      assert.equal(resolve({ session_id: 'session' }, event), null);
    }
    assert.equal(resolve({ session_id: 'session' }, 'chat.final'), 'session');
  } finally {
    clearOrganizationRequests('session');
  }
});


for (const newerAction of ['child-progress', 'owner-input']) {
  test(`persisted Owner stop recovery distinguishes ${newerAction} during metadata query`, async () => {
    seed();
    clearOrganizationRequests('session');
    const chat = useChatStore.getState();
    chat.setProcessing('session', true);
    let complete;
    const response = new Promise(resolve => { complete = resolve; });
    const refresh = loadCallback('App.tsx', 'fetchSessionMetadata', {
      useChatStore, request: async () => response, sessionIdRef: { current: 'other-session' },
      upsertSessionMetadata() {}, useWorkspaceStore: { getState: () => ({ upsertSession() {} }) },
      setProcessing: chat.setProcessing, setThinking: chat.setThinking,
      isOrganizationOwnerStopped, setOrganizationOwnerStopped, organizationOwnerStateRevision,
    });
    try {
      const pending = refresh('session');
      startExpertRequest('session::expert', 'child-running', 'org_expert_background');
      if (newerAction === 'owner-input') setOrganizationOwnerStopped('session', false);
      complete({ session_id: 'session', is_processing: true, organization_owner_stopped: true });
      assert.ok(await pending);
      assert.equal(isOrganizationOwnerStopped('session'), newerAction === 'child-progress');
      assert.equal(chat.getRuntime('session').isProcessing, newerAction === 'owner-input');
      assert.equal(chat.getRuntime('session::expert').isProcessing, true);
    } finally {
      chat.setProcessing('session', false);
      clearOrganizationRequests('session');
    }
  });
}
