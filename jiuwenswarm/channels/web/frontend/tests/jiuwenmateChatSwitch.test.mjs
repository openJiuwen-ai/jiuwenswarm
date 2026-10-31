import assert from 'node:assert/strict';
import test from 'node:test';
import React, { act, useLayoutEffect } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  IS_REACT_ACT_ENVIRONMENT: true,
});
const { usePrivateChat } = await import('../node_modules/.cache/jiuwenmate-chat-switch/usePrivateChat.mjs');
const requestA = {
  requestId: 'request-a',
  conversationId: 'conversation-a',
  messageId: 'message-a',
  text: 'old conversation',
  receivedAt: '2026-10-22T00:00:00Z',
  status: 'ended',
  outcome: 'success',
  running: false,
  replies: ['old reply'],
  files: [],
  reason: null,
  draft: null,
  control: null,
  recovery: null,
};
const api = {
  history: (_agentId, conversationId) => {
    if (conversationId === 'conversation-a') return Promise.resolve([requestA]);
    return new Promise((resolve) => {
      resolveConversationB = resolve;
    });
  },
  onStream: () => () => {},
  getRequest: async () => requestA,
};
const commits = [];
let resolveConversationB;

function View({ conversationId }) {
  const chat = usePrivateChat(api, 'agent', conversationId, 0);
  useLayoutEffect(() => {
    commits.push([conversationId, chat.requests.map((request) => request.text), chat.loading]);
  }, [conversationId, chat.requests, chat.loading]);
  return React.createElement('div', null, chat.requests.map((request) => request.text).join(','));
}

test('does not render previous conversation while the newly selected history loads', async () => {
  const root = createRoot(document.getElementById('root'));
  await act(async () => {
    root.render(React.createElement(View, { conversationId: 'conversation-a' }));
    await Promise.resolve();
  });
  assert.equal(document.getElementById('root').textContent, 'old conversation');

  commits.length = 0;
  await act(async () => {
    root.render(React.createElement(View, { conversationId: 'conversation-b' }));
  });

  assert.ok(commits.length > 0);
  assert.ok(commits.every(([, visibleRequests]) => visibleRequests.length === 0));
  assert.equal(commits.at(-1)[2], true);

  await act(async () => {
    resolveConversationB([]);
    await Promise.resolve();
  });
  assert.equal(commits.at(-1)[2], false);
  await act(async () => root.unmount());
});

test('keeps polling while a manually reviewed request resumes', async () => {
  const paused = {
    ...requestA,
    status: 'ended',
    outcome: 'paused',
    replies: [],
    approval: {
      approvalId: 'approval-1',
      requestId: 'request-a',
      expiresAt: '2026-10-22T00:15:00Z',
      status: 'pending',
      intercepted: [{ toolCallId: 'call-1', toolName: 'write_file', arguments: 'path=report.md' }],
    },
  };
  const approved = { ...paused, approval: { ...paused.approval, status: 'approved' } };
  const completed = { ...requestA, replies: ['resumed reply'], approval: null };
  const calls = [];
  let chat;
  const approvalApi = {
    history: async () => [paused],
    onStream: () => () => {},
    getRequest: async () => {
      calls.push('get');
      return completed;
    },
    decidePermission: async () => {
      return approved;
    },
  };
  function ApprovalView() {
    chat = usePrivateChat(approvalApi, 'agent', 'conversation-a', 0);
    return React.createElement('div', null, chat.requests[0]?.outcome ?? 'loading');
  }

  const root = createRoot(document.getElementById('root'));
  await act(async () => {
    root.render(React.createElement(ApprovalView));
    await Promise.resolve();
  });
  await act(async () => {
    await chat.decidePermission('approval-1', 'approve', 'call-1');
    await Promise.resolve();
  });
  await act(async () => new Promise((resolve) => window.setTimeout(resolve, 1600)));

  assert.deepEqual(calls, ['get']);
  assert.equal(chat.requests[0].outcome, 'success');
  assert.equal(chat.requests[0].replies[0], 'resumed reply');
  await act(async () => root.unmount());
});

