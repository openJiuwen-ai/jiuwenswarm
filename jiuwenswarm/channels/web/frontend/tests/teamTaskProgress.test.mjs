import assert from 'node:assert/strict';
import test from 'node:test';
import { buildSync } from 'esbuild';

const { outputFiles } = buildSync({
  entryPoints: ['src/components/teamArea/taskProgress.ts'],
  bundle: true,
  platform: 'node',
  format: 'esm',
  write: false,
});
const { getTaskProgressPercent } = await import(
  `data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`
);

const tasks = (counts) => Object.entries(counts).flatMap(([status, count]) =>
  Array.from({ length: count }, (_, index) => ({ task_id: `${status}-${index}`, status, timestamp: 0 })),
);

test('progress is completed / (total - cancelled)', () => {
  // 截图场景：已完成 45、进行中 1、任务失败 2 → 45 / 46。
  assert.equal(getTaskProgressPercent(tasks({ completed: 45, in_progress: 1, cancelled: 2 })), 98);
  assert.equal(getTaskProgressPercent(tasks({ completed: 1, pending: 1, blocked: 1, planning: 1 })), 25);
  assert.equal(getTaskProgressPercent(tasks({ completed: 3, cancelled: 1 })), 100);
});

test('running tasks contribute nothing until they complete', () => {
  assert.equal(getTaskProgressPercent(tasks({ in_progress: 2, in_review: 1, planning: 1 })), 0);
});

test('empty or all-cancelled boards report 0', () => {
  assert.equal(getTaskProgressPercent([]), 0);
  assert.equal(getTaskProgressPercent(tasks({ cancelled: 2 })), 0);
});

test('progress does not depend on wall-clock time', () => {
  const board = tasks({ completed: 1, in_progress: 1 });
  const before = getTaskProgressPercent(board);
  const realNow = Date.now;
  try {
    Date.now = () => realNow() + 60 * 60 * 1000;
    assert.equal(getTaskProgressPercent(board), before);
  } finally {
    Date.now = realNow;
  }
  assert.equal(before, 50);
});

test('creating a new task keeps earlier completed tasks in the count', () => {
  const board = tasks({ completed: 45, cancelled: 2 });
  assert.equal(getTaskProgressPercent(board), 100);
  board.push({ task_id: 'new-task', status: 'pending', timestamp: 0 });
  assert.equal(getTaskProgressPercent(board), 98);
});
