import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { setImmediate as settle } from 'node:timers/promises';
import ts from 'typescript';
import { transform } from 'esbuild';

const source = fs.readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const tree = ts.createSourceFile('App.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);

function findNode(predicate) {
  let found;
  function visit(node) {
    if (predicate(node)) found = node;
    ts.forEachChild(node, visit);
  }
  visit(tree);
  assert.ok(found, 'production callback must exist');
  return found;
}

async function evaluate(node, context) {
  const { code } = await transform(`const run = ${node.getText(tree)};`, { loader: 'tsx', target: 'es2020' });
  return new Function('context', `with (context) { ${code}; return run; }`)(context);
}

function callback(name, context) {
  const node = findNode(node => ts.isVariableDeclaration(node) && node.name.getText(tree) === name);
  return evaluate(node.initializer.arguments[0], context);
}

function effect(containing, context) {
  const node = findNode(node => ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect'
    && node.arguments[0].getText(tree).includes(containing));
  return evaluate(node.arguments[0], context);
}

function designFixture(enterprise = false) {
  const calls = { design: [], ordinary: [], navigations: [], errors: [], processing: [] };
  const runtime = { mode: 'agent', agentGroupSelectionIntent: { kind: 'keep' } };
  const workspace = {
    workMode: 'design', expandedProjectIds: {}, projects: [],
    toggleProjectExpanded() {}, async loadProjects() {}, async loadProjectSessions() {},
  };
  const noop = () => {};
  const context = {
    isEnterprise: () => enterprise,
    route: { kind: 'chat-new' },
    sessionIdRef: { current: 'new' }, creatingSessionRef: { current: false }, NEW_CONVERSATION_ID: 'new',
    useSessionStore: { getState: () => ({ getRuntime: () => runtime, sessions: [], getEffectiveModelName: () => 'test-model' }) },
    useChatStore: { getState: () => ({ getRuntime: () => ({}),
      setProcessing: (...args) => calls.processing.push(args), setThinking: noop }) },
    useWorkspaceStore: { getState: () => workspace },
    usePlanStore: { getState: () => ({ isActive: () => false }) },
    parsePersistSessionCommand: content => ({ content, persistSession: false }),
    submitDesignComposer: async input => {
      calls.design.push(input);
      return { projectId: 'design-project', sessionId: 'design-session' };
    },
    getWorkContextForSession: () => ({ work_mode: 'design' }),
    generateUuidV4: () => 'test-view', resolvePlanWireMode: mode => mode,
    createConversationTitle: text => text, mode: 'agent',
    sessionViewIdRef: { current: 'test-view' }, newConversationPreviousSessionRef: { current: null },
    createConversationSession: async (_request, params) => {
      calls.ordinary.push(params);
      throw new Error('stop at ordinary network boundary');
    },
    request: noop, restoreUnsentInput: noop, navigate: route => calls.navigations.push(route),
    toast: { open: error => calls.errors.push(error) }, t: text => text,
    window: { alert: noop }, console: { error: noop },
  };
  return { calls, context };
}

test('personal Design first submission creates a design session and opens its canvas', async () => {
  const { calls, context } = designFixture();
  const send = await callback('handleSendMessage', context);
  const references = [{ type: 'image', filename: 'reference.png' }];
  await send('design a scene', references);
  assert.deepEqual(calls.design, [{ prompt: 'design a scene', mediaItems: references }]);
  assert.deepEqual(calls.ordinary, []);
  assert.deepEqual(calls.navigations, [{ kind: 'design-project', projectId: 'design-project', sessionId: 'design-session' }]);
  assert.equal(context.creatingSessionRef.current, false);
  assert.deepEqual(calls.processing.at(-1), ['new', false]);
});

test('Design creation failure clears the composer busy state without creating an ordinary session', async () => {
  const { calls, context } = designFixture();
  context.submitDesignComposer = async () => { throw new Error('image_limit'); };
  await (await callback('handleSendMessage', context))('design a scene');
  assert.equal(calls.errors[0].content, 'designer.chat.limitImages');
  assert.equal(context.creatingSessionRef.current, false);
  assert.deepEqual(calls.processing.at(-1), ['new', false]);
  assert.deepEqual(calls.ordinary, []);
});

test('enterprise does not enter Design creation even with a stale design workspace value', async () => {
  const { calls, context } = designFixture(true);
  context.isConversationModelGroupBlocked = () => false;
  context.isMultiModelCollaborationEnabled = () => false;
  await (await callback('handleSendMessage', context))('ordinary enterprise chat');
  assert.deepEqual(calls.design, []);
  assert.equal(calls.ordinary.length, 1);
});

test('Team attachment waits for connection and restored Team metadata', async () => {
  const calls = [];
  const context = { isConnected: false, mode: 'agent', sessionId: 'team-session',
    isTeamAgentMode: mode => mode === 'team', attachTeamSession: async (...args) => calls.push(args) };
  const attach = await effect('void attachTeamSession(', context);
  attach();
  context.isConnected = true;
  attach();
  assert.deepEqual(calls, []);
  context.mode = 'team';
  attach();
  await Promise.resolve();
  assert.deepEqual(calls, [['team-session', 'team']]);
});

test('connection readiness resets Team subscriptions and retains Cron list reload', async () => {
  const calls = [];
  const node = findNode(node => ts.isPropertyAssignment(node) && node.name.getText(tree) === 'onConnect');
  const connect = await evaluate(node.initializer, {
    resetTeamAttachments: () => calls.push('reset'),
    useCronStore: { getState: () => ({ loadJobs: async () => calls.push('cron') }) },
    console: { log() {} },
  });
  connect();
  connect();
  assert.deepEqual(calls, ['reset', 'cron', 'reset', 'cron']);
});

function shareFixture() {
  const calls = { requests: [], remembered: [], monitored: [] };
  const status = { job_id: 'a'.repeat(32), state: 'queued', renderer: 'browser', filename: 'share.png', client_token: 'b'.repeat(64) };
  const context = {
    isEnterprise: () => false, sessionIdRef: { current: 'share-session' }, NEW_CONVERSATION_ID: 'new',
    shareExportMonitorTokensRef: { current: new Map() }, waitingBrowserExportsRef: { current: new Map() },
    isProcessing: false, isPaused: false, setShareExportSessionActive() {}, setShareExportProgress() {},
    findShareImageJobForSession: async () => null, readShareImageJobResponse: async () => status,
    rememberPendingShareImageJob: (...args) => calls.remembered.push(args),
    monitorAndSaveShareImageJob: async (...args) => calls.monitored.push(args),
    window: { sessionStorage: {}, alert() {} }, fetch: async (...args) => { calls.requests.push(args); return {}; },
    t: text => text, i18n: { language: 'en', resolvedLanguage: 'en' },
  };
  return { calls, status, context };
}

test('plain Web enables browser rendering and persists its authorization for refresh recovery', async () => {
  const { calls, status, context } = shareFixture();
  await (await callback('handleExportShare', context))();
  assert.equal(JSON.parse(calls.requests[0][1].body).allow_browser_render, true);
  assert.equal(calls.remembered[0][2], status);
  assert.equal(calls.monitored[0][3], true);
});

test('a blocked export popup can be reopened without creating another export job', async () => {
  const { calls, status, context } = shareFixture();
  const tab = {};
  context.waitingBrowserExportsRef.current.set('share-session', { status, locale: 'en' });
  context.openShareImageExportTab = () => tab;
  context.browserExportTabsRef = { current: new Map() };
  context.setWaitingShareExportSessionIds = () => {};
  await (await callback('handleExportShare', context))();
  assert.equal(context.browserExportTabsRef.current.get('share-session'), tab);
  assert.equal(context.waitingBrowserExportsRef.current.size, 0);
  assert.deepEqual(calls.requests, []);
});

test('enterprise export and recovery do not start browser work or background queries', async () => {
  const calls = [];
  const record = name => (...args) => { calls.push([name, args]); return Promise.resolve(null); };
  const context = {
    ...shareFixture().context, isEnterprise: () => true, sessionId: 'enterprise-session',
    readPendingShareImageJobId: () => null,
    findShareImageJobForSession: record('find'), fetch: record('fetch'),
    monitorAndSaveShareImageJob: record('monitor'), rememberPendingShareImageJob: record('remember'),
    openShareImageExportTab: record('open'),
  };
  await (await callback('handleExportShare', context))();
  (await effect('findShareImageJobForSession(targetSessionId', context))();
  await settle();
  await settle();
  assert.deepEqual(calls, []);
});

test('Design routing keeps the real canvas session and does not persist a sidebar preference', async () => {
  const calls = [];
  const context = {
    route: { kind: 'design-project', projectId: 'project', sessionId: 'canvas' },
    isEnterprise: () => false, sessionIdRef: { current: 'new' },
    setSessionId: value => calls.push(['session', value]), setActiveNav() {},
    setDesignSidebarCollapsed: value => calls.push(['canvas-sidebar', value]),
    setSecondarySidebarCollapsed: value => calls.push(['persistent-sidebar', value]),
    setTeamAreaExpanded() {}, setSingleAgentPanelExpanded() {}, setToolPanelHidden() {},
    useWorkspaceStore: { getState: () => ({ setWorkMode: async () => {}, projects: [], setSelectedProject() {} }) },
  };
  const dispose = (await effect('setDesignSidebarCollapsed(true)', context))();
  await settle();
  assert.equal(context.sessionIdRef.current, 'canvas');
  assert.deepEqual(calls, [['session', 'canvas'], ['canvas-sidebar', true]]);
  dispose();
});

test('selecting a design session does not invoke the ordinary chat route restore', async () => {
  const calls = [];
  const select = await callback('handleSelectDesignSession', {
    confirmLeaveSettings: () => true, isToolPanelAutoHideViewport: false,
    setSelectedProject() {}, setActiveNav() {},
    navigate: route => calls.push(['navigate', route]),
    handleRestoreSession: sid => calls.push(['ordinary-restore', sid]),
  });
  select({ project_id: 'project', is_default: false }, 'canvas');
  assert.deepEqual(calls, [['navigate', { kind: 'design-project', projectId: 'project', sessionId: 'canvas' }]]);
});

test('the canvas uses its own history loader instead of restoring ordinary chat history', async () => {
  const run = await effect('The canvas restores its own director history', { route: { kind: 'design-project' } });
  assert.equal(run(), undefined);
});

test('canvas sidebar toggles never overwrite the ordinary sidebar preference', async () => {
  for (const designCanvasOpen of [true, false]) {
    const calls = [];
    const toggle = await callback('toggleConversationSidebar', {
      designCanvasOpen, closeSidebarPeek() {},
      setDesignSidebarCollapsed: update => calls.push(['canvas', update(true)]),
      setSecondarySidebarCollapsed: update => calls.push(['ordinary', update(true)]),
    });
    toggle();
    assert.deepEqual(calls, [[designCanvasOpen ? 'canvas' : 'ordinary', false]]);
  }
});

test('pinning the canvas sidebar also leaves the ordinary sidebar preference unchanged', async () => {
  const node = findNode(node => ts.isVariableDeclaration(node) && node.name.getText(tree) === 'pinSecondarySidebar');
  const calls = [];
  const pin = await evaluate(node.initializer, {
    designCanvasOpen: true, handleNavigate: () => true, activeNav: 'chat', sidebarAutoCollapsed: false,
    setDesignSidebarCollapsed: value => calls.push(['canvas', value]),
    setSecondarySidebarCollapsed: value => calls.push(['ordinary', value]),
  });
  pin('chat');
  assert.deepEqual(calls, [['canvas', false]]);
});

async function shareWait(context) {
  const node = findNode(node => ts.isFunctionDeclaration(node) && node.name?.text === 'waitForShareImageJob');
  return evaluate(node, {
    SHARE_IMAGE_EXPORT_POLL_MS: 0, SHARE_IMAGE_EXPORT_MAX_QUERY_FAILURES: 3,
    window: { setTimeout: resolve => setTimeout(resolve, 0) }, ...context,
  });
}

test('repeated share query network failures stop with a retryable timeout', async () => {
  let attempts = 0;
  const wait = await shareWait({ fetchShareImageJobStatus: async () => { attempts++; throw new TypeError('offline'); } });
  await assert.rejects(wait({ state: 'queued' }, () => true, async status => status, () => {}), /share_export_status_timeout/);
  assert.equal(attempts, 3);
});

test('a successful share status query resets the consecutive failure budget', async () => {
  let attempts = 0;
  const wait = await shareWait({ fetchShareImageJobStatus: async () => {
    attempts++;
    if (attempts % 3) throw new TypeError('temporary interruption');
    return { state: attempts === 6 ? 'completed' : 'running' };
  } });
  const result = await wait({ state: 'queued' }, () => true, async status => status, () => {});
  assert.equal(result.state, 'completed');
  assert.equal(attempts, 6);
});

test('share export and recovery share the same known-error mapping with a safe fallback', async () => {
  const declaration = findNode(node => ts.isVariableDeclaration(node) && node.name.getText(tree) === 'SHARE_IMAGE_EXPORT_ERROR_KEYS');
  const keys = await evaluate(declaration.initializer, {});
  const node = findNode(node => ts.isFunctionDeclaration(node) && node.name?.text === 'shareImageExportErrorKey');
  const keyFor = await evaluate(node, { SHARE_IMAGE_EXPORT_ERROR_KEYS: keys });
  for (const [message, expected] of Object.entries(keys)) assert.equal(keyFor(new Error(message)), expected);
  assert.equal(keyFor(new Error('unknown')), 'share.exportFailed');
  assert.equal(keyFor(new Error('toString')), 'share.exportFailed');
  assert.equal(keyFor(null), 'share.exportFailed');
});
