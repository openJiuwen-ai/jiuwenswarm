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
