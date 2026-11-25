import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import { createPresentationRequests, parsePresentationRequest, conversationActivity, presentationConversations, useChatStore,
  useSessionStore, useWorkspaceStore, ApplicationPresentationHost, usePresentationHost, WebUIProvider, componentCatalog,
  createRendererRegistry, createWebUIRuntime, createMockTransport, createPresentationReplyReader, lastPresentationReply, lastPresentationUserMessage, selectConversationModel, webClient,
  WebUIApplicationSurface,
} from '../node_modules/.cache/application-presentation/protocol.mjs';

const request = (id, action = 'send', params = { conversationKey: 'session::team', text: 'hello' }) => ({
  protocol: 'workswarm.presentation', version: 1, type: 'request', id, action, params,
});

test('presentation only publishes normalized last-user-message times without imposing a recent order', () => {
  const sessions = [
    { session_id: 'pinned', mode: 'agent', last_user_message_at: 1794210000 },
    { session_id: 'recent', mode: 'agent', last_user_message_at: 1794215000123 },
    { session_id: 'unknown', mode: 'agent' },
  ];
  const result = presentationConversations(sessions, {}, 'New');
  assert.deepEqual(result.map(item => item.conversationKey), ['new', 'pinned', 'recent', 'unknown']);
  assert.deepEqual(result.map(item => item.lastUserMessageAt), [null, 1794210000000, 1794215000123, null]);
  assert.equal(sessions[0].last_user_message_at, 1794210000);
});

test('the complete final is read losslessly in bounded replies without restoring history', () => {
  const store = useChatStore.getState(); store.ensureRuntime('final');
  const content = ('完整 chat.final 🐝\n\u0000').repeat(24000);
  store.addMessage('final', { id: 'segment', role: 'assistant', content: 'a deduplicated tail', timestamp: '1' });
  store.setLastFinalReply('final', content);
  const reader = createPresentationReplyReader();
  const replyId = reader.identify(lastPresentationReply(useChatStore.getState().runtimes.final));
  const parts = []; let offset = 0;
  do {
    const result = reader.read(useChatStore.getState().runtimes.final, replyId, offset);
    assert.ok(Buffer.byteLength(JSON.stringify(result)) < 256 * 1024);
    parts.push(result.content); offset = result.nextOffset;
  } while (offset !== null);
  assert.equal(parts.join(''), content);
  store.setLastFinalReply('final', 'new final');
  assert.throws(() => reader.read(useChatStore.getState().runtimes.final, replyId, 0), /REPLY_CHANGED/);
  store.clearMessages('final');
  assert.equal(lastPresentationReply(useChatStore.getState().runtimes.final), null);
  for (const action of ['history', 'interaction', 'answer', 'authorize']) {
    assert.equal(parsePresentationRequest(request(1, action, { conversationKey: 'final' })), null);
  }
});

test('only the most recent user message is readable and replacements invalidate old chunks', () => {
  const store = useChatStore.getState(); store.ensureRuntime('user-last');
  store.addMessage('user-last', { id: 'u1', role: 'user', content: 'first', timestamp: '1' });
  const reader = createPresentationReplyReader();
  const firstId = reader.identify(lastPresentationUserMessage(useChatStore.getState().runtimes['user-last']));
  const full = '后续问题🐝'.repeat(20000);
  store.addMessage('user-last', { id: 'u2', role: 'user', content: full, timestamp: '2' });
  const runtime = useChatStore.getState().runtimes['user-last'];
  const id = reader.identify(lastPresentationUserMessage(runtime));
  const summary = presentationConversations([{ session_id: 'user-last', mode: 'agent', title: 'first' }], { 'user-last': runtime }, 'New', reader.identify).find(item => item.sessionId === 'user-last');
  assert.equal(summary.userMessageId, id); assert.ok(summary.latestUserMessage.endsWith('…'));
  assert.throws(() => reader.read(runtime, firstId, 0, 'user'), /REPLY_CHANGED/);
  let offset = 0; const parts = [];
  do { const chunk = reader.read(runtime, id, offset, 'user'); parts.push(chunk.content); offset = chunk.nextOffset; } while (offset !== null);
  assert.equal(parts.join(''), full);
  assert.ok(parsePresentationRequest(request(1, 'reply', { conversationKey: 'user-last', replyId: id, offset: 0, role: 'user' })));
  assert.equal(parsePresentationRequest(request(1, 'reply', { conversationKey: 'user-last', replyId: id, offset: 0, role: 'system' })), null);
});

test('workbench and forms share model validation and stable selection persistence', async () => {
  const original = webClient.request;
  const calls = [];
  webClient.request = async (method, params) => { calls.push({ method, params }); return { success: true }; };
  try {
    useSessionStore.getState().ensureRuntime('model-session');
    useSessionStore.getState().ensureRuntime('new');
    useChatStore.getState().ensureRuntime('model-session');
    useSessionStore.setState({ chatAvailableModels: [{ model_id: 'm1', model_name: 'Model one' }] });
    await selectConversationModel('model-session', { type: 'model', id: 'm1' });
    assert.deepEqual(calls, [{ method: 'session.selection.set', params: { session_id: 'model-session', model_selection: { type: 'model', id: 'm1' } } }]);
    await selectConversationModel('new', { type: 'model', id: 'm1' });
    assert.equal(calls.length, 1);
    useChatStore.getState().setProcessing('model-session', true);
    await assert.rejects(selectConversationModel('model-session', { type: 'model', id: 'm1' }), /CONVERSATION_BUSY/);
    useChatStore.getState().setProcessing('model-session', false);
    await assert.rejects(selectConversationModel('model-session', { type: 'model', id: 'removed' }), /MODEL_UNAVAILABLE/);
    assert.equal(calls.length, 1);
  } finally { webClient.request = original; }
});

