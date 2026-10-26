import assert from 'node:assert/strict';
import test from 'node:test';

/**
 * Mirrors shouldRetainCronPermissionOnProcessingFalse in cronLiveWatch.ts.
 * Keep in sync when changing retain rules for cron HITL + spurious processing=false.
 */
function isCronPermissionPromptSource(source) {
  const src = String(source || '').trim();
  return !src || src === 'permission_interrupt' || src === 'confirm_interrupt';
}

function shouldRetainCronPermissionOnProcessingFalse(sessionId, pending) {
  const sid = String(sessionId || '').trim();
  if (!sid.startsWith('cron_')) return false;
  if (!pending) return false;
  return isCronPermissionPromptSource(pending.source);
}

test('retain only on cron_* with permission/confirm pending', () => {
  assert.equal(
    shouldRetainCronPermissionOnProcessingFalse('cron_abc', {
      source: 'permission_interrupt',
    }),
    true,
  );
  assert.equal(
    shouldRetainCronPermissionOnProcessingFalse('cron_abc', {
      source: 'confirm_interrupt',
    }),
    true,
  );
  assert.equal(
    shouldRetainCronPermissionOnProcessingFalse('cron_abc', { source: '' }),
    true,
  );
  assert.equal(
    shouldRetainCronPermissionOnProcessingFalse('chat_abc', {
      source: 'permission_interrupt',
    }),
    false,
  );
  assert.equal(
    shouldRetainCronPermissionOnProcessingFalse('cron_abc', {
      source: 'ask_user_interrupt',
    }),
    false,
  );
  assert.equal(shouldRetainCronPermissionOnProcessingFalse('cron_abc', null), false);
});
