import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import {
  formatContextPercent,
  formatContextLimitTokens,
  formatContextTokens,
  getContextRingPercent,
  parseContextUsageSnapshot,
} from '../node_modules/.cache/context-usage/features/contextUsage/contextUsageModel.js';
import { useSessionStore } from '../node_modules/.cache/context-usage/stores/sessionStore.js';

// Serialized by PR 5623 / Core a2a49ff's actual post-call pipeline using controlled provider usage.
const fixture = JSON.parse(readFileSync(new URL('./fixtures/contextUsage.v1.json', import.meta.url), 'utf8'));
const snapshot = (changes = {}) => ({ ...structuredClone(fixture), ...changes });

test('consumes the canonical v1 fields and all four returned categories', () => {
  const result = parseContextUsageSnapshot(snapshot({ rate: 99, context_max: 1, tokens_used: 1 }));
  assert.equal(result.role, null);
  assert.deepEqual(result.context_window, { limit_tokens: 2000, input_tokens: 1000, occupancy_rate: 0.5 });
  assert.deepEqual(Object.keys(result.parts).sort(), ['messages', 'skills', 'system_prompt', 'tools']);
  assert.equal(result.parts.skills.tokens, 24);
  assert.equal(result.parts.messages.percentage_of_window, 0.403);
  assert.equal(result.session_kv_cache_hit_rate, 0.6);
});

test('uses only the top-level session KV rate without per-call or nested-session fallbacks', () => {
  const payload = snapshot({ session_kv_cache_hit_rate: 0.8829792874980116 });
  payload.kv_cache.request.hit_rate = 0.9976856905811974;
  payload.kv_cache.session.weighted_hit_rate = 0.25;
  const result = parseContextUsageSnapshot(payload);
  assert.equal(result.session_kv_cache_hit_rate, payload.session_kv_cache_hit_rate);
  assert.equal(formatContextPercent(result.session_kv_cache_hit_rate), '88.3%');
  assert.equal('kv_cache' in result, false);

  for (const rate of [null, 0, 1]) {
    payload.session_kv_cache_hit_rate = rate;
    assert.equal(parseContextUsageSnapshot(payload).session_kv_cache_hit_rate, rate);
  }
  delete payload.session_kv_cache_hit_rate;
  assert.equal(parseContextUsageSnapshot(payload), null);

  for (const kvCache of [undefined, null, {}, { request: { hit_rate: 'invalid' } }]) {
    assert.equal(
      parseContextUsageSnapshot(snapshot({ kv_cache: kvCache })).session_kv_cache_hit_rate,
      fixture.session_kv_cache_hit_rate,
    );
  }
});

test('rejects legacy, unsupported, incomplete and malformed payloads without aliases', () => {
  for (const payload of [
    null,
    [],
    {},
    { rate: 50, context_max: 2000, tokens_used: 1000 },
    { context_window_limit: 2000, total_tokens: 1000, occupancy_rate: 50 },
    snapshot({ schema_version: 'context-usage.v2' }),
    snapshot({ event_type: 'chat.usage_summary' }),
    snapshot({ phase: 'pre_call' }),
    snapshot({ product_session_id: null }),
    snapshot({ product_session_id: '' }),
    snapshot({ request_id: '' }),
    snapshot({ role: 1 }),
    snapshot({ role: 'assistant' }),
    snapshot({ depth: -1 }),
    snapshot({ team_id: undefined }),
    snapshot({ parts: [] }),
    snapshot({ session_kv_cache_hit_rate: undefined }),
  ])
    assert.equal(parseContextUsageSnapshot(payload), null);
  for (const invalid of [undefined, '100', -1, NaN, Infinity, 1.5]) {
    const payload = snapshot();
    payload.context_window.input_tokens = invalid;
    assert.equal(parseContextUsageSnapshot(payload), null);
  }
  for (const invalid of [undefined, '0.5', -0.1, NaN, Infinity, 1.1]) {
    const payload = snapshot();
    payload.session_kv_cache_hit_rate = invalid;
    assert.equal(parseContextUsageSnapshot(payload), null);
  }
});

