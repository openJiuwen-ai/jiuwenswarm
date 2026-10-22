/**
 * Mirrors src/features/cron/cronLiveSkipReplayPolicy.ts
 * (no tsc/esbuild required in minimal CI images).
 */
import assert from 'node:assert/strict';
import test from 'node:test';

function shouldDeferCronLiveSkipReplayUpgrade(runtime) {
  if (!runtime) return false;
  return Boolean(
    runtime.isProcessing || runtime.currentStreamId || runtime.pendingQuestion,
  );
}

test('shouldDeferCronLiveSkipReplayUpgrade: streaming / processing / ask', () => {
  assert.equal(shouldDeferCronLiveSkipReplayUpgrade(null), false);
  assert.equal(shouldDeferCronLiveSkipReplayUpgrade({}), false);
  assert.equal(
    shouldDeferCronLiveSkipReplayUpgrade({ isProcessing: true }),
    true,
  );
  assert.equal(
    shouldDeferCronLiveSkipReplayUpgrade({ currentStreamId: 'assistant-1' }),
    true,
  );
  assert.equal(
    shouldDeferCronLiveSkipReplayUpgrade({
      pendingQuestion: { request_id: 'ask-1' },
    }),
    true,
  );
  assert.equal(
    shouldDeferCronLiveSkipReplayUpgrade({
      isProcessing: false,
      currentStreamId: null,
      pendingQuestion: null,
    }),
    false,
  );
});
