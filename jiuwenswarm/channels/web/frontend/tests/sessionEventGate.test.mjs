import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

import { transform } from 'esbuild';

import { createSessionEventGate, getEventSessionId } from '../node_modules/.cache/session-event-gate/services/sessionEventGate.js';

function event(name, sessionId, payload = {}) {
  return {
    type: 'event',
    event: name,
    payload: sessionId ? { ...payload, session_id: sessionId } : payload,
  };
}

test('extracts direct and nested session ids used by live conversation events', () => {
  assert.equal(getEventSessionId(event('chat.delta', 'session-direct')), 'session-direct');
  assert.equal(getEventSessionId(event('team.event', '', { payload: { event: { session_id: 'session-nested' } } })), 'session-nested');
});

test('replays one session live events in arrival order after history restoration', async () => {
  const received = [];
  const gate = createSessionEventGate(incoming => received.push(incoming));
  const release = gate.suspend('session-a');

  gate.dispatch(event('chat.delta', 'session-a', { content: 'first' }));
  gate.dispatch(event('chat.tool_call', 'session-a', { id: 'tool-1' }));
  gate.dispatch(event('chat.delta', 'session-b', { content: 'other session' }));

  assert.deepEqual(
    received.map(incoming => incoming.payload.content),
    ['other session'],
  );

  release();
  await Promise.resolve();

  assert.deepEqual(
    received.map(incoming => incoming.event),
    ['chat.delta', 'chat.delta', 'chat.tool_call'],
  );
  assert.deepEqual(
    received.slice(1).map(incoming => incoming.payload.content ?? incoming.payload.id),
    ['first', 'tool-1'],
  );
});

test('applies the persisted history snapshot before appending the live stream tail', async () => {
  const transcript = [];
  const gate = createSessionEventGate(incoming => {
    if (incoming.event === 'history.message') {
      transcript.splice(0, transcript.length, incoming.payload.message.content);
      return;
    }
    if (incoming.event === 'chat.delta') {
      transcript.push(incoming.payload.content);
    }
  });
  const release = gate.suspend('session-a');

  gate.dispatch(event('chat.delta', 'session-a', { content: 'live tail' }));
  gate.dispatch(event('history.message', 'session-a', {
    message: { content: 'persisted prefix' },
  }));

  assert.deepEqual(transcript, ['persisted prefix']);

  release();
  await Promise.resolve();

  assert.deepEqual(transcript, ['persisted prefix', 'live tail']);
});

test('keeps history and error control events immediate while live events are suspended', () => {
  const received = [];
  const gate = createSessionEventGate(incoming => received.push(incoming.event));
  gate.suspend('session-a');

  gate.dispatch(event('chat.delta', 'session-a'));
  gate.dispatch(event('history.message', 'session-a'));
  gate.dispatch(event('chat.error', 'session-a'));
  gate.dispatch(event('security.alert', 'session-a'));

  assert.deepEqual(received, ['history.message', 'chat.error', 'security.alert']);
});

test('transfers queued events across overlapping history restore generations', async () => {
  const received = [];
  const gate = createSessionEventGate(incoming => received.push(incoming.payload.content));
  const releaseFirst = gate.suspend('session-a');

  gate.dispatch(event('chat.delta', 'session-a', { content: 'before replacement' }));
  const releaseSecond = gate.suspend('session-a');
  releaseFirst();
  await Promise.resolve();
  assert.deepEqual(received, []);

  gate.dispatch(event('chat.delta', 'session-a', { content: 'during replacement' }));
  releaseSecond();
  releaseSecond();
  await Promise.resolve();

  assert.deepEqual(received, ['before replacement', 'during replacement']);
});

test('releases the gate when a cancelled activation abandons the restore (JW-80)', async () => {
  // performSessionRestore 挂起闸门后,若二次 canActivate 检查失败,
  // 必须与元数据加载失败等路径一样调用 release,否则该会话事件永久积压。
  const received = [];
  const gate = createSessionEventGate(incoming => received.push(incoming.payload.content));
  const release = gate.suspend('session-a');

  gate.dispatch(event('chat.delta', 'session-a', { content: 'queued while suspended' }));

  // canActivate 失败:释放闸门(修复后的 App 行为),而不是直接丢弃 release。
  release();
  await Promise.resolve();

  assert.deepEqual(received, ['queued while suspended']);
});

