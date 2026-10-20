import assert from 'node:assert/strict';
import test from 'node:test';

import { validateCronExpr } from '../node_modules/.cache/cron-expr-validation/components/CronPanel/cronExprValidation.js';

test('accepts "start/step" fields (issue #5138 regression)', () => {
  assert.deepEqual(validateCronExpr('0 0/30 9-18 * * ? *'), { valid: true });
  assert.deepEqual(validateCronExpr('0 00/030 9-18 * * ? *'), { valid: true });
  assert.deepEqual(validateCronExpr('0 5/15 * * * ? *'), { valid: true });
  assert.deepEqual(validateCronExpr('0 0 9/2 * * ? *'), { valid: true });
});

test('still accepts star/step and range/step fields', () => {
  assert.deepEqual(validateCronExpr('0 */30 9-18 * * ? *'), { valid: true });
  assert.deepEqual(validateCronExpr('0 0 9-18/2 * * ? *'), { valid: true });
});

test('rejects start/step fields with out-of-range start or non-positive step', () => {
  assert.equal(validateCronExpr('0 60/30 * * * ? *').error, 'cron.errors.cronSecondOrMinute');
  assert.equal(validateCronExpr('0 0/0 * * * ? *').error, 'cron.errors.cronSecondOrMinute');
  assert.equal(validateCronExpr('0 0 25/2 * * ? *').error, 'cron.errors.cronHour');
});

test('rejects malformed steps and extra slash segments for all supported bases', () => {
  for (const base of ['0', '*', '0-59']) {
    for (const step of ['1.5', '30foo', '30/2', '+30', '-30', '', '/30', '30/']) {
      const expr = `0 ${base}/${step} * * * ? *`;
      assert.deepEqual(validateCronExpr(expr), { valid: false, error: 'cron.errors.cronSecondOrMinute' }, expr);
    }
  }
});
