import './fixtures/browserEnvironment.mjs';
import assert from 'node:assert/strict';
import test from 'node:test';

const {
  currentSessionTurnError,
  forgetSessionTurnError,
  isTurnFailureCode,
  lastTurnErrorMessage,
  reconcileSessionTurnError,
  rememberSessionTurnError,
  sessionFailureMessage,
} = await import('../node_modules/.cache/session-turn-error/sessionTurnError.mjs');
const { useChatStore } = await import('../node_modules/.cache/session-turn-error/chatStore.mjs');
const { parseHistoryJsonFileToTimelinePreview } = await import(
  '../node_modules/.cache/session-turn-error/historyRestore.mjs'
);
const { getSessionIndicator } = await import(
  '../node_modules/.cache/session-turn-error/sidebarModel.mjs'
);

globalThis.localStorage = globalThis.window.localStorage;
const chat = () => useChatStore.getState();
const runtimeOf = (sessionId) => chat().getRuntime(sessionId);

const TS = '2026-11-20T10:00:00.000Z';
const user = (id, timestamp = TS, content = '再试一次') => ({
  id, role: 'user', content, timestamp,
});
const final = (id, timestamp = TS, content = '回答') => ({
  id, role: 'assistant', content, timestamp, turnFinal: true,
});
// 被打断的半截回答：没有 turnFinal 标记，不能推出「本轮成功收尾」。
const partial = (id, timestamp = TS, content = '半截回答') => ({
  id, role: 'assistant', content, timestamp,
});
const errorBubble = (id, timestamp = TS, content = '模型调用失败') => ({
  id, role: 'system', content, timestamp, terminalError: true,
});

const errorRecord = (id = 'e1', timestamp = 1_790_000_000, content = 'model call failed') => ({
  id,
  role: 'assistant',
  event_type: 'chat.error',
  content,
  timestamp,
  code: 'INTERNAL_ERROR',
});

// ── 时间线推导 ──────────────────────────────────────────────────
// 回归 GitCode #6920：红叹号只活在 runtime.error 里，刷新后历史恢复不回填 → 标记消失。

test('a trailing error bubble means the last turn failed', () => {
  assert.equal(lastTurnErrorMessage([user('u1'), errorBubble('e1')]), '模型调用失败');
});

test('a new user message after the error starts a fresh turn', () => {
  assert.equal(lastTurnErrorMessage([errorBubble('e1'), user('u2')]), null);
});

test('a successful final after the error clears the mark', () => {
  assert.equal(lastTurnErrorMessage([errorBubble('e1'), user('u2'), final('f2')]), null);
});

test('a trailing user message with no terminal event is not a failure', () => {
  assert.equal(lastTurnErrorMessage([user('u1'), final('f1'), user('u2')]), null);
});

test('tool results, automation turns and cross-session replies are not turn boundaries', () => {
  const noise = [
    { id: 't1', role: 'assistant', content: 'tool', timestamp: TS, presentation: 'tool_result' },
    { id: 'hb', role: 'user', content: '心跳', timestamp: TS, automation: { run_id: 'hb-1' } },
    { id: 'x1', role: 'assistant', content: '跨会话', timestamp: TS, crossSession: { messageId: 'm1' } },
  ];
  assert.equal(lastTurnErrorMessage([user('u1'), errorBubble('e1'), ...noise]), '模型调用失败');
});

// 检视意见：被打断的半截回答（无 turnFinal 标记）不是「本轮成功收尾」的证据。
test('an interrupted half-answer without a final mark is not a succeeded turn', () => {
  assert.equal(lastTurnErrorMessage([user('u1'), partial('p1')]), null);
  assert.equal(lastTurnErrorMessage([user('u1'), partial('p1'), errorBubble('e1')]), '模型调用失败');
  // 半截回答 + 后续成功 final：成功收尾在末尾，正常判 succeeded。
  assert.equal(lastTurnErrorMessage([user('u1'), partial('p1'), final('f2')]), null);
});

// 检视意见：历史里只有「被杀前落下的 assistant 气泡」时，绝不能推出 succeeded
// 抹掉缓存里的真实失败（worker 被杀 / unary 拒绝只活在缓存里）。
test('an interrupted half-answer keeps the cached failure instead of clearing it', () => {
  const sid = 'cache_half_answer';
  rememberSessionTurnError(sid, 'worker 被杀');
  const halfTimeline = [user('u1'), partial('p1')];
  assert.equal(currentSessionTurnError(sid, halfTimeline), 'worker 被杀');
  assert.equal(reconcileSessionTurnError(sid, halfTimeline), 'worker 被杀');
  assert.equal(currentSessionTurnError(sid), 'worker 被杀');
});