test('keeps null and zero distinct, never estimates missing values or synthesizes categories', () => {
  const payload = snapshot();
  payload.context_window = { limit_tokens: null, input_tokens: 0, occupancy_rate: null };
  payload.session_kv_cache_hit_rate = null;
  payload.parts = { skills: { category: 'skills', tokens: 0, percentage_of_window: null } };
  const result = parseContextUsageSnapshot(payload);
  assert.deepEqual(result.context_window, payload.context_window);
  assert.deepEqual(result.parts, payload.parts);
  assert.equal(result.session_kv_cache_hit_rate, null);
  payload.parts = {};
  assert.deepEqual(parseContextUsageSnapshot(payload).parts, {});
});

test('accepts unmapped categories and preserves their backend keys and values', () => {
  const payload = snapshot();
  payload.parts.attachments = { category: 'attachments', tokens: 10, percentage_of_window: 0.123 };
  payload.parts.memory = { category: 'memory', tokens: 0, percentage_of_window: null };
  const result = parseContextUsageSnapshot(payload);
  assert.deepEqual(Object.keys(result.parts), Object.keys(payload.parts));
  assert.deepEqual(result.parts.attachments, payload.parts.attachments);
  assert.deepEqual(result.parts.memory, payload.parts.memory);
  assert.equal(result.context_window.input_tokens, fixture.context_window.input_tokens);
});

test('preserves special category keys as own properties without changing the parts prototype', () => {
  const parts = Object.fromEntries(
    ['__proto__', 'constructor', 'toString', 'hasOwnProperty'].map((key) => [
      key,
      { category: key, tokens: 10, percentage_of_window: 0.01 },
    ]),
  );
  const result = parseContextUsageSnapshot(snapshot({ parts }));
  assert.deepEqual(result.parts, parts);
  assert.equal(Object.getPrototypeOf(result.parts), Object.prototype);
  assert.equal(Object.prototype.hasOwnProperty.call(result.parts, '__proto__'), true);
});

test('rejects malformed parts regardless of whether their categories have a display mapping', () => {
  for (const key of ['skills', 'attachments']) {
    for (const part of [
      null,
      { category: 'different_key', tokens: 10, percentage_of_window: 0.1 },
      { category: key, tokens: '10', percentage_of_window: 0.1 },
      { category: key, tokens: 10 },
      { category: key, tokens: 10, percentage_of_window: -1 },
    ]) {
      assert.equal(parseContextUsageSnapshot(snapshot({ parts: { [key]: part } })), null);
    }
  }
  assert.equal(
    parseContextUsageSnapshot(snapshot({ parts: { '': { category: '', tokens: 0, percentage_of_window: 0 } } })),
    null,
  );
});

test('uses backend ratios even when they differ from locally calculated token shares', () => {
  const payload = snapshot();
  payload.context_window.occupancy_rate = 1.2;
  payload.parts.messages.percentage_of_window = 0.9;
  const result = parseContextUsageSnapshot(payload);
  assert.equal(formatContextPercent(result.context_window.occupancy_rate), '120%');
  assert.equal(result.parts.messages.percentage_of_window, 0.9);
  assert.equal(getContextRingPercent(result.context_window.occupancy_rate), 100);
  assert.equal(formatContextPercent(0), '0%');
  assert.equal(formatContextPercent(0.2584), '25.8%');
  assert.equal(formatContextTokens(999), '999');
  assert.equal(formatContextTokens(1250), '1.3K');
  assert.equal(formatContextLimitTokens(1_000_000), '1000.0K');
});

test('routes single-agent root snapshots by product session, never the currently visible session', () => {
  const store = useSessionStore.getState();
  const first = fixture.product_session_id;
  const second = 'context-session-b';
  store.ensureRuntime(first);
  store.ensureRuntime(second);
  try {
    store.receiveContextUsage(snapshot({ session_id: 'execution-id' }));
    const original = useSessionStore.getState().getRuntime(first).contextUsageSnapshot;
    assert.equal(original.context_window.input_tokens, 1000);
    assert.equal(useSessionStore.getState().getRuntime(second).contextUsageSnapshot, null);
    for (const changes of [
      { depth: 1 },
      { team_id: 'team' },
      { member_name: 'member' },
      { product_session_id: 'unknown-session' },
      { product_session_id: null },
    ])
      store.receiveContextUsage(snapshot(changes));
    assert.equal(useSessionStore.getState().getRuntime(first).contextUsageSnapshot, original);
    assert.equal(useSessionStore.getState().getRuntime('unknown-session'), undefined);
    store.setMode(second, 'team');
    store.receiveContextUsage(snapshot({ product_session_id: second }));
    assert.equal(useSessionStore.getState().getRuntime(second).contextUsageSnapshot, null);
  } finally {
    store.removeRuntime(first);
    store.removeRuntime(second);
  }
});