test('host retains completed replies with input ownership independently of presentation state', () => {
  const store = useChatStore.getState(); store.ensureRuntime('round-pair');
  const runtime = () => useChatStore.getState().runtimes['round-pair'];
  const reader = createPresentationReplyReader();
  const snapshot = () => presentationConversations([{ session_id: 'round-pair', mode: 'agent' }],
    { 'round-pair': runtime() }, 'New', reader.identify).find(item => item.sessionId === 'round-pair');
  store.addMessage('round-pair', { id: 'u1', role: 'user', content: 'same question', timestamp: '1', inputRequestId: 'r1' });
  store.setLastFinalReply('round-pair', 'first answer', 'r1');
  const first = lastPresentationReply(runtime()); const firstId = reader.identify(first);
  assert.equal(first.content, 'first answer');
  store.addMessage('round-pair', { id: 'u2', role: 'user', content: 'same question', timestamp: '2', inputRequestId: null });
  assert.equal(runtime().lastFinalReply, first);
  assert.equal(reader.read(runtime(), firstId, 0).content, 'first answer');
  assert.equal(snapshot().inputMessageId, 'u2');
  assert.equal(snapshot().replyToMessageId, 'u1');
  assert.equal(snapshot().inputOutcome, null);
  store.setLastFinalReply('round-pair', 'old final while preparing the new request', 'r1');
  assert.equal(snapshot().replyToMessageId, 'u1');
  store.updateMessage('round-pair', 'u2', { inputRequestId: 'r2' });
  store.addMessage('round-pair', { id: 'late-old', role: 'assistant', content: 'late old segment', timestamp: '3', outputOrder: { requestId: 'r1', sequence: 9 } });
  store.setLastFinalReply('round-pair', 'late old complete answer', 'r1');
  assert.equal(lastPresentationReply(runtime()).content, 'late old complete answer');
  assert.equal(snapshot().replyToMessageId, 'u1');
  store.setProcessing('round-pair', true);
  store.setLastFinalReply('round-pair', 'second complete answer', 'r2');
  const second = lastPresentationReply(runtime()); const secondId = reader.identify(second);
  assert.equal(snapshot().replyToMessageId, 'u2');
  assert.equal(snapshot().activity, 'replying');
  for (const flags of [{ isProcessing: true }, { isThinking: true }, { isPaused: true },
    { error: 'request failed' }, { pendingQuestions: [{}] }]) {
    assert.equal(reader.read({ ...runtime(), ...flags }, secondId, 0).content, 'second complete answer');
  }
  store.setProcessing('round-pair', false);
  store.setLastFinalReply('round-pair', 'older input cannot replace the newer completed result', 'r1');
  assert.equal(lastPresentationReply(runtime()), second);
  assert.equal(conversationActivity(runtime()), 'done');
  assert.equal(snapshot().latestUserMessage, 'same question');
  assert.notEqual(snapshot().replyId, firstId);
});

test('host exposes input outcomes as facts without hiding available reply content', () => {
  const store = useChatStore.getState(); store.ensureRuntime('round-outcome');
  const runtime = () => useChatStore.getState().runtimes['round-outcome'];
  const snapshot = () => presentationConversations([{ session_id: 'round-outcome', mode: 'agent' }],
    { 'round-outcome': runtime() }, 'New').find(item => item.sessionId === 'round-outcome');
  store.addMessage('round-outcome', { id: 'u1', role: 'user', content: 'old question', timestamp: '1' });
  store.setLastFinalReply('round-outcome', 'old answer');
  store.addMessage('round-outcome', { id: 'u2', role: 'user', content: 'stop this', timestamp: '2', inputRequestId: 'r2' });
  store.updateMessage('round-outcome', 'u2', { inputOutcome: 'stopped' });
  store.setInterruptResult('round-outcome', null);
  assert.equal(snapshot().inputOutcome, 'stopped');
  assert.equal(snapshot().replyToMessageId, 'u1');
  assert.equal(lastPresentationReply(runtime()).content, 'old answer');
  store.setLastFinalReply('round-outcome', 'late final after cancellation', 'r2');
  assert.equal(snapshot().replyToMessageId, 'u2');
  assert.equal(lastPresentationReply(runtime()).content, 'late final after cancellation');
  store.addMessage('round-outcome', { id: 'u3', role: 'user', content: 'new failed send', timestamp: '4', inputOutcome: 'failed' });
  assert.equal(snapshot().inputMessageId, 'u3');
  assert.equal(snapshot().replyToMessageId, 'u2');
  assert.equal(snapshot().inputOutcome, 'failed');
  assert.equal(lastPresentationReply(runtime()).content, 'late final after cancellation');
  store.addMessage('round-outcome', { id: 'u4', role: 'user', content: 'try again', timestamp: '5' });
  store.setLastFinalReply('round-outcome', 'a new complete answer');
  assert.equal(snapshot().inputOutcome, null);
  assert.equal(snapshot().replyToMessageId, 'u4');
  assert.equal(conversationActivity(runtime()), 'done');
  assert.equal(lastPresentationReply(runtime()).content, 'a new complete answer');
});

test('restored history identifies reply ownership without removing previous completed messages', () => {
  const store = useChatStore.getState(); store.ensureRuntime('round-history');
  const runtime = () => useChatStore.getState().runtimes['round-history'];
  const snapshot = () => presentationConversations([{ session_id: 'round-history', mode: 'agent' }],
    { 'round-history': runtime() }, 'New').find(item => item.sessionId === 'round-history');
  store.replaceHistoryMessages('round-history', [
    { id: 'u1', role: 'user', content: 'old', timestamp: '1' },
    { id: 'a1', role: 'assistant', content: 'old answer', timestamp: '2' },
    { id: 'u2', role: 'user', content: 'new', timestamp: '3' },
  ]);
  assert.equal(lastPresentationReply(runtime()).content, 'old answer');
  assert.equal(snapshot().inputMessageId, 'u2'); assert.equal(snapshot().replyToMessageId, 'u1');
  store.addMessage('round-history', { id: 'a2', role: 'assistant', content: 'new answer', timestamp: '5' });
  assert.equal(lastPresentationReply(runtime()).content, 'new answer');
  assert.equal(snapshot().replyToMessageId, 'u2');
  store.addMessage('round-history', { id: 'u3', role: 'user', content: 'pending request', timestamp: '6', inputRequestId: 'r3' });
  store.addMessage('round-history', { id: 'unmatched', role: 'assistant', content: 'unmatched request', timestamp: '7', outputOrder: { requestId: 'unknown', sequence: 1 } });
  assert.equal(snapshot().replyToMessageId, null);
});

