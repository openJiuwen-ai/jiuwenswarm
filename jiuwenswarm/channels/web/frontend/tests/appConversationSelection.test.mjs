import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { build } from 'esbuild';

// Exercise the production navigation effect alongside the real draft/session store boundary.
const source = fs.readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const marker = "  useEffect(() => {\n    if (activeNav === 'chat') {";
const start = source.indexOf(marker) + '  useEffect('.length;
const end = source.indexOf('}, [activeNav, sessionId]);', start);
assert.ok(start >= '  useEffect('.length && end > start, 'chat navigation effect is present');
const callback = source.slice(start, end + 1);
await build({
  stdin: {
    contents: `
      import { useSessionStore } from './src/stores/sessionStore';
      export { useSessionStore };
      export { resetNewConversationRuntime, registerCreatedConversation, forgetCreatedConversation }
        from './src/multi-session/state/newConversationLifecycle';
      export { setMultiModelCollaborationEnabled } from './src/features/multiModelCollaboration/featureConfig';
      export function createChatNavigationEffect(activeNav, sessionId) { return ${callback}; }
    `,
    resolveDir: process.cwd(),
    loader: 'ts',
  },
  outfile: 'node_modules/.cache/app-conversation-selection.mjs',
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  define: { 'import.meta.env': '{"DEV":false}' },
});
const {
  useSessionStore, resetNewConversationRuntime, registerCreatedConversation,
  forgetCreatedConversation, setMultiModelCollaborationEnabled, createChatNavigationEffect,
} = await import('../node_modules/.cache/app-conversation-selection.mjs');
const models = [
  { model_id: 'mdl-glm', model_name: 'glm-5.3', alias: 'GLM', is_default: true },
  { model_id: 'mdl-deepseek', model_name: 'deepseek-v4.1-flash', alias: 'DeepSeek', is_default: false },
];
const group = { model_group_id: 'mgp-selected', display_name: 'Test collaboration', enabled: true,
  routes: [{ route_id: 'r-glm', model_id: 'mdl-glm' }, { route_id: 'r-deepseek', model_id: 'mdl-deepseek' }] };
const selection = { type: 'model_group', id: 'mgp-selected' };

async function fixture(run) {
  const previous = useSessionStore.getState();
  const createdIds = [];
  try {
    useSessionStore.getState().setAvailableModels(models, 'glm-5.3');
    useSessionStore.getState().setAvailableModelGroups([group]);
    await run((id) => {
      createdIds.push(id);
      return registerCreatedConversation(id, { mode: 'agent', selectedModelName: null,
        selectedModelSelection: selection }, Date.now(), 'read the test ledger');
    });
  } finally {
    createdIds.forEach(forgetCreatedConversation);
    useSessionStore.setState(previous, true);
    setMultiModelCollaborationEnabled(false);
  }
}

test('a first-message group survives draft-to-real navigation and returning from settings after a catalog update', async () => fixture(async (create) => {
  setMultiModelCollaborationEnabled(true);
  resetNewConversationRuntime({ mode: 'agent', selectedModelName: null, selectedModelSelection: selection });
  createChatNavigationEffect('chat', 'new')();
  assert.deepEqual(useSessionStore.getState().getRuntime('new').selectedModelSelection, selection);
  create('first-group-message');
  createChatNavigationEffect('chat', 'first-group-message')();
  assert.deepEqual(useSessionStore.getState().getRuntime('first-group-message').selectedModelSelection, selection);

  useSessionStore.getState().setAvailableModels(models, 'deepseek-v4.1-flash');
  useSessionStore.getState().setAvailableModelGroups([{ ...group, routes: [...group.routes].reverse() }]);
  createChatNavigationEffect('chat', 'first-group-message')();
  assert.deepEqual(useSessionStore.getState().getRuntime('first-group-message').selectedModelSelection, selection);
  assert.deepEqual(useSessionStore.getState().getOutgoingModelParams('first-group-message'), { model_selection: selection });
}));

test('turning collaboration off never replaces the existing group when returning to chat', async () => fixture(async (create) => {
  create('disabled-group-chat');
  setMultiModelCollaborationEnabled(false);
  createChatNavigationEffect('chat', 'disabled-group-chat')();
  assert.deepEqual(useSessionStore.getState().getRuntime('disabled-group-chat').selectedModelSelection, selection);
  assert.equal(useSessionStore.getState().getRuntime('disabled-group-chat').selectedModelName, null);
}));

test('group metadata survives refresh and fork even when the execution model is the ordinary default', async () => fixture(async (create) => {
  const created = create('refresh-group-chat');
  useSessionStore.getState().removeRuntime(created.session_id);
  useSessionStore.getState().setCurrentSession({ ...created, model: 'glm-5.3', moa_enabled: true });
  createChatNavigationEffect('chat', created.session_id)();
  assert.deepEqual(useSessionStore.getState().getRuntime(created.session_id).selectedModelSelection, selection);
  useSessionStore.getState().setCurrentSession({ ...created, session_id: 'fork-group-chat', model: 'glm-5.3', moa_enabled: true });
  createChatNavigationEffect('chat', 'fork-group-chat')();
  assert.deepEqual(useSessionStore.getState().getRuntime('fork-group-chat').selectedModelSelection, selection);
}));

test('a truly unselected chat still receives the ordinary default stable model', async () => fixture(async () => {
  useSessionStore.getState().ensureRuntime('unselected-chat');
  useSessionStore.getState().setSelectedModelName('unselected-chat', null);
  useSessionStore.getState().setSelectedModelSelection('unselected-chat', null);
  createChatNavigationEffect('chat', 'unselected-chat')();
  assert.deepEqual(useSessionStore.getState().getRuntime('unselected-chat').selectedModelSelection, { type: 'model', id: 'mdl-glm' });
}));