test('team mode accepts only semantic leader frames and keeps the latest leader during worker activity', () => {
  const store = useSessionStore.getState();
  const sessionId = 'team-context-session';
  store.ensureRuntime(sessionId);
  store.setMode(sessionId, 'team');
  try {
    store.receiveContextUsage(
      snapshot({
        product_session_id: sessionId,
        role: 'teammate',
        member_name: 'worker-1',
      }),
    );
    assert.equal(useSessionStore.getState().getRuntime(sessionId).contextUsageSnapshot, null);

    const leader = snapshot({
      product_session_id: sessionId,
      role: 'leader',
      depth: 3,
      team_id: 'runtime-team',
      member_name: 'explicit-leader-name',
      timestamp: '2026-09-03T03:00:00.000Z',
    });
    leader.context_window.input_tokens = 700;
    store.receiveContextUsage(leader);
    assert.equal(
      useSessionStore.getState().getRuntime(sessionId).contextUsageSnapshot.context_window.input_tokens,
      700,
    );

    const newerWorker = snapshot({
      product_session_id: sessionId,
      role: 'teammate',
      member_name: 'worker-2',
      timestamp: '2026-09-03T03:01:00.000Z',
    });
    newerWorker.context_window.input_tokens = 900;
    store.receiveContextUsage(newerWorker);
    assert.equal(
      useSessionStore.getState().getRuntime(sessionId).contextUsageSnapshot.context_window.input_tokens,
      700,
    );

    const olderLeader = structuredClone(leader);
    olderLeader.timestamp = '2026-09-03T02:59:00.000Z';
    olderLeader.context_window.input_tokens = 600;
    store.receiveContextUsage(olderLeader);
    assert.equal(
      useSessionStore.getState().getRuntime(sessionId).contextUsageSnapshot.context_window.input_tokens,
      700,
    );

    store.setMode(sessionId, 'agent');
    assert.equal(useSessionStore.getState().getRuntime(sessionId).contextUsageSnapshot, null);
  } finally {
    store.removeRuntime(sessionId);
  }
});

test('replaces overview, parts and KV together and accepts a new request after sequence resets', () => {
  const store = useSessionStore.getState();
  const sessionId = fixture.product_session_id;
  store.ensureRuntime(sessionId);
  try {
    store.receiveContextUsage(snapshot({ sequence: 10 }));
    const next = snapshot({ sequence: 0, request_id: 'new-request' });
    next.context_window.input_tokens = 800;
    next.parts = {};
    next.session_kv_cache_hit_rate = null;
    store.receiveContextUsage(next);
    const current = useSessionStore.getState().getRuntime(sessionId).contextUsageSnapshot;
    assert.equal(current.request_id, 'new-request');
    assert.equal(current.context_window.input_tokens, 800);
    assert.deepEqual(current.parts, {});
    assert.equal(current.session_kv_cache_hit_rate, null);
    store.removeRuntime(sessionId);
    store.ensureRuntime(sessionId);
    assert.equal(useSessionStore.getState().getRuntime(sessionId).contextUsageSnapshot, null);
  } finally {
    store.removeRuntime(sessionId);
  }
});

test('history restore accepts the full context usage event while live delivery stays unchanged', () => {
  const source = (file) => readFileSync(new URL('../src/' + file, import.meta.url), 'utf8');
  const hook = source('hooks/useWebSocket.ts');
  assert.match(hook, /webClient.on\('context.usage', \(\{ payload \}\) => \{\s*receiveContextUsage\(payload\);/);
  for (const file of ['hooks/useWebSocket.ts', 'components/ChatPanel/ContextUsageIndicator.tsx']) {
    assert.doesNotMatch(
      source(file),
      /command\.context|contextRestore|normalizeContextUsageOverview|normalizeContextUsageDetail/,
    );
  }
  assert.match(source('features/historyRestore.ts'), /'context\.usage'/);
  assert.match(source('features/historyRestore.ts'), /onContextUsage/);
  assert.match(
    source('components/ChatPanel/InputArea.tsx'),
    /\(isAgentMode \|\| isTeamMode\) && <ContextUsageIndicator/,
  );
});