test('current-turn activity and tool names exclude pending tools from an older input', () => {
  const store = useChatStore.getState(); store.ensureRuntime('round-tools');
  store.addMessage('round-tools', { id: 'u2', role: 'user', content: 'new question', timestamp: '2', inputRequestId: 'r2' });
  const oldTool = { status: 'pending', requestId: 'r1', toolCall: { name: 'old_tool' } };
  const currentTool = { status: 'pending', requestId: 'r2', toolCall: { name: 'current_tool' } };
  const runtime = { ...useChatStore.getState().runtimes['round-tools'], isProcessing: true, isThinking: true,
    toolExecutions: new Map([['old', oldTool], ['current', currentTool]]) };
  assert.equal(conversationActivity(runtime), 'toolRunning');
  const snapshot = presentationConversations([{ session_id: 'round-tools', mode: 'agent' }], { 'round-tools': runtime }, 'New');
  assert.deepEqual(snapshot.find(item => item.sessionId === 'round-tools').toolNames, ['current_tool']);
  currentTool.status = 'completed';
  assert.equal(conversationActivity(runtime), 'thinking');
});







test('the sandbox protocol accepts only explicit bounded operations and complete targets', () => {
  assert.deepEqual(parsePresentationRequest(request(1)), request(1));
  for (const value of [
    request(0), request(1.5), { ...request(1), version: 2 }, { ...request(1), extra: true },
    request(1, 'eval', { expression: 'process.exit()' }), request(1, 'send', { text: 'hello' }),
    request(1, 'send', { conversationKey: 'session', text: ' ' }),
    request(1, 'resize', { width: 300.1, height: 200 }),
    request(1, 'move', { deltaX: Infinity, deltaY: 0 }),
    request(1, 'workspace', { conversationKey: 'session', native: true }),
  ]) assert.equal(parsePresentationRequest(value), null);
  assert.ok(parsePresentationRequest(request(2, 'resize', { width: 160, height: 960 })));
  assert.ok(parsePresentationRequest(request(3, 'workspace', {})));
});

test('duplicate sends share one in-flight result and cannot change their content', async () => {
  let calls = 0;
  let finish;
  const ledger = createPresentationRequests(() => { calls += 1; return new Promise(resolve => { finish = resolve; }); },
    error => ({ code: error.message, message: error.message }));
  const first = ledger(request(1));
  const duplicate = ledger(request(1));
  await Promise.resolve();
  assert.equal(calls, 1);
  assert.equal((await ledger(request(1, 'send', { conversationKey: 'other', text: 'hello' }))).error.code, 'REQUEST_CONFLICT');
  finish(true);
  assert.deepEqual(await first, await duplicate);
});

test('eviction stays bounded and expired ids cannot execute again', async () => {
  let calls = 0;
  const ledger = createPresentationRequests(async () => { calls += 1; return true; },
    error => ({ code: error.message, message: error.message }));
  for (let id = 1; id <= 600; id += 1) assert.equal((await ledger(request(id))).ok, true);
  assert.equal((await ledger(request(1))).error.code, 'STALE_REQUEST');
  assert.equal(calls, 600);
  assert.equal((await ledger(request(600))).ok, true);
  assert.equal(calls, 600);
});

test('one completed parallel tool does not clear the other pending tool', () => {
  useChatStore.getState().ensureRuntime('parallel');
  const runtime = { ...useChatStore.getState().runtimes.parallel, isProcessing: true, isThinking: true,
    toolExecutions: new Map([
      ['first', { status: 'pending', toolCall: { name: 'read_file' } }],
      ['second', { status: 'pending', toolCall: { name: 'list_dir' } }],
    ]) };
  assert.equal(conversationActivity(runtime), 'toolRunning');
  runtime.toolExecutions.get('first').status = 'completed';
  assert.equal(conversationActivity(runtime), 'toolRunning');
  runtime.toolExecutions.get('second').status = 'completed';
  assert.equal(conversationActivity(runtime), 'thinking');
  runtime.pendingQuestions = [{ request_id: 'confirmation' }];
  assert.equal(conversationActivity(runtime), 'needsInput');
});

test('snapshots expose only single Agent summaries, with the last user message and no hidden reasoning', () => {
  const chat = useChatStore.getState();
  chat.ensureRuntime('a'); chat.ensureTeamRuntime('a', 'expert'); chat.ensureRuntime('invisible');
  chat.addMessage('a', { id: 'u', role: 'user', content: '最近的问题', timestamp: '1' });
  chat.setLastFinalReply('a', '完整的最终回复'); chat.setInputValue('a', 'draft');
  const runtimes = { ...useChatStore.getState().runtimes,
    a: { ...useChatStore.getState().runtimes.a, reasoningSegments: [{ text: 'private reasoning' }] } };
  const snapshot = presentationConversations([{ session_id: 'a', title: 'A', mode: 'agent' },
    { session_id: 'team', title: 'Team', mode: 'team' }], runtimes, 'New');
  assert.equal(snapshot.find(item => item.conversationKey === 'a').latestUserMessage, '最近的问题');
  assert.equal(snapshot.find(item => item.conversationKey === 'a').draft, 'draft');
  assert.ok(!snapshot.some(item => item.teamId || item.sessionId === 'team' || item.sessionId === 'invisible'));
  assert.ok(!JSON.stringify(snapshot).includes('private reasoning'));
});

