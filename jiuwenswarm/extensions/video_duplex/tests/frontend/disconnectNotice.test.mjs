import assert from 'node:assert/strict';
import test from 'node:test';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(new URL('../../../../channels/web/frontend/package.json', import.meta.url));
const { build } = require('esbuild');
const runtimePath = fileURLToPath(new URL('../../frontend/taskFullDuplexRuntimeStore.ts', import.meta.url));
const errorMessagePath = fileURLToPath(new URL('../../frontend/duplexErrorMessage.ts', import.meta.url));
const toastPath = fileURLToPath(new URL('../../../../channels/web/frontend/src/components/ui/Toast/toastStore.ts', import.meta.url));
const compiled = await build({
  stdin: { contents: `export * from ${JSON.stringify(runtimePath)}; export * from ${JSON.stringify(errorMessagePath)}; export * from ${JSON.stringify(toastPath)};`,
    resolveDir: fileURLToPath(new URL('../../../../channels/web/frontend/', import.meta.url)) },
  bundle: true, platform: 'node', format: 'esm', write: false,
  alias: { react: require.resolve('react') },
});
const { describeDuplexError, setTaskFullDuplexRuntimeError, setTaskFullDuplexRuntimeState, toastStore } =
  await import(`data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`);

test('duplex error uses the native persistent notice, survives idle and deduplicates repeats', () => {
  setTaskFullDuplexRuntimeError('quota exhausted');
  const notices = toastStore.getSnapshot();
  assert.equal(notices.length, 1);
  assert.match(notices[0].content, /原因：quota exhausted。结果：本次语音交互可能未完成。解决方式：请检查服务额度/);
  assert.equal(notices[0].durationMs, 0);
  assert.equal(notices[0].variant, 'error');
  setTaskFullDuplexRuntimeState('idle');
  setTaskFullDuplexRuntimeError('quota exhausted');
  assert.equal(toastStore.getSnapshot().length, 1);
});

test('duplex errors explain the actual cause, impact and next step', () => {
  const quota = describeDuplexError('insufficient_quota');
  assert.match(quota, /原因：insufficient_quota/);
  assert.match(quota, /结果：本次语音交互可能未完成/);
  assert.match(quota, /解决方式：请检查服务额度和限流状态/);

  const joyaiRateLimit = describeDuplexError('JoyAI 服务商限流 (429，每分钟 token 额度已用完): {"cause":"tokens limit for minute"}');
  assert.match(joyaiRateLimit, /结果：模型供应商额度已耗尽，本次请求被拒/);
  assert.match(joyaiRateLimit, /解决方式：JoyAI 服务商限流（429，每分钟 token 额度已用完），请稍后重试或提高供应商额度/);

  const missing = describeDuplexError('请配置 QWEN_OMNI_API_KEY');
  assert.match(missing, /结果：全双工会话无法正常启动或继续/);
  assert.match(missing, /解决方式：请在全双工设置中补全/);

  const task = describeDuplexError('Jiuwen Core Agent returned empty output', 'task');
  assert.match(task, /结果：本次任务未完成，无法使用任务结果/);
  assert.match(task, /解决方式：请重试；若再次失败，查看 Jiuwen Core Agent 的任务日志/);
  assert.equal(describeDuplexError(task, 'task'), task);

  const unsupported = describeDuplexError('Not supported');
  assert.match(unsupported, /结果：当前运行环境不支持该功能/);
  assert.doesNotMatch(unsupported, /服务日志/);
});
