import assert from 'node:assert/strict';
import test from 'node:test';

import {
  emptyHeartbeatScheduleForm,
  scheduleDtoToForm,
  scheduleFormToDto,
  onceLocalToEpochSeconds,
  epochSecondsToOnceLocal,
} from '../node_modules/.cache/heartbeat-schedule-convert/components/HeartbeatPanel/heartbeatScheduleConvert.js';

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

test('scheduleFormToDto trims cron_expr whitespace', () => {
  const form = emptyHeartbeatScheduleForm('Asia/Shanghai');
  form.kind = 'cron';
  form.cronExpr = '  0 9 * * 1-5  ';
  assert.deepEqual(scheduleFormToDto(form), { type: 'cron', cron_expr: '0 9 * * 1-5', timezone: 'Asia/Shanghai' });
});

test('once epoch <-> local date/time round-trips regardless of host timezone', () => {
  // 用"整分钟"时间戳做往返测试，不依赖运行测试的机器处于哪个时区
  const epoch = Math.floor(Date.now() / 1000 / 60) * 60;
  const { date, time } = epochSecondsToOnceLocal(epoch);
  assert.notEqual(date, '');
  assert.notEqual(time, '');
  assert.equal(onceLocalToEpochSeconds(date, time), epoch);
});

test('epochSecondsToOnceLocal returns empty strings for falsy input', () => {
  assert.deepEqual(epochSecondsToOnceLocal(null), { date: '', time: '' });
  assert.deepEqual(epochSecondsToOnceLocal(undefined), { date: '', time: '' });
  assert.deepEqual(epochSecondsToOnceLocal(0), { date: '', time: '' });
});

test('onceLocalToEpochSeconds returns 0 when date or time missing', () => {
  assert.equal(onceLocalToEpochSeconds('', '09:00'), 0);
  assert.equal(onceLocalToEpochSeconds('2026-08-10', ''), 0);
});

test('scheduleDtoToForm/scheduleFormToDto round-trip for once', () => {
  const epoch = Math.floor(Date.now() / 1000 / 60) * 60 + 3600;
  const dto = { type: 'once', run_at: epoch };
  const form = scheduleDtoToForm(dto, 'Asia/Shanghai');
  assert.equal(form.kind, 'once');
  assert.deepEqual(scheduleFormToDto(form), dto);
});
