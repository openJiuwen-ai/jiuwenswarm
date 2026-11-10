import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, { window: dom.window, document: dom.window.document,
  localStorage: dom.window.localStorage, HTMLElement: dom.window.HTMLElement,
  IS_REACT_ACT_ENVIRONMENT: true });
Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true });
dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });

const requests = [];
const first = '{"role":"user","content":"HEAD 中文🙂';
const last = 'TAIL 原文"}';
let rejectReads = false;
const transport = process.env.AUDIT_TEST_TRANSPORT || 'http';
class MockWebSocket {
  static OPEN = 1;
  static CLOSED = 3;
  static instances = [];
  readyState = 0;
  listeners = new Map();
  addEventListener(name, listener) {
    this.listeners.set(name, [...(this.listeners.get(name) || []), listener]);
  }
  constructor() {
    MockWebSocket.instances.push(this);
    queueMicrotask(() => { this.readyState = 1; this.onopen?.({}); });
  }
  send(raw) {
    const message = JSON.parse(raw);
    requests.push(message);
    const query = message.params.llm_audit;
    let payload;
    if (query.action === 'list') {
      payload = { calls: [{ call_id: 'native-call', request_id: 'request', model_name: 'controlled-model',
        started_ns: 1794300000000000000, status: 'completed', latency_ms: 123,
        usage: { total_tokens: 45 }, input: { messages: 1, tools: 1 } }], next_cursor: null };
    } else if (query.action === 'parts') {
      payload = { collection: query.collection, total: 1,
        parts: [{ index: 0, sha256: 'a'.repeat(64), bytes: 100000, media_type: 'application/json' }] };
    } else if (query.action === 'read') {
      const content = query.offset === 0 ? first : last;
      payload = { content, sha256: 'a'.repeat(64), bytes: 100000, offset: query.offset,
        next_offset: query.offset + Buffer.byteLength(content), eof: query.offset > 0 };
    }
    queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({ type: 'res', id: message.id,
      ok: !(rejectReads && query.action === 'read'), payload: { llm_audit: payload }, error: 'controlled read failure' }) }));
  }
  close() {
    this.readyState = 3;
    this.onclose?.({ code: 1000, reason: '' });
    for (const listener of this.listeners.get('close') || []) listener({});
  }
}
globalThis.WebSocket = MockWebSocket;
dom.window.WebSocket = MockWebSocket;

let LlmCallAudit;
let root;
before(async () => {
  ({ LlmCallAudit } = await import(`../node_modules/.cache/llm-audit/${transport}.mjs`));
  root = createRoot(document.getElementById('root'));
});
after(async () => {
  await act(async () => root.unmount());
  dom.window.close();
});

const find = (id) => document.querySelector(`[data-testid="${id}"]`);
async function click(id) {
  assert.ok(find(id), id);
  await act(async () => { find(id).click(); await new Promise((resolve) => setTimeout(resolve, 20)); });
}

test('full original is requested only on selection, and byte pages replace the visible chunk', async () => {
  await act(async () => root.render(React.createElement(LlmCallAudit, { sessionId: 'selected-session' })));
  assert.equal(requests.length, 0);
  await click('chat-panel-llm-audit-open');
  assert.equal(requests[0].method, 'session.get_metadata');
  assert.equal(requests[0].params.session_id, 'selected-session');
  assert.equal(requests.filter((request) => request.params.llm_audit.action === 'read').length, 0);
  await click('chat-panel-llm-audit-call');
  await click('chat-panel-llm-audit-part');
  assert.equal(find('chat-panel-llm-audit-original').textContent, first);
  const request = requests.at(-1).params.llm_audit;
  assert.equal(request.max_bytes, 65536);
  assert.equal(request.call_id, 'native-call');
  await click('chat-panel-llm-audit-next-chunk');
  assert.equal(find('chat-panel-llm-audit-original').textContent, last);
  await click('chat-panel-llm-audit-previous-chunk');
  assert.equal(find('chat-panel-llm-audit-original').textContent, first);
  assert.match(find('chat-panel-llm-audit-summary').textContent, /controlled-model/);
  await click('chat-panel-llm-audit-close');
  assert.equal(MockWebSocket.instances.at(-1).readyState,
    transport === 'http' ? MockWebSocket.CLOSED : MockWebSocket.OPEN);
});

test('read failure is visible and a new session uses its own request scope', async () => {
  await act(async () => root.render(React.createElement(LlmCallAudit, { sessionId: 'another-session' })));
  await click('chat-panel-llm-audit-open');
  assert.equal(requests.at(-1).params.session_id, 'another-session');
  await click('chat-panel-llm-audit-call');
  rejectReads = true;
  await click('chat-panel-llm-audit-part');
  assert.ok(find('chat-panel-llm-audit-error').textContent.length > 0);
  await click('chat-panel-llm-audit-close');
});
