import assert from 'node:assert/strict';
import test from 'node:test';

import {
  contextCompressionElapsedText,
  contextCompressionRunningText,
  resolveCompactStartedAt,
} from '../node_modules/.cache/context-compression-elapsed/contextCompression.mjs';

/** 只关心分档与插值，用固定的英文单位代替真实 i18n 资源。 */
const t = (key, options = {}) => {
  const branch = key.split('.').pop();
  if (branch === 'seconds') return `${options.seconds}s`;
  if (branch === 'minutesSeconds') return `${options.minutes}m ${options.seconds}s`;
  if (branch === 'hoursMinutes') return `${options.hours}h ${options.minutes}m`;
  return key;
};

test('elapsed text renders seconds below one minute', () => {
  assert.equal(contextCompressionElapsedText(t, 0), '0s');
  assert.equal(contextCompressionElapsedText(t, 7), '7s');
  assert.equal(contextCompressionElapsedText(t, 59), '59s');
});

test('elapsed text switches to minutes at 60 seconds and keeps the remainder', () => {
  assert.equal(contextCompressionElapsedText(t, 60), '1m 0s');
  assert.equal(contextCompressionElapsedText(t, 125), '2m 5s');
  assert.equal(contextCompressionElapsedText(t, 3599), '59m 59s');
});

test('elapsed text switches to hours past 3600 seconds', () => {
  assert.equal(contextCompressionElapsedText(t, 3600), '1h 0m');
  assert.equal(contextCompressionElapsedText(t, 3725), '1h 2m');
});

test('elapsed text clamps negative and fractional input', () => {
  assert.equal(contextCompressionElapsedText(t, -5), '0s');
  assert.equal(contextCompressionElapsedText(t, 2.9), '2s');
});

test('running text keeps per-compressor wording and falls back to the backend summary', () => {
  const localize = (key) => `localized:${key}`;
  assert.equal(
    contextCompressionRunningText(localize, 'DialogueCompressor', 'ignored'),
    'localized:chat.contextCompressionStarted.dialogue',
  );
  assert.equal(
    contextCompressionRunningText(localize, 'CurrentRoundCompressor', 'ignored'),
    'localized:chat.contextCompressionStarted.currentRound',
  );
  assert.equal(
    contextCompressionRunningText(localize, 'RoundLevelCompressor', 'ignored'),
    'localized:chat.contextCompressionStarted.roundLevel',
  );
  assert.equal(
    contextCompressionRunningText(localize, 'SessionMemoryCompressor', 'backend summary'),
    'backend summary',
  );
});

test('compact start time resolves per session, not from whichever session started last', () => {
  const startedAtBySession = new Map([
    ['session-a', 1_000],
    ['session-b', 2_500],
  ]);
  // 会话 A 压缩中、B 也发起压缩后切回 A：必须拿到 A 的起点，而不是 B 覆盖后的值。
  assert.equal(resolveCompactStartedAt(startedAtBySession, 'session-a'), 1_000);
  assert.equal(resolveCompactStartedAt(startedAtBySession, 'session-b'), 2_500);
  assert.equal(resolveCompactStartedAt(startedAtBySession, 'session-c'), undefined);
  assert.equal(resolveCompactStartedAt(startedAtBySession, null), undefined);
  assert.equal(resolveCompactStartedAt(startedAtBySession, undefined), undefined);
});