test.after(() => dom.window.close());

const { build } = await import('esbuild');
const progressiveBundle = 'node_modules/.cache/jiuwenmate-chat-switch/useProgressiveText.mjs';
await build({
  entryPoints: ['src/hooks/useProgressiveText.ts'], outfile: progressiveBundle,
  bundle: true, platform: 'node', format: 'esm', packages: 'external',
});
const { useProgressiveText } = await import(`../${progressiveBundle}`);

test('live replies reveal Unicode characters and keep their prefix when the final text arrives', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] });
  const root = createRoot(document.getElementById('root'));
  function Reply({ text, animate }) {
    return React.createElement('div', null, useProgressiveText(text, animate));
  }
  const render = async (text, animate) => act(async () => {
    root.render(React.createElement(Reply, { text, animate }));
  });
  await render('历史回复', false);
  assert.equal(document.getElementById('root').textContent, '历史回复');
  await render('A😀B', true);
  assert.equal(document.getElementById('root').textContent, '');
  await act(async () => t.mock.timers.tick(16));
  assert.equal(document.getElementById('root').textContent, 'A');
  await render('A😀BCD', true);
  assert.equal(document.getElementById('root').textContent, 'A');
  await act(async () => t.mock.timers.tick(16));
  assert.equal(document.getElementById('root').textContent, 'A😀');
  await act(async () => t.mock.timers.tick(160));
  assert.equal(document.getElementById('root').textContent, 'A😀BCD');
  await act(async () => root.unmount());
});

const groupContentBundle = 'node_modules/.cache/jiuwenmate-chat-switch/GroupMessageContent.mjs';
await build({
  entryPoints: ['src/features/groupChat/GroupMessageContent.tsx'], outfile: groupContentBundle,
  bundle: true, platform: 'node', format: 'esm', packages: 'external', jsx: 'automatic',
  plugins: [{ name: 'group-markdown', setup(plugin) {
    plugin.onResolve({ filter: /components\/MarkdownRenderer$/ }, () => ({ path: 'markdown', namespace: 'test' }));
    plugin.onLoad({ filter: /.*/, namespace: 'test' }, () => ({
      contents: 'export const MarkdownRenderer = ({ content }) => content;', loader: 'js',
    }));
  } }],
});
const { GroupMessageContent } = await import(`../${groupContentBundle}`);

test('group replies animate even when delivered as a complete message, while history and user text stay instant', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] });
  const root = createRoot(document.getElementById('root'));
  const render = async (id, sender, animate) => act(async () => {
    root.render(React.createElement(GroupMessageContent, {
      key: id, message: { message_id: id, sender, content: '你好😀，欢迎一起规划旅行', attachments: [] }, animate,
    }));
  });
  await render('history', 'travel-team', false);
  assert.equal(document.getElementById('root').textContent, '你好😀，欢迎一起规划旅行');
  await render('new-reply', 'travel-team', true);
  assert.equal(document.getElementById('root').textContent, '');
  await act(async () => t.mock.timers.tick(16));
  assert.equal(document.getElementById('root').textContent, '你');
  await act(async () => t.mock.timers.tick(1600));
  assert.equal(document.getElementById('root').textContent, '你好😀，欢迎一起规划旅行');
  await render('user', 'user', true);
  assert.equal(document.getElementById('root').textContent, '你好😀，欢迎一起规划旅行');
  await act(async () => root.unmount());
});

