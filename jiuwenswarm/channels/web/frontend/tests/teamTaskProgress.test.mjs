import assert from 'node:assert/strict';
import test from 'node:test';
import { buildSync } from 'esbuild';

const { outputFiles } = buildSync({
  entryPoints: ['src/components/teamArea/taskProgress.ts'],
  bundle: true, platform: 'node', format: 'esm', write: false,
});
const { getTaskProgressPercent, getExecutionProgress, getWorkflowAgentCounts, hasRunningTask } = await import(
  `data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`
);

test('task completion includes unsuccessful tasks in the denominator', () => {
  assert.equal(getTaskProgressPercent([{ status: 'completed' }, { status: 'in_progress' }]), 50);
  assert.equal(getTaskProgressPercent([{ status: 'completed' }, { status: 'cancelled' }]), 50);
  assert.equal(getTaskProgressPercent([{ status: 'in_progress' }]), 0);
  assert.equal(getTaskProgressPercent([...Array(999).fill({ status: 'completed' }), { status: 'pending' }]), 99);
  assert.equal(getTaskProgressPercent([]), 0);
});

test('execution progress uses workflow counters without counting mirrored phases twice', () => {
  const phase = { task_id: 'run:phase', workflow_run_id: 'run', status: 'in_progress' };
  const run = { id: 'run', status: 'running', agent_count: 17, completed_agent_count: 16 };
  const progress = getExecutionProgress([phase], [run]);
  assert.ok(Math.abs(progress.percent - 1600 / 17) < 0.001);
  assert.equal(progress.estimated, false);
  assert.equal(getExecutionProgress([phase, { ...phase, task_id: 'run:child' }], [run]).percent, progress.percent);
  const mixed = getExecutionProgress([
    phase, { task_id: 'ordinary', status: 'completed' },
  ], [run]);
  assert.ok(Math.abs(mixed.percent - (1 + 16 / 17) * 50) < 0.001);
  assert.equal(getExecutionProgress([], [{ ...run, completed_agent_count: 17 }]).percent, 99);
  assert.equal(getExecutionProgress([], [{ ...run, status: 'failed', completed_agent_count: 17 }]).percent, 99);
  assert.equal(getExecutionProgress([], [{ ...run, status: 'completed' }]).percent, 100);
});

test('ordinary estimates are capped, ignore terminal tasks and never change completion counts', () => {
  const running = { task_id: 'ordinary', status: 'in_progress' };
  assert.deepEqual(getExecutionProgress([running], [], { ordinary: 5 }), { percent: 90, estimated: true });
  assert.equal(getTaskProgressPercent([running]), 0);
  assert.deepEqual(getExecutionProgress([{ ...running, status: 'pending' }], [], { ordinary: 0.8 }), {
    percent: 10, estimated: true,
  });
  for (const status of ['cancelled']) {
    assert.deepEqual(getExecutionProgress([{ ...running, status }], [], { ordinary: 0.8 }), { percent: 0, estimated: false });
  }
  assert.deepEqual(getExecutionProgress([{ ...running, status: 'completed' }], [], { ordinary: 0.8 }), {
    percent: 100, estimated: false,
  });
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
  for (const status of ['planning', 'in_review']) {
    const gated = { ...ordinary, status };
    assert.equal(hasRunningTask([gated], [], true), true);
    assert.equal(hasRunningTask([gated], [], false), false);
    assert.deepEqual(getExecutionProgress([gated], [], { ordinary: 0.3 }), { percent: 30, estimated: true });
  }
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

test('missing or invalid run counts never produce a partial aggregate', () => {
  const valid = { agent_count: 2, completed_agent_count: 1 };
  for (const invalid of [
    {},
    { agent_count: 2 },
    { agent_count: null, completed_agent_count: 0 },
    { agent_count: -1, completed_agent_count: 0 },
    { agent_count: 2, completed_agent_count: -1 },
    { agent_count: 1.5, completed_agent_count: 1 },
    { agent_count: 2, completed_agent_count: NaN },
    { agent_count: Infinity, completed_agent_count: 0 },
    { agent_count: 2, completed_agent_count: 3 },
  ]) {
    assert.equal(getWorkflowAgentCounts([valid, invalid]), null);
  }
  assert.deepEqual(getWorkflowAgentCounts([{ agent_count: 0, completed_agent_count: 0 }]), { ended: 0, total: 0 });
});
