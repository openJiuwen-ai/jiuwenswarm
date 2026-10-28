import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { build } from 'esbuild';

// Execute App's actual send callback with its network and navigation boundaries replaced.
const source = fs.readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const marker = 'const handleSendMessage = useCallback(';
const start = source.indexOf(marker) + marker.length;
const end = source.indexOf(
  '}, [disposeInFlightHistoryHandles, mode, navigate, request, sendMessage, setGoalObjective, t]);', start,
);
assert.ok(start >= marker.length && end > start, 'App send callback is present');
const dependencyNames = [
  'sessionIdRef', 'creatingSessionRef', 'useSessionStore', 'useChatStore', 'useWorkspaceStore',
  'submitDesignComposer', 'navigate', 'toast', 't', 'window', 'sendMessage', 'disposeInFlightHistoryHandles',
];
await build({
  stdin: {
    contents: `
      import { isConversationModelGroupBlocked } from './src/multi-session/state/conversationModelSelection';
      import { parsePersistSessionCommand } from './src/multi-session/state/createConversationSession';
      import { isMultiModelCollaborationEnabled } from './src/features/multiModelCollaboration/featureConfig';
      export { setMultiModelCollaborationEnabled } from './src/features/multiModelCollaboration/featureConfig';
      const NEW_CONVERSATION_ID = 'new';
      export function createAppSend({ ${dependencyNames.join(', ')} }) {
        return ${source.slice(start, end + 1)};
      }
    `,
    resolveDir: process.cwd(),
    loader: 'ts',
  },
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outfile: 'node_modules/.cache/app-designer-moa-routing.mjs',
});
const { createAppSend, setMultiModelCollaborationEnabled } =
  await import('../node_modules/.cache/app-designer-moa-routing.mjs');

function fixture({ workMode = 'design', sessionId = 'new', mode = 'agent', cronId = null, enabled = false } = {}) {
  setMultiModelCollaborationEnabled(enabled);
  const selection = { type: 'model_group', id: 'saved-group' };
  const runtime = { mode, selectedModelSelection: selection };
  const metadata = { session_id: sessionId, cron_id: cronId };
  const processing = { isProcessing: false };
  const calls = { designs: [], sends: [], navigation: [], errors: [], alerts: [], processing: [] };
  const creatingSessionRef = { current: false };
  const created = { projectId: 'design-project', sessionId: 'design-session' };
  const workspace = {
    workMode,
    expandedProjectIds: {},
    projects: [{ project_id: created.projectId, is_default: false }],
    toggleProjectExpanded: (id) => { workspace.expandedProjectIds[id] = true; },
    loadProjects: async () => {},
    loadProjectSessions: async (id) => assert.equal(id, created.projectId),
    setSelectedProject: (project) => { workspace.selectedProject = project; },
    refreshSessionWorkspace: async (session) => assert.equal(session, metadata),
  };
  const dependencies = {
    sessionIdRef: { current: sessionId },
    creatingSessionRef,
    useSessionStore: { getState: () => ({ getRuntime: () => runtime, currentSession: metadata, sessions: [] }) },
    useChatStore: { getState: () => ({
      getRuntime: () => processing,
      setProcessing: (id, value) => {
        assert.equal(id, sessionId);
        processing.isProcessing = value;
        calls.processing.push(value);
      },
    }) },
    useWorkspaceStore: { getState: () => workspace },
    submitDesignComposer: async (request) => {
      calls.designs.push(request);
      return created;
    },
    navigate: (route) => calls.navigation.push(route),
    toast: { open: (error) => calls.errors.push(error) },
    t: (key) => key,
    window: { alert: (message) => calls.alerts.push(message) },
    sendMessage: async (...args) => { calls.sends.push(args); return true; },
    disposeInFlightHistoryHandles: (id) => assert.equal(id, sessionId),
  };
  return { dependencies, calls, created, creatingSessionRef, processing, runtime, selection, workspace };
}

test('Designer ignores a retained model group, forwards media, and rejects duplicate submissions', async () => {
  const state = fixture();
  let finish;
  const pending = new Promise((resolve) => { finish = resolve; });
  state.dependencies.submitDesignComposer = (request) => {
    state.calls.designs.push(request);
    return pending;
  };
  const send = createAppSend(state.dependencies);
  const mediaItems = [{ type: 'video', filename: 'reference.mp4', path: '/reference.mp4' }];
  const first = send('create a scene', mediaItems);
  try {
    assert.equal(state.processing.isProcessing, true);
    assert.equal(state.creatingSessionRef.current, true);
    await send('duplicate prompt');
    assert.deepEqual(state.calls.designs, [{ prompt: 'create a scene', mediaItems }]);
    assert.deepEqual(state.calls.sends, []);
    assert.deepEqual(state.calls.errors, []);
  } finally {
    finish(state.created);
    await first;
  }
  assert.equal(state.processing.isProcessing, false);
  assert.equal(state.creatingSessionRef.current, false);
  assert.deepEqual(state.calls.navigation, [{ kind: 'design-project', ...state.created }]);
  assert.equal(state.workspace.selectedProject.project_id, state.created.projectId);
  assert.deepEqual(state.runtime.selectedModelSelection, state.selection);
});

test('a failed Designer submission releases the draft for retry without changing its selection', async () => {
  const state = fixture();
  let attempts = 0;
  state.dependencies.submitDesignComposer = async () => {
    attempts += 1;
    if (attempts === 1) throw new Error('video_limit');
    return state.created;
  };
  const send = createAppSend(state.dependencies);
  await send('create a scene');
  assert.deepEqual(state.calls.alerts, ['designer.chat.limitVideo']);
  assert.equal(state.processing.isProcessing, false);
  assert.equal(state.creatingSessionRef.current, false);
  await send('retry scene');
  assert.equal(attempts, 2);
  assert.equal(state.calls.navigation.length, 1);
  assert.deepEqual(state.runtime.selectedModelSelection, state.selection);
});

for (const workMode of ['work', 'code']) {
  test(`${workMode} drafts still block model groups when collaboration is disabled`, async () => {
    const state = fixture({ workMode });
    await createAppSend(state.dependencies)('ordinary draft');
    assert.equal(state.calls.errors.length, 1);
    assert.deepEqual(state.calls.designs, []);
    assert.deepEqual(state.calls.sends, []);
    assert.deepEqual(state.calls.processing, []);
    assert.equal(state.creatingSessionRef.current, false);
    assert.deepEqual(state.runtime.selectedModelSelection, state.selection);
  });

  for (const enabled of [false, true]) {
    test(`${workMode} existing sessions respect collaboration=${enabled}`, async () => {
      const state = fixture({ workMode, sessionId: 'existing-session', enabled });
      await createAppSend(state.dependencies)('next message');
      assert.equal(state.calls.sends.length, enabled ? 1 : 0);
      assert.equal(state.calls.errors.length, enabled ? 0 : 1);
      assert.deepEqual(state.calls.designs, []);
    });
  }
}

for (const settings of [{ mode: 'team.design' }, { cronId: 'scheduled-task' }]) {
  test(`existing ${settings.mode ?? 'cron'} sessions retain their routing`, async () => {
    const state = fixture({ sessionId: 'existing-session', ...settings });
    await createAppSend(state.dependencies)('next message');
    assert.equal(state.calls.sends.length, 1);
    assert.deepEqual(state.calls.errors, []);
    assert.deepEqual(state.calls.designs, []);
  });
}
