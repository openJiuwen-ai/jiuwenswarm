import test from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import {
  setTeamOrganizationUiEnabled,
  isTeamOrganizationUiEnabled,
  selectedExpertTeamId,
  useOrganizationEvents,
  useChatStore,
  useSessionStore,
  useTeamSelectorStore,
  applyTeamSnapshotToSession,
  webClient,
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
    assert.equal(subscriptions.length, 9);
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