const workspaceBundle = 'node_modules/.cache/jiuwenmate-chat-switch/GroupChatWorkspace.mjs';
const workspaceMocks = {
  'services/webClient': `export const webClient = globalThis.groupTest.client;`,
  'services/teamAttach': `export const attachTeamSession = async () => {};`,
  'stores/sessionStore': `export const useSessionStore = (select) => select({ defaultModelName: 'test' });`,
  'stores/workspaceStore': `export const useWorkspaceStore = {};`,
  '/ModelPicker': `export default () => null;`,
  '/TeamMemberAvatar': `export const TeamMemberAvatar = () => null;`,
  '/Dialog/Dialog': `export const Dialog = () => null;`,
  '/MessageItem': `export const FileDownloadList = () => null;`,
  '/fileDownloadDedup': `export const getFileIdentityKey = (file) => file.path;`,
  './client': `export const groupChatClient = globalThis.groupTest.api;`,
  './MemberPicker': `export const MemberPicker = () => null;`,
  './ExecutionCard': `export const ExecutionCard = () => null;`,
  './GroupFileRow': `export const GroupFilePreviewPanel = () => null; export const GroupFileRow = () => null;
    export const groupFileTarget = () => ({});`,
  'agentManagement/groupClient': `export const createAgentGroupManagementClient = () => ({ listGroups: async () => [] });`,
  '/InteractionPrompt': `export const InteractionPrompt = () => null;`,
  '/MarkdownRenderer': `export const MarkdownRenderer = ({ content }) => content;`,
  'react-i18next': `const t = (key) => key; export const initReactI18next = { type: '3rdParty' };
    export const useTranslation = () => ({ t, i18n: { language: 'zh' } });`,
};
await build({
  entryPoints: ['src/features/groupChat/GroupChatWorkspace.tsx'], outfile: workspaceBundle,
  bundle: true, platform: 'node', format: 'esm', packages: 'external', jsx: 'automatic', loader: { '.css': 'empty' },
  plugins: [{ name: 'workspace-boundaries', setup(plugin) {
    plugin.onResolve({ filter: /.*/ }, (args) => {
      const key = Object.keys(workspaceMocks).find((name) => args.path.endsWith(name));
      return key ? { path: key, namespace: 'workspace-test' } : undefined;
    });
    plugin.onLoad({ filter: /.*/, namespace: 'workspace-test' }, ({ path }) => ({ contents: workspaceMocks[path] }));
  } }],
});

test('group WebSocket delivery triggers typing and follows resized replies until the user scrolls up', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] });
  const listeners = new Map();
  const history = [{ message_id: 'old', sender: 'team', content: '已有消息', timestamp: 1, attachments: [] }];
  globalThis.groupTest = {
    client: { getState: () => 'ready', on: (name, callback) => {
      listeners.set(name, callback); return () => listeners.delete(name);
    }, onStateChange: () => () => {} },
    api: { get: async () => ({ name: 'Test group', members: [
      { kind: 'agent_template', id: 'claude', member_name: 'claude', display_name: '11', cli_agent: 'claude' },
      { kind: 'agent_template', id: 'codex', member_name: 'codex', display_name: '22', cli_agent: 'codex' },
      { kind: 'agent_template', id: 'native', member_name: 'native', display_name: '普通专家' },
    ] }),
      history: async (_groupId, page) => ({ total: 1, messages: history.slice(0, page.limit) }),
      executions: async () => ({ executions: [], total: 0 }) },
  };
  const resizeCallbacks = new Set();
  globalThis.ResizeObserver = class {
    constructor(callback) { this.callback = callback; resizeCallbacks.add(callback); }
    observe() {}
    disconnect() { resizeCallbacks.delete(this.callback); }
  };
  const { GroupChatWorkspace } = await import(`../${workspaceBundle}`);
  const root = createRoot(document.getElementById('root'));
  await act(async () => {
    root.render(React.createElement(GroupChatWorkspace, { groupId: 'test-group', directory: {} }));
  });
  const list = document.querySelector('[data-testid="group-chat-messages"]');
  assert.ok(list.textContent.includes('已有消息'));
  const providers = [...document.querySelectorAll('[data-testid="group-chat-member-provider"]')];
  assert.deepEqual(providers.map((item) => item.textContent), ['Claude Code', 'Codex']);
  await act(async () => listeners.get('group.message')({ event: 'group.message', payload: {
    group_id: 'test-group', message: { message_id: 'new', sender: 'team', content: '新的群聊回复',
      timestamp: 2, attachments: [] },
  } }));
  const reply = document.querySelector('[data-variant="new"]');
  assert.ok(!reply.textContent.includes('新的群聊回复'));
  await act(async () => t.mock.timers.tick(16));
  assert.ok(reply.textContent.endsWith('新'));
  let height = 1000;
  Object.defineProperty(list, 'scrollHeight', { get: () => height });
  Object.defineProperty(list, 'clientHeight', { value: 100 });
  list.scrollTop = 900;
  await act(async () => list.dispatchEvent(new dom.window.Event('scroll', { bubbles: true })));
  height = 1200;
  for (const callback of resizeCallbacks) callback();
  assert.equal(list.scrollTop, 1200);
  list.scrollTop = 100;
  await act(async () => list.dispatchEvent(new dom.window.Event('scroll', { bubbles: true })));
  height = 1400;
  for (const callback of resizeCallbacks) callback();
  assert.equal(list.scrollTop, 100);
  await act(async () => t.mock.timers.tick(1000));
  assert.ok(reply.textContent.endsWith('新的群聊回复'));
  await act(async () => root.unmount());
});


