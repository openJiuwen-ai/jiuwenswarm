import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { setImmediate as settle } from 'node:timers/promises';
import ts from 'typescript';

const entrySource = fs.readFileSync(new URL('../src/features/designer/designerEntry.ts', import.meta.url), 'utf8');
const entryTree = ts.createSourceFile('designerEntry.ts', entrySource, ts.ScriptTarget.Latest, true);
const entryScript = entryTree.statements
  .filter(node => !ts.isImportDeclaration(node))
  .map(node => node.getText(entryTree).replace(/^export /, ''))
  .join('\n');

function evaluate(source, context, result) {
  const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020 } });
  return new Function('context', `with (context) { ${outputText}; return ${result}; }`)(context);
}

// designerEntry imports the "@" mention helpers; load the real (pure) module the same way.
const mentionsSource = fs.readFileSync(new URL('../src/features/designer/designerMentions.ts', import.meta.url), 'utf8');
const mentionsTree = ts.createSourceFile('designerMentions.ts', mentionsSource, ts.ScriptTarget.Latest, true);
const mentions = evaluate(
  mentionsTree.statements
    .filter(node => !ts.isImportDeclaration(node))
    .map(node => node.getText(mentionsTree).replace(/^export /, ''))
    .join('\n'),
  {},
  '{ stripMentionAppendix, withMentionAppendix }',
);

function fixture() {
  const calls = { requests: [], navigations: [], errors: [] };
  const chat = {
    activeSessionId: 'new', runtimes: { new: { messages: [] } },
    ensureRuntime(id) { return this.runtimes[id] ??= { messages: [] }; },
    getRuntime(id) { return this.runtimes[id]; },
    setActiveSessionId(id) { this.activeSessionId = id; },
    addMessage(id, message) { this.runtimes[id].messages.push(message); },
    replaceHistoryMessages(id, messages) { this.runtimes[id].messages = messages; },
    setProcessing(id, value) { this.runtimes[id].isProcessing = value; },
    setThinking(id, value) { this.runtimes[id].isThinking = value; },
  };
  const sessions = {
    runtimes: { new: { mode: 'agent', selectedModelName: 'chosen-model' } },
    sessions: [], chatAvailableModels: [{ model_name: 'chosen-model' }], defaultModelName: 'default-model',
    ensureRuntime(id) { return this.runtimes[id] ??= { mode: 'agent' }; },
    getRuntime(id) { return this.runtimes[id]; },
    setMode(id, mode) { this.ensureRuntime(id).mode = mode; },
    setSelectedModelName(id, name) { this.runtimes[id].selectedModelName = name; },
  };
  const graph = { graph_id: 'design-graph', metadata: { session_id: 'design-session' } };
  const workspace = {
    workMode: 'design', projects: [{ project_id: 'default', is_default: true }], expandedProjectIds: {},
    toggleProjectExpanded() {}, async loadProjects() {}, async loadProjectSessions() {},
    setSelectedProject(project) { this.selectedProject = project; },
  };
  const context = {
    useChatStore: { getState: () => chat }, useSessionStore: { getState: () => sessions },
    useWorkspaceStore: { getState: () => workspace },
    resolveChatModelSelection: (_models, name, fallback) => ({ model_name: name || fallback }),
    flushApplicationConversationHistory: async () => {},
    mediaItemsToBootstrapReferences: () => ({ refs: [] }), findDesignModelsProblem: async () => null,
    ...mentions,
    designerWorkspaceClient: {
      async createSession() { return { session: { session_id: 'design-session' } }; },
      async composeSession() { return { graph, messages: [] }; },
    },
    useDesignerStore: { getState: () => ({ applyGraph() {}, async flushSave() {} }) },
    useDesignerChatStore: { getState: () => ({ replaceMessages() {} }) },
    buildConversationModelPayload: ({ selectedModelName }) => ({ model_name: selectedModelName }),
    flushApplicationConversationHistory: async () => {},
    webClient: { async request(method, params) { calls.requests.push({ method, params }); } },
  };
  const entry = evaluate(entryScript, context, '{ bindDesignerChatSession, seedDesignerChatHistory, sendDesignerLeaderMessage, submitDesignComposer }');
  return { calls, chat, sessions, workspace, context, entry };
}

test('preparing a design runtime preserves the route-selected conversation', () => {
  const { chat, sessions, entry } = fixture();
  entry.bindDesignerChatSession('design-session');
  assert.equal(chat.activeSessionId, 'new');
  assert.equal(sessions.runtimes['design-session'].mode, 'team');
  assert.deepEqual(chat.runtimes['design-session'].messages, []);
});

test('the first prompt stays on the landing until the route changes and is sent exactly once', async () => {
  const { chat, sessions, calls, entry } = fixture();
  const created = await entry.submitDesignComposer({ prompt: 'create a scene' });
  await settle();
  assert.equal(chat.activeSessionId, 'new');
  assert.deepEqual(created, { projectId: 'default', sessionId: 'design-session' });
  assert.deepEqual(chat.runtimes.new.messages, []);
  assert.equal(sessions.runtimes['design-session'].selectedModelName, 'chosen-model');
  assert.equal(chat.runtimes['design-session'].messages.length, 1);
  assert.equal(chat.runtimes['design-session'].messages[0].content, 'create a scene');
  assert.deepEqual(calls.requests, [{ method: 'chat.send', params: {
    session_id: 'design-session', query: 'create a scene', mode: 'team.design', model_name: 'chosen-model',
  } }]);
});

