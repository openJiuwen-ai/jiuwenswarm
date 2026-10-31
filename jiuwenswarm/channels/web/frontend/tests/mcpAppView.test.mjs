import assert from 'node:assert/strict';
import { test } from 'node:test';
import React, { act } from 'react';
import { JSDOM } from 'jsdom';
import { LATEST_PROTOCOL_VERSION } from '@modelcontextprotocol/ext-apps/app-bridge';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  localStorage: dom.window.localStorage,
  sessionStorage: dom.window.sessionStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
});
const { createRoot } = await import('react-dom/client');
const { McpAppView } = await import('../node_modules/.cache/mcp-app-view/components/ChatPanel/McpAppView.js');
const { webClient } = await import('../node_modules/.cache/mcp-app-view/services/webClient.js');
const { getMcpAppContexts, setMcpAppMessageHandler } =
  await import('../node_modules/.cache/mcp-app-view/features/mcpApps/mcpAppContext.js');
const tick = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
const find = (id) => document.querySelector(`[data-testid="chat-panel-mcp-app-view-${id}"]`);

test('real AppBridge stores context, confirms messages, preserves tool approval and cleans up', async () => {
  const originalRequest = webClient.request;
  const originalFetch = globalThis.fetch;
  const root = createRoot(document.getElementById('root'));
  const sent = [];
  const calls = [];
  const replies = [];
  const clearHandler = setMcpAppMessageHandler(async (sessionId, content) => {
    sent.push({ sessionId, content });
    return true;
  });
  globalThis.fetch = async () => ({ ok: false });
  webClient.request = async (method) => {
    calls.push(method);
    if (method === 'mcp_app.read_resource') {
      return { contents: [{ uri: 'ui://test/app.html', mimeType: 'text/html;profile=mcp-app', text: '<html></html>' }] };
    }
    if (method === 'mcp_app.list_tools') return { tools: [{ name: 'read', annotations: { readOnlyHint: true } }] };
    if (method === 'mcp_app.call_tool') return { content: [{ type: 'text', text: 'ok' }] };
    throw new Error(`Unexpected RPC: ${method}`);
  };
  try {
    await act(async () => root.render(React.createElement(McpAppView, {
      app: { server: 'test', tool: 'show', resourceUri: 'ui://test/app.html', arguments: {} },
      sessionId: 'session-1',
      appInstanceId: 'call-1',
    })));
    await tick();
    const frameWindow = find('frame').contentWindow;
    frameWindow.postMessage = (message) => {
      replies.push(message);
      if (message.method === 'ui/resource-teardown') {
        queueMicrotask(() => receive({ id: message.id, result: {} }));
      }
    };
    function receive(payload) {
      window.dispatchEvent(new dom.window.MessageEvent('message', {
        source: frameWindow,
        data: { jsonrpc: '2.0', ...payload },
      }));
    }
    async function request(id, method, params) {
      await act(async () => receive({ id, method, params }));
      await tick();
    }
    await request(1, 'ui/initialize', {
      protocolVersion: LATEST_PROTOCOL_VERSION,
      appInfo: { name: 'test', version: '1.0' },
      appCapabilities: {},
    });
    assert.deepEqual(replies.find((reply) => reply.id === 1).result.hostCapabilities.message, { text: {} });
    await act(async () => receive({ method: 'ui/notifications/initialized' }));
    await request(2, 'ui/update-model-context', { structuredContent: { selected: 42 } });
    assert.equal(getMcpAppContexts('session-1')[0].structured_content.selected, 42);
    assert.deepEqual(getMcpAppContexts('another-session'), []);

    await request(3, 'ui/message', { role: 'user', content: [{ type: 'text', text: 'use my selection' }] });
    assert.deepEqual(sent, []);
    assert.ok(find('message-confirmation'));
    await act(async () => find('message-send-btn').click());
    await tick();
    assert.deepEqual(sent, [{ sessionId: 'session-1', content: 'use my selection' }]);
    assert.deepEqual(replies.find((reply) => reply.id === 3).result, {});

    await request(4, 'tools/call', { name: 'read', arguments: {} });
    assert.ok(find('approval-read-only-hint'));
    assert.equal(calls.includes('mcp_app.call_tool'), false);
    await act(async () => find('approval-deny-btn').click());
    await tick();
    assert.equal(replies.find((reply) => reply.id === 4).result.isError, true);

    await request(5, 'ui/message', { role: 'user', content: [{ type: 'text', text: 'cancel me' }] });
    await act(async () => find('message-cancel-btn').click());
    await tick();
    assert.equal(sent.length, 1);
    assert.equal(replies.find((reply) => reply.id === 5).result.isError, true);
  } finally {
    await act(async () => root.unmount());
    assert.deepEqual(getMcpAppContexts('session-1'), []);
    clearHandler();
    webClient.request = originalRequest;
    globalThis.fetch = originalFetch;
    dom.window.close();
  }
});
