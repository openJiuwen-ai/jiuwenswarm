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
  markCronOptimisticProcessing,
  resetCronOptimisticProcessingForTests,
  CRON_OPTIMISTIC_PROCESSING_GRACE_MS,
} = await import(`${outFile}?t=${Date.now()}`);

test('isCronExecutionSession: cron_ / __cron__ 前缀才算执行会话', () => {
  assert.equal(isCronExecutionSession('cron_123_job'), true);
  assert.equal(isCronExecutionSession('__cron__abc'), true);
  assert.equal(isCronExecutionSession('sess_normal'), false);
});

test('shouldKeepLocalCronProcessing: 宽限期内挡住 metadata false', () => {
  resetCronOptimisticProcessingForTests();
  markCronOptimisticProcessing('cron_1_job');
  assert.equal(shouldKeepLocalCronProcessing('cron_1_job', false, true), true);
  assert.equal(shouldKeepLocalCronProcessing('cron_1_job', true, true), false);
  assert.equal(shouldKeepLocalCronProcessing('cron_1_job', false, false), false);
});

test('shouldKeepLocalCronProcessing: 无乐观标记时不挡 metadata', () => {
  resetCronOptimisticProcessingForTests();
  assert.equal(shouldKeepLocalCronProcessing('cron_1_job', false, true), false);
});

test('shouldKeepLocalCronProcessing: hasCronMetadata 覆盖无前缀统一会话', () => {
  resetCronOptimisticProcessingForTests();
  markCronOptimisticProcessing('unified_sess');
  assert.equal(
    shouldKeepLocalCronProcessing('unified_sess', false, true, { hasCronMetadata: true }),
    true,
  );
  assert.equal(
    shouldKeepLocalCronProcessing('unified_sess', false, true, { hasCronMetadata: false }),
    false,
  );
});

test('shouldKeepLocalCronProcessing: 权威 idle 必须放行', () => {
  resetCronOptimisticProcessingForTests();
  markCronOptimisticProcessing('cron_1_job');
  assert.equal(
    shouldKeepLocalCronProcessing('cron_1_job', false, true, { authoritativeIdle: true }),
    false,
  );
});

test('shouldKeepLocalCronProcessing: 宽限过期后放行', () => {
  resetCronOptimisticProcessingForTests();
  markCronOptimisticProcessing('cron_1_job', 1000);
  assert.equal(
    shouldKeepLocalCronProcessing('cron_1_job', false, true, {
      nowMs: Date.now() + CRON_OPTIMISTIC_PROCESSING_GRACE_MS + 1,
    }),
    false,
  );
});
