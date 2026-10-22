import assert from 'node:assert/strict';
import test from 'node:test';

import { validateCronExpr } from '../node_modules/.cache/cron-expr-validation/components/CronPanel/cronExprValidation.js';

test('validateCronExpr accepts a normal 7-field expression', () => {
  assert.equal(validateCronExpr('0 0 9 * * ? *').valid, true);
  assert.equal(validateCronExpr('0 */5 * * * * *').valid, true);
});

test('validateCronExpr rejects impure numeric tokens that parseInt would accept', () => {
  assert.equal(validateCronExpr('0 0 12abc * * * *').valid, false);
  assert.equal(validateCronExpr('0 */1.5 * * * * *').valid, false);
  assert.equal(validateCronExpr('0 0 */1e+21 * * * *').valid, false);
  assert.equal(validateCronExpr('0 0 1e2 * * * *').valid, false);
});

test('validateCronExpr rejects wrong field count', () => {
  assert.equal(validateCronExpr('0 0 9 * * ?').valid, false);
});
