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
  periodHeatmap,
  selectOfficeMember,
  selectOfficeTab,
  setActivityPeriod,
  setActivityRange,
  setActivityScope,
  taskColumns,
  responseAge,
  usageChange,
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

test('period heatmap lists recent days with relative labels and is capped at 31 days', () => {
  const hourly = [Array(24).fill(0).map((_, hour) => (hour === 0 ? 60 : 0)), Array(24).fill(0)];
  const rows = periodHeatmap('2026-10-10', '2026-10-12', today, hourly);
  assert.deepEqual(rows.map((row) => row.relative), ['today', 'yesterday', null]);
  assert.equal(rows[0].entries, 1);
  assert.equal(rows[0].hours, 1);
  assert.equal(rows[0].cells[0].level, 4);

  const clamped = setActivityPeriod(createOfficeState(null, today), '2026-01-01', '2026-10-12');
  assert.equal(clamped.periodStart, '2026-09-12');
});

test('period heatmap keeps subminute work visible by using hourly task counts', () => {
  const hourly = hourlyActivity([{ date: '2026-10-12', hour: 10, minutes: 0, tasks: 1, tokens: 42 }], '2026-10-12', today);
  const row = periodHeatmap('2026-10-12', '2026-10-12', today, hourly)[0];

  assert.equal(row.entries, 1);
  assert.equal(row.cells[10].level, 1);
});

test('summary and token formatting follow the design', () => {
  const heatmap = yearHeatmap(2026, today, activities);
  const summary = activitySummary(heatmap, 2, [{ tokens: 108000 }, { tokens: 112000 }, { tokens: 8000 }]);
  assert.deepEqual(summary, { members: 2, activeDays: 2, tasks: 132, tokens: 220000 });
  assert.equal(formatSummaryTokens(336000), '336k');
  assert.equal(formatTableTokens(108000), '108.0K');
});

test('usage change compares the selected window with the previous one', () => {
  const days = [
    { date: '2026-10-05', tasks: 1, minutes: 0, calls: 1, tokens: 100 },
    { date: '2026-10-12', tasks: 1, minutes: 0, calls: 1, tokens: 50 },
  ];
  assert.deepEqual(usageChange(days, '7d', today), { tokens: 50, percent: -50 });
  assert.deepEqual(usageChange(days, 'all', today), { tokens: 150, percent: null });
  assert.equal(usageChange([], '7d', today).percent, null);
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