test('an empty or missing timeline has no failure', () => {
  assert.equal(lastTurnErrorMessage([]), null);
  assert.equal(lastTurnErrorMessage(undefined), null);
});

// ── 发送失败码分级 ──────────────────────────────────────────────
// 回归 GitCode #6919：模型调用在首帧前失败走 unary ok=false，旧实现不给会话打标记。

test('a rejected turn counts as a failed turn', () => {
  for (const code of ['model_not_configured', 'QUOTA_EXHAUSTED', 'INTERNAL_ERROR', undefined, null, '']) {
    assert.equal(isTurnFailureCode(code), true, `code ${String(code)} should light the mark`);
  }
});

test('transport problems and user cancels do not count as a failed turn', () => {
  for (const code of [
    'REQUEST_ABORTED',
    'request_cancelled',
    'REQUEST_TIMEOUT',
    'WS_DISCONNECTED',
    ' ws_closed ',
    'WS_ERROR',
    'WS_NOT_READY',
    // 登录预检（未登录/免费模型未登录）不是本轮失败，不该点亮失败标记。
    'login_required',
    'LOGIN_REQUIRED',
  ]) {
    assert.equal(isTurnFailureCode(code), false, `code ${code} must not light the mark`);
  }
});

// ── 本地缓存兜底 ────────────────────────────────────────────────
// worker 被杀（#6794）由前端合成 WORKER_EXECUTION_INTERRUPTED，后端从不落盘历史。

test('a cached failure survives a page reload with no runtime and no history', () => {
  const sid = 'cache_reload';
  rememberSessionTurnError(sid, '执行被中断');
  assert.equal(currentSessionTurnError(sid), '执行被中断');
  assert.equal(currentSessionTurnError(sid, []), '执行被中断');
});

test('the timeline wins over the cache, and a successful timeline clears it', () => {
  const sid = 'cache_timeline';
  rememberSessionTurnError(sid, '旧失败');
  assert.equal(currentSessionTurnError(sid, [user('u1'), errorBubble('e1', TS, '新失败')]), '新失败');
  forgetSessionTurnError(sid);
  assert.equal(currentSessionTurnError(sid), null);
});

test('a retried-and-succeeded turn does not show the cached failure', () => {
  const sid = 'cache_retried';
  rememberSessionTurnError(sid, '旧失败');
  const succeededTimeline = [user('u1'), errorBubble('e1'), user('u2'), final('f2')];
  // 历史显示最近一轮正常收尾：缓存里的失败必须作废，否则重试成功后刷新还挂红叹号。
  assert.equal(currentSessionTurnError(sid, succeededTimeline), null);
  assert.equal(reconcileSessionTurnError(sid, succeededTimeline), null);
  assert.equal(currentSessionTurnError(sid), null, 'reconcile purges the stale cache entry');
});

test('an empty timeline keeps the cached failure (backend never wrote history)', () => {
  const sid = 'cache_empty_history';
  rememberSessionTurnError(sid, 'worker 被杀');
  assert.equal(currentSessionTurnError(sid, []), 'worker 被杀');
  assert.equal(reconcileSessionTurnError(sid, []), 'worker 被杀');
});

test('a cache entry is forgotten when the next turn starts', () => {
  const sid = 'cache_new_turn';
  rememberSessionTurnError(sid, '失败');
  chat().ensureRuntime(sid);
  chat().setSessionError(sid, '失败');
  chat().setExecutionError(sid, '失败');
  assert.equal(currentSessionTurnError(sid), '失败');

  chat().setProcessing(sid, true, { newTurn: true });
  assert.equal(currentSessionTurnError(sid), null, 'red mark must not outlive a retry');
  // runtime 侧同步清空，侧栏优先读 runtime.error。
  assert.equal(runtimeOf(sid).error, null);
  assert.equal(runtimeOf(sid).executionError, null);
});

test('reconcile keeps the failure visible when history has no trailing final', () => {
  const sid = 'cache_reconcile_failed';
  chat().ensureRuntime(sid);
  chat().replaceHistoryMessages(sid, [user('u1'), errorBubble('e1', TS, '历史里的失败')]);
  assert.equal(runtimeOf(sid).error, '历史里的失败');
  assert.equal(runtimeOf(sid).executionError, '历史里的失败');
  assert.equal(currentSessionTurnError(sid), '历史里的失败');
});

