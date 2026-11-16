import type { TeamTask } from '../../stores/sessionStore';
import type { WorkflowRun } from './workflowTypes';
import { workflowPhaseForTask } from './workflowTypes';

/** Completed tasks out of all displayed tasks, including unsuccessful ones. */
export function getTaskProgressPercent(tasks: Pick<TeamTask, 'status'>[]): number {
  return tasks.length ? Math.floor(tasks.filter((task) => task.status === 'completed').length * 100 / tasks.length) : 0;
}

export const getCompletedTaskPercent = getTaskProgressPercent;

export function hasRunningTask(tasks: TeamTask[], runs: WorkflowRun[], sessionActive: boolean): boolean {
  return tasks.some((task) => {
    if (task.status !== 'in_progress') return false;
    if (!task.workflow_run_id) return task.task_id.startsWith('application:') || sessionActive;
    const run = runs.find((item) => item.id === task.workflow_run_id);
    const phase = workflowPhaseForTask(task, runs)?.phase;
    return run?.status === 'running' && phase?.status === 'running';
  });
}

/** Run-level counters already include child phases; never sum phase counters here. */
export function getWorkflowAgentCounts(runs: WorkflowRun[]): { ended: number; total: number } | null {
  if (!runs.length || runs.some((run) =>
    !Number.isInteger(run.agent_count) || !Number.isInteger(run.completed_agent_count) ||
    run.agent_count == null || run.completed_agent_count == null ||
    run.agent_count < 0 || run.completed_agent_count < 0 || run.completed_agent_count > run.agent_count
  )) return null;
  return runs.reduce((counts, run) => ({
    total: counts.total + run.agent_count!,
    ended: counts.ended + run.completed_agent_count!,
  }), { total: 0, ended: 0 });
}
