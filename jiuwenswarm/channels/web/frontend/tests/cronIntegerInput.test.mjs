import assert from 'node:assert/strict';
import test from 'node:test';

import {
  INTERVAL_MAX_HOURS,
  INTERVAL_MAX_MINUTES,
  INTERVAL_MIN_MINUTES,
  isIntervalValueInRange,
  normalizeDigitsInput,
  parseDigitInt,
  parseIntervalField,
  stripLeadingZeros,
} from '../node_modules/.cache/cron-integer-input/components/CronPanel/cronIntegerInput.js';

test('stripLeadingZeros keeps numeric text without Number round-trip', () => {
  assert.equal(stripLeadingZeros(''), '');
  assert.equal(stripLeadingZeros('0'), '0');
  assert.equal(stripLeadingZeros('00'), '0');
  assert.equal(stripLeadingZeros('01'), '1');
  assert.equal(stripLeadingZeros('010'), '10');
});

test('normalizeDigitsInput never emits scientific notation for huge digit strings', () => {
  const huge = '1' + '0'.repeat(30);
  const out = normalizeDigitsInput(huge);
  assert.equal(out.includes('e'), false);
  assert.equal(out.includes('E'), false);
  assert.equal(out, huge);
  assert.equal(normalizeDigitsInput('01a2'), '12');
});

test('parseDigitInt maps oversized digit strings to Infinity sentinel', () => {
  assert.equal(parseDigitInt(''), undefined);
  assert.equal(parseDigitInt('24'), 24);
  assert.equal(parseDigitInt('1' + '0'.repeat(20)), Number.POSITIVE_INFINITY);
});

test('parseIntervalField uses finite max+1 for digit strings longer than the limit', () => {
  assert.equal(parseIntervalField('', 'hours'), undefined);
  assert.equal(parseIntervalField('24', 'hours'), 24);
  assert.equal(parseIntervalField('25', 'hours'), 25);
  assert.equal(parseIntervalField('999', 'hours'), INTERVAL_MAX_HOURS + 1);
  assert.equal(parseIntervalField('1440', 'minutes'), 1440);
  assert.equal(parseIntervalField('1441', 'minutes'), 1441);
  assert.equal(parseIntervalField('1' + '0'.repeat(20), 'minutes'), INTERVAL_MAX_MINUTES + 1);
});

test('isIntervalValueInRange matches 1–24 hours and 5–1440 minutes', () => {
  assert.equal(isIntervalValueInRange(undefined, 'hours'), false);
  assert.equal(isIntervalValueInRange(0, 'hours'), false);
  assert.equal(isIntervalValueInRange(1, 'hours'), true);
  assert.equal(isIntervalValueInRange(INTERVAL_MAX_HOURS, 'hours'), true);
  assert.equal(isIntervalValueInRange(INTERVAL_MAX_HOURS + 1, 'hours'), false);
  assert.equal(isIntervalValueInRange(INTERVAL_MIN_MINUTES - 1, 'minutes'), false);
  assert.equal(isIntervalValueInRange(INTERVAL_MIN_MINUTES, 'minutes'), true);
  assert.equal(isIntervalValueInRange(INTERVAL_MAX_MINUTES, 'minutes'), true);
  assert.equal(isIntervalValueInRange(INTERVAL_MAX_MINUTES + 1, 'minutes'), false);
  assert.equal(isIntervalValueInRange(Number.POSITIVE_INFINITY, 'minutes'), false);
});
