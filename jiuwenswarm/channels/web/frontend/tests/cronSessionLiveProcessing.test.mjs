import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const outFile = join(root, 'node_modules/.cache/cron-processing-guard/cronProcessingGuard.mjs');
mkdirSync(dirname(outFile), { recursive: true });
await build({
  entryPoints: [join(root, 'src/features/cron/cronProcessingGuard.ts')],
  outfile: outFile,
  bundle: true,
  platform: 'node',
  format: 'esm',
});

const {
  isCronExecutionSession,
  shouldKeepLocalCronProcessing,
} = await import(outFile);

test('isCronExecutionSession: cron_ / __cron__ 前缀才算执行会话', () => {
  assert.equal(isCronExecutionSession('cron_123_job'), true);
  assert.equal(isCronExecutionSession('__cron__abc'), true);
  assert.equal(isCronExecutionSession('sess_normal'), false);
});

test('shouldKeepLocalCronProcessing: metadata false 不得盖掉本地 cron 执行中', () => {
  assert.equal(shouldKeepLocalCronProcessing('cron_1_job', false, true), true);
  assert.equal(shouldKeepLocalCronProcessing('cron_1_job', true, true), false);
  assert.equal(shouldKeepLocalCronProcessing('cron_1_job', false, false), false);
  assert.equal(shouldKeepLocalCronProcessing('sess_normal', false, true), false);
});
