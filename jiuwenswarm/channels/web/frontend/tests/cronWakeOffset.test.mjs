import assert from 'node:assert/strict';
import test from 'node:test';

import {
  WAKE_OFFSET_MAX_MINUTES,
  isWakeOffsetMinutesValid,
  normalizeWakeOffsetMinutesInput,
  normalizeWakeOffsetSeconds,
  wakeOffsetMinutesToSeconds,
  wakeOffsetSecondsToMinutes,
} from '../node_modules/.cache/cron-wake-offset/components/CronPanel/cronWakeOffset.js';

test('normalizeWakeOffsetSeconds clamps negatives and non-numbers to 0', () => {
  assert.equal(normalizeWakeOffsetSeconds(undefined), 0);
  assert.equal(normalizeWakeOffsetSeconds(null), 0);
  assert.equal(normalizeWakeOffsetSeconds(''), 0);
  assert.equal(normalizeWakeOffsetSeconds('abc'), 0);
  assert.equal(normalizeWakeOffsetSeconds(-10), 0);
  assert.equal(normalizeWakeOffsetSeconds(300.9), 300);
  assert.equal(normalizeWakeOffsetSeconds('300'), 300);
});

test('wakeOffsetSecondsToMinutes floors seconds without capping display', () => {
  assert.equal(wakeOffsetSecondsToMinutes(0), 0);
  assert.equal(wakeOffsetSecondsToMinutes(299), 4);
  assert.equal(wakeOffsetSecondsToMinutes(300), 5);
  // 超限秒数不截断展示，便于面板红框校验
  assert.equal(
    wakeOffsetSecondsToMinutes(WAKE_OFFSET_MAX_MINUTES * 60 + 120),
    WAKE_OFFSET_MAX_MINUTES + 2,
  );
});

test('isWakeOffsetMinutesValid enforces 0–max inclusive; empty means 0', () => {
  assert.equal(isWakeOffsetMinutesValid(''), true);
  assert.equal(isWakeOffsetMinutesValid(null), true);
  assert.equal(isWakeOffsetMinutesValid('0'), true);
  assert.equal(isWakeOffsetMinutesValid(String(WAKE_OFFSET_MAX_MINUTES)), true);
  assert.equal(isWakeOffsetMinutesValid(String(WAKE_OFFSET_MAX_MINUTES + 1)), false);
  assert.equal(isWakeOffsetMinutesValid('999999999999999999999'), false);
});

test('wakeOffsetMinutesToSeconds converts valid minutes; invalid → NaN (no silent clamp)', () => {
  assert.equal(wakeOffsetMinutesToSeconds(''), 0);
  assert.equal(wakeOffsetMinutesToSeconds(null), 0);
  assert.equal(wakeOffsetMinutesToSeconds(5), 300);
  assert.equal(wakeOffsetMinutesToSeconds('5'), 300);
  assert.equal(wakeOffsetMinutesToSeconds(WAKE_OFFSET_MAX_MINUTES), WAKE_OFFSET_MAX_MINUTES * 60);
  assert.ok(Number.isNaN(wakeOffsetMinutesToSeconds(WAKE_OFFSET_MAX_MINUTES + 10)));
});

test('normalizeWakeOffsetMinutesInput strips non-digits and leading zeros without scientific notation', () => {
  assert.equal(normalizeWakeOffsetMinutesInput(''), '');
  assert.equal(normalizeWakeOffsetMinutesInput('0'), '0');
  assert.equal(normalizeWakeOffsetMinutesInput('01'), '1');
  assert.equal(normalizeWakeOffsetMinutesInput('5m'), '5');
  assert.equal(normalizeWakeOffsetMinutesInput('a12b'), '12');
  const huge = '1' + '0'.repeat(30);
  const normalized = normalizeWakeOffsetMinutesInput(huge);
  assert.equal(normalized.includes('e'), false);
  assert.equal(normalized.includes('E'), false);
  assert.equal(normalized, huge);
});

test('round-trip minutes used by ScheduleEditor matches create/update payload seconds', () => {
  // 对话创建任务常见 wake_offset_seconds=300 → 面板展示 5 分钟 → 再提交仍为 300
  const fromJob = wakeOffsetSecondsToMinutes(300);
  assert.equal(fromJob, 5);
  assert.equal(wakeOffsetMinutesToSeconds(String(fromJob)), 300);

  // 用户把提前唤醒清零后，create/update 应提交 0（到点执行）
  assert.equal(wakeOffsetMinutesToSeconds(normalizeWakeOffsetMinutesInput('')), 0);
});
