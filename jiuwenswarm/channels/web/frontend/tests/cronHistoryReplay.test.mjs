import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { setImmediate as settle } from 'node:timers/promises';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
for (const [name, value] of Object.entries({
  window: dom.window, document: dom.window.document, localStorage: dom.window.localStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
})) Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
window.__JIUWENSWARM_EDITION__ = 'enterprise';
window.__JIUWEN_WEB_TRANSPORT__ = 'http';
after(() => dom.window.close());

await build({
  entryPoints: {
    hook: 'src/hooks/useWebSocket.ts', watch: 'src/features/cron/cronSessionLive.ts',
    chat: 'src/stores/chatStore.ts', session: 'src/stores/sessionStore.ts', client: 'src/services/webClient.ts',
  },
  bundle: true, splitting: true, packages: 'external', platform: 'node', format: 'esm',
  outdir: 'node_modules/.cache/cron-history-replay', outExtension: { '.js': '.mjs' },
  define: { 'import.meta.env': '{}' },
});
const { useWebSocket } = await import('../node_modules/.cache/cron-history-replay/hook.mjs');
const { watchCronExecutionSession } = await import('../node_modules/.cache/cron-history-replay/watch.mjs');
const { useChatStore } = await import('../node_modules/.cache/cron-history-replay/chat.mjs');
const { useSessionStore } = await import('../node_modules/.cache/cron-history-replay/session.mjs');
const { webClient } = await import('../node_modules/.cache/cron-history-replay/client.mjs');

async function fixture(run) {
  const sid = 'cron_history_replay';
  const originals = { fetch: globalThis.fetch, connect: webClient.connect, request: webClient.request };
  const previousChat = useChatStore.getState();
  const previousSession = useSessionStore.getState();
  let stream;
  globalThis.fetch = async () => new Response(new ReadableStream({ start(controller) { stream = controller; } }));
  // Replace only transport I/O; all production subscriptions and chat reducers run.
  webClient.connect = async () => {};
  webClient.request = async () => ({});
  useChatStore.getState().ensureRuntime(sid);
  useSessionStore.getState().ensureRuntime(sid);
  useSessionStore.getState().setMode(sid, 'agent');
  const root = createRoot(document.getElementById('root'));
  function Probe() { useWebSocket({ activeSessionId: sid }); return null; }
  await act(async () => root.render(createElement(Probe)));
  const stop = watchCronExecutionSession(sid);
  await settle();
  const control = {
    showHistory(contents) {
      useChatStore.getState().replaceHistoryMessages(sid, contents.map((content, index) => ({
        id: `history-${index}`, role: index === 0 ? 'user' : 'assistant', content,
        timestamp: new Date().toISOString(),
      })));
      useChatStore.getState().setHistoryPagerMeta(sid, {
        nextCursor: null, hasMore: false, snapshotId: 'snapshot', snapshotEnd: 99999,
        loadedBatchSeq: 1, publishedBatchSeq: 1, historyComplete: true,
      });
    },
    async send(event, payload) {
      await act(async () => {
        stream.enqueue(new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify({ ...payload, session_id: sid })}\n\n`));
        await settle();
        await new Promise(resolve => setTimeout(resolve, 60));
      });
    },
    runtime: () => useChatStore.getState().getRuntime(sid),
  };
  try { await run(control); }
  finally {
    stop();
    stream.close();
    await act(async () => root.unmount());
    globalThis.fetch = originals.fetch;
    webClient.connect = originals.connect;
    webClient.request = originals.request;
    useChatStore.setState(previousChat, true);
    useSessionStore.setState(previousSession, true);
  }
}

test('history replay adds no second answer, while subsequent live output is rendered and completed', () => fixture(async control => {
  control.showHistory(['question', 'stored answer']);
  await control.send('chat.delta', { content: 'stored ', live_seq: 1, live_replay: true });
  await control.send('chat.delta', { content: 'answer', live_seq: 2, live_replay: true });
  await control.send('chat.final', { content: 'stored answer', live_seq: 3, live_replay: true });
  assert.deepEqual(control.runtime().messages.map(message => message.content), ['question', 'stored answer']);
  await control.send('chat.delta', { content: 'new answer', live_seq: 4, live_replay: false });
  await control.send('chat.final', { content: 'new complete answer', live_seq: 5, live_replay: false });
  await control.send('chat.processing_status', { is_processing: false, live_seq: 6, live_replay: false });
  assert.deepEqual(control.runtime().messages.map(message => message.content), ['question', 'stored answer', 'new complete answer']);
  assert.equal(control.runtime().isProcessing, false);
}));

test('a final published after the history snapshot is rendered even when it arrives from the replay buffer', () => fixture(async control => {
  control.showHistory(['question']);
  await control.send('chat.final', { content: 'late answer', live_seq: 1, live_replay: true });
  await control.send('chat.processing_status', { is_processing: false, live_seq: 2, live_replay: true });
  assert.deepEqual(control.runtime().messages.map(message => message.content), ['question', 'late answer']);
  assert.equal(control.runtime().isProcessing, false);
}));
