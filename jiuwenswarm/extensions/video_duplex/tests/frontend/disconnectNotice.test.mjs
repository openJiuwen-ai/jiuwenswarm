import assert from 'node:assert/strict';
import test from 'node:test';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(new URL('../../../../channels/web/frontend/package.json', import.meta.url));
const { build } = require('esbuild');
const runtimePath = fileURLToPath(new URL('../../frontend/taskFullDuplexRuntimeStore.ts', import.meta.url));
const toastPath = fileURLToPath(new URL('../../../../channels/web/frontend/src/components/ui/Toast/toastStore.ts', import.meta.url));
const compiled = await build({
  stdin: { contents: `export * from ${JSON.stringify(runtimePath)}; export * from ${JSON.stringify(toastPath)};`,
    resolveDir: fileURLToPath(new URL('../../../../channels/web/frontend/', import.meta.url)) },
  bundle: true, platform: 'node', format: 'esm', write: false,
  alias: { react: require.resolve('react') },
});
const { setTaskFullDuplexRuntimeError, setTaskFullDuplexRuntimeState, toastStore } =
  await import(`data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`);

test('duplex error uses the native persistent notice, survives idle and deduplicates repeats', () => {
  setTaskFullDuplexRuntimeError('quota exhausted');
  const notices = toastStore.getSnapshot();
  assert.equal(notices.length, 1);
  assert.equal(notices[0].content, 'quota exhausted');
  assert.equal(notices[0].durationMs, 0);
  assert.equal(notices[0].variant, 'error');
  setTaskFullDuplexRuntimeState('idle');
  setTaskFullDuplexRuntimeError('quota exhausted');
  assert.equal(toastStore.getSnapshot().length, 1);
});
