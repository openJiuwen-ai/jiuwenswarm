/**
 * 定时任务面板里「纯数字输入」共用工具：去前导零时不用 Number→String，
 * 避免超长数字变成科学计数法（如 1e+21）。
 */

/** 按间隔：小时上限（含） */
export const INTERVAL_MAX_HOURS = 24;
/** 按间隔：分钟上限（含） */
export const INTERVAL_MAX_MINUTES = 1440;

/**
 * 去掉纯数字字符串的前导零：""→""，"0"→"0"，"00"→"0"，"01"→"1"，"010"→"10"。
 * 全程字符串处理，不经过 Number。
 */
export function stripLeadingZeros(digitsOnly: string): string {
  if (digitsOnly === '') return '';
  const stripped = digitsOnly.replace(/^0+/, '');
  return stripped === '' ? '0' : stripped;
}

/** 只保留数字并去掉前导零，供间隔/唤醒等输入框 onChange 使用 */
export function normalizeDigitsInput(raw: string): string {
  return stripLeadingZeros(String(raw ?? '').replace(/\D/g, ''));
}

/**
 * 把已归一的数字文本解析为有限整数。
 * 空串 → undefined；过长或非有限 → POSITIVE_INFINITY（调用方勿用 String(n) 回填输入框）。
 */
export function parseDigitInt(raw: string): number | undefined {
  if (raw === '') return undefined;
  // 超过安全整数位数时不再走 Number（会丢精度/变 Infinity/科学计数法）
  if (raw.length > 15) return Number.POSITIVE_INFINITY;
  const n = Number(raw);
  if (!Number.isFinite(n) || !Number.isInteger(n)) return Number.POSITIVE_INFINITY;
  return n;
}

export function intervalMaxForUnit(unit: 'hours' | 'minutes'): number {
  return unit === 'minutes' ? INTERVAL_MAX_MINUTES : INTERVAL_MAX_HOURS;
}

/**
 * 间隔输入 → schedule 字段值。空串 → undefined。
 * 位数已超过上限十进制长度时返回 max+1（有限、必失败范围校验），避免 Infinity/科学计数法进 schedule。
 */
export function parseIntervalField(raw: string, unit: 'hours' | 'minutes'): number | undefined {
  if (raw === '') return undefined;
  const max = intervalMaxForUnit(unit);
  if (raw.length > String(max).length) return max + 1;
  const n = Number(raw);
  if (!Number.isFinite(n) || !Number.isInteger(n)) return max + 1;
  return n;
}

/** 间隔步长是否在 [1, max]（max 随单位） */
export function isIntervalValueInRange(
  value: number | undefined,
  unit: 'hours' | 'minutes',
): boolean {
  if (value === undefined) return false;
  if (!Number.isFinite(value) || !Number.isInteger(value)) return false;
  const max = intervalMaxForUnit(unit);
  return value >= 1 && value <= max;
}
