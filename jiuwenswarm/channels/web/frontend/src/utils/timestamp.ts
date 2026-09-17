/**
 * 会话事件时间戳 → epoch 毫秒。
 * 历史落盘可能是秒、毫秒或 ISO；解析失败返回 NaN，调用方不要用 Date.now() 填。
 */

const VALID_YEAR_MIN = 2000;
const VALID_YEAR_MAX = 2100;

function yearOfEpochMs(ms: number): number {
  return new Date(ms).getUTCFullYear();
}

function looksLikePlausibleEpochMs(ms: number): boolean {
  if (!Number.isFinite(ms)) {
    return false;
  }
  const year = yearOfEpochMs(ms);
  return year >= VALID_YEAR_MIN && year < VALID_YEAR_MAX;
}

/** 把数值 epoch（秒或毫秒）规范成毫秒；无法判断则 NaN */
function normalizeNumericEpoch(epoch: number): number {
  if (!Number.isFinite(epoch) || epoch <= 0) {
    return Number.NaN;
  }
  if (looksLikePlausibleEpochMs(epoch)) {
    return epoch;
  }
  const asMillis = epoch * 1000;
  if (looksLikePlausibleEpochMs(asMillis)) {
    return asMillis;
  }
  return Number.NaN;
}

export function parseTimestampToMs(value: unknown): number {
  if (typeof value === 'number') {
    return normalizeNumericEpoch(value);
  }
  if (typeof value !== 'string') {
    return Number.NaN;
  }
  const text = value.trim();
  if (!text) {
    return Number.NaN;
  }

  // ISO / RFC 类字符串优先
  const fromDateParse = Date.parse(text);
  if (!Number.isNaN(fromDateParse) && looksLikePlausibleEpochMs(fromDateParse)) {
    return fromDateParse;
  }

  // 纯数字字符串（含小数秒）
  if (/^-?\d+(\.\d+)?$/.test(text)) {
    return normalizeNumericEpoch(Number(text));
  }

  return Number.NaN;
}

export function timestampMsToIso(ms: number): string | undefined {
  if (!looksLikePlausibleEpochMs(ms)) {
    return undefined;
  }
  return new Date(ms).toISOString();
}
