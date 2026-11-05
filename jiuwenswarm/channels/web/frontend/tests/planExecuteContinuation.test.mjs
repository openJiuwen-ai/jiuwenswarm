// issue 5223 方案 A：plan「执行」批准的服务端幂等续跑——前端撤发约束。
//
// useWebSocket.ts 无法独立 bundle（React hook + 全量 store 依赖），这里按
// cronLiveSkipReplayUpgrade / chatErrorSseLifecycle 的源断言模式：锁定
// 「补发已移除、回显分支在位、失败复位保留」，并核对前端 i18n 与服务端
// 代发文案（plan_approval.PLAN_EXECUTE_PROMPT_OUTPUT）逐字一致。
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const frontendRoot = fileURLToPath(new URL('../', import.meta.url));

const hookSource = (
  await readFile(new URL('../src/hooks/useWebSocket.ts', import.meta.url), 'utf8')
).replace(/\r\n/g, '\n');

test('plan.execute 补发链已整体移除（服务端幂等续跑接管）', () => {
  // pendingPlanExecuteRef 及其登记/补发/断线清理全部撤除
  assert.equal(
    hookSource.includes('pendingPlanExecuteRef'),
    false,
    'pendingPlanExecuteRef 不得复活：执行轮由服务端代发（issue 5223）',
  );
  // 旧补发位点留下说明注释，防止后续误加回来
  assert.match(hookSource, /执行轮已改由服务端幂等续跑代发（issue 5223 方案 A）/);
  assert.match(hookSource, /前端不再在此补发/);
});

test('processing_status 回显分支：按 source=plan_execute_continuation upsert 用户气泡', () => {
  assert.match(
    hookSource,
    /payload\.source === 'plan_execute_continuation'/,
  );
  // 回显消息是用户气泡（与 heartbeat automation 回显同款）
  const branch = hookSource.match(
    /if \(isProcessingNow && payload\.source === 'plan_execute_continuation'\)[\s\S]*?\n      \}/,
  );
  assert.ok(branch, '回显分支缺失');
  assert.match(branch[0], /role: 'user'/);
  assert.match(branch[0], /plan-continuation-user-/);
  // 幂等：已存在同 id 消息不重复插入
  assert.match(branch[0], /getRuntime\(sessionId\)[\s\S]*?find\(\(m\) => m\.id === planMsgId\)/);
});

test('点「执行」乐观关 Plan 开关 + 失败复位保留（不自愈链路撤除后的兜底）', () => {
  assert.match(hookSource, /planExecuteOptimistic = true;/);
  // catch 分支复位：请求没送出去时把开关拨回 plan
  const restore = hookSource.match(
    /if \(planExecuteOptimistic\) \{[\s\S]*?setActive\(sessionId, true\)[\s\S]*?\n      \}/,
  );
  assert.ok(restore, '失败复位 plan 开关的逻辑缺失');
});

test('服务端代发文案与前端 i18n plan.executePrompt 逐字一致', async () => {
  const serverSource = await readFile(
    new URL('../../../../agents/harness/code/prompt/plan_approval.py', import.meta.url),
    'utf8',
  );
  const prompt = serverSource.match(
    /PLAN_EXECUTE_PROMPT_OUTPUT = \{\s*"cn": "([^"]+)",\s*"en": "([^"]+)",\s*\}/,
  );
  assert.ok(prompt, 'PLAN_EXECUTE_PROMPT_OUTPUT 缺失');
  for (const [locale, expected] of [
    ['zh', prompt[1]],
    ['en', prompt[2]],
  ]) {
    const localeSource = await readFile(
      new URL(`../src/i18n/locales/${locale}.json`, import.meta.url),
      'utf8',
    );
    const entry = localeSource.match(/"executePrompt": "([^"]+)"/);
    assert.ok(entry, `${locale}.json 缺 executePrompt`);
    assert.equal(entry[1], expected, `${locale} 执行文案与服务端不一致`);
  }
});
