import assert from 'node:assert/strict';
import test from 'node:test';

globalThis.window = globalThis;
globalThis.window.location = { protocol: 'http:', host: 'localhost:18180' };
const values = new Map();
globalThis.localStorage = {
  getItem: (key) => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, value),
  removeItem: (key) => values.delete(key),
};

const { useCronStore } = await import('../node_modules/.cache/cron-run-notifications/stores/cronStore.mjs');
const { useChatStore } = await import('../node_modules/.cache/cron-run-notifications/stores/chatStore.mjs');
const { webClient } = await import('../node_modules/.cache/cron-run-notifications/services/webClient.mjs');
const { watchCronExecutionSession, watchCronRunUpdates } = await import('../node_modules/.cache/cron-run-notifications/features/cron/cronSessionLive.mjs');
const { mergeCronRunUserSeed } = await import('../node_modules/.cache/cron-run-notifications/features/cron/cronSeedMessage.mjs');

async function collectLiveEvents(frames, watch, expectedCount) {
  const originalFetch = globalThis.fetch;
  const originalInject = webClient.injectEvent;
  const transport = window.__JIUWEN_WEB_TRANSPORT__;
  window.__JIUWEN_WEB_TRANSPORT__ = 'http';
  const received = [];
  let requestedUrl;
  let resolveRead;
  const blocked = new Promise((resolve) => { resolveRead = resolve; });
  let reads = 0;
  globalThis.fetch = async (url) => {
    requestedUrl = url;
    return { ok: true, body: { getReader: () => ({
      read: async () => ++reads === 1 ? { done: false, value: new TextEncoder().encode(frames) } : blocked,
      cancel: async () => undefined,
    }) } };
  };
  webClient.injectEvent = (event) => received.push(event);
  const stop = watch();
  try {
    for (let attempt = 0; attempt < 20 && received.length < expectedCount; attempt++) {
      await new Promise((resolve) => setImmediate(resolve));
    }
    assert.equal(received.length, expectedCount);
    return { received, requestedUrl };
  } finally {
    stop();
    resolveRead({ done: true });
    globalThis.fetch = originalFetch;
    webClient.injectEvent = originalInject;
    window.__JIUWEN_WEB_TRANSPORT__ = transport;
  }
}

test('HTTP receives startup failures without opening an execution session', async () => {
  const payload = update({ status: 'failed', phase: 'reminder', session_id: null, notify: true });
  const { received, requestedUrl } = await collectLiveEvents(
    `event: cron.run.updated\ndata: ${JSON.stringify(payload)}\n\n`,
    () => watchCronRunUpdates(() => assert.fail('unexpected disconnect')), 1,
  );
  assert.ok(requestedUrl.endsWith('/cron/runs/live'));
  assert.equal(received[0].event, 'cron.run.updated');
  assert.equal(received[0].payload.session_id, null);
  assert.equal(received[0].payload.notify, true);
});

test('run-now restores its input when the first history snapshot precedes the chat request', () => {
  const sid = 'web_empty_history';
  useChatStore.getState().ensureRuntime(sid);
  useChatStore.getState().replaceHistoryMessages(sid, mergeCronRunUserSeed([], sid, 'scheduled request'));
  useChatStore.getState().addMessage(sid, { id: 'reply', role: 'assistant', content: 'result' });
  const messages = useChatStore.getState().getRuntime(sid).messages;
  assert.deepEqual(messages.map((message) => [message.role, message.content]), [
    ['user', 'scheduled request'], ['assistant', 'result'],
  ]);
  assert.equal(mergeCronRunUserSeed(messages, sid, 'scheduled request'), messages);
});

test('run-now keeps persisted user input and does not seed reused approval sessions', () => {
  const saved = [{ id: 'saved', role: 'user', content: 'server input', timestamp: '2026-10-30T01:00:00Z' }];
  assert.equal(mergeCronRunUserSeed(saved, 'web_saved', 'client input'), saved);
  const empty = [];
  assert.equal(mergeCronRunUserSeed(empty, 'web_reused'), empty);
  const output = [{ id: 'reply', role: 'assistant', content: 'result', timestamp: '2026-10-30T01:00:00Z' }];
  const seeded = mergeCronRunUserSeed(output, 'web_live', 'scheduled request');
  assert.equal(seeded[0].content, 'scheduled request');
  assert.ok(Date.parse(seeded[0].timestamp) < Date.parse(output[0].timestamp));
  assert.equal(seeded[1], output[0]);
});

function update(overrides = {}) {
  return {
    event_id: 'run:1', run_id: 'run', job_id: 'job', session_id: 'session',
    revision: 1, status: 'running', phase: 'started', project_id: 'default',
    mode: 'agent.work.normal', timestamp: 1, notify: false, ...overrides,
  };
}

