import assert from 'node:assert/strict';
import test, { before } from 'node:test';
import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM, VirtualConsole } from 'jsdom';

const frontendRoot = new URL('..', import.meta.url);
const prefix = 'PROVENANCE 记录了来源是 DSH（';
const tail = 'MIT）。我再确认 DSH 的原始仓库地址/作者，以及轨迹 UI 的挂载结构。';
const fullContent = prefix + tail;
let useWebSocket;
let useChatStore;
let useSessionStore;
let webClient;

before(async () => {
  const cacheDir = fileURLToPath(new URL('node_modules/.cache/websocket-stream-boundary/', frontendRoot));
  await mkdir(cacheDir, { recursive: true });
  // One split build keeps the hook and assertions on the same real store/client instances.
  // Only the native WebSocket transport is replaced below; no event handlers are stubbed.
  await build({
    entryPoints: [
      { in: 'src/hooks/useWebSocket.ts', out: 'hook' },
      { in: 'src/stores/chatStore.ts', out: 'chatStore' },
      { in: 'src/stores/sessionStore.ts', out: 'sessionStore' },
      { in: 'src/services/webClient.ts', out: 'webClient' },
    ],
    absWorkingDir: fileURLToPath(frontendRoot),
    bundle: true,
    splitting: true,
    format: 'esm',
    platform: 'node',
    packages: 'external',
    define: { 'import.meta.env': '{"DEV":false}' },
    outdir: cacheDir,
  });
  ({ useWebSocket } = await import(pathToFileURL(join(cacheDir, 'hook.js')).href));
  ({ useChatStore } = await import(pathToFileURL(join(cacheDir, 'chatStore.js')).href));
  ({ useSessionStore } = await import(pathToFileURL(join(cacheDir, 'sessionStore.js')).href));
  ({ webClient } = await import(pathToFileURL(join(cacheDir, 'webClient.js')).href));
});

function installDom() {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', {
    url: 'http://localhost/',
    virtualConsole: new VirtualConsole(),
  });
  const sockets = [];
  class TestWebSocket {
    static OPEN = 1;
    readyState = 0;
    closeListeners = [];

    constructor() {
      sockets.push(this);
      queueMicrotask(() => {
        this.readyState = TestWebSocket.OPEN;
        this.onopen?.();
      });
    }

    receive(event, payload) {
      assert.equal(this.readyState, TestWebSocket.OPEN);
      this.onmessage({ data: JSON.stringify({ type: 'event', event, payload }) });
    }

    addEventListener(name, listener) {
      assert.equal(name, 'close');
      this.closeListeners.push(listener);
    }

    close(code, reason) {
      this.readyState = 3;
      const event = { code, reason, wasClean: true };
      this.onclose?.(event);
      this.closeListeners.splice(0).forEach((listener) => listener(event));
    }
  }
  const globals = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    localStorage: dom.window.localStorage,
    WebSocket: TestWebSocket,
    IS_REACT_ACT_ENVIRONMENT: true,
  };
  const previous = new Map();
  for (const [name, value] of Object.entries(globals)) {
    previous.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  return {
    sockets,
    restore() {
      dom.window.close();
      for (const [name, descriptor] of previous) {
        if (descriptor) Object.defineProperty(globalThis, name, descriptor);
        else delete globalThis[name];
      }
    },
  };
}

async function mountConnection(context, sessionIds) {
  const dom = installDom();
  context.mock.timers.enable({ apis: ['Date', 'setTimeout', 'setInterval'], now: Date.parse('2026-09-08T07:33:51.119Z') });
  const root = createRoot(document.getElementById('root'));
  for (const sessionId of sessionIds) {
    useChatStore.getState().ensureRuntime(sessionId);
    useSessionStore.getState().ensureRuntime(sessionId);
    useSessionStore.getState().setMode(sessionId, 'agent');
  }
  function Probe() {
    useWebSocket({ activeSessionId: sessionIds[0] });
    return null;
  }
  await act(async () => root.render(createElement(Probe)));
  assert.equal(webClient.getState(), 'ready');
  assert.equal(dom.sockets.length, 1);
  const socket = dom.sockets[0];
  return {
    runtime: (sessionId = sessionIds[0]) => useChatStore.getState().getRuntime(sessionId),
    delta(sessionId, content) {
      act(() => socket.receive('chat.delta', { session_id: sessionId, content }));
    },
    tool(sessionId, id) {
      act(() => socket.receive('chat.tool_call', {
        session_id: sessionId,
        tool_call_id: id,
        name: 'grep',
        arguments: { pattern: 'DSH' },
        timestamp: new Date().toISOString(),
      }));
    },
    tick(ms) {
      act(() => context.mock.timers.tick(ms));
    },
    async dispose() {
      await act(async () => root.unmount());
      for (const sessionId of sessionIds) {
        useChatStore.getState().removeRuntime(sessionId);
        useSessionStore.getState().removeRuntime(sessionId);
      }
      dom.restore();
      context.mock.timers.reset();
    },
  };
}