test('restoring design history does not steal another conversation or duplicate a live turn', () => {
  const { chat, entry } = fixture();
  const history = [{ id: 'saved', role: 'user', content: 'saved prompt', createdAt: 1 }];
  entry.seedDesignerChatHistory('design-session', history);
  entry.seedDesignerChatHistory('design-session', history);
  assert.equal(chat.activeSessionId, 'new');
  assert.equal(chat.runtimes['design-session'].messages.length, 1);
  chat.runtimes['design-session'].isProcessing = true;
  entry.seedDesignerChatHistory('design-session', [{ ...history[0], content: 'stale' }]);
  assert.equal(chat.runtimes['design-session'].messages[0].content, 'saved prompt');
});

test('a background leader error remains in its own session', async () => {
  const { chat, context, entry } = fixture();
  context.webClient.request = async () => { throw new Error('offline'); };
  await entry.sendDesignerLeaderMessage({ sessionId: 'design-session', prompt: 'continue' });
  assert.equal(chat.activeSessionId, 'new');
  assert.equal(chat.runtimes['design-session'].isProcessing, false);
  assert.match(chat.runtimes['design-session'].messages.at(-1).content, /offline/);
  assert.deepEqual(chat.runtimes.new.messages, []);
});

test('a compose failure does not activate the new session or send a leader round', async () => {
  const { chat, calls, context, entry } = fixture();
  context.designerWorkspaceClient.composeSession = async () => { throw new Error('compose failed'); };
  await assert.rejects(entry.submitDesignComposer({ prompt: 'create' }), /compose failed/);
  assert.equal(chat.activeSessionId, 'new');
  assert.deepEqual(calls.requests, []);
});

test('an existing canvas keeps its selected model and session for follow-up messages', async () => {
  const { chat, sessions, calls, entry } = fixture();
  entry.bindDesignerChatSession('design-session');
  chat.setActiveSessionId('design-session');
  sessions.setSelectedModelName('design-session', 'canvas-model');
  await entry.sendDesignerLeaderMessage({ sessionId: 'design-session', prompt: 'revise' });
  assert.equal(chat.activeSessionId, 'design-session');
  assert.equal(calls.requests[0].params.model_name, 'canvas-model');
  assert.equal(calls.requests[0].params.mode, 'team.design');
});

const appSource = fs.readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const appTree = ts.createSourceFile('App.tsx', appSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let sendNode;
function findSend(node) {
  if (ts.isVariableDeclaration(node) && node.name.getText(appTree) === 'handleSendMessage') {
    sendNode = node.initializer.arguments[0];
  }
  ts.forEachChild(node, findSend);
}
findSend(appTree);

function appSend(state) {
  const { chat, context, calls, entry } = state;
  Object.assign(context, {
    isEnterprise: () => false, route: { kind: 'chat-new' }, NEW_CONVERSATION_ID: 'new',
    sessionIdRef: { current: 'new' }, creatingSessionRef: { current: false },
    parsePersistSessionCommand: content => ({ content, persistSession: false }),
    submitDesignComposer: entry.submitDesignComposer,
    navigate: route => { calls.navigations.push(route); context.route = route; },
    toast: { open: error => calls.errors.push(error) }, t: key => key,
  });
  assert.ok(chat.runtimes.new);
  return evaluate(`const send = ${sendNode.getText(appTree)};`, context, 'send');
}

for (const slowList of ['loadProjects', 'loadProjectSessions']) {
  test(`the canvas route opens before a slow ${slowList} finishes`, async () => {
    const state = fixture();
    let release;
    state.workspace[slowList] = () => new Promise(resolve => { release = resolve; });
    const send = appSend(state);
    const pending = send('create a scene');
    try {
      for (let i = 0; i < 20 && !release; i++) await settle();
      assert.ok(release, 'the sidebar refresh must have started');
      assert.deepEqual(state.calls.navigations, [{ kind: 'design-project', projectId: 'default', sessionId: 'design-session' }]);
      assert.equal(state.chat.activeSessionId, 'new', 'only the route owner may select the new conversation');
      await send('duplicate');
      assert.equal(state.calls.requests.length, 1);
    } finally {
      release?.();
      await pending;
    }
    assert.equal(state.context.creatingSessionRef.current, false);
    assert.equal(state.chat.runtimes.new.isProcessing, false);
  });
}

test('failed creation stays on the landing and releases it for a successful retry', async () => {
  const state = fixture();
  const compose = state.context.designerWorkspaceClient.composeSession;
  state.context.designerWorkspaceClient.composeSession = async () => { throw new Error('compose failed'); };
  const send = appSend(state);
  await send('create');
  assert.deepEqual(state.calls.navigations, []);
  assert.equal(state.chat.activeSessionId, 'new');
  assert.equal(state.chat.runtimes.new.isProcessing, false);
  assert.equal(state.context.creatingSessionRef.current, false);
  assert.match(state.calls.errors[0].content, /compose failed/);
  state.context.designerWorkspaceClient.composeSession = compose;
  await send('retry');
  assert.equal(state.calls.navigations.length, 1);
  assert.equal(state.calls.requests.length, 1);
});

test('a late sidebar refresh does not overwrite a project selected after entering the canvas', async () => {
  const state = fixture();
  const target = { project_id: 'target-project', is_default: false };
  const next = { project_id: 'next-project', is_default: false };
  state.workspace.projects = [target, next];
  state.workspace.selectedProject = target;
  let release;
  state.workspace.loadProjects = () => new Promise(resolve => { release = resolve; });
  const send = appSend(state);
  const pending = send('create');
  try {
    for (let i = 0; i < 20 && !release; i++) await settle();
    assert.equal(state.calls.navigations.length, 1);
    state.workspace.setSelectedProject(next);
  } finally {
    release?.();
    await pending;
  }
  assert.equal(state.workspace.selectedProject, next);
});
