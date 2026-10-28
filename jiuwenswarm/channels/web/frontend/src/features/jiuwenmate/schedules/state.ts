import type { Schedule, ScheduleDraft, ScheduleFrequency } from './types';

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

export function buildCron(frequency: 'daily' | 'weekly' | 'monthly', time: string, day: number): string {
  const [hour, minute] = time.split(':').map((part) => Number(part));
  if (frequency === 'daily') return `${minute} ${hour} * * *`;
  if (frequency === 'weekly') return `${minute} ${hour} * * ${day}`;
  return `${minute} ${hour} ${day} * *`;
}

export function parseCron(expr: string): { frequency: ScheduleFrequency; time: string; day: number } {
  const fallback = { frequency: 'custom' as const, time: '09:00', day: 1 };
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return fallback;
  const [minute, hour, day, month, weekday] = parts;
  const clock = `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}`;
  if (/^\d+$/.test(minute) && /^\d+$/.test(hour) && /^\d+$/.test(day) && month === '*' && weekday === '*') {
    return { frequency: 'monthly', time: clock, day: Number(day) };
  }
  if (/^\d+$/.test(minute) && /^\d+$/.test(hour) && day === '*' && month === '*' && weekday === '*') {
    return { frequency: 'daily', time: clock, day: 1 };
  }
  if (/^\d+$/.test(minute) && /^\d+$/.test(hour) && day === '*' && month === '*' && /^\d+$/.test(weekday)) {
    return { frequency: 'weekly', time: clock, day: Number(weekday) };
  }
  return fallback;
}

export function toZonedInput(iso: string, timezone: string): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(new Date(iso));
  const read = (type: Intl.DateTimeFormatPartTypes) => parts.find((part) => part.type === type)?.value ?? '';
  return `${read('year')}-${read('month')}-${read('day')}T${read('hour')}:${read('minute')}`;
}

export function fromZonedInput(value: string, timezone: string): string | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!match) return null;
  const naive = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]), Number(match[4]), Number(match[5]));
  for (let offset = -14; offset <= 14; offset += 1) {
    const candidate = new Date(naive - offset * 3_600_000);
    if (toZonedInput(candidate.toISOString(), timezone) === value) return candidate.toISOString();
  }
  return null;
}

export function validateDraft(draft: ScheduleDraft, executorId: string): string | null {
  if (!executorId) return 'executorRequired';
  if (!draft.name.trim() || !draft.prompt.trim()) return 'required';
  if (draft.scheduleKind === 'interval' && (!draft.intervalSeconds || draft.intervalSeconds < 1)) return 'intervalInvalid';
  if (draft.scheduleKind === 'once' && (!draft.runAt || new Date(draft.runAt).getTime() <= Date.now())) return 'futureRequired';
  if (draft.scheduleKind === 'cron' && !draft.cronExpr.trim()) return 'required';
  if (draft.startsAt && draft.endsAt && new Date(draft.endsAt).getTime() <= new Date(draft.startsAt).getTime()) return 'rangeInvalid';
  return null;
}

export function filterSchedules<T extends { name: string; agentId: string; lastStatus: string }>(
  plans: readonly T[],
  query: string,
  agentId: string,
  status: string,
): T[] {
  const keyword = query.trim().toLowerCase();
  return plans.filter((plan) => {
    if (keyword && !plan.name.toLowerCase().includes(keyword)) return false;
    if (agentId && plan.agentId !== agentId) return false;
    if (status && plan.lastStatus !== status) return false;
    return true;
  });
}

export function clockLabel(time: string): string {
  const [hour, minute] = time.split(':');
  return `${Number(hour)}:${minute}`;
}

export function scheduleSummary(plan: Pick<Schedule, 'scheduleKind' | 'intervalSeconds' | 'runAt' | 'cronExpr' | 'timezone'>): string {
  if (plan.scheduleKind === 'interval' && plan.intervalSeconds) {
    if (plan.intervalSeconds % 3600 === 0) return `每 ${plan.intervalSeconds / 3600} 小时`;
    if (plan.intervalSeconds % 60 === 0) return `每 ${plan.intervalSeconds / 60} 分钟`;
    return `每 ${plan.intervalSeconds} 秒`;
  }
  if (plan.scheduleKind === 'once' && plan.runAt) return toZonedInput(plan.runAt, plan.timezone || 'Asia/Shanghai').replace('T', ' ');
  const parsed = parseCron(plan.cronExpr);
  if (parsed.frequency === 'daily') return `每天 ${clockLabel(parsed.time)}`;
  if (parsed.frequency === 'weekly') return `每${WEEKDAYS[parsed.day] ?? '周'} ${clockLabel(parsed.time)}`;
  if (parsed.frequency === 'monthly') return `每月${parsed.day}日 ${clockLabel(parsed.time)}`;
  return plan.cronExpr;
}

export function weekdayName(day: number): string {
  return WEEKDAYS[day] ?? '';
}
