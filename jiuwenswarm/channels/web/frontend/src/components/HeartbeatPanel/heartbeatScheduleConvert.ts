import type { HeartbeatScheduleDTO, HeartbeatScheduleKind } from '../../types/heartbeat';
import { epochSecondsToWallClock, wallClockToEpochSeconds } from '../CronPanel/scheduleConvert';

/** ScheduleEditor 内部表单状态：三种 kind 共用一个结构，提交时按 kind 只取对应字段，见 scheduleFormToDto */
export interface HeartbeatScheduleFormValue {
  kind: HeartbeatScheduleKind;
  intervalSeconds: number; // interval 用
  cronExpr: string; // cron 用，支持 5 段 crontab 或普通 Cron 任务使用的 7 段格式
  timezone: string; // cron/once 共用：禁选「今天」、once 墙钟编解码的唯一时区来源（#5610）
  onceDate: string; // once 用，YYYY-MM-DD，按 timezone 墙钟
  onceTime: string; // once 用，HH:mm，按 timezone 墙钟
}

const MIN_INTERVAL_SECONDS = 60;
const DEFAULT_ONCE_TIMEZONE = 'Asia/Shanghai';

export function emptyHeartbeatScheduleForm(defaultTimezone: string): HeartbeatScheduleFormValue {
  return {
    kind: 'interval',
    intervalSeconds: 1800,
    cronExpr: '',
    timezone: defaultTimezone,
    onceDate: '',
    onceTime: '',
  };
}

/**
 * run_at（Unix 秒） -> 指定时区墙钟日期/时间。
 * 必须传 timezone：主机本地时区不得参与 once 编解码（GitCode #5610）。
 */
export function epochSecondsToOnceLocal(
  epochSeconds: number | null | undefined,
  timezone: string = DEFAULT_ONCE_TIMEZONE,
): { date: string; time: string } {
  return epochSecondsToWallClock(epochSeconds, timezone || DEFAULT_ONCE_TIMEZONE);
}

/**
 * 指定时区墙钟日期/时间 -> run_at（Unix 秒）。
 * 缺任一项按 0 处理，交给表单校验层拦截。
 */
export function onceLocalToEpochSeconds(
  date: string,
  time: string,
  timezone: string = DEFAULT_ONCE_TIMEZONE,
): number {
  return wallClockToEpochSeconds(date, time, timezone || DEFAULT_ONCE_TIMEZONE);
}

export function scheduleDtoToForm(schedule: HeartbeatScheduleDTO, fallbackTimezone: string): HeartbeatScheduleFormValue {
  if (schedule.type === 'interval') {
    return { kind: 'interval', intervalSeconds: schedule.interval_seconds, cronExpr: '', timezone: fallbackTimezone, onceDate: '', onceTime: '' };
  }
  if (schedule.type === 'cron') {
    return { kind: 'cron', intervalSeconds: 1800, cronExpr: schedule.cron_expr, timezone: schedule.timezone, onceDate: '', onceTime: '' };
  }
  const tz = fallbackTimezone || DEFAULT_ONCE_TIMEZONE;
  const { date, time } = epochSecondsToOnceLocal(schedule.run_at, tz);
  return { kind: 'once', intervalSeconds: 1800, cronExpr: '', timezone: tz, onceDate: date, onceTime: time };
}

export function scheduleFormToDto(form: HeartbeatScheduleFormValue): HeartbeatScheduleDTO {
  if (form.kind === 'interval') {
    const seconds = Math.max(MIN_INTERVAL_SECONDS, Math.floor(form.intervalSeconds) || 0);
    return { type: 'interval', interval_seconds: seconds };
  }
  if (form.kind === 'cron') {
    return { type: 'cron', cron_expr: form.cronExpr.trim(), timezone: form.timezone };
  }
  return {
    type: 'once',
    run_at: onceLocalToEpochSeconds(form.onceDate, form.onceTime, form.timezone),
  };
}

/** 列表/详情里的一行摘要文案，t 传 i18next 的 t 函数 */
export function summarizeHeartbeatSchedule(
  schedule: HeartbeatScheduleDTO,
  t: (key: string, opts?: Record<string, unknown>) => string,
  timezone: string = DEFAULT_ONCE_TIMEZONE,
): string {
  if (schedule.type === 'interval') {
    const minutes = Math.round(schedule.interval_seconds / 60);
    return t('heartbeat.schedule.summary.interval', { minutes });
  }
  if (schedule.type === 'cron') {
    return t('heartbeat.schedule.summary.cron', { expr: schedule.cron_expr, timezone: schedule.timezone });
  }
  const { date, time } = epochSecondsToOnceLocal(schedule.run_at, timezone);
  return t('heartbeat.schedule.summary.once', { date, time });
}
