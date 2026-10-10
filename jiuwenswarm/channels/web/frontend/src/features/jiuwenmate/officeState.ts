import type { JiuwenMateDailyActivity, JiuwenMateTask, JiuwenMateTaskStatus } from './types';

export type JiuwenMateOfficeTab = 'members' | 'activity' | 'tasks';
export type JiuwenMateActivityScope = 'all' | 'individual';
export type JiuwenMateActivityRange = 'year' | 'period';
export type JiuwenMateHeatLevel = 0 | 1 | 2 | 3 | 4;

export interface JiuwenMateOfficeState {
  tab: JiuwenMateOfficeTab;
  selectedColleagueId: string | null;
  activityScope: JiuwenMateActivityScope;
  activityRange: JiuwenMateActivityRange;
  year: number;
  periodStart: string;
  periodEnd: string;
  hoveredCellId: string | null;
}

export interface JiuwenMateYearCell {
  id: string;
  date: string;
  inRange: boolean;
  tasks: number;
  hours: number;
  level: JiuwenMateHeatLevel;
}

export interface JiuwenMateYearHeatmap {
  weeks: JiuwenMateYearCell[][];
  months: { month: number; column: number }[];
}

export interface JiuwenMatePeriodRow {
  date: string;
  relative: 'today' | 'yesterday' | null;
  entries: number;
  hours: number;
  cells: { id: string; hour: number; value: number; level: JiuwenMateHeatLevel }[];
}

export interface JiuwenMateMemberStats {
  tokens: number;
  companionDays: number;
  chats: number;
  lastResponseMinutes: number;
}

const DAY_MS = 24 * 60 * 60 * 1000;
const MAX_PERIOD_DAYS = 31;

