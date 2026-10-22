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

test.after(() => dom.window.close());
