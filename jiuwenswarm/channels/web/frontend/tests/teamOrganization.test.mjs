import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import {
  setTeamOrganizationUiEnabled,
  isTeamOrganizationUiEnabled,
  selectedExpertTeamId,
  expertTeamId,
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
} from '../node_modules/.cache/team-organization/organization.mjs';

function seed() {
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
    assert.equal(subscriptions.length, 10);
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