test('reconcile clears the runtime mark after a later successful turn', () => {
  const sid = 'cache_reconcile_ok';
  chat().ensureRuntime(sid);
  chat().replaceHistoryMessages(sid, [
    user('u1'), errorBubble('e1'),
    user('u2'), final('f2'),
  ]);
  assert.equal(runtimeOf(sid).error, null);
  assert.equal(reconcileSessionTurnError(sid, [user('u1'), errorBubble('e1'), user('u2'), final('f2')]), null);
});

test('history restore keeps the cache fallback for failures the backend never wrote', () => {
  const sid = 'cache_reconcile_worker_killed';
  rememberSessionTurnError(sid, '执行被中断');
  chat().ensureRuntime(sid);
  // 历史里只有中断前的那半轮：user 气泡在末尾，时间线推不出失败。
  chat().replaceHistoryMessages(sid, [user('u1', TS, '跑一下')]);
  assert.equal(runtimeOf(sid).error, '执行被中断');
});

// ── 历史恢复链路（chat.error → terminalError → 侧栏标记）──────────
test('a persisted chat.error record restores the failed-turn mark', () => {
  const sid = 'history_chat_error';
  const { messages } = parseHistoryJsonFileToTimelinePreview([errorRecord()], sid);
  assert.equal(messages.length, 1);

  chat().ensureRuntime(sid);
  chat().replaceHistoryMessages(sid, messages);
  const runtime = runtimeOf(sid);
  assert.equal(runtime.error, 'model call failed');
  assert.equal(runtime.executionError, 'model call failed');
  assert.equal(currentSessionTurnError(sid, runtime.messages), 'model call failed');
});

test('a resumed conversation after the error restores no mark', () => {
  const sid = 'history_after_error';
  const { messages } = parseHistoryJsonFileToTimelinePreview([
    errorRecord('e1', 1_790_000_000),
    { id: 'u2', role: 'user', content: '再来一次', timestamp: 1_790_000_100 },
    { id: 'f2', role: 'assistant', event_type: 'chat.final', content: '这次好了', timestamp: 1_790_000_200 },
  ], sid);
  chat().ensureRuntime(sid);
  chat().replaceHistoryMessages(sid, messages);
  assert.equal(runtimeOf(sid).error, null);
  assert.equal(currentSessionTurnError(sid, runtimeOf(sid).messages), null);
});

test('an in-flight turn is not overwritten by the restored failure', () => {
  const sid = 'history_processing';
  chat().ensureRuntime(sid);
  chat().setProcessing(sid, true);
  rememberSessionTurnError(sid, '上一次失败');
  const { messages } = parseHistoryJsonFileToTimelinePreview([errorRecord()], sid);
  chat().replaceHistoryMessages(sid, messages);
  assert.equal(runtimeOf(sid).error, null, 'live turn owns the state');
});

// ── 侧栏取值优先级 ──────────────────────────────────────────────
test('the sidebar prefers the live runtime error, then the timeline, then the cache', () => {
  const sid = 'sidebar_priority';
  // 只有缓存：刷新后没有 runtime 也能画红叹号（#6920 的主场景）。
  rememberSessionTurnError(sid, '缓存里的失败');
  assert.equal(sessionFailureMessage(sid), '缓存里的失败');

  // 时间线可推导时以时间线为准。
  assert.equal(
    sessionFailureMessage(sid, { messages: [user('u1'), errorBubble('e1', TS, '时间线里的失败')] }),
    '时间线里的失败',
  );

  // 实时错误优先（文案更贴近本次失败）。
  assert.equal(
    sessionFailureMessage(sid, {
      error: null,
      executionError: '实时失败',
      messages: [user('u1'), errorBubble('e1', TS, '时间线里的失败')],
    }),
    '实时失败',
  );
});

test('the sidebar hides a stale failure while the turn is running again', () => {
  const sid = 'sidebar_retry';
  rememberSessionTurnError(sid, '失败');
  assert.equal(sessionFailureMessage(sid, { isProcessing: true, messages: [] }), null);
});

test('the sidebar shows a red mark for a session with no runtime (fresh page load)', () => {
  const sid = 'sidebar_fresh_page';
  const { messages } = parseHistoryJsonFileToTimelinePreview([errorRecord()], sid);
  chat().ensureRuntime(sid);
  chat().replaceHistoryMessages(sid, messages);
  const runtime = runtimeOf(sid);

  assert.equal(sessionFailureMessage(sid, runtime), 'model call failed');
  assert.equal(
    getSessionIndicator(runtime, false, false, Boolean(sessionFailureMessage(sid, runtime))),
    'error',
  );
});
