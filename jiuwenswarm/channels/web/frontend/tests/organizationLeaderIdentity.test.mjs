import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM, VirtualConsole } from 'jsdom';
import { A2UIProvider } from '@a2ui/react';

await build({
  entryPoints: ['tests/fixtures/teamConversationIdentityExports.ts'],
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outfile: 'node_modules/.cache/organization-leader-identity/identity.mjs',
  define: { 'import.meta.env.DEV': 'false' },
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  plugins: [
    {
      name: 'ui-test-svg-components',
      setup(builder) {
        builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'svg-react-stub' }));
        builder.onLoad({ filter: /.*/, namespace: 'svg-react-stub' }, () => ({
          contents: 'export default function SvgStub() { return null; }',
          loader: 'js',
        }));
      },
    },
  ],
});

const {
  ChatTimelineList, MessageItem, useChatStore, useSessionStore, useTeamSelectorStore,
  applyTeamSnapshotToSession, setTeamOrganizationUiEnabled,
} = await import('../node_modules/.cache/organization-leader-identity/identity.mjs');

async function withOrganizationIdentity(run) {
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (error) => {
    if (error.type !== 'css parsing') console.error(error);
  });
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost', virtualConsole });
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const root = createRoot(document.getElementById('root'));
  const sessionId = 'identity-session';
  useChatStore.getState().ensureRuntime(sessionId);
  useChatStore.setState({ activeSessionId: sessionId });
  useSessionStore.getState().ensureRuntime(sessionId);
  useSessionStore.getState().setMode(sessionId, 'team');
  useSessionStore.getState().setTeamLeaderIdentity(sessionId, {
    agentTemplateId: 'business-leader', displayName: 'Business Planning Leader',
    avatar: 'http://localhost/business-leader.png',
  });
  useSessionStore.getState().setAgentGroupBinding(sessionId, 'business-planning');
  for (const teamId of ['summary', 'expert']) {
    useSessionStore.getState().ensureRuntime(`${sessionId}::${teamId}`);
    useSessionStore.getState().setTeamLeaderIdentity(`${sessionId}::${teamId}`, null);
    useSessionStore.getState().setTeamMembers(`${sessionId}::${teamId}`, []);
  }
  useTeamSelectorStore.setState({
    runtimes: {
      [sessionId]: {
        selectedTeamId: 'summary',
        teams: [
          { team_id: 'owner', is_owner: true, display_name: 'Business Team' },
          { team_id: 'summary', is_owner: false, display_name: 'Summary Leader', leader_id: 'summary-leader' },
          { team_id: 'expert', is_owner: false, display_name: 'Expert Team', leader_id: 'expert-leader' },
        ],
      },
    },
  });
  setTeamOrganizationUiEnabled(true);
  const select = (teamId) => useTeamSelectorStore.setState((state) => ({
    runtimes: { [sessionId]: { ...state.runtimes[sessionId], selectedTeamId: teamId } },
  }));
  try {
    await run({ root, sessionId, select });
  } finally {
    await act(async () => root.unmount());
    setTeamOrganizationUiEnabled(false);
    useChatStore.setState({ activeSessionId: null });
    globalThis.window = previousWindow;
    globalThis.document = previousDocument;
    delete globalThis.IS_REACT_ACT_ENVIRONMENT;
    dom.window.close();
  }
}

const summaryMessage = {
  id: 'team-leader-summary', role: 'system', content: 'Summary response',
  timestamp: '2026-10-26T08:40:00Z',
};

for (const surface of ['timeline', 'message', 'tools']) {
  test(`organization ${surface} uses its own identity across switching and restored snapshots`, async () => {
    await withOrganizationIdentity(async ({ root, sessionId, select }) => {
      const content = surface !== 'message'
        ? createElement(ChatTimelineList, {
          messages: surface === 'tools' ? [] : [summaryMessage],
          executions: surface === 'tools' ? [{
            toolCallId: 'summary-tool', toolCall: { id: 'summary-tool', name: 'read_file', arguments: {} },
            status: 'pending', startedAt: summaryMessage.timestamp, updatedAt: summaryMessage.timestamp,
          }] : [],
          mode: 'team', staticTimeline: true, virtualized: false,
        })
        : createElement(MessageItem, { message: summaryMessage });
      await act(async () => root.render(createElement(A2UIProvider, null, content)));
      const label = () => document.querySelector('.chat-avatar-name')?.textContent;
      assert.equal(label(), 'Summary Leader');
      assert.doesNotMatch(document.body.innerHTML, /Business Planning Leader|business-leader\.png/);

      // A late snapshot updates this Team's label without overwriting the Owner identity.
      await act(async () => applyTeamSnapshotToSession(sessionId, 'summary', {
        members: [{ member_id: 'summary-leader', name: 'Restored Summary Leader' }], tasks: [],
      }));
      assert.equal(label(), 'Restored Summary Leader');
      await act(async () => useSessionStore.getState().setTeamLeaderIdentity(`${sessionId}::summary`, {
        agentTemplateId: 'summary-template', displayName: 'Scoped Summary Leader',
      }));
      assert.equal(label(), 'Scoped Summary Leader');

      await act(async () => {
        applyTeamSnapshotToSession(sessionId, 'expert', {
          members: [{ member_id: 'expert-leader', name: 'Expert Leader' }], tasks: [],
        });
        select('expert');
      });
      assert.equal(label(), 'Expert Leader');
      await act(async () => select('owner'));
      assert.equal(label(), 'Business Planning Leader');
      await act(async () => select('summary'));
      assert.equal(label(), 'Scoped Summary Leader');

      // Simulate reloading a child conversation with only its persisted snapshot available.
      await act(async () => {
        useSessionStore.getState().setTeamLeaderIdentity(`${sessionId}::summary`, null);
        applyTeamSnapshotToSession(sessionId, 'summary', {
          members: [{ member_id: 'summary-leader', name: 'Restored Summary Leader' }], tasks: [],
        });
      });
      assert.equal(label(), 'Restored Summary Leader');
      await act(async () => setTeamOrganizationUiEnabled(false));
      assert.equal(label(), 'Business Planning Leader');
      assert.equal(useSessionStore.getState().runtimes[sessionId].agentGroupBinding, 'business-planning');
    });
  });
}
