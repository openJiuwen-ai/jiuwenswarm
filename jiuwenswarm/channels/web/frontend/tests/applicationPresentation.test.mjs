// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import { createPresentationRequests, parsePresentationRequest, conversationActivity, presentationConversations, useChatStore,
  useSessionStore, useWorkspaceStore, ApplicationPresentationHost, usePresentationHost, WebUIProvider, componentCatalog,
  createRendererRegistry, createWebUIRuntime, createMockTransport, createPresentationReplyReader, lastPresentationReply, lastPresentationUserMessage, selectConversationModel, webClient,
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

test('Team leader public finals are decoded without requiring a user recipient', () => {
  const store = useChatStore.getState(); store.ensureRuntime('leader-final');
  store.addMessage('leader-final', { id: 'question', role: 'user', content: '需求', timestamp: '1' });
  const content = '队长公开回复🐝'.repeat(5000);
  store.addMessage('leader-final', { id: 'team-leader-final', role: 'system', content: `team.leader:${JSON.stringify({ content })}`, timestamp: '2' });
  for (const [id, event] of [['private', { type: 'team.message.p2p', from_member: 'developer', to_member: 'qa', content: 'private' }],
    ['broadcast', { type: 'team.message.broadcast', from_member: 'team_leader', content: 'broadcast' }],
    ['monitor', { type: 'team.member.created', from_member: 'team_leader', content: 'monitor' }]]) {
    store.addMessage('leader-final', { id, role: 'system', content: `team.event:${JSON.stringify({ event })}`, timestamp: '3' });
  }
  const runtime = useChatStore.getState().runtimes['leader-final'];
  const reader = createPresentationReplyReader(), replyId = reader.identify(lastPresentationReply(runtime));
  const summary = presentationConversations([{ session_id: 'leader-final', mode: 'team' }], { 'leader-final': runtime }, 'New', reader.identify).at(-1);
  assert.equal(summary.replyToMessageId, 'question');
  assert.ok(summary.preview.startsWith('队长公开回复'));
  let offset = 0; const parts = [];
  do { const part = reader.read(runtime, replyId, offset); parts.push(part.content); offset = part.nextOffset; } while (offset !== null);
  assert.equal(parts.join(''), content);
  store.addMessage('leader-final', { id: 'team-leader-next', role: 'system', content: 'team.leader:{"content":"下一条"}', timestamp: '4' });
  assert.throws(() => reader.read(useChatStore.getState().runtimes['leader-final'], replyId, 0), /REPLY_CHANGED/);
});

test('Team leader message events are public but ordinary member user messages remain in Team Ops', () => {
  const store = useChatStore.getState(); store.ensureRuntime('leader-event');
  store.addMessage('leader-event', { id: 'leader', role: 'system', timestamp: '1',
    content: 'team.event:{"event":{"type":"team.message","from_member":"team_leader","content":"公开答复"}}' });
  store.addMessage('leader-event', { id: 'member', role: 'system', timestamp: '2',
    content: 'team.event:{"event":{"type":"team.message.p2p","from_member":"developer","to_member":"user","content":"成员定向答复"}}' });
  store.addMessage('leader-event', { id: 'leader-private', role: 'system', timestamp: '3',
    content: 'team.event:{"event":{"type":"team.message","from_member":"team_leader","to_member":"qa","content":"队长给成员的消息"}}' });
  const runtime = useChatStore.getState().runtimes['leader-event'], reader = createPresentationReplyReader();
  const id = reader.identify(lastPresentationReply(runtime));
  assert.equal(reader.read(runtime, id, 0).content, '公开答复');
});

