import assert from 'node:assert/strict';
import test from 'node:test';

import {
  activitySummary,
  createOfficeState,
  formatSummaryTokens,
  formatTableTokens,
  halfYearHeatmap,
  hoverHeatmapCell,
  hourlyActivity,
  joinedColleagueCount,
  periodHeatmap,
  selectOfficeMember,
  selectOfficeTab,
  setActivityPeriod,
  exceedsActivityPeriod,
  adjustedPeriodEdge,
  activityRangeBounds,
  memberRangeTotals,
  setActivityRange,
  setActivityScope,
  setActivityYear,
  taskColumns,
  responseAge,
  periodUsage,
  weeklyUsageChange,
  yearHeatmap,
} from '../node_modules/.cache/jiuwenmate-office/officeState.js';

const today = new Date(2026, 9, 12);
const activities = [
  { date: '2026-01-16', tasks: 112, hours: 3.1 },
  { date: '2026-10-07', tasks: 20, hours: 0.6 },
];

test('office tabs and member selection keep the active tab', () => {
  const initial = createOfficeState('system-architect', today);
  assert.equal(initial.tab, 'members');
  assert.equal(initial.year, 2026);
  assert.equal(initial.periodEnd, '2026-10-12');
  assert.equal(initial.periodStart, '2026-10-06');

  const activity = selectOfficeTab(initial, 'activity');
  assert.equal(selectOfficeMember(activity, 'english-coach').tab, 'activity');
  assert.equal(setActivityScope(activity, 'individual').activityScope, 'individual');
  assert.equal(setActivityRange(activity, 'period').activityRange, 'period');
  assert.equal(hoverHeatmapCell(activity, '2026-01-16').hoveredCellId, '2026-01-16');
});

test('the current year shows a rolling twelve months starting on Sunday', () => {
  const heatmap = yearHeatmap(2026, today, activities);
  const cells = heatmap.weeks.flat().filter((cell) => cell.inRange);
  assert.equal(cells[0].date, '2025-11-01');
  assert.equal(cells.at(-1).date, '2026-10-12');
  assert.ok(heatmap.weeks.every((week) => week.length === 7));
  assert.equal(new Date(heatmap.weeks[0][0].date).getDay(), 0);
  assert.equal(heatmap.months[0].month, 11);

  const hovered = cells.find((cell) => cell.date === '2026-01-16');
  assert.equal(hovered.tasks, 112);
  assert.equal(hovered.level, 4);

  const past = yearHeatmap(2025, today, activities).weeks.flat().filter((cell) => cell.inRange);
  assert.equal(past[0].date, '2025-01-01');
  assert.equal(past.at(-1).date, '2025-12-31');
});

test('half-year heatmap covers the selected half only', () => {
  const second = halfYearHeatmap(2026, 2, []).weeks.flat().filter((cell) => cell.inRange);
  assert.equal(second[0].date, '2026-07-01');
  assert.equal(second.at(-1).date, '2026-12-31');
  assert.ok(second.every((cell) => cell.level === 0));
});

test('period heatmap lists recent days with relative labels and is capped at 30 days', () => {
  const hourly = [Array(24).fill(0).map((_, hour) => (hour === 0 ? 60 : 0)), Array(24).fill(0)];
  const rows = periodHeatmap('2026-10-10', '2026-10-12', today, hourly);
  assert.deepEqual(rows.map((row) => row.relative), ['today', 'yesterday', null]);
  assert.equal(rows[0].entries, 1);
  assert.equal(rows[0].hours, 1);
  assert.equal(rows[0].cells[0].level, 4);

  const clamped = setActivityPeriod(createOfficeState(null, today), '2026-01-01', '2026-10-12');
  assert.equal(clamped.periodStart, '2026-09-13');
  assert.equal(exceedsActivityPeriod('2026-01-01', '2026-10-12'), true);
  assert.equal(exceedsActivityPeriod('2026-09-13', '2026-10-12'), false);

  const reversed = setActivityPeriod(createOfficeState(null, today), '2026-10-12', '2026-01-01');
  assert.deepEqual([reversed.periodStart, reversed.periodEnd], ['2026-09-13', '2026-10-12']);
});

test('an over-long period keeps the date the user just picked and moves the other one', () => {
  const state = createOfficeState(null, new Date(2026, 10, 16));
  const range = (next) => [next.periodStart, next.periodEnd];

  assert.equal(adjustedPeriodEdge('2026-09-01', '2026-11-16', 'end'), 'start');
  assert.deepEqual(range(setActivityPeriod(state, '2026-09-01', '2026-11-16', 'end', '2026-11-16')), ['2026-10-18', '2026-11-16']);

  assert.equal(adjustedPeriodEdge('2026-09-01', '2026-11-16', 'start'), 'end');
  assert.deepEqual(range(setActivityPeriod(state, '2026-09-01', '2026-11-16', 'start', '2026-11-16')), ['2026-09-01', '2026-09-30']);

  assert.equal(adjustedPeriodEdge('2026-11-01', '2026-11-16', 'start'), null);
  assert.deepEqual(range(setActivityPeriod(state, '2026-11-01', '2026-11-16', 'start', '2026-11-16')), ['2026-11-01', '2026-11-16']);

  assert.equal(adjustedPeriodEdge('2026-11-16', '2026-09-01', 'start'), 'start');
  assert.deepEqual(range(setActivityPeriod(state, '2026-11-16', '2026-09-01', 'start', '2026-11-16')), ['2026-10-18', '2026-11-16']);

  assert.deepEqual(range(setActivityPeriod(state, '2026-10-01', '2026-01-01', 'end', '2026-10-20')), ['2026-01-01', '2026-01-30']);
});

