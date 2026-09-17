import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer } from 'vite';

const vite = await createServer({
  configFile: false,
  cacheDir: 'node_modules/.cache/quota-reset/vite',
  server: { middlewareMode: true, hmr: false, watch: null },
});
let mod;
let points;
try {
  mod = await vite.ssrLoadModule('/src/features/free-models/quotaReset.ts');
  points = await vite.ssrLoadModule('/src/features/free-models/points.ts');
} finally {
  await vite.close();
}
const { describeQuotaReset, describeQuotaExhausted, parseResetPeriod } = mod;
const { formatPoints, usedRatio } = points;

// 用本地时间构造，断言不受运行机器时区影响
const mondayMorning = new Date(2026, 8, 21, 8, 0, 0).toISOString(); // 2026-09-21 周一 08:00（本地）

test('LiteLLM duration strings are parsed', () => {
  assert.deepEqual(parseResetPeriod('7d'), { count: 7, unit: 'd' });
  assert.deepEqual(parseResetPeriod('1mo'), { count: 1, unit: 'mo' });
  assert.deepEqual(parseResetPeriod('5h'), { count: 5, unit: 'h' });
  assert.equal(parseResetPeriod(''), null);
  assert.equal(parseResetPeriod('weekly'), null);
  assert.equal(parseResetPeriod('0d'), null);
});

test('weekly budget is described by weekday and time', () => {
  const zh = describeQuotaReset('7d', mondayMorning, 'zh-CN');
  assert.equal(zh.key, 'auth.huawei.quota.resetWeekly');
  assert.equal(zh.params.weekday, '周一');
  assert.equal(zh.params.time, '08:00');
  assert.equal(describeQuotaReset('1w', mondayMorning, 'en-US').params.weekday, 'Monday');
});

test('daily and monthly budgets are described by their rhythm', () => {
  assert.equal(describeQuotaReset('1d', mondayMorning, 'zh-CN').key, 'auth.huawei.quota.resetDaily');
  assert.equal(describeQuotaReset('24h', mondayMorning, 'zh-CN').key, 'auth.huawei.quota.resetDaily');
  const monthly = describeQuotaReset('1mo', mondayMorning, 'en-US');
  assert.equal(monthly.key, 'auth.huawei.quota.resetMonthly');
  assert.equal(monthly.params.day, '21');
});

test('irregular periods give the interval and the next refresh time', () => {
  const every3 = describeQuotaReset('3d', mondayMorning, 'zh-CN');
  assert.equal(every3.key, 'auth.huawei.quota.resetEveryDays');
  assert.equal(every3.params.count, '3');
  assert.match(every3.params.date, /21/);
  assert.equal(describeQuotaReset('3d', '', 'zh-CN').key, 'auth.huawei.quota.resetEveryDaysPlain');
  assert.equal(describeQuotaReset('2w', mondayMorning, 'zh-CN').params.count, '14');
});

test('no period means the quota does not come back', () => {
  assert.equal(describeQuotaReset('', '', 'zh-CN'), null);
  assert.equal(describeQuotaReset(undefined, undefined, 'zh-CN'), null);
});

test('exhausted quota says when it refreshes, if it does', () => {
  const text = describeQuotaExhausted(mondayMorning, 'zh-CN');
  assert.equal(text.key, 'auth.huawei.quota.exhaustedUntil');
  assert.match(text.params.date, /08:00/);
  assert.equal(describeQuotaExhausted('', 'zh-CN'), null);
});

test('points are shown without currency, grouped, at most two decimals, never rounded up', () => {
  assert.equal(formatPoints(100, 'zh-CN'), '100');
  assert.equal(formatPoints(97.86, 'zh-CN'), '97.86');
  assert.equal(formatPoints(1200.5, 'en-US'), '1,200.5');
  assert.equal(formatPoints(99.996, 'zh-CN'), '99.99', '还没用满就不能显示成 100');
  assert.equal(formatPoints(Number.NaN, 'zh-CN'), '—');
});

test('the meter shows what is used, and only when total and used are known', () => {
  assert.equal(usedRatio({ total: 100, used: 25, balance: 75, exhausted: false }), 0.25);
  assert.equal(usedRatio({ total: 100, used: 120, balance: -20, exhausted: true }), 1, '透支也只画满');
  assert.equal(usedRatio({ total: -1, used: 3, balance: -1, exhausted: false }), null, '不限量：没有进度条');
  assert.equal(usedRatio({ total: 100, used: -1, balance: -1, exhausted: false }), null);
});
