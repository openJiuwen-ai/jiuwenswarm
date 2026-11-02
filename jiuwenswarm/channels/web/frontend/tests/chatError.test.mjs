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