async function mountHost(onSend = async () => ({ accepted: true, conversationKey: 'a' }), onWorkspace = async () => {}, callbacks = {}, activateInitial = true, liveSurface = false, desktop = true) {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  const globals = ['window', 'document', 'getComputedStyle', 'MutationObserver', 'HTMLElement', 'Element', 'ShadowRoot', 'CustomEvent',
    'IS_REACT_ACT_ENVIRONMENT'];
  const previous = new Map(globals.map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  for (const key of globals.slice(0, -1)) Object.defineProperty(globalThis, key, { configurable: true, writable: true,
    value: key === 'getComputedStyle' ? dom.window.getComputedStyle.bind(dom.window) : dom.window[key] });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  dom.window.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} });
  useChatStore.setState({ runtimes: {} });
  useSessionStore.setState({ sessions: [{ session_id: 'a', title: 'A' }, { session_id: 'b', title: 'B' }], currentSession: null });
  useWorkspaceStore.setState({ projectSessions: {}, pinnedSessions: [] });
  useChatStore.getState().ensureRuntime('a');
  useChatStore.getState().setInputValue('a', 'draft');
  const requests = [], results = [], updates = [], closes = [], editorUpdates = [];
  window.addEventListener('chat-input-sync', event => editorUpdates.push(event.detail));
  let resolveOpen, onRequest, onClosed, port;
  const api = {
    open: config => { requests.push(config); return new Promise(resolve => { resolveOpen = resolve; }); },
    onRequest: callback => { onRequest = callback; return () => {}; },
    onClosed: callback => { onClosed = callback; return () => {}; },
    update: async (_key, payload) => { updates.push(payload); return true; },
    respond: async (generation, result) => { results.push({ generation, ...result }); return true; },
    close: async key => { closes.push(key); return true; },
    resize: async () => true, move: async () => true, hitTest: async () => true,
  };
  if (desktop) window.jiuwenDesktop = { presentation: api };
  function Probe(props) { port = usePresentationHost(); return liveSurface ? React.createElement(WebUIApplicationSurface, props) : null; }
  const registry = createRendererRegistry(Object.fromEntries(componentCatalog.listComponents().map(rule =>
    [rule.name, rule.name === 'ApplicationSurface' ? Probe : () => null])));
  const transport = createMockTransport();
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(WebUIProvider, { runtime, registry },
    React.createElement(ApplicationPresentationHost, { sessionId: 'a', connected: true, onSend,
      onSelect: async () => {}, onStop: async () => true, onWorkspace,
      ...callbacks }))));
  const target = { scope: 'application', slot_id: 'application.presentation', package_id: 'bee', instance_key: 'main' };
  await act(async () => transport.injectRenderFrame({ target, revision: 1, mode: 'replace', schema_version: 1,
    root: { component: 'ApplicationSurface', props: { title: 'Bee', src: '/extension-assets/bee/g1/index.html' } } }));
  assert.ok(port);
  const declaration = { key: liveSurface ? '|application.presentation|bee|main' : 'bee', revision: liveSurface ? 1 : undefined,
    title: 'Bee', src: '/extension-assets/bee/g1/index.html',
    resourceUrl: '/extension-assets/bee/g1/index.html', width: 300, height: 320, config: {} };
  let opening;
  if (activateInitial) await act(async () => { opening = port.activate(declaration); });
  return {
    requests, results, updates, opening, closes, editorUpdates,
    nativeApi: api, runtime, transport, target, declaration,
    register: (changes = {}) => port.update({ ...declaration, ...changes }),
    release: (key = declaration.key) => port.release(key),
    activate: (changes = {}) => port.activate({ ...declaration, ...changes }),
    ready: (generation = 'current') => resolveOpen({ generation }),
    receive: (message, generation = 'current') => onRequest({ key: declaration.key, generation, request: message }),
    closed: (generation, reason = 'closed') => onClosed({ key: declaration.key, generation, reason }),
    unmount: () => root.unmount(),
    async dispose() {
      await act(async () => root.unmount());
      runtime.dispose(); dom.window.close();
      for (const [key, descriptor] of previous) {
        if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
      }
    },
  };
}

test('render-controlled desktop forms stay out of the launcher and retain the current page', async () => {
  const selections = [];
  const host = await mountHost(undefined, undefined, { onSelect: async conversation => selections.push(conversation) }, false);
  try {
    await act(async () => host.register({ autoStart: true }));
    assert.equal(document.querySelector('[data-testid="webui-presentation-launcher"]'), null);
    assert.equal(host.requests.length, 1);
    await act(async () => host.ready());
    assert.deepEqual(selections, [], 'starting the current conversation does not navigate away from settings');
    await act(async () => host.receive(request(1, 'select', { conversationKey: 'b' })));
    assert.equal(selections.at(-1).conversationKey, 'b', 'explicit conversation selection still works');
  } finally { await host.dispose(); }
});

test('physical overlay close reports the exact rendered instance without waiting on the plugin', async () => {
  const host = await mountHost(undefined, undefined, {}, false, true);
  let finishClose;
  const dispatch = host.transport.dispatch;
  host.transport.dispatch = value => value.event === 'ui.closed'
    ? new Promise(resolve => { finishClose = () => resolve({ accepted: true, status: 'closed' }); dispatch(value); })
    : dispatch(value);
  try {
    await act(async () => host.transport.injectRenderFrame({ target: host.target, revision: 2, mode: 'replace', schema_version: 1,
      root: { component: 'ApplicationSurface', props: { title: 'Bee', src: host.declaration.src, mode: 'overlay', autoStart: true } } }));
    const frame = document.querySelector('iframe');
    await act(async () => {
      window.dispatchEvent(new window.MessageEvent('message', { source: frame.contentWindow,
        data: { protocol: 'workswarm.presentation', version: 1, type: 'ready' } }));
    });
    await act(async () => document.querySelector('[data-testid="webui-presentation-overlay-close"]').click());
    assert.equal(document.querySelector('iframe'), null);
    const closed = host.transport.dispatches.filter(item => item.event === 'ui.closed');
    const closedId = closed[0].payload.presentation_id;
    assert.ok(Number.isSafeInteger(closedId) && closedId > 0);
    assert.deepEqual(closed, [{ event: 'ui.closed', target: { scope: 'application', sessionId: '',
      slotId: 'application.presentation', packageId: 'bee', instanceKey: 'main' }, payload: { reason: 'closed', revision: 2, presentation_id: closedId } }]);
    assert.ok(finishClose, 'the plugin receipt is still pending after physical destruction');
    finishClose();
    assert.equal(await host.runtime.notifyClosed(host.declaration.key, 'closed', 1), null);
    assert.equal(host.transport.dispatches.filter(item => item.event === 'ui.closed').length, 1, 'old revisions are never reported as current');
  } finally { finishClose?.(); await host.dispose(); }
});

