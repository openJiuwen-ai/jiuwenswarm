import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import test from 'node:test';
import { build } from 'esbuild';

const outfile = resolve('node_modules/.cache/designer-conversation/entry.mjs');
await build({
  entryPoints: ['src/features/designer/designerEntry.ts'],
  outfile, bundle: true, platform: 'node', format: 'esm', packages: 'external',
  plugins: [{ name: 'designer-conversation-ports', setup(builder) {
    builder.onResolve({ filter: /stores$|stores\/(?:chatStore|sessionStore)$|designerStore$|designerChatStore$|designerGraphClient$|services\/webClient$|ApplicationPluginOutlet$/ }, ({ path }) => ({ path, namespace: 'ports' }));
    builder.onLoad({ filter: /.*/, namespace: 'ports' }, ({ path }) => {
      let contents;
      if (path.endsWith('/stores')) contents = 'export const useWorkspaceStore = {getState: () => globalThis.designCreate.workspace};';
      else if (path.endsWith('stores/chatStore')) contents = 'export const useChatStore = {getState: () => globalThis.designCreate.chat};';
      else if (path.endsWith('stores/sessionStore')) contents = 'export const useSessionStore = {getState: () => globalThis.designCreate.session}; export const resolveChatModelSelection = () => null;';
      else if (path.endsWith('designerStore')) contents = 'export const useDesignerStore = {getState: () => ({applyGraph: graph => globalThis.designCreate.applied.push(graph)})};';
      else if (path.endsWith('designerChatStore')) contents = 'export const useDesignerChatStore = {getState: () => ({replaceMessages: (...args) => globalThis.designCreate.history.push(args)})};';
      else if (path.endsWith('designerGraphClient')) contents = 'export const designerWorkspaceClient = {createSession: params => globalThis.designCreate.create(params)};';
      else if (path.endsWith('ApplicationPluginOutlet')) contents = 'export const flushApplicationConversationHistory = async () => {};';
      else contents = 'export const webRequest = () => {throw new Error("Must not send a fabricated prompt")}; export const webClient = {request: webRequest};';
      return { contents, loader: 'js' };
    });
  } }],
});
const { createDesignerConversation } = await import(pathToFileURL(outfile));

function fixture() {
  const events = [];
  const state = {
    workspace: { selectedProject: null, projects: [] },
    chat: { ensureRuntime: id => events.push(['ensure', id]), setActiveSessionId: id => events.push(['active', id]) },
    session: { getRuntime: () => null, setMode: (...args) => events.push(['mode', ...args]) },
    applied: [], history: [], events,
    create: async params => {
      events.push(['create', params]);
      return {session: {session_id: 'design-new'}, graph: {graph_id: 'canvas', metadata: {session_id: 'design-new'}, nodes: []}, messages: []};
    },
  };
  globalThis.designCreate = state;
  return state;
}

test('Design duplex creates and binds an empty saved canvas without sending a user prompt', async () => {
  const state = fixture();
  try {
    assert.deepEqual(await createDesignerConversation(), {projectId: 'default', sessionId: 'design-new'});
    assert.deepEqual(state.events, [['create', {projectId: 'default', title: ''}], ['ensure', 'design-new'], ['mode', 'design-new', 'team'], ['active', 'design-new']]);
    assert.equal(state.applied[0].metadata.session_id, 'design-new');
    assert.deepEqual(state.history, [['canvas', []]]);
  } finally { delete globalThis.designCreate; }
});

test('Design duplex retains the selected Design project', async () => {
  const state = fixture();
  state.workspace.selectedProject = {project_id: 'design-project', work_mode: 'design'};
  try {
    assert.equal((await createDesignerConversation('Voice task')).projectId, 'design-project');
    assert.deepEqual(state.events[0], ['create', {projectId: 'design-project', title: 'Voice task'}]);
  } finally { delete globalThis.designCreate; }
});

test('failed or unbound Design creation cannot activate a conversation', async () => {
  const state = fixture();
  try {
    state.create = async () => { throw new Error('Save failed'); };
    await assert.rejects(createDesignerConversation(), /Save failed/);
    state.create = async () => ({session: {session_id: 'design-new'}, graph: {metadata: {session_id: 'other'}}});
    await assert.rejects(createDesignerConversation(), /missing design session/);
    assert.deepEqual(state.events, []);
    assert.deepEqual(state.applied, []);
  } finally { delete globalThis.designCreate; }
});

test('application session creation routes Design before generic session.create', () => {
  const app = readFileSync('src/App.tsx', 'utf8');
  const start = app.indexOf('const ensureApplicationPluginSession');
  const end = app.indexOf('const handleUseAgentGroup', start);
  const entry = app.slice(start, end);
  assert.ok(entry.indexOf("workContext.work_mode === 'design'") < entry.indexOf('createConversationSession('));
  assert.match(entry, /await createDesignerConversation\(initialTitle\)/);
  assert.match(entry, /navigate\(\{ kind: 'design-project', projectId: created.projectId, sessionId: created.sessionId \}\)/);
  assert.match(entry, /finally\s*\{\s*creatingSessionRef.current = false/);
});
