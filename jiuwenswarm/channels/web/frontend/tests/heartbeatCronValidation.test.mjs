import assert from 'node:assert/strict';
import test from 'node:test';

import { validateHeartbeatCronExpr } from '../node_modules/.cache/heartbeat-cron-validation/components/HeartbeatPanel/heartbeatCronValidation.js';

test('accepts a valid 5-field weekday cron expression', () => {
  assert.deepEqual(validateHeartbeatCronExpr('0 9 * * 1-5'), { valid: true });
});

test('accepts a valid 5-field wildcard expression', () => {
  assert.deepEqual(validateHeartbeatCronExpr('*/15 * * * *'), { valid: true });
});

test('accepts valid 7-field expressions with seconds and year', () => {
  assert.deepEqual(validateHeartbeatCronExpr('0 0 9 * * ? *'), { valid: true });
  assert.deepEqual(validateHeartbeatCronExpr('30 15 10 20 12 ? 2027'), { valid: true });
});

test('rejects expressions that are neither 5 nor 7 fields', () => {
  assert.equal(validateHeartbeatCronExpr('0 9 * * * *').valid, false);
  assert.equal(validateHeartbeatCronExpr('0 9 * *').valid, false);
  assert.equal(validateHeartbeatCronExpr('9 * * *').valid, false);
});

test('validates 7-field second and year ranges', () => {
  assert.equal(validateHeartbeatCronExpr('60 0 9 * * ? *').error, 'cron.errors.cronSecondOrMinute');
  assert.equal(validateHeartbeatCronExpr('0 0 9 * * ? 2100').error, 'cron.errors.cronYear');
});

test('rejects an out-of-range field and reuses CronPanel field-range error keys', () => {
  const result = validateHeartbeatCronExpr('0 25 * * *'); // 小时 25 超出 0-23
  assert.equal(result.valid, false);
  assert.equal(result.error, 'cron.errors.cronHour');
});

test('rejects an invalid weekday field', () => {
  const result = validateHeartbeatCronExpr('0 9 * * 8'); // 周字段 0-6，8 非法
  assert.equal(result.valid, false);
  assert.equal(result.error, 'cron.errors.cronWeek');
});

test('trims surrounding whitespace before field-count check', () => {
  assert.deepEqual(validateHeartbeatCronExpr('  0 9 * * 1-5  '), { valid: true });
});