test('returning to the workspace reports a normal close without a restore notice', async () => {
  const host = await mountHost(undefined, undefined, {}, true, true);
  try {
    await act(async () => { host.ready(); await host.opening; });
    await act(async () => host.closed('current', 'workspace'));
    const closed = host.transport.dispatches.filter(item => item.event === 'ui.closed');
    assert.deepEqual(closed.map(item => item.payload.reason), ['closed']);
    assert.equal(document.querySelector('[data-testid="webui-presentation-notice"]'), null);
  } finally { await host.dispose(); }
});

test('render updates switch the same resource between hosts, preserve config updates, and reopen after CLEAR', async () => {
  const host = await mountHost(undefined, undefined, {}, false, true);
  const render = (revision, mode, config = {}) => host.transport.injectRenderFrame({
    target: host.target, revision, mode: 'replace', schema_version: 1,
    root: { component: 'ApplicationSurface', props: {
      title: 'Bee', src: host.declaration.src, mode, autoStart: true, config,
    } },
  });
  try {
    await act(async () => render(2, 'desktop'));
    assert.equal(host.requests.length, 1);
    await act(async () => host.ready('desktop'));
    await act(async () => render(3, 'overlay'));
    const frame = document.querySelector('iframe');
    assert.ok(frame);
    assert.deepEqual(host.closes, [host.declaration.key]);
    await act(async () => window.dispatchEvent(new window.MessageEvent('message', { source: frame.contentWindow,
      data: { protocol: 'workswarm.presentation', version: 1, type: 'ready' } })));
    await act(async () => render(4, 'overlay', { expanded: true }));
    assert.equal(document.querySelector('iframe'), frame);
    await act(async () => document.querySelector('[data-testid="webui-presentation-overlay-close"]').click());
    assert.equal(host.transport.dispatches.filter(item => item.event === 'ui.closed').at(-1).payload.revision, 4);
    await act(async () => host.transport.injectClearFrame({ target: host.target, revision: 5, reason: 'requested' }));
    await act(async () => render(6, 'desktop'));
    assert.equal(host.requests.length, 2);
    await act(async () => host.ready('reopened'));
    assert.equal(document.querySelector('[data-testid="webui-presentation-launcher"]'), null);
  } finally { await host.dispose(); }
});

test('native open failure reports one lifecycle close for the current rendered revision', async () => {
  const host = await mountHost(undefined, undefined, {}, false, true);
  host.nativeApi.open = async () => { throw new Error('native open failed'); };
  try {
    await act(async () => host.transport.injectRenderFrame({ target: host.target, revision: 2, mode: 'replace', schema_version: 1,
      root: { component: 'ApplicationSurface', props: { title: 'Bee', src: host.declaration.src, autoStart: true } } }));
    const closes = host.transport.dispatches.filter(item => item.event === 'ui.closed');
    assert.equal(closes.length, 1);
    assert.equal(closes[0].payload.reason, 'open_failed');
    assert.equal(closes[0].payload.revision, 2);
    assert.ok(closes[0].payload.presentation_id > 0);
    assert.deepEqual(host.closes, [host.declaration.key]);
  } finally { await host.dispose(); }
});

test('an overlay that fails before ready is destroyed and reports one lifecycle close', async () => {
  const host = await mountHost(undefined, undefined, {}, false, true);
  try {
    await act(async () => host.transport.injectRenderFrame({ target: host.target, revision: 2, mode: 'replace', schema_version: 1,
      root: { component: 'ApplicationSurface', props: { title: 'Bee', src: host.declaration.src, mode: 'overlay', autoStart: true } } }));
    const frame = document.querySelector('iframe');
    assert.ok(frame);
    await act(async () => window.dispatchEvent(new window.MessageEvent('message', { source: frame.contentWindow,
      data: { protocol: 'workswarm.presentation', version: 1, type: 'ready', transition: { durationMs: 300 } } })));
    assert.ok(document.querySelector('iframe') === null, 'a rejected overlay transition must close the pending container');
    const closes = host.transport.dispatches.filter(item => item.event === 'ui.closed');
    assert.equal(closes.length, 1);
    assert.equal(closes[0].payload.reason, 'open_failed');
    assert.equal(closes[0].payload.revision, 2);
  } finally { await host.dispose(); }
});

test('desktop autoStart in a browser reports failure instead of leaving a requested form without a container', async () => {
  const host = await mountHost(undefined, undefined, {}, false, true, false);
  try {
    await act(async () => host.transport.injectRenderFrame({ target: host.target, revision: 2, mode: 'replace', schema_version: 1,
      root: { component: 'ApplicationSurface', props: { title: 'Bee', src: host.declaration.src, autoStart: true } } }));
    assert.equal(host.requests.length, 0);
    const closes = host.transport.dispatches.filter(item => item.event === 'ui.closed');
    assert.equal(closes.length, 1);
    assert.equal(closes[0].payload.reason, 'open_failed');
    assert.equal(closes[0].payload.revision, 2);
  } finally { await host.dispose(); }
});

test('late close from a retired native generation does not notify the new form owner', async () => {
  const host = await mountHost(undefined, undefined, {}, true, true);
  try {
    await act(async () => { host.ready('old'); await host.opening; });
    await act(async () => host.transport.injectRenderFrame({ target: host.target, revision: 2, mode: 'replace', schema_version: 1,
      root: { component: 'ApplicationSurface', props: { title: 'Next', src: '/extension-assets/bee/g1/next.html' } } }));
    let next;
    await act(async () => { next = host.activate({ src: '/extension-assets/bee/g1/next.html', revision: 2 }); });
    await act(async () => { host.ready('new'); await next; });
    await act(async () => host.closed('old'));
    assert.equal(host.transport.dispatches.filter(item => item.event === 'ui.closed').length, 0);
    await act(async () => host.closed('new'));
    assert.deepEqual(host.transport.dispatches.filter(item => item.event === 'ui.closed').map(item => ({
      reason: item.payload.reason, revision: item.payload.revision })),
      [{ reason: 'closed', revision: 2 }]);
  } finally { await host.dispose(); }
});

