import assert from 'node:assert/strict';
import test from 'node:test';
import { buildSync } from 'esbuild';

const { outputFiles } = buildSync({
  entryPoints: ['src/components/teamArea/taskProgress.ts'],
  bundle: true, platform: 'node', format: 'esm', write: false,
});
const { getTaskProgressPercent, getWorkflowAgentCounts, hasRunningTask } = await import(
  `data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`
);

test('task completion includes unsuccessful tasks in the denominator', () => {
  assert.equal(getTaskProgressPercent([{ status: 'completed' }, { status: 'in_progress' }]), 50);
  assert.equal(getTaskProgressPercent([{ status: 'completed' }, { status: 'cancelled' }]), 50);
  assert.equal(getTaskProgressPercent([{ status: 'in_progress' }]), 0);
  assert.equal(getTaskProgressPercent([...Array(999).fill({ status: 'completed' }), { status: 'pending' }]), 99);
  assert.equal(getTaskProgressPercent([]), 0);
});

test('only running workflow phases animate; ordinary running tasks remain active', () => {
  const task = { task_id: 'run:phase', workflow_run_id: 'run', status: 'in_progress' };
  const run = { id: 'run', status: 'running', phases: [{ id: 'phase', status: 'running' }] };
  assert.equal(hasRunningTask([task], [run], false), true);
  assert.equal(hasRunningTask([task], [{ ...run, status: 'paused' }], true), false);
  assert.equal(hasRunningTask([task], [{ ...run, phases: [{ id: 'phase', status: 'paused' }] }], true), false);
  assert.equal(hasRunningTask([task], [], true), false);
  const ordinary = { task_id: 'ordinary', status: 'in_progress' };
  assert.equal(hasRunningTask([ordinary], [], true), true);
  assert.equal(hasRunningTask([ordinary], [], false), false);
  assert.equal(hasRunningTask([{ task_id: 'application:plugin:1', status: 'in_progress' }], [], false), true);
});

test('agent counts use run aggregates without counting nested phases twice', () => {
  assert.deepEqual(getWorkflowAgentCounts([
    { agent_count: 1, completed_agent_count: 1, phases: [{ agent_count: 1, completed_agent_count: 1 }] },
    { agent_count: 10, completed_agent_count: 0 },
  ]), { ended: 1, total: 11 });
  assert.equal(getWorkflowAgentCounts([{ agent_count: 10 }]), null);
  assert.equal(getWorkflowAgentCounts([{ agent_count: 1, completed_agent_count: 2 }]), null);
  assert.equal(getWorkflowAgentCounts([]), null);
});
