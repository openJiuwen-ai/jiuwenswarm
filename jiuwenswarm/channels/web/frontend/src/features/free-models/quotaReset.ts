export interface QuotaResetText {
  key: string;
  params: Record<string, string>;
}

type Unit = 's' | 'm' | 'h' | 'd' | 'w' | 'mo';

export function parseResetPeriod(period: string | undefined): { count: number; unit: Unit } | null {
  const match = /^\s*(\d+)\s*(mo|s|m|h|d|w)\s*$/.exec(period ?? '');
  if (!match) return null;
  const count = Number(match[1]);
  return count > 0 ? { count, unit: match[2] as Unit } : null;
}

function parseResetAt(resetAt: string | undefined): Date | null {
  if (!resetAt) return null;
  const date = new Date(resetAt);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatTime(date: Date, locale: string): string {
  return date.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit', hour12: false });
}

export function formatResetDate(date: Date, locale: string): string {
  const day = date.toLocaleDateString(locale, { month: 'short', day: 'numeric', weekday: 'short' });
  return `${day} ${formatTime(date, locale)}`;
}

export function describeQuotaReset(
  period: string | undefined,
  resetAt: string | undefined,
  locale: string,
): QuotaResetText | null {
  const parsed = parseResetPeriod(period);
  const next = parseResetAt(resetAt);
  if (!parsed) {
    return next ? { key: 'auth.huawei.quota.resetNext', params: { date: formatResetDate(next, locale) } } : null;
  }
  const { count, unit } = parsed;
  if (next) {
    const time = formatTime(next, locale);
    if ((unit === 'd' && count === 1) || (unit === 'h' && count === 24)) {
      return { key: 'auth.huawei.quota.resetDaily', params: { time } };
    }
    if ((unit === 'd' && count === 7) || (unit === 'w' && count === 1)) {
      const weekday = next.toLocaleDateString(locale, { weekday: locale.startsWith('zh') ? 'short' : 'long' });
      return { key: 'auth.huawei.quota.resetWeekly', params: { weekday, time } };
    }
    if (unit === 'mo' && count === 1) {
      return {
        key: 'auth.huawei.quota.resetMonthly',
        params: { day: next.toLocaleDateString(locale, { day: 'numeric' }), time },
      };
    }
  }
  const every =
    unit === 'h'
      ? { key: 'auth.huawei.quota.resetEveryHours', count: String(count) }
      : unit === 'w'
        ? { key: 'auth.huawei.quota.resetEveryDays', count: String(count * 7) }
        : unit === 'mo'
          ? { key: 'auth.huawei.quota.resetEveryMonths', count: String(count) }
          : unit === 'd'
            ? { key: 'auth.huawei.quota.resetEveryDays', count: String(count) }
            : null;
  if (!every) {
    return next ? { key: 'auth.huawei.quota.resetNext', params: { date: formatResetDate(next, locale) } } : null;
  }
  return next
    ? { key: every.key, params: { count: every.count, date: formatResetDate(next, locale) } }
    : { key: `${every.key}Plain`, params: { count: every.count } };
}

export function describeQuotaExhausted(resetAt: string | undefined, locale: string): QuotaResetText | null {
  const next = parseResetAt(resetAt);
  return next ? { key: 'auth.huawei.quota.exhaustedUntil', params: { date: formatResetDate(next, locale) } } : null;
}