test('an unchanged legacy candidate can reopen and close with a new container identity', async () => {
  const host = await mountHost(undefined, undefined, {}, true, true);
  try {
    await act(async () => { host.ready('first'); await host.opening; });
    await act(async () => host.closed('first'));
    assert.equal(host.runtime.getInstance(host.declaration.key).revision, 1);
    let opening;
    await act(async () => { opening = host.activate(); });
    await act(async () => { host.ready('second'); await opening; });
    await act(async () => host.closed('second'));
    const closes = host.transport.dispatches.filter(item => item.event === 'ui.closed');
    assert.equal(closes.length, 2);
    assert.equal(closes[0].payload.revision, 1);
    assert.equal(closes[1].payload.revision, 1);
    assert.ok(closes[1].payload.presentation_id > closes[0].payload.presentation_id);
  } finally { await host.dispose(); }
});

test('automatic overlays follow declaration enable and release without a switch-menu entry', async () => {
  const host = await mountHost(undefined, undefined, {}, false);
  const declaration = { mode: 'overlay', autoStart: true };
  const ready = async () => {
    const frame = document.querySelector('iframe');
    assert.ok(frame);
    await act(async () => window.dispatchEvent(new window.MessageEvent('message', { source: frame.contentWindow,
      data: { protocol: 'workswarm.presentation', version: 1, type: 'ready' } })));
    return frame;
  };
  try {
    await act(async () => host.register(declaration));
    const first = await ready();
    assert.equal(document.querySelector('[data-testid="webui-presentation-launcher"]'), null);
    assert.equal(host.requests.length, 0, 'automatic overlays never open a native window');
    await act(async () => {
      useChatStore.getState().setInputValue('a', 'editing');
      host.register({ ...declaration, title: 'Updated title', config: { expanded: true } });
    });
    assert.equal(document.querySelector('iframe'), first);
    await act(async () => document.querySelector('[data-testid="webui-presentation-overlay-close"]').click());
    assert.equal(document.querySelector('iframe'), null);
    await act(async () => host.register({ ...declaration, config: { expanded: false } }));
    assert.equal(document.querySelector('iframe'), null, 'ordinary updates do not reopen a manually closed pet');
    await act(async () => host.release());
    await act(async () => host.register(declaration));
    const second = await ready();
    assert.notEqual(second, first);
    await act(async () => host.release());
    assert.equal(document.querySelector('iframe'), null, 'disabling releases the pet');
    await act(async () => host.register(declaration));
    assert.ok(document.querySelector('iframe'));
    await act(async () => host.release());
    assert.equal(document.querySelector('iframe'), null, 'disable cancels pending readiness too');
    assert.equal(document.querySelector('[data-testid="webui-presentation-notice"]'), null);
  } finally { await host.dispose(); }
});

test('automatic overlays do not replace an active form or retry on unrelated updates', async () => {
  const host = await mountHost();
  const declaration = { key: 'pet', mode: 'overlay', autoStart: true };
  try {
    await act(async () => { host.ready(); await host.opening; });
    assert.equal('autoStart' in host.requests[0], false);
    await act(async () => host.register(declaration));
    assert.equal(document.querySelector('iframe'), null);
    assert.deepEqual(host.closes, []);
    assert.match(document.querySelector('[data-testid="webui-presentation-notice"]').textContent, /已有|already/);
    await act(async () => host.closed('current'));
    await act(async () => host.register({ ...declaration, config: { changed: true } }));
    assert.equal(document.querySelector('iframe'), null);
    await act(async () => host.release('pet'));
    await act(async () => host.register(declaration));
    assert.ok(document.querySelector('iframe'), 're-enabling explicitly requests another automatic start');
    await act(async () => host.release('pet'));
  } finally { await host.dispose(); }
});



test('model catalogs expose display fields only and unavailable or busy choices cannot execute', async () => {
  const host = await mountHost();
  try {
    await act(async () => { host.ready(); await host.opening; });
    await act(async () => useSessionStore.setState({ chatAvailableModels: [{ model_id: 'm1', model_name: 'Model one',
      api_key: 'private credential', api_base: 'private endpoint' }] }));
    const state = host.updates.at(-1).state;
    assert.deepEqual(state.models, [{ id: 'm1', label: 'Model one' }]);
    assert.ok(!JSON.stringify(state).includes('private'));
    await act(async () => { await host.receive(request(1, 'model', { conversationKey: 'a', modelId: 'removed' })); });
    assert.equal(host.results.at(-1).error.code, 'MODEL_UNAVAILABLE');
    await act(async () => useChatStore.getState().setProcessing('a', true));
    await act(async () => { await host.receive(request(2, 'model', { conversationKey: 'a', modelId: 'm1' })); });
    assert.equal(host.results.at(-1).error.code, 'CONVERSATION_BUSY');
  } finally { await host.dispose(); }
});

test('closing a form stops its store projection until it is activated again', async () => {
  const host = await mountHost();
  const original = useSessionStore.getState().getEffectiveModelName;
  try {
    await act(async () => { host.ready(); await host.opening; await host.closed('current'); });
    let reads = 0;
    await act(async () => useSessionStore.setState({ getEffectiveModelName: (...args) => { reads += 1; return original(...args); } }));
    const updates = host.updates.length;
    await act(async () => useChatStore.getState().addMessage('a', { id: 'inactive', role: 'assistant', content: 'do not project', timestamp: '1' }));
    assert.equal(reads, 0); assert.equal(host.updates.length, updates);
  } finally { useSessionStore.setState({ getEffectiveModelName: original }); await host.dispose(); }
});

test('reopening a form follows the workspace conversation after its window has closed', async () => {
  const host = await mountHost();
  try {
    await act(async () => { host.ready(); await host.opening; });
    await act(async () => { await host.receive(request(1, 'select', { conversationKey: 'b' })); });
    assert.equal(host.updates.at(-1).state.selectedConversationKey, 'b');
    await act(async () => { await host.closed('current'); });
    let next;
    await act(async () => { next = host.activate(); });
    assert.equal(host.requests.at(-1).state.selectedConversationKey, 'a');
    await act(async () => { host.ready('next'); await next; });
  } finally { await host.dispose(); }
});