test('Team leader public process segments remain visible before a final and later segments preserve earlier text', () => {
  const store = useChatStore.getState(); store.ensureRuntime('leader-process');
  store.addMessage('leader-process', { id: 'question', role: 'user', inputRequestId: 'turn', content: '创建团队', timestamp: '1' });
  store.addMessage('leader-process', { id: 'team-leader-intro', role: 'system', isStreaming: true, teamStream: { requestId: 'turn' },
    content: '我来创建一个3人团队，让每人报一个数字。', timestamp: '2' });
  const reader = createPresentationReplyReader();
  const first = lastPresentationReply(useChatStore.getState().runtimes['leader-process']);
  const firstId = reader.identify(first);
  assert.equal(reader.read(useChatStore.getState().runtimes['leader-process'], firstId, 0).content, '我来创建一个3人团队，让每人报一个数字。');
  store.updateMessage('leader-process', 'team-leader-intro', { isStreaming: false, teamStream: undefined });
  store.addMessage('leader-process', { id: 'tool-log', role: 'system', content: 'internal tool log', timestamp: '3' });
  store.addMessage('leader-process', { id: 'team-leader-followup', role: 'system', isStreaming: true, teamStream: { requestId: 'turn' }, content: '成员已加入。', timestamp: '4' });
  const runtime = useChatStore.getState().runtimes['leader-process'], current = lastPresentationReply(runtime);
  const id = reader.identify(current), summary = presentationConversations([{ session_id: 'leader-process', mode: 'team' }], { 'leader-process': runtime }, 'New', reader.identify).at(-1);
  assert.equal(reader.read(runtime, id, 0).content, '我来创建一个3人团队，让每人报一个数字。\n\n成员已加入。');
  assert.equal(summary.replyToMessageId, 'question');
  assert.equal(reader.identify(lastPresentationReply({ ...runtime, isThinking: true })), id);
  assert.throws(() => reader.read(runtime, firstId, 0), /REPLY_CHANGED/);
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
  store.addMessage('round-history', { id: 'auto', role: 'assistant', content: 'unrelated recommendation', timestamp: '4', isProactiveRecommendation: true });
  assert.equal(lastPresentationReply(runtime()).content, 'old answer');
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
  const child = { id: 'member', title: '成员', width: 180, height: 200, config: { member: 'one' }, x: -1500, y: 500 };
  assert.ok(parsePresentationRequest(request(4, 'children', { windows: [child] })));
  for (const windows of [[child, child], [{ ...child, src: 'https://foreign/' }], [{ ...child, config: { huge: '中'.repeat(6000) } }]]) {
    assert.equal(parsePresentationRequest(request(5, 'children', { windows })), null);
  }
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

test('new conversation requests require an explicit mode and bounded initial message', () => {
  for (const mode of ['agent', 'team']) {
    assert.ok(parsePresentationRequest(request(1, 'createConversation', { mode, text: '创建协作团队' })));
  }
  for (const params of [{ mode: 'swarm', text: 'hello' }, { mode: 'team', text: ' ' },
    { mode: 'team', text: '中'.repeat(11000) }, { mode: 'team', text: 'hello', conversationKey: 'a' }]) {
    assert.equal(parsePresentationRequest(request(1, 'createConversation', params)), null);
  }
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

test('snapshots expose visible Agent and Team sessions without hidden reasoning', () => {
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
  assert.equal(snapshot.find(item => item.sessionId === 'team').mode, 'team');
  assert.ok(!snapshot.some(item => item.teamId || item.sessionId === 'invisible'));
  assert.ok(!JSON.stringify(snapshot).includes('private reasoning'));
});

test('Team input remains available during execution while approvals still block sending', () => {
  const sessions = [{ session_id: 'team', mode: 'team' }, { session_id: 'agent', mode: 'agent' }];
  const busy = { isProcessing: true, pendingQuestions: [], toolExecutions: new Map(), messages: [] };
  const summaries = presentationConversations(sessions, { team: busy, agent: busy }, 'New');
  assert.equal(summaries.find(item => item.sessionId === 'team').canSend, true);
  assert.equal(summaries.find(item => item.sessionId === 'agent').canSend, false);
  const blocked = presentationConversations(sessions, {
    team: { ...busy, pendingA4PAuthorization: { id: 'approval' } },
  }, 'New');
  assert.equal(blocked.find(item => item.sessionId === 'team').canSend, false);
});

async function mountHost(onSend = async () => ({ accepted: true, conversationKey: 'a' }), onWorkspace = async () => {}, callbacks = {}) {
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
  const requests = [], results = [], updates = [], closes = [], editorUpdates = [], movements = [];
  window.addEventListener('chat-input-sync', event => editorUpdates.push(event.detail));
  let resolveOpen, onRequest, onClosed, port;
  const api = {
    open: config => { requests.push(config); return new Promise(resolve => { resolveOpen = resolve; }); },
    onRequest: callback => { onRequest = callback; return () => {}; },
    onClosed: callback => { onClosed = callback; return () => {}; },
    update: async (_key, payload) => { updates.push(payload); return true; },
    respond: async (generation, result) => { results.push({ generation, ...result }); return true; },
    close: async key => { closes.push(key); return true; },
    resize: async () => true, move: async (key, x, y) => { movements.push({ key, x, y }); return true; }, hitTest: async () => true,
    children: async (key, generation, windows) => ({ windows: windows.map((item) => ({
      id: item.id, key: `${key}:child:${item.id}`, generation: `child-${item.id}`,
    })) }),
  };
  window.jiuwenDesktop = { presentation: api };
  function Probe() { port = usePresentationHost(); return null; }
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
  const declaration = { key: 'bee', title: 'Bee', src: '/extension-assets/bee/g1/index.html',
    resourceUrl: '/extension-assets/bee/g1/index.html', width: 300, height: 320, config: {} };
  let opening;
  await act(async () => { opening = port.activate(declaration); });
  return {
    requests, results, updates, opening, closes, editorUpdates, movements,
    activate: () => port.activate(declaration),
    ready: (generation = 'current') => resolveOpen({ generation }),
    receive: (message, generation = 'current') => onRequest({ key: 'bee', generation, request: message }),
    receiveChild: (message, id, parentGeneration = 'current') => onRequest({ key: `bee:child:${id}`, generation: `child-${id}`,
      parentKey: 'bee', parentGeneration, request: message }),
    closed: generation => onClosed({ key: 'bee', generation, reason: 'closed' }),
    async dispose() {
      await act(async () => root.unmount());
      runtime.dispose(); dom.window.close();
      for (const [key, descriptor] of previous) {
        if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
      }
    },
  };
}



test('creating a Team prepares its mode before sending and selects the persisted conversation exactly once', async () => {
  const calls = [];
  const host = await mountHost(async (conversation, text) => {
    calls.push(['send', conversation.sessionId, conversation.mode, text]);
    useSessionStore.setState({ sessions: [{ session_id: 'created', title: 'Created', mode: 'team' }] });
    return { accepted: true, conversationKey: 'created' };
  }, undefined, { onSelect: async (conversation) => calls.push(['select', conversation.sessionId, conversation.mode]) });
  try {
    await act(async () => { host.ready(); await host.opening; });
    calls.length = 0;
    const create = request(1, 'createConversation', { mode: 'team', text: '协作需求' });
    await act(async () => { await host.receive(create); });
    await act(async () => { await host.receive(create); });
    assert.deepEqual(calls, [['select', 'new', 'team'], ['send', 'new', 'team', '协作需求']]);
    assert.deepEqual(host.results.at(-1).value, { conversationKey: 'created' });
    assert.equal(host.updates.at(-1).state.selectedConversationKey, 'created');
  } finally { await host.dispose(); }
});

test('a failed initial send keeps the draft and concurrent creation cannot switch its target', async () => {
  let finish;
  const host = await mountHost(() => new Promise(resolve => { finish = resolve; }));
  try {
    await act(async () => { host.ready(); await host.opening; });
    await act(async () => { await host.receive(request(1, 'createConversation', { mode: 'team', text: '保留需求' })); });
    await act(async () => { await host.receive(request(2, 'createConversation', { mode: 'team', text: '重复请求' })); });
    assert.equal(host.results.at(-1).error.code, 'REQUEST_BUSY');
    await act(async () => { await host.receive(request(3, 'select', { conversationKey: 'b' })); });
    assert.equal(host.results.at(-1).error.code, 'REQUEST_BUSY');
    await act(async () => { finish({ accepted: false, conversationKey: 'new' }); });
    assert.equal(host.results.at(-1).error.code, 'SEND_FAILED');
    assert.equal(useChatStore.getState().getRuntime('new').inputValue, '保留需求');
  } finally { await host.dispose(); }
});

test('closing during new-conversation preparation prevents a stale first send', async () => {
  let finish, sends = 0, preparing = false;
  const host = await mountHost(async () => { sends += 1; return { accepted: true, conversationKey: 'created' }; }, undefined,
    { onSelect: async () => { if (preparing) await new Promise(resolve => { finish = resolve; }); } });
  try {
    await act(async () => { host.ready(); await host.opening; });
    preparing = true;
    await act(async () => { await host.receive(request(1, 'createConversation', { mode: 'team', text: '需求' })); });
    await act(async () => { await host.closed('current'); finish(); });
    assert.equal(sends, 0);
  } finally { await host.dispose(); }
});

test('the host forwards addressed Team messages through the existing send callback during execution', async () => {
  const sent = [];
  const host = await mountHost(async (conversation, text) => {
    sent.push({ sessionId: conversation.sessionId, mode: conversation.mode, text });
    return { accepted: true, conversationKey: conversation.conversationKey };
  });
  try {
    await act(async () => { host.ready(); await host.opening; });
    await act(async () => {
      useSessionStore.setState({ sessions: [{ session_id: 'a', title: 'Team', mode: 'team' }] });
      useSessionStore.getState().setMode('a', 'team');
      useChatStore.getState().setProcessing('a', true);
    });
    await act(async () => { await host.receive(request(1, 'send', { conversationKey: 'a', text: '@research 请同步进展' })); });
    await act(async () => {});
    assert.deepEqual(sent, [{ sessionId: 'a', mode: 'team', text: '@research 请同步进展' }]);
    assert.equal(host.results.find(item => item.id === 1).ok, true);
  } finally { await host.dispose(); }
});

test('each child has its own request ledger and cannot route movement to a sibling or spawn children', async () => {
  const sends = [];
  const host = await mountHost(async (conversation, text) => {
    sends.push(text); return { accepted: true, conversationKey: conversation.conversationKey };
  });
  try {
    await act(async () => { host.ready(); await host.opening; });
    const windows = ['one', 'two'].map((id) => ({ id, title: id, width: 180, height: 200, config: {} }));
    await act(async () => { await host.receive(request(1, 'children', { windows })); });
    await act(async () => { await host.receiveChild(request(1, 'send', { conversationKey: 'a', text: '@one hi' }), 'one'); });
    await act(async () => { await host.receiveChild(request(1, 'send', { conversationKey: 'a', text: '@two hi' }), 'two'); });
    assert.deepEqual(sends, ['@one hi', '@two hi']);
    await act(async () => { await host.receiveChild(request(2, 'move', { deltaX: 20, deltaY: 30 }), 'one'); });
    assert.deepEqual(host.movements, [{ key: 'bee:child:one', x: 20, y: 30 }]);
    await act(async () => { await host.receiveChild(request(3, 'children', { windows: [] }), 'one'); });
    assert.equal(host.results.find((item) => item.generation === 'child-one' && item.id === 3).error.code, 'TARGET_INACTIVE');
    await act(async () => { await host.receiveChild(request(4, 'send', { conversationKey: 'a', text: 'stale parent' }), 'one', 'old'); });
    assert.equal(sends.length, 2);
    await act(async () => { await host.receive(request(2, 'children', { windows: [] })); });
    await act(async () => { await host.receiveChild(request(5, 'send', { conversationKey: 'a', text: 'removed child' }), 'one'); });
    assert.equal(sends.length, 2);
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
    // Activating an open candidate is idempotent; close it before testing a new generation.
    await act(async () => { host.closed('current'); });
    let reopened;
    await act(async () => { reopened = host.activate(); });
    await act(async () => { host.ready('next'); await reopened; });
    await act(async () => { finishWorkspace(); });
    assert.deepEqual(host.closes, []);
    assert.equal(host.results.find(item => item.id === 1).error.code, 'STALE_REQUEST');
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