export function toIsoDate(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${date.getFullYear()}-${month}-${day}`;
}

function parseIsoDate(value: string): Date {
  const [year, month, day] = value.split('-').map(Number);
  return new Date(year, month - 1, day);
}

function addDays(date: Date, days: number): Date {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

function daysBetween(from: Date, to: Date): number {
  return Math.round((to.getTime() - from.getTime()) / DAY_MS);
}

export function createOfficeState(selectedColleagueId: string | null, today: Date = new Date()): JiuwenMateOfficeState {
  return {
    tab: 'members',
    selectedColleagueId,
    activityScope: 'all',
    activityRange: 'year',
    year: today.getFullYear(),
    periodStart: toIsoDate(addDays(today, -6)),
    periodEnd: toIsoDate(today),
    hoveredCellId: null,
  };
}

export function selectOfficeTab(state: JiuwenMateOfficeState, tab: JiuwenMateOfficeTab): JiuwenMateOfficeState {
  return { ...state, tab, hoveredCellId: null };
}

export function selectOfficeMember(state: JiuwenMateOfficeState, selectedColleagueId: string): JiuwenMateOfficeState {
  return { ...state, selectedColleagueId };
}

export function setActivityScope(state: JiuwenMateOfficeState, activityScope: JiuwenMateActivityScope): JiuwenMateOfficeState {
  return { ...state, activityScope, hoveredCellId: null };
}

export function setActivityRange(state: JiuwenMateOfficeState, activityRange: JiuwenMateActivityRange): JiuwenMateOfficeState {
  return { ...state, activityRange, hoveredCellId: null };
}

export function setActivityYear(state: JiuwenMateOfficeState, year: number): JiuwenMateOfficeState {
  return { ...state, year, hoveredCellId: null };
}

export function setActivityPeriod(state: JiuwenMateOfficeState, periodStart: string, periodEnd: string): JiuwenMateOfficeState {
  const start = parseIsoDate(periodStart);
  const end = parseIsoDate(periodEnd);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return state;
  if (start > end) return { ...state, periodStart: periodEnd, periodEnd: periodStart, hoveredCellId: null };
  const clampedStart = daysBetween(start, end) >= MAX_PERIOD_DAYS ? toIsoDate(addDays(end, -(MAX_PERIOD_DAYS - 1))) : periodStart;
  return { ...state, periodStart: clampedStart, periodEnd, hoveredCellId: null };
}

export function setActivityColleague(state: JiuwenMateOfficeState, selectedColleagueId: string): JiuwenMateOfficeState {
  return { ...state, selectedColleagueId };
}

export function hoverHeatmapCell(state: JiuwenMateOfficeState, hoveredCellId: string | null): JiuwenMateOfficeState {
  return { ...state, hoveredCellId };
}

export function dailyLevel(tasks: number): JiuwenMateHeatLevel {
  if (tasks <= 0) return 0;
  if (tasks < 30) return 1;
  if (tasks < 60) return 2;
  if (tasks < 100) return 3;
  return 4;
}

export function hourlyLevel(value: number): JiuwenMateHeatLevel {
  if (value <= 0) return 0;
  if (value < 20) return 1;
  if (value < 40) return 2;
  if (value < 60) return 3;
  return 4;
}

export function yearHeatmap(
  year: number,
  today: Date,
  activities: readonly JiuwenMateDailyActivity[],
): JiuwenMateYearHeatmap {
  const rolling = year === today.getFullYear();
  const rangeStart = rolling ? new Date(today.getFullYear() - 1, today.getMonth() + 1, 1) : new Date(year, 0, 1);
  const rangeEnd = rolling ? today : new Date(year, 11, 31);
  return buildHeatmap(rangeStart, rangeEnd, activities);
}

export function halfYearHeatmap(
  year: number,
  half: 1 | 2,
  activities: readonly JiuwenMateDailyActivity[],
): JiuwenMateYearHeatmap {
  return half === 1
    ? buildHeatmap(new Date(year, 0, 1), new Date(year, 5, 30), activities)
    : buildHeatmap(new Date(year, 6, 1), new Date(year, 11, 31), activities);
}

function buildHeatmap(
  rangeStart: Date,
  rangeEnd: Date,
  activities: readonly JiuwenMateDailyActivity[],
): JiuwenMateYearHeatmap {
  const gridStart = addDays(rangeStart, -rangeStart.getDay());
  const byDate = new Map(activities.map((item) => [item.date, item]));
  const weeks: JiuwenMateYearCell[][] = [];
  const months: { month: number; column: number }[] = [];
  let cursor = gridStart;
  while (cursor <= rangeEnd) {
    const column: JiuwenMateYearCell[] = [];
    for (let day = 0; day < 7; day += 1) {
      const date = toIsoDate(cursor);
      const inRange = cursor >= rangeStart && cursor <= rangeEnd;
      const activity = inRange ? byDate.get(date) : undefined;
      if (inRange && cursor.getDate() === 1) months.push({ month: cursor.getMonth() + 1, column: weeks.length });
      column.push({
        id: date,
        date,
        inRange,
        tasks: activity?.tasks ?? 0,
        hours: activity?.hours ?? 0,
        level: dailyLevel(activity?.tasks ?? 0),
      });
      cursor = addDays(cursor, 1);
    }
    weeks.push(column);
  }
  return { weeks, months };
}

export function periodHeatmap(
  periodStart: string,
  periodEnd: string,
  today: Date,
  hourly: ReadonlyArray<readonly number[]>,
): JiuwenMatePeriodRow[] {
  const start = parseIsoDate(periodStart);
  const end = parseIsoDate(periodEnd);
  const todayDate = parseIsoDate(toIsoDate(today));
  const rows: JiuwenMatePeriodRow[] = [];
  for (let cursor = end; cursor >= start; cursor = addDays(cursor, -1)) {
    const offset = daysBetween(cursor, todayDate);
    const values = offset >= 0 && offset < hourly.length ? hourly[offset] : [];
    const date = toIsoDate(cursor);
    const cells = Array.from({ length: 24 }, (_, hour) => {
      const value = values[hour] ?? 0;
      return { id: `${date}-${hour}`, hour, value, level: hourlyLevel(value) };
    });
    const total = cells.reduce((sum, cell) => sum + cell.value, 0);
    rows.push({
      date,
      relative: offset === 0 ? 'today' : offset === 1 ? 'yesterday' : null,
      entries: cells.filter((cell) => cell.value > 0).length,
      hours: Math.round((total / 60) * 10) / 10,
      cells,
    });
  }
  return rows;
}

export function activitySummary(
  heatmap: JiuwenMateYearHeatmap,
  memberCount: number,
  stats: readonly JiuwenMateMemberStats[],
): { members: number; activeDays: number; tasks: number; tokens: number } {
  const cells = heatmap.weeks.flat().filter((cell) => cell.inRange);
  return {
    members: memberCount,
    activeDays: cells.filter((cell) => cell.tasks > 0).length,
    tasks: cells.reduce((sum, cell) => sum + cell.tasks, 0),
    tokens: stats.slice(0, memberCount).reduce((sum, item) => sum + item.tokens, 0),
  };
}

export function memberStats(index: number, stats: readonly JiuwenMateMemberStats[]): JiuwenMateMemberStats {
  return stats[index % stats.length];
}

export function formatTableTokens(tokens: number): string {
  return `${(tokens / 1000).toFixed(1)}K`;
}

export function formatSummaryTokens(tokens: number): string {
  if (tokens < 1000) return String(tokens);
  return `${Math.round(tokens / 1000)}k`;
}

export function formatSlashDate(isoDate: string): string {
  return isoDate.replace(/-/g, '/');
}

export function formatMonthDay(isoDate: string): string {
  return isoDate.slice(5).replace('-', '/');
}

export const TASK_STATUSES: readonly JiuwenMateTaskStatus[] = ['waiting', 'running', 'done', 'failed'];

export function taskColumns(
  tasks: readonly JiuwenMateTask[],
  query: string,
): { status: JiuwenMateTaskStatus; tasks: JiuwenMateTask[] }[] {
  const keyword = query.trim();
  const visible = keyword ? tasks.filter((task) => task.title.includes(keyword) || task.group.includes(keyword)) : tasks;
  return TASK_STATUSES.map((status) => ({ status, tasks: visible.filter((task) => task.status === status) }));
}
