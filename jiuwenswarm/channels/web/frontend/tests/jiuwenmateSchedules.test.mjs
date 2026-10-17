import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
const built = await build({ entryPoints: ['src/features/jiuwenmate/schedules/state.ts'], bundle: true, write: false, platform: 'node', format: 'esm' });
const state = await import(`data:text/javascript;base64,${Buffer.from(built.outputFiles[0].text).toString('base64')}`);
test('daily weekly and monthly controls preserve wall time and custom cron', () => {
  assert.equal(state.buildCron('daily', '17:30', 1), '30 17 * * *');
  assert.equal(state.buildCron('weekly', '09:05', 0), '5 9 * * 0');
  assert.equal(state.buildCron('monthly', '08:00', 31), '0 8 31 * *');
  assert.deepEqual(state.parseCron('*/5 * * * *'), { frequency: 'custom', time: '09:00', day: 1 });
});
test('date controls preserve timezone and reject nonexistent DST times', () => {
  assert.equal(state.toZonedInput('2026-10-18T01:30:00Z', 'Asia/Shanghai'), '2026-10-18T09:30');
  assert.equal(state.fromZonedInput('2026-10-18T09:30', 'Asia/Shanghai'), '2026-10-18T01:30:00.000Z');
  assert.equal(state.fromZonedInput('2026-03-08T02:30', 'America/New_York'), null);
});
const draft = { name: 'Summary', prompt: 'Summarize', scheduleKind: 'cron', cronExpr: '0 9 * * *', timezone: 'Asia/Shanghai', intervalSeconds: null, runAt: null, startsAt: null, endsAt: null };
test('validation rejects empty fields, zero intervals, expired one-offs and reversed windows', () => {
  assert.equal(state.validateDraft(draft, 'agent'), null);
  assert.equal(state.validateDraft({ ...draft, name: ' ' }, 'agent'), 'required');
  assert.equal(state.validateDraft(draft, ''), 'executorRequired');
  assert.equal(state.validateDraft({ ...draft, scheduleKind: 'interval', intervalSeconds: 0 }, 'agent'), 'intervalInvalid');
  assert.equal(state.validateDraft({ ...draft, scheduleKind: 'once', runAt: '2020-01-01T00:00:00Z' }, 'agent'), 'futureRequired');
  assert.equal(state.validateDraft({ ...draft, startsAt: '2030-02-01T00:00:00Z', endsAt: '2030-01-01T00:00:00Z' }, 'agent'), 'rangeInvalid');
});
test('search and executor/status filters intersect and ignore case', () => {
  const plans = [{ ...draft, id: 'one', agentId: 'a', lastStatus: 'running' }, { ...draft, id: 'two', agentId: 'b', lastStatus: 'failed' }];
  assert.deepEqual(state.filterSchedules(plans, 'SUM', 'b', 'failed').map(p => p.id), ['two']);
  assert.deepEqual(state.filterSchedules(plans, '', 'a', 'paused'), []);
});