test('keeps events suspended indefinitely when the release is leaked (JW-80 leak symptom)', async () => {
  const received = [];
  const gate = createSessionEventGate(incoming => received.push(incoming.payload.content));
  gate.suspend('session-a');

  gate.dispatch(event('chat.delta', 'session-a', { content: 'first' }));
  gate.dispatch(event('chat.delta', 'session-a', { content: 'second' }));
  await Promise.resolve();
  await Promise.resolve();

  // 泄漏(修复前的 App 行为):depth 永不归零,事件全部积压、永不下发。
  assert.deepEqual(received, []);
});

// 仅验证闸门原语无法发现 App 侧漏调 release 的回归:这里把 App.tsx 的
// performSessionRestore 回调切出来独立编译,直接跑真实的二次 canActivate
// 失败路径。沿用 appMarketPanel.test.mjs 的切片 + esbuild 编译方式。
// 先归一 CRLF,保证 Windows 检出也能命中锚点。
const appSource = fs.readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8').replace(/\r\n/g, '\n');
const restoreMarker = 'const performSessionRestore = useCallback(';
const restoreStart = appSource.indexOf(restoreMarker);
const restoreArrowStart = appSource.indexOf('async (targetSessionId: string', restoreStart);
const restoreBodyEnd = appSource.indexOf('\n    },\n    [\n      addMessage,', restoreArrowStart);
assert.ok(
  restoreStart >= 0 && restoreArrowStart > restoreStart && restoreBodyEnd > restoreArrowStart,
  'performSessionRestore callback is present in App.tsx',
);
const restoreCode = appSource.slice(restoreArrowStart, restoreBodyEnd) + '\n    }';
const compiledRestore = (await transform(`export default ${restoreCode}`, { loader: 'tsx', format: 'cjs' })).code;
function restoreHandler(dependencies) {
  return new Function(
    ...Object.keys(dependencies),
    `const module = { exports: {} }; ${compiledRestore}; return module.exports.default;`,
  )(...Object.values(dependencies));
}

test('cancelled restore releases the gate through the real App early-return path (JW-80)', async () => {
  const received = [];
  const gate = createSessionEventGate(incoming => received.push(incoming.payload.content));
  const pendingReleases = new Map();
  const releasePendingSessionEvents = sessionId => {
    const release = pendingReleases.get(sessionId);
    pendingReleases.delete(sessionId);
    release?.();
  };
  let activations = 0;
  const performSessionRestore = restoreHandler({
    pendingSessionEventReleasesRef: { current: pendingReleases },
    releasePendingSessionEvents,
    webClient: { suspendSessionEvents: sessionId => gate.suspend(sessionId) },
    setChatWelcomeVariant: () => {},
    sessionIdRef: { current: 'session-a' },
    useSessionStore: { getState: () => ({ getRuntime: () => ({ mode: 'agent' }) }) },
    useChatStore: { getState: () => ({ runtimes: {}, getRuntime: () => undefined }) },
    disposeInFlightHistoryHandles: () => {},
    mode: 'agent',
  });

  await performSessionRestore('session-a', 'agent', { session_id: 'session-a', mode: 'agent' }, undefined, {
    canActivate: () => {
      activations += 1;
      if (activations === 1) return true;
      // 复现缺陷窗口:闸门已挂起、意图在 await 期间翻转,期间仍有实时事件到达。
      gate.dispatch(event('chat.delta', 'session-a', { content: 'queued before cancel' }));
      return false;
    },
  });
  await Promise.resolve();

  assert.equal(activations, 2);
  assert.equal(pendingReleases.size, 0, 'cancelled restore must consume its gate release');
  assert.deepEqual(received, ['queued before cancel']);
});