test('period heatmap keeps subminute work visible by using hourly task counts', () => {
  const hourly = hourlyActivity([{ date: '2026-10-12', hour: 10, minutes: 0, tasks: 1, tokens: 42 }], '2026-10-12', today);
  const row = periodHeatmap('2026-10-12', '2026-10-12', today, hourly)[0];

  assert.equal(row.entries, 1);
  assert.equal(row.cells[10].level, 1);
});

test('summary and token formatting follow the design', () => {
  const summary = activitySummary(activities, 2, [{ tokens: 108000 }, { tokens: 112000 }, { tokens: 8000 }]);
  assert.deepEqual(summary, { members: 2, activeDays: 2, tasks: 132, tokens: 220000 });
  assert.equal(formatSummaryTokens(336000), '336k');
  assert.equal(formatTableTokens(108000), '108.0K');
});

test('period usage sums the selected window', () => {
  const days = [
    { date: '2026-10-05', tasks: 1, minutes: 0, calls: 1, tokens: 100 },
    { date: '2026-10-12', tasks: 1, minutes: 0, calls: 1, tokens: 50 },
  ];
  assert.equal(periodUsage(days, '7d', today), 50);
  assert.equal(periodUsage(days, 'all', today), 150);
  assert.equal(periodUsage([], '7d', today), 0);
});

test('weekly usage change compares this week so far with the whole previous calendar week', () => {
  const monday = new Date(2026, 10, 16);
  const days = [
    { date: '2026-11-08', tasks: 1, minutes: 0, calls: 1, tokens: 999 },
    { date: '2026-11-12', tasks: 1, minutes: 0, calls: 1, tokens: 120 },
    { date: '2026-11-15', tasks: 1, minutes: 0, calls: 1, tokens: 80 },
    { date: '2026-11-16', tasks: 1, minutes: 0, calls: 1, tokens: 150 },
  ];
  assert.equal(weeklyUsageChange(days, monday), -25);
  assert.equal(weeklyUsageChange(days, new Date(2026, 10, 22)), -25);
  assert.equal(weeklyUsageChange(days, new Date(2026, 10, 23)), -100);
  assert.equal(weeklyUsageChange(days.slice(3), monday), null);
});

test('last response carries minutes into hours and days', () => {
  assert.deepEqual(responseAge(0), { unit: 'minutes', count: 0 });
  assert.deepEqual(responseAge(59), { unit: 'minutes', count: 59 });
  assert.deepEqual(responseAge(60), { unit: 'hours', count: 1 });
  assert.deepEqual(responseAge(119), { unit: 'hours', count: 1 });
  assert.deepEqual(responseAge(23 * 60), { unit: 'hours', count: 23 });
  assert.deepEqual(responseAge(24 * 60), { unit: 'days', count: 1 });
  assert.deepEqual(responseAge(24 * 60 + 90), { unit: 'days', count: 1 });
});

test('task columns follow the four statuses and filter by keyword', () => {
  const tasks = [
    { id: 'a', title: '需求分析', group: '试用群', status: 'waiting' },
    { id: 'b', title: '定价策略分析', group: '试用群', status: 'running' },
  ];
  assert.deepEqual(taskColumns(tasks, '').map((column) => column.status), ['waiting', 'running', 'done', 'failed']);
  assert.deepEqual(taskColumns(tasks, '定价').map((column) => column.tasks.length), [0, 1, 0, 0]);
});

test('overview usage and replies follow the picked year or period instead of all history', () => {
  const today = new Date(2026, 10, 16);
  const state = createOfficeState(null, today);
  const days = [
    { date: '2025-03-02', tasks: 3, replies: 2, minutes: 0, calls: 3, tokens: 100 },
    { date: '2025-12-20', tasks: 1, replies: 1, minutes: 0, calls: 1, tokens: 40 },
    { date: '2026-11-15', tasks: 5, replies: 4, minutes: 0, calls: 5, tokens: 900 },
  ];
  const totals = (next) => {
    const range = activityRangeBounds(next, today);
    return memberRangeTotals(days, range.start, range.end);
  };

  assert.deepEqual(activityRangeBounds(state, today), { start: '2025-12-01', end: '2026-11-16' });
  assert.deepEqual(totals(state), { tokens: 940, replies: 5 });
  assert.deepEqual(totals(setActivityYear(state, 2025)), { tokens: 140, replies: 3 });
  assert.deepEqual(totals(setActivityYear(state, 2024)), { tokens: 0, replies: 0 });

  const period = setActivityPeriod(setActivityRange(state, 'period'), '2026-11-10', '2026-11-16');
  assert.deepEqual(totals(period), { tokens: 900, replies: 4 });
  assert.deepEqual(memberRangeTotals([{ date: '2026-11-15', tasks: 1, minutes: 0, calls: 1, tokens: 5 }], '2026-11-01', '2026-11-30'), { tokens: 5, replies: 0 });
});

test('overview member count only includes colleagues who had joined by the end of the range', () => {
  const joinedAts = [new Date(2025, 5, 1, 10).toISOString(), new Date(2026, 10, 11, 9).toISOString(), '', 'unknown'];
  assert.equal(joinedColleagueCount(joinedAts, '2026-11-16'), 2);
  assert.equal(joinedColleagueCount(joinedAts, '2025-12-31'), 1);
  assert.equal(joinedColleagueCount(joinedAts, '2025-06-01'), 1);
  assert.equal(joinedColleagueCount(joinedAts, '2024-12-31'), 0);
});
