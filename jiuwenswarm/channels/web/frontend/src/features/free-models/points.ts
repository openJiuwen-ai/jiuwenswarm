export interface PointsQuota {
  total: number;
  balance: number;
  used: number;
  exhausted: boolean;
}

export function formatPoints(value: number, locale: string): string {
  if (!Number.isFinite(value)) return '—';
  const floored = Math.floor(value * 100) / 100;
  return floored.toLocaleString(locale, { maximumFractionDigits: 2 });
}

export function usedRatio(quota: PointsQuota): number | null {
  if (!(quota.total > 0) || !(quota.used >= 0)) return null;
  if (quota.exhausted) return 1;
  return Math.min(1, Math.max(0, quota.used / quota.total));
}
