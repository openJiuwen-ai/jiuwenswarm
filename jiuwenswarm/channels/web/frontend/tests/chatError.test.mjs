import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer } from 'vite';

const vite = await createServer({
  configFile: false,
  cacheDir: 'node_modules/.cache/chat-error/vite',
  server: { middlewareMode: true, hmr: false, watch: null },
});
let describeChatError;
try {
  ({ describeChatError } = await vite.ssrLoadModule('/src/features/free-models/chatError.ts'));
} finally {
  await vite.close();
}

const t = (key) => `<${key}>`;
const RAW = "[181001] model call failed: Error code: 401 - {'error_code': 'APIG.0305'}";

test('upstream free-model failures show actionable guidance without raw diagnostics', () => {
  for (const [code, key] of [
    ['login_required', 'auth.huawei.modelError.loginRequired'],
    ['quota_exhausted', 'auth.huawei.quota.exhaustedHint'],
    ['rate_limited', 'auth.huawei.modelError.rateLimited'],
    ['free_model_unavailable', 'auth.huawei.modelError.unavailable'],
  ]) {
    assert.equal(describeChatError({ code, upstream: true }, RAW, t), `<${key}>`);
  }
});

test('precheck login_required already reads well and is not wrapped', () => {
  const precheck = '该模型需要登录华为账号后使用（未登录或登录已过期），请登录后重试';
  assert.equal(describeChatError({ code: 'login_required' }, precheck, t), precheck);
});

test('model_not_configured is replaced by the setup guidance', () => {
  assert.equal(describeChatError({ code: 'model_not_configured' }, 'x', t), '<chat.modelNotConfigured>');
});

test('supplemental delivery failure explains that the original task and supplement are incomplete', () => {
  assert.equal(
    describeChatError({ code: 'SESSION_INPUT_DELIVERY_FAILED' }, 'private reconciliation diagnostics', t),
    '<chat.steerDeliveryFailed>',
  );
});

test('errors without a known code are shown as-is', () => {
  assert.equal(describeChatError({}, RAW, t), RAW);
  assert.equal(describeChatError({ code: 'something_else', upstream: true }, RAW, t), RAW);
});

test('quota exhaustion only overrides the generic free-model failure', () => {
  for (const raw of [
    "Error code: 402 - {'error_code': 'APIG.0305', 'message': 'insufficient_quota'}",
    'Error code: 402 - amount due',
    'status code 402',
    '免费积分已用完，请改用已配置的模型',
    '当前额度已耗尽',
    'You exceeded your current quota, please check your plan and billing details.',
  ]) {
    assert.equal(
      describeChatError({ code: 'free_model_unavailable', upstream: true }, raw, t),
      '<auth.huawei.quota.exhaustedHint>',
    );
  }
  // No code / unknown code must keep the original text: self-configured providers also return
  // insufficient_quota for their own billing, where the free-credit hint is wrong.
  const quotaError = 'insufficient_quota';
  assert.equal(describeChatError({}, quotaError, t), quotaError);
  assert.equal(describeChatError({ code: 'AGENT_SERVER_ERROR' }, quotaError, t), quotaError);
  // Self-configured OpenAI/DeepSeek keys report their own billing failures without a code.
  const selfConfigured = "Error code: 429 - {'error': {'type': 'insufficient_quota'}}";
  assert.equal(describeChatError({}, selfConfigured, t), selfConfigured);
});

test('free-credit hint requires the upstream marker of a login/free-model session', () => {
  // `upstream` is only set by `_annotate_model_error` after classifying a login (free) model
  // session; payloads without it must keep the generic service-unavailable guidance.
  assert.equal(
    describeChatError({ code: 'free_model_unavailable' }, 'insufficient_quota', t),
    '<auth.huawei.modelError.unavailable>',
  );
  assert.equal(
    describeChatError({ code: 'free_model_unavailable', upstream: true }, 'insufficient_quota', t),
    '<auth.huawei.quota.exhaustedHint>',
  );
});

test('non-exhaustion wording keeps the generic free-model failure', () => {
  for (const raw of [
    'quota exceeded per minute, please retry later',
    'request id 4020 failed',
    'error 402 from proxy',
    'concurrent request limit exceeded',
    // 402 without status-code context is a timeout/token count, not an HTTP status.
    'request timed out after 402 seconds',
    'truncated 402 tokens in response',
    'retried 402 times before giving up',
    'context length 402 tokens exceeds limit',
  ]) {
    assert.equal(
      describeChatError({ code: 'free_model_unavailable', upstream: true }, raw, t),
      '<auth.huawei.modelError.unavailable>',
    );
  }
});

test('quota wording in explicitly classified errors keeps the original classification', () => {
  assert.equal(
    describeChatError({ code: 'rate_limited', upstream: true }, 'quota exhausted', t),
    '<auth.huawei.modelError.rateLimited>',
  );
});

test('ambiguous model-selection errors gain actionable guidance', () => {
  // 回归：重名模型触发 "ambiguous legacy model name" 时，报错要告诉用户
  // 怎么修（会话重选模型 / 编辑任务重选或重建），而不是只给技术文案。
  const cronFailure = '[cron] 任务执行失败: ambiguous legacy model name; select a stable model_id';
  assert.equal(
    describeChatError({}, cronFailure, t),
    `${cronFailure}\n<chat.modelNameAmbiguousHint>`,
  );
  const streamFailure = { code: 'AGENT_SERVER_ERROR' };
  assert.equal(
    describeChatError(streamFailure, 'ambiguous legacy model name; select a stable model_id', t),
    'ambiguous legacy model name; select a stable model_id\n<chat.modelNameAmbiguousHint>',
  );
});