test('run notifications survive approval, deduplicate updates and reject terminal regression', () => {
  useCronStore.setState({ runUpdates: {} });
  const store = useCronStore.getState();
  assert.ok(store.applyRunUpdate(update()));
  assert.equal(store.applyRunUpdate(update()), null);
  assert.ok(store.applyRunUpdate(update({ revision: 2, phase: 'paused', status: 'awaiting_approval' })));
  assert.equal(useCronStore.getState().runUpdates.run.status, 'awaiting_approval');
  assert.ok(store.applyRunUpdate(update({ revision: 3, phase: 'finished', status: 'succeeded' })));
  assert.equal(store.applyRunUpdate(update({ revision: 4, phase: 'paused', status: 'awaiting_approval' })), null);
  assert.ok(store.applyRunUpdate(update({ revision: 5, status: 'succeeded', phase: 'reminder', notify: true })));
  assert.equal(store.applyRunUpdate(update({ revision: Number.NaN })), null);
  assert.equal(store.applyRunUpdate(update({ project_id: null })), null);
  assert.equal(useCronStore.getState().runUpdates.run.status, 'succeeded');
});

for (const completedReply of [false, true]) {
  test(`SSE replays preserve history and live output with completed reply: ${completedReply}`, async () => {
    const sid = `web_live_${completedReply}`;
    const requestId = 'cron-run';
    useChatStore.getState().ensureRuntime(sid);
    useChatStore.setState((state) => ({ runtimes: { ...state.runtimes, [sid]: {
      ...state.runtimes[sid], historyPagerMeta: { pageIdx: 1 },
      messages: completedReply ? [{
        id: 'saved', role: 'assistant', content: 'saved answer', isStreaming: false,
        outputOrder: { requestId, sequence: 1 },
      }] : [{ id: 'input', role: 'user', content: 'scheduled request' }],
    } } }));
    const frames = [
      ['chat.delta', 1], ['chat.delta', 1], ['team.state_changed', 2], ['cron.run.updated', 3],
    ].map(([event, live_seq]) => `event: ${event}\ndata: ${JSON.stringify({
      request_id: requestId, live_seq, content: 'live output', session_id: sid,
    })}\n\n`).join('');
    const expected = completedReply ? ['cron.run.updated'] : ['chat.delta', 'team.state_changed', 'cron.run.updated'];
    const { received } = await collectLiveEvents(
      frames,
      () => watchCronExecutionSession(sid, { hasCronMetadata: true }),
      expected.length,
    );
    assert.deepEqual(received.map((event) => event.event), expected);
  });
}

test('an older run finishing or reminding cannot replace a newer running label', () => {
  useCronStore.setState({ runUpdates: {} });
  const store = useCronStore.getState();
  store.applyRunUpdate(update({ timestamp: 10 }));
  store.applyRunUpdate(update({ run_id: 'second', event_id: 'second:1', session_id: 'second-session', timestamp: 20 }));
  for (const [revision, phase] of [[2, 'finished'], [3, 'reminder']]) {
    store.applyRunUpdate(update({
      revision, event_id: `run:${revision}`, timestamp: 20 + revision,
      status: 'succeeded', phase, notify: phase === 'reminder',
    }));
    const runs = Object.values(useCronStore.getState().runUpdates);
    const latest = runs.sort((left, right) => right.firstSeenAt - left.firstSeenAt)[0];
    assert.equal(latest.run_id, 'second');
    assert.equal(latest.status, 'running');
  }
  assert.equal(useCronStore.getState().runUpdates.run.firstSeenAt, 10);
  assert.equal(useCronStore.getState().runUpdates.run.timestamp, 23);
  assert.equal(useCronStore.getState().runUpdates.run.notify, true);
  assert.equal(useCronStore.getState().runUpdates.run.session_id, 'session');
  assert.equal(useCronStore.getState().runUpdates.second.session_id, 'second-session');
});


test('disconnect clears unconfirmed runs without inventing a terminal status', () => {
  useCronStore.setState({ runUpdates: {} });
  const store = useCronStore.getState();
  store.applyRunUpdate(update());
  store.applyRunUpdate(update({ run_id: 'done', event_id: 'done:2', revision: 2, status: 'succeeded', phase: 'finished' }));
  store.clearUnconfirmedRuns();
  assert.equal(useCronStore.getState().runUpdates.run, undefined);
  assert.equal(useCronStore.getState().runUpdates.done.status, 'succeeded');
  store.applyRunUpdate(update({ revision: 2, status: 'failed', phase: 'finished' }));
  assert.equal(useCronStore.getState().runUpdates.run.status, 'failed');
});