test('tool call within the batch interval preserves the entire previous segment and isolates the next one', async (context) => {
  const sessionId = 'stream-boundary';
  const connection = await mountConnection(context, [sessionId]);
  try {
    connection.delta(sessionId, prefix);
    connection.tick(16);
    assert.equal(connection.runtime().messages[0].content, prefix);
    const firstMessageId = connection.runtime().currentStreamId;

    connection.delta(sessionId, tail);
    connection.tick(1);
    assert.equal(connection.runtime().messages[0].content, prefix, 'the tail is still pending before the tool event');
    connection.tool(sessionId, 'grep-source');

    let runtime = connection.runtime();
    assert.equal(runtime.messages.length, 1);
    assert.equal(runtime.messages[0].content, fullContent);
    assert.equal(runtime.messages[0].isStreaming, false);
    assert.equal(runtime.currentStreamId, null);
    assert.equal(runtime.assistantStreamSplit, true);
    assert.deepEqual(runtime.toolExecutionOrder, ['grep-source']);

    connection.delta(sessionId, '下一段正文。');
    assert.notEqual(connection.runtime().currentStreamId, firstMessageId);
    connection.tick(16);
    runtime = connection.runtime();
    assert.deepEqual(runtime.messages.map((message) => message.content), [fullContent, '下一段正文。']);
    assert.equal(runtime.messages[1].isStreaming, true);
    connection.tick(16);
    assert.deepEqual(connection.runtime().messages.map((message) => message.content), [fullContent, '下一段正文。']);
  } finally {
    await connection.dispose();
  }
});

test('a tool boundary flushes only its own session while another session continues batching', async (context) => {
  const first = 'stream-boundary-first';
  const second = 'stream-boundary-second';
  const connection = await mountConnection(context, [first, second]);
  try {
    connection.delta(first, prefix);
    connection.delta(second, '另一个会话：');
    connection.tick(16);
    connection.delta(first, tail);
    connection.delta(second, '内容完整。');
    const secondStreamId = connection.runtime(second).currentStreamId;
    connection.tick(1);
    connection.tool(first, 'first-session-tool');

    assert.equal(connection.runtime(first).messages[0].content, fullContent);
    assert.equal(connection.runtime(second).messages[0].content, '另一个会话：');
    assert.equal(connection.runtime(second).currentStreamId, secondStreamId);
    assert.equal(connection.runtime(second).messages[0].isStreaming, true);
    assert.deepEqual(connection.runtime(second).toolExecutionOrder, []);

    connection.tick(15);
    assert.equal(connection.runtime(second).messages[0].content, '另一个会话：内容完整。');
    assert.equal(connection.runtime(first).messages[0].content, fullContent);
  } finally {
    await connection.dispose();
  }
});

test('tool calls without pending deltas preserve finalized text and do not create empty segments', async (context) => {
  const sessionId = 'stream-boundary-no-pending';
  const connection = await mountConnection(context, [sessionId]);
  try {
    connection.tool(sessionId, 'tool-before-text');
    assert.deepEqual(connection.runtime().messages, []);
    connection.delta(sessionId, fullContent);
    connection.tick(16);
    connection.tool(sessionId, 'tool-after-flush');
    connection.tool(sessionId, 'tool-after-finalize');
    connection.tick(32);

    const runtime = connection.runtime();
    assert.equal(runtime.messages.length, 1);
    assert.equal(runtime.messages[0].content, fullContent);
    assert.equal(runtime.messages[0].isStreaming, false);
    assert.equal(runtime.currentStreamId, null);
    assert.deepEqual(runtime.toolExecutionOrder, ['tool-before-text', 'tool-after-flush', 'tool-after-finalize']);
  } finally {
    await connection.dispose();
  }
});