test('queued sandbox messages wait for native readiness and cannot select a stale generation', async () => {
  const host = await mountHost();
  try {
    let stale, current, closed;
    await act(async () => {
      stale = host.receive(request(1, 'draft', { conversationKey: 'a', text: 'stale' }), 'previous');
      current = host.receive(request(1, 'draft', { conversationKey: 'a', text: 'current' }));
      closed = host.closed('previous');
    });
    assert.equal(useChatStore.getState().getRuntime('a').inputValue, 'draft');
    await act(async () => { host.ready(); await host.opening; await Promise.all([stale, current, closed]); });
    assert.equal(useChatStore.getState().getRuntime('a').inputValue, 'current');
    assert.deepEqual(host.editorUpdates, [{ sessionId: 'a', value: 'current' }]);
    assert.equal(host.results.length, 1);
    assert.equal(host.results[0].generation, 'current');
    assert.equal(host.results[0].ok, true);
  } finally { await host.dispose(); }
});

test('send receipts preserve newer edits and removed targets never turn into another conversation', async () => {
  let finish, sends = 0;
  const host = await mountHost(async () => { sends += 1; return new Promise(resolve => { finish = resolve; }); });
  try {
    await act(async () => { host.ready(); await host.opening; });
    await act(async () => { await host.receive(request(1, 'send', { conversationKey: 'a', text: 'draft' })); });
    assert.equal(sends, 1);
    await act(async () => { await host.receive(request(2, 'draft', { conversationKey: 'a', text: 'newer edit' })); });
    await act(async () => { finish({ accepted: true, conversationKey: 'a' }); });
    assert.equal(useChatStore.getState().getRuntime('a').inputValue, 'newer edit');
    assert.deepEqual(host.editorUpdates, [{ sessionId: 'a', value: 'newer edit' }]);
    await act(async () => { await host.receive(request(3, 'send', { conversationKey: 'a', text: 'newer edit' })); });
    await act(async () => { finish({ accepted: false, conversationKey: 'a' }); });
    assert.equal(useChatStore.getState().getRuntime('a').inputValue, 'newer edit');
    assert.equal(host.results.find(item => item.id === 3).error.code, 'SEND_FAILED');
    await act(async () => useSessionStore.setState({ sessions: [{ session_id: 'b', title: 'B' }] }));
    assert.equal(host.updates.at(-1).state.selectedConversationKey, null);
    await act(async () => { await host.receive(request(4, 'send', { conversationKey: 'a', text: 'must not move' })); });
    assert.equal(sends, 2);
    assert.equal(host.results.find(item => item.id === 4).error.code, 'TARGET_INACTIVE');
  } finally { await host.dispose(); }
});

test('a previous window workspace request cannot close a newer window after its asynchronous selection', async () => {
  let finishWorkspace;
  const host = await mountHost(undefined, () => new Promise(resolve => { finishWorkspace = resolve; }));
  try {
    await act(async () => { host.ready(); await host.opening; });
    await act(async () => { await host.receive(request(1, 'workspace', { conversationKey: 'a' })); });
    assert.equal(typeof finishWorkspace, 'function');
    await act(async () => { host.closed('current'); });
    let reopened;
    await act(async () => { reopened = host.activate({ src: '/extension-assets/bee/g2/index.html' }); });
    await act(async () => { host.ready('next'); await reopened; });
    await act(async () => { finishWorkspace(); });
    assert.deepEqual(host.closes, []);
    assert.equal(host.results.find(item => item.id === 1).error.code, 'STALE_REQUEST');
  } finally { await host.dispose(); }
});

test('unmount invalidates a pending workspace action before it can close a later host', async () => {
  let finishWorkspace;
  const host = await mountHost(undefined, () => new Promise(resolve => { finishWorkspace = resolve; }));
  try {
    await act(async () => { host.ready(); await host.opening; });
    await act(async () => { await host.receive(request(1, 'workspace', { conversationKey: 'a' })); });
    await act(async () => host.unmount());
    assert.deepEqual(host.closes, ['bee']);
    await act(async () => finishWorkspace());
    assert.deepEqual(host.closes, ['bee']);
    assert.equal(host.results.find(item => item.id === 1).error.code, 'STALE_REQUEST');
  } finally { await host.dispose(); }
});

test('the new container waits for an old asynchronous close and accepts its matching render registration', async () => {
  const host = await mountHost();
  try {
    await act(async () => { host.ready(); await host.opening; });
    let finishClose;
    host.nativeApi.close = async () => new Promise(resolve => { finishClose = resolve; });
    let replacing;
    await act(async () => { replacing = host.activate({ mode: 'overlay', revision: 2 }); });
    assert.equal(document.querySelector('iframe'), null, 'new container cannot open before physical close finishes');
    await act(async () => host.register({ mode: 'overlay', revision: 2 }));
    await act(async () => finishClose(true));
    const frame = document.querySelector('iframe');
    assert.ok(frame);
    await act(async () => {
      window.dispatchEvent(new window.MessageEvent('message', { source: frame.contentWindow,
        data: { protocol: 'workswarm.presentation', version: 1, type: 'ready' } }));
      await replacing;
    });
  } finally { await host.dispose(); }
});

test('failed closes retain the old surface until a later activation confirms its close', async () => {
  const host = await mountHost();
  let finishClose;
  try {
    await act(async () => { host.ready(); await host.opening; });
    let closeCalls = 0;
    host.nativeApi.close = async key => {
      host.closes.push(key);
      if (++closeCalls <= 2) throw new Error('close failed');
      return new Promise(resolve => { finishClose = resolve; });
    };
    for (let attempt = 0; attempt < 2; attempt += 1) {
      await act(async () => { await assert.rejects(host.activate({ mode: 'overlay' }), /close failed/); });
      assert.equal(document.querySelector('iframe'), null);
    }
    assert.deepEqual(host.closes, ['bee', 'bee']);
    assert.equal(host.requests.length, 1);
    let replacing, retryError;
    await act(async () => {
      replacing = host.activate({ mode: 'overlay' }).catch(error => { retryError = error; });
    });
    assert.equal(typeof finishClose, 'function');
    assert.equal(document.querySelector('iframe'), null, 'replacement must wait for the retried close');
    await act(async () => finishClose(true));
    const frame = document.querySelector('iframe');
    assert.ok(frame);
    await act(async () => {
      window.dispatchEvent(new window.MessageEvent('message', { source: frame.contentWindow,
        data: { protocol: 'workswarm.presentation', version: 1, type: 'ready' } }));
      await replacing;
    });
    assert.equal(retryError, undefined);
    assert.deepEqual(host.closes, ['bee', 'bee', 'bee']);
  } finally { finishClose?.(true); await host.dispose(); }
});