test('slow initial history and repeated reloads share one request and eventually leave loading', async () => {
  let resolveHistory;
  let calls = 0;
  let chat;
  const history = new Promise((resolve) => { resolveHistory = resolve; });
  const testApi = { history: () => { calls += 1; return history; }, onStream: () => () => {} };
  function Chat() {
    chat = usePrivateChat(testApi, 'agent', 'conversation-a', 0);
    return React.createElement('div', null, chat.loading ? 'loading' : chat.requests.map((r) => r.text).join(','));
  }
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(React.createElement(Chat)));
    await act(async () => { void chat.reload(); void chat.reload(); void chat.reload(); });
    assert.equal(calls, 1, 'Repeated reloads must not stack full histories behind the initial read');
    await act(async () => resolveHistory([requestA]));
    assert.equal(chat.loading, false);
    assert.equal(document.getElementById('root').textContent, 'old conversation');
  } finally { await act(async () => root.unmount()); }
});

test('reconnect starts a fresh history and ignores the older same-conversation response', async () => {
  const responses = [];
  let chat;
  const testApi = { history: () => new Promise((resolve) => responses.push(resolve)), onStream: () => () => {} };
  function Chat({ epoch }) {
    chat = usePrivateChat(testApi, 'agent', 'conversation-a', epoch);
    return React.createElement('div', null, chat.requests.map((r) => r.text).join(','));
  }
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(React.createElement(Chat, { epoch: 0 })));
    await act(async () => root.render(React.createElement(Chat, { epoch: 1 })));
    assert.equal(responses.length, 2);
    await act(async () => responses[1]([{ ...requestA, text: 'reconnected history' }]));
    await act(async () => responses[0]([requestA]));
    assert.equal(chat.loading, false);
    assert.equal(document.getElementById('root').textContent, 'reconnected history');
  } finally { await act(async () => root.unmount()); }
});


test('a slow history snapshot does not erase a request refreshed from a live ended event', async () => {
  let resolveHistory;
  let emit;
  let chat;
  const testApi = {
    history: () => new Promise((resolve) => { resolveHistory = resolve; }),
    onStream: (callback) => { emit = callback; return () => {}; },
    getRequest: async () => requestA,
  };
  function Chat() {
    chat = usePrivateChat(testApi, 'agent', 'conversation-a', 0);
    return React.createElement('div', null, chat.requests.map((r) => r.text).join(','));
  }
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(React.createElement(Chat)));
    await act(async () => emit({ kind: 'ended', agentId: 'agent', requestId: requestA.requestId, executionId: 'exec', seq: 1, text: '' }));
    await act(async () => resolveHistory([]));
    assert.equal(chat.loading, false);
    assert.equal(document.getElementById('root').textContent, 'old conversation');
  } finally { await act(async () => root.unmount()); }
});

