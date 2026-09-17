/** 提前唤醒（wake_offset_seconds）表单换算：与 ScheduleEditor / create·update 提交共用 */

/** 允许的最大提前唤醒分钟数（24h）；超出按上限截断 */
export const WAKE_OFFSET_MAX_MINUTES = 24 * 60;

/** 把后端/表单里的秒数归一成非负整数秒 */
export function normalizeWakeOffsetSeconds(raw: unknown): number {
  const n = Math.floor(Number(raw) || 0);
  if (!Number.isFinite(n)) return 0;
  return Math.max(0, n);
}

/** 秒 → 分钟（向下取整），供输入框展示；超过上限截断 */
export function wakeOffsetSecondsToMinutes(seconds: unknown): number {
  return Math.min(
    WAKE_OFFSET_MAX_MINUTES,
    Math.max(0, Math.floor(normalizeWakeOffsetSeconds(seconds) / 60)),
  );
}

/**
 * 分钟输入 → 秒。空串/非法值按 0；超出上限截断。
 * 用于 ScheduleEditor 回写 form.wakeOffsetSeconds，以及提交前兜底。
 */
export function wakeOffsetMinutesToSeconds(minutesRaw: unknown): number {
  if (minutesRaw === '' || minutesRaw == null) return 0;
  const minutes = Math.floor(Number(minutesRaw) || 0);
  if (!Number.isFinite(minutes)) return 0;
  const clamped = Math.min(WAKE_OFFSET_MAX_MINUTES, Math.max(0, minutes));
  return clamped * 60;
}

/** 分钟输入框：只保留数字，并去掉前导零（"" / "0" / "01"→"1"） */
export function normalizeWakeOffsetMinutesInput(raw: string): string {
  const digitsOnly = String(raw ?? '').replace(/\D/g, '');
  return digitsOnly === '' ? '' : String(Number(digitsOnly));
}
