/** 提前唤醒（wake_offset_seconds）表单换算：与 ScheduleEditor / create·update 提交共用 */

import { normalizeDigitsInput, parseDigitInt } from './cronIntegerInput';

/** 允许的最大提前唤醒分钟数（24h）；超出不截断，由 UI 红框+禁止保存 */
export const WAKE_OFFSET_MAX_MINUTES = 24 * 60;

/** 把后端/表单里的秒数归一成非负整数秒（提交兜底；不含上限截断） */
export function normalizeWakeOffsetSeconds(raw: unknown): number {
  const n = Math.floor(Number(raw) || 0);
  if (!Number.isFinite(n)) return 0;
  return Math.max(0, n);
}

/**
 * 秒 → 分钟（向下取整），供输入框展示。
 * 不夹到上限：存量/异常超大秒数原样反映到分钟，便于红框校验。
 */
export function wakeOffsetSecondsToMinutes(seconds: unknown): number {
  return Math.max(0, Math.floor(normalizeWakeOffsetSeconds(seconds) / 60));
}

/** 分钟文本是否满足 0–WAKE_OFFSET_MAX_MINUTES（空串视为 0，合法） */
export function isWakeOffsetMinutesValid(minutesRaw: unknown): boolean {
  if (minutesRaw === '' || minutesRaw == null) return true;
  const raw = String(minutesRaw);
  if (!/^\d+$/.test(raw)) return false;
  const n = parseDigitInt(raw);
  if (n === undefined || !Number.isFinite(n)) return false;
  return n >= 0 && n <= WAKE_OFFSET_MAX_MINUTES;
}

/**
 * 分钟输入 → 秒。空串按 0。
 * 调用方应先用 isWakeOffsetMinutesValid；非法时返回 NaN，避免静默截断。
 */
export function wakeOffsetMinutesToSeconds(minutesRaw: unknown): number {
  if (minutesRaw === '' || minutesRaw == null) return 0;
  if (!isWakeOffsetMinutesValid(minutesRaw)) return Number.NaN;
  const minutes = parseDigitInt(String(minutesRaw));
  if (minutes === undefined || !Number.isFinite(minutes)) return Number.NaN;
  return minutes * 60;
}

/** 分钟输入框：只保留数字，并去掉前导零（不用 Number→String，避免科学计数法） */
export function normalizeWakeOffsetMinutesInput(raw: string): string {
  return normalizeDigitsInput(raw);
}
