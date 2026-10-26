/**
 * 定时任务面板里「纯数字输入」共用工具：去前导零时不用 Number→String，
 * 避免超长数字变成科学计数法（如 1e+21）。
 */

/** 按间隔：小时下限（含） */
export const INTERVAL_MIN_HOURS = 1;
/** 按间隔：小时上限（含） */
export const INTERVAL_MAX_HOURS = 24;
/** 按间隔：分钟下限（含）。只约束「按间隔 → 分钟」，Cron 表达式栏不走这里。 */
export const INTERVAL_MIN_MINUTES = 5;
/** 按间隔：分钟上限（含） */
export const INTERVAL_MAX_MINUTES = 1440;
/** 面板未读到 cron.job.meta 时，运行中任务个数上限的默认值。 */
export const DEFAULT_MAX_ENABLED_CRON_JOBS = 5;

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

export function intervalMinForUnit(unit: 'hours' | 'minutes'): number {
  return unit === 'minutes' ? INTERVAL_MIN_MINUTES : INTERVAL_MIN_HOURS;
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

/** 间隔步长是否在 [min, max]（分钟下限为 5，小时下限为 1） */
export function isIntervalValueInRange(
  value: number | undefined,
  unit: 'hours' | 'minutes',
): boolean {
  if (value === undefined) return false;
  if (!Number.isFinite(value) || !Number.isInteger(value)) return false;
  return value >= intervalMinForUnit(unit) && value <= intervalMaxForUnit(unit);
}
