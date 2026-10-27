import assert from 'node:assert/strict';
import test from 'node:test';

import {
  emptyHeartbeatScheduleForm,
  scheduleDtoToForm,
  scheduleFormToDto,
  onceLocalToEpochSeconds,
  epochSecondsToOnceLocal,
  summarizeHeartbeatSchedule,
} from '../node_modules/.cache/heartbeat-schedule-convert/heartbeatScheduleConvert.mjs';

test('emptyHeartbeatScheduleForm defaults to a 30-minute interval with the given timezone', () => {
  const form = emptyHeartbeatScheduleForm('Asia/Shanghai');
  assert.equal(form.kind, 'interval');
  assert.equal(form.intervalSeconds, 1800);
  assert.equal(form.timezone, 'Asia/Shanghai');
});

test('scheduleDtoToForm/scheduleFormToDto round-trip for interval', () => {
  const dto = { type: 'interval', interval_seconds: 900 };
  const form = scheduleDtoToForm(dto, 'Asia/Shanghai');
  assert.equal(form.kind, 'interval');
  assert.equal(form.intervalSeconds, 900);
  assert.deepEqual(scheduleFormToDto(form), dto);
});

test('scheduleFormToDto clamps interval below 60 seconds up to 60', () => {
  const form = emptyHeartbeatScheduleForm('Asia/Shanghai');
  form.intervalSeconds = 10;
  assert.deepEqual(scheduleFormToDto(form), { type: 'interval', interval_seconds: 60 });
});

test('scheduleDtoToForm/scheduleFormToDto round-trip for cron', () => {
  const dto = { type: 'cron', cron_expr: '0 9 * * 1-5', timezone: 'Asia/Tokyo' };
  const form = scheduleDtoToForm(dto, 'Asia/Shanghai');
  assert.equal(form.kind, 'cron');
  assert.equal(form.cronExpr, '0 9 * * 1-5');
  assert.equal(form.timezone, 'Asia/Tokyo');
  assert.deepEqual(scheduleFormToDto(form), dto);
});

test('scheduleDtoToForm/scheduleFormToDto preserves a 7-field cron expression', () => {
  const dto = { type: 'cron', cron_expr: '30 15 10 20 12 ? 2027', timezone: 'Asia/Shanghai' };
  const form = scheduleDtoToForm(dto, 'UTC');
  assert.equal(form.cronExpr, dto.cron_expr);
  assert.deepEqual(scheduleFormToDto(form), dto);
});

test('scheduleFormToDto trims cron_expr whitespace', () => {
  const form = emptyHeartbeatScheduleForm('Asia/Shanghai');
  form.kind = 'cron';
  form.cronExpr = '  0 9 * * 1-5  ';
  assert.deepEqual(scheduleFormToDto(form), { type: 'cron', cron_expr: '0 9 * * 1-5', timezone: 'Asia/Shanghai' });
});

test('once epoch <-> wall clock round-trips in Asia/Shanghai regardless of host timezone (#5610)', () => {
  // 2026-10-21 10:30 Asia/Shanghai == 2026-10-21 02:30 UTC
  const epoch = Date.UTC(2026, 9, 21, 2, 30, 0) / 1000;
  const { date, time } = epochSecondsToOnceLocal(epoch, 'Asia/Shanghai');
  assert.equal(date, '2026-10-21');
  assert.equal(time, '10:30');
  assert.equal(onceLocalToEpochSeconds(date, time, 'Asia/Shanghai'), epoch);
});

test('once wall clock in UTC differs from Asia/Shanghai for the same instant (#5610)', () => {
  const epoch = Date.UTC(2026, 9, 21, 2, 30, 0) / 1000;
  assert.deepEqual(epochSecondsToOnceLocal(epoch, 'UTC'), { date: '2026-10-21', time: '02:30' });
  assert.deepEqual(epochSecondsToOnceLocal(epoch, 'Asia/Shanghai'), { date: '2026-10-21', time: '10:30' });
  assert.equal(onceLocalToEpochSeconds('2026-10-21', '10:30', 'Asia/Shanghai'), epoch);
  assert.equal(onceLocalToEpochSeconds('2026-10-21', '02:30', 'UTC'), epoch);
});

test('epochSecondsToOnceLocal returns empty strings for falsy input', () => {
  assert.deepEqual(epochSecondsToOnceLocal(null, 'Asia/Shanghai'), { date: '', time: '' });
  assert.deepEqual(epochSecondsToOnceLocal(undefined, 'Asia/Shanghai'), { date: '', time: '' });
  assert.deepEqual(epochSecondsToOnceLocal(0, 'Asia/Shanghai'), { date: '', time: '' });
});

test('onceLocalToEpochSeconds returns 0 when date or time missing', () => {
  assert.equal(onceLocalToEpochSeconds('', '09:00', 'Asia/Shanghai'), 0);
  assert.equal(onceLocalToEpochSeconds('2026-08-10', '', 'Asia/Shanghai'), 0);
});

test('scheduleDtoToForm/scheduleFormToDto round-trip for once uses job timezone (#5610)', () => {
  const epoch = Date.UTC(2026, 9, 22, 4, 0, 0) / 1000; // 12:00 Shanghai
  const dto = { type: 'once', run_at: epoch };
  const form = scheduleDtoToForm(dto, 'Asia/Shanghai');
  assert.equal(form.kind, 'once');
  assert.equal(form.timezone, 'Asia/Shanghai');
  assert.equal(form.onceDate, '2026-10-22');
  assert.equal(form.onceTime, '12:00');
  assert.deepEqual(scheduleFormToDto(form), dto);
});

test('summarizeHeartbeatSchedule once uses provided timezone for wall clock', () => {
  const epoch = Date.UTC(2026, 9, 21, 2, 30, 0) / 1000;
  const summary = summarizeHeartbeatSchedule(
    { type: 'once', run_at: epoch },
    (key, opts) => `${key}:${opts?.date} ${opts?.time}`,
    'Asia/Shanghai',
  );
  assert.equal(summary, 'heartbeat.schedule.summary.once:2026-10-21 10:30');
});