for (const pending of [{ status: 'dispatched', outcome: null }, { status: 'ended', outcome: 'paused' }]) {
test(`a terminal history snapshot is not downgraded by an earlier ${pending.status}/${pending.outcome} response`, async () => {
  let resolveHistory;
  let emit;
  let chat;
  const testApi = {
    history: () => new Promise((resolve) => { resolveHistory = resolve; }),
    onStream: (callback) => { emit = callback; return () => {}; },
    getRequest: async () => ({ ...requestA, ...pending, replies: [], running: pending.status === 'dispatched', approval: null }),
  };
  function Chat() { chat = usePrivateChat(testApi, 'agent', 'conversation-a', 0); return null; }
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(React.createElement(Chat)));
    await act(async () => emit({ kind: 'delta', agentId: 'agent', requestId: requestA.requestId, executionId: 'exec', seq: 1, text: 'draft' }));
    await act(async () => resolveHistory([{ ...requestA, outcome: 'completed' }]));
    assert.equal(chat.requests[0].status, 'ended');
    assert.deepEqual(chat.requests[0].replies, ['old reply']);
  } finally { await act(async () => root.unmount()); }
});

}

const pickerBundle = 'node_modules/.cache/jiuwenmate-chat-switch/MemberPicker.mjs';
const pickerMocks = {
  '/agentManagement/client': `export const createAgentManagementClient = () => ({ listCatalog: async () => [{
    id: 'external', runtimePackageName: 'external', installed: true, enabled: true,
    displayName: 'Third-party expert', description: '', avatarUrl: null, runtime: { provider_name: 'codex' },
  }] });`,
  '/agentManagement/groupClient': `export const createAgentGroupManagementClient = () => ({ listGroups: async () => [] });`,
  './client': `export const groupChatClient = globalThis.pickerTest.api;`,
  '/Dialog/Dialog': `import React from 'react'; export const Dialog = ({ open, children }) =>
    open ? React.createElement('div', { role: 'dialog' }, children) : null;`,
  'react-i18next': `const t = (key, values) => values?.agents ? key + ': ' + values.agents : key;
    export const useTranslation = () => ({ t });`,
};
await build({
  entryPoints: ['src/features/groupChat/MemberPicker.tsx'], outfile: pickerBundle,
  bundle: true, platform: 'node', format: 'esm', packages: 'external', jsx: 'automatic',
  external: ['react', 'react/jsx-runtime'],
  plugins: [{ name: 'picker-boundaries', setup(plugin) {
    plugin.onResolve({ filter: /.*/ }, (args) => {
      const key = Object.keys(pickerMocks).find((name) => args.path.endsWith(name));
      return key ? { path: key, namespace: 'picker-test' } : undefined;
    });
    plugin.onLoad({ filter: /.*/, namespace: 'picker-test' }, ({ path }) => ({ contents: pickerMocks[path] }));
  } }],
});

test('member picker explains a disabled CLI and preserves the selection for retry', async () => {
  const error = Object.assign(new Error('codex'), { code: 'EXTERNAL_CLI_DISABLED' });
  let allowed = false;
  let saved = false;
  const group = { group_id: 'test', members: [] };
  globalThis.pickerTest = { api: {
    addMember: async () => { if (!allowed) throw error; },
    get: async () => group,
  } };
  const { MemberPicker } = await import(`../${pickerBundle}`);
  const root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(MemberPicker, {
    group, onClose: () => {}, onSaved: () => { saved = true; },
  })));
  await act(async () => document.querySelector('[data-testid="group-chat-candidate-select"]').click());
  await act(async () => document.querySelector('[data-testid="group-chat-picker-save"]').click());
  assert.equal(saved, false);
  assert.equal(document.querySelector('[data-testid="group-chat-runtime-notice-title"]').textContent,
    'chat.agentGroupRuntimeDisabledTitle');
  assert.equal(document.querySelector('[data-testid="group-chat-runtime-notice-message"]').textContent,
    'groupChat.externalRuntimeDisabled: Codex');
  assert.equal(document.querySelector('[data-testid="group-chat-picker-error"]'), null);
  await act(async () => document.querySelector('[data-testid="group-chat-runtime-notice-confirm"]').click());
  assert.equal(document.querySelectorAll('[data-testid="group-chat-selected-member"]').length, 1);
  allowed = true;
  await act(async () => document.querySelector('[data-testid="group-chat-picker-save"]').click());
  assert.equal(saved, true);
  await act(async () => root.unmount());
});