test('a failed release close is retried before reopening on the same native host', async () => {
  const host = await mountHost();
  try {
    await act(async () => { host.ready(); await host.opening; });
    let closeCalls = 0;
    host.nativeApi.close = async key => {
      host.closes.push(key);
      if (++closeCalls === 1) throw new Error('release close failed');
      return false;
    };
    await act(async () => host.release());
    assert.equal(closeCalls, 1);
    let reopened, retryError;
    await act(async () => { reopened = host.activate().catch(error => { retryError = error; }); });
    assert.equal(host.requests.length, 2);
    await act(async () => { host.ready('reopened'); await reopened; });
    assert.equal(retryError, undefined);
    assert.deepEqual(host.closes, ['bee', 'bee']);
  } finally { await host.dispose(); }
});

test('unmount cleans a surface retained after a failed close', async () => {
  const host = await mountHost();
  try {
    await act(async () => { host.ready(); await host.opening; });
    host.nativeApi.close = async key => { host.closes.push(key); throw new Error('close failed'); };
    await act(async () => { await assert.rejects(host.activate({ mode: 'overlay' }), /close failed/); });
    host.nativeApi.close = async key => { host.closes.push(key); return true; };
    await act(async () => host.unmount());
    assert.deepEqual(host.closes, ['bee', 'bee']);
  } finally { await host.dispose(); }
});

test('unmount while closing the old surface cannot open a replacement afterward', async () => {
  const host = await mountHost();
  try {
    await act(async () => { host.ready(); await host.opening; });
    let finishClose;
    host.nativeApi.close = async key => {
      host.closes.push(key);
      return new Promise(resolve => { finishClose = resolve; });
    };
    let replacing;
    await act(async () => { replacing = host.activate({ mode: 'overlay' }); });
    const rejected = assert.rejects(replacing, /STALE_REQUEST/);
    await act(async () => { host.unmount(); finishClose(true); await rejected; });
    assert.equal(host.requests.length, 1);
    assert.equal(document.querySelector('iframe'), null);
    assert.deepEqual(host.closes, ['bee']);
  } finally { await host.dispose(); }
});

test('replacing one in-app form does not let the old close event clear the new activation', async () => {
  const host = await mountHost();
  const ready = async opening => {
    const frame = document.querySelector('iframe');
    await act(async () => {
      window.dispatchEvent(new window.MessageEvent('message', { source: frame.contentWindow,
        data: { protocol: 'workswarm.presentation', version: 1, type: 'ready' } }));
      await opening;
    });
    return frame;
  };
  try {
    await act(async () => { host.ready(); await host.opening; });
    let first, second;
    await act(async () => { first = host.activate({ mode: 'overlay' }); });
    const previousFrame = await ready(first);
    await act(async () => { second = host.activate({ key: 'second', mode: 'overlay' }); });
    const frame = await ready(second);
    assert.notEqual(frame, previousFrame);
    assert.equal(document.querySelector('[data-testid="webui-presentation-overlay"]').style.visibility, 'visible');
    await act(async () => window.dispatchEvent(new window.MessageEvent('message', { source: frame.contentWindow,
      data: request(1, 'draft', { conversationKey: 'a', text: 'replacement draft' }) })));
    assert.equal(useChatStore.getState().getRuntime('a').inputValue, 'replacement draft');
  } finally { await host.dispose(); }
});

test('selecting the current conversation preserves the canonical list order and refreshes its metadata', async () => {
  const host = await mountHost();
  try {
    await act(async () => { host.ready(); await host.opening; });
    const order = host.requests.at(-1).state.conversations.map(item => item.conversationKey);
    await act(async () => useSessionStore.getState().setCurrentSession({ session_id: 'b', title: 'Updated B' }));
    const conversations = host.updates.at(-1).state.conversations;
    assert.deepEqual(conversations.map(item => item.conversationKey), order);
    assert.equal(conversations.find(item => item.conversationKey === 'b').title, 'Updated B');
  } finally { await host.dispose(); }
});

test('project and pinned conversations are available and removed project targets cannot receive replies', async () => {
  let sends = 0;
  const host = await mountHost(async () => { sends += 1; return { accepted: true, conversationKey: 'project-chat' }; });
  try {
    await act(async () => { host.ready(); await host.opening; });
    await act(async () => useWorkspaceStore.setState({
      projectSessions: { project: [{ session_id: 'project-chat', title: 'Project conversation' }] },
      pinnedSessions: [{ session_id: 'pinned-chat', title: 'Pinned conversation' }],
    }));
    const conversations = host.updates.at(-1).state.conversations;
    assert.ok(conversations.some(item => item.conversationKey === 'project-chat'));
    assert.ok(conversations.some(item => item.conversationKey === 'pinned-chat'));
    const order = conversations.map(item => item.conversationKey);
    await act(async () => { await host.receive(request(1, 'select', { conversationKey: 'project-chat' })); });
    await act(async () => useSessionStore.getState().addSession({ session_id: 'project-chat', title: 'Loaded project metadata' }));
    assert.deepEqual(host.updates.at(-1).state.conversations.map(item => item.conversationKey), order);
    assert.equal(host.updates.at(-1).state.selectedConversationKey, 'project-chat');
    await act(async () => { await host.receive(request(2, 'draft', { conversationKey: 'project-chat', text: 'project draft' })); });
    assert.equal(useChatStore.getState().getRuntime('project-chat').inputValue, 'project draft');
    await act(async () => {
      useSessionStore.setState({ sessions: useSessionStore.getState().sessions.filter(item => item.session_id !== 'project-chat') });
      useWorkspaceStore.setState({ projectSessions: {} });
    });
    assert.equal(host.updates.at(-1).state.selectedConversationKey, null);
    await act(async () => { await host.receive(request(3, 'send', { conversationKey: 'project-chat', text: 'do not send' })); });
    assert.equal(sends, 0);
    assert.equal(host.results.find(item => item.id === 3).error.code, 'TARGET_INACTIVE');
  } finally { await host.dispose(); }
});
