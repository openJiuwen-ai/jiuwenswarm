import type { HeartbeatScheduleDTO, HeartbeatScheduleKind } from '../../types/heartbeat';

/** ScheduleEditor 内部表单状态：三种 kind 共用一个结构，提交时按 kind 只取对应字段，见 scheduleFormToDto */
export interface HeartbeatScheduleFormValue {
  kind: HeartbeatScheduleKind;
  intervalSeconds: number; // interval 用
  cronExpr: string; // cron 用，标准 5 段（分 时 日 月 周），不是 CronPanel 的 7 段格式
  timezone: string; // cron 用；也作为整个表单/任务顶层 timezone 的唯一来源，见 HeartbeatTaskDrawer
  onceDate: string; // once 用，YYYY-MM-DD，本地时区
  onceTime: string; // once 用，HH:mm，本地时区
}

const MIN_INTERVAL_SECONDS = 60;

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

/** run_at（Unix 秒） -> 本地日期/时间字符串；用于回填 once 表单和摘要展示 */
export function epochSecondsToOnceLocal(epochSeconds: number | null | undefined): { date: string; time: string } {
  if (!epochSeconds) return { date: '', time: '' };
  const d = new Date(epochSeconds * 1000);
  if (Number.isNaN(d.getTime())) return { date: '', time: '' };
  const pad = (n: number) => String(n).padStart(2, '0');
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return { date, time };
}

/** 本地日期/时间字符串 -> run_at（Unix 秒）；缺任一项按 0 处理，交给表单校验层拦截 */
export function onceLocalToEpochSeconds(date: string, time: string): number {
  if (!date || !time) return 0;
  const ms = new Date(`${date}T${time}:00`).getTime();
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : 0;
}

export function scheduleDtoToForm(schedule: HeartbeatScheduleDTO, fallbackTimezone: string): HeartbeatScheduleFormValue {
  if (schedule.type === 'interval') {
    return { kind: 'interval', intervalSeconds: schedule.interval_seconds, cronExpr: '', timezone: fallbackTimezone, onceDate: '', onceTime: '' };
  }
  if (schedule.type === 'cron') {
    return { kind: 'cron', intervalSeconds: 1800, cronExpr: schedule.cron_expr, timezone: schedule.timezone, onceDate: '', onceTime: '' };
  }
  const { date, time } = epochSecondsToOnceLocal(schedule.run_at);
  return { kind: 'once', intervalSeconds: 1800, cronExpr: '', timezone: fallbackTimezone, onceDate: date, onceTime: time };
}

export function scheduleFormToDto(form: HeartbeatScheduleFormValue): HeartbeatScheduleDTO {
  if (form.kind === 'interval') {
    const seconds = Math.max(MIN_INTERVAL_SECONDS, Math.floor(form.intervalSeconds) || 0);
    return { type: 'interval', interval_seconds: seconds };
  }
  if (form.kind === 'cron') {
    return { type: 'cron', cron_expr: form.cronExpr.trim(), timezone: form.timezone };
  }
  return { type: 'once', run_at: onceLocalToEpochSeconds(form.onceDate, form.onceTime) };
}

/** 列表/详情里的一行摘要文案，t 传 i18next 的 t 函数 */
export function summarizeHeartbeatSchedule(
  schedule: HeartbeatScheduleDTO,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  if (schedule.type === 'interval') {
    const minutes = Math.round(schedule.interval_seconds / 60);
    return t('heartbeat.schedule.summary.interval', { minutes });
  }
  if (schedule.type === 'cron') {
    return t('heartbeat.schedule.summary.cron', { expr: schedule.cron_expr, timezone: schedule.timezone });
  }
  const { date, time } = epochSecondsToOnceLocal(schedule.run_at);
  return t('heartbeat.schedule.summary.once', { date, time });
}
