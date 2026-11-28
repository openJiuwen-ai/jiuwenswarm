import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

await build({
  entryPoints: ['src/components/CronPanel/cronTimeFormat.ts'],
  outfile: 'node_modules/.cache/cron-time-format/cronTimeFormat.mjs',
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
});

const { formatCronPreviewTime, formatCronRunTime } = await import(
  '../node_modules/.cache/cron-time-format/cronTimeFormat.mjs'
);

const NY = { timeZone: 'America/New_York', locale: 'zh-CN' };

test('formats in the job timezone and appends the zone name', () => {
  const zh = formatCronRunTime('2026-11-28T03:30:00-05:00', NY);
  assert.match(zh, /03:30:00/);
  assert.match(zh, /\(America\/New_York\)$/);
  assert.doesNotMatch(zh, /16:30/);

  const en = formatCronRunTime('2026-11-28T03:30:00-05:00', { ...NY, locale: 'en-US' });
  assert.match(en, /03:30:00/);
  assert.match(en, /\(America\/New_York\)$/);
});

test('accepts both seconds and milliseconds without changing the result', () => {
  const epochMs = Date.parse('2026-11-28T03:30:00-05:00');
  const fromSeconds = formatCronRunTime(Math.floor(epochMs / 1000), NY);
  const fromMs = formatCronRunTime(epochMs, NY);
  assert.equal(fromSeconds, fromMs);
});

test('returns null for missing, zero and unparsable values', () => {
  for (const value of [null, undefined, '', 0, -1, Number.NaN, 'not-a-date']) {
    assert.equal(formatCronRunTime(value, NY), null);
  }
});

test('falls back without a zone label when the zone is unknown', () => {
  const text = formatCronRunTime('2026-11-28T03:30:00-05:00', { timeZone: 'Not/AZone', locale: 'zh-CN' });
  assert.equal(typeof text, 'string');
  assert.doesNotMatch(text, /Not\/AZone/);
});

test('renders midnight as 00:00:00 and never 24:00:00', () => {
  const midnight = '2026-11-28T00:00:00+08:00';
  for (const locale of ['zh-CN', 'en-US']) {
    const text = formatCronRunTime(midnight, { timeZone: 'Asia/Shanghai', locale });
    assert.match(text, /00:00:00/);
    assert.doesNotMatch(text, /24:00:00/);
    assert.match(text, /\(Asia\/Shanghai\)$/);
  }
});

test('preview keeps an unparsable value verbatim', () => {
  assert.equal(formatCronPreviewTime('not-a-date', NY), 'not-a-date');
  assert.match(formatCronPreviewTime('2026-11-28T03:30:00-05:00', NY), /\(America\/New_York\)$/);
});
