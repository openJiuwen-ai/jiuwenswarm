import type { TeamTask } from '../../stores/sessionStore';
import type { WorkflowRun } from './workflowTypes';
import { workflowPhaseForTask } from './workflowTypes';

/** Completed tasks out of all displayed tasks, including unsuccessful ones. */
export function getTaskProgressPercent(tasks: Pick<TeamTask, 'status'>[]): number {
  return tasks.length ? Math.floor(tasks.filter((task) => task.status === 'completed').length * 100 / tasks.length) : 0;
}

export const getCompletedTaskPercent = getTaskProgressPercent;

export function isRunningTaskStatus(status: TeamTask['status']): boolean {
  return status === 'planning' || status === 'in_progress' || status === 'in_review';
}

export function getTaskEstimateLimit(task: TeamTask): number {
  if (task.workflow_run_id) return 0;
  if (isRunningTaskStatus(task.status)) return 0.9;
  return task.status === 'pending' ? 0.1 : 0;
}

/** A workflow is one work item; its mirrored phase tasks must not be counted again. */
export function getExecutionProgress(
  tasks: TeamTask[],
  runs: WorkflowRun[],
  estimates: Record<string, number> = {},
): { percent: number; estimated: boolean } {
  const runIds = new Set(runs.map((run) => run.id));
  const ordinaryTasks = tasks.filter((task) => !task.workflow_run_id || !runIds.has(task.workflow_run_id));
  let ended = 0;
  let estimated = false;
  for (const task of ordinaryTasks) {
    if (task.status === 'completed') {
      ended += 1;
    } else {
      const fraction = Math.min(getTaskEstimateLimit(task), Math.max(0, estimates[task.task_id] ?? 0));
      ended += fraction;
      estimated ||= fraction > 0;
    }
  }
  for (const run of runs) {
    if (run.status === 'completed') {
      ended += 1;
      continue;
    }
    const counts = getWorkflowAgentCounts([run]);
    const phaseTasks = tasks.filter((task) => task.workflow_run_id === run.id);
    const fraction = counts && counts.total > 0
      ? counts.ended / counts.total
      : getCompletedTaskPercent(phaseTasks) / 100;
    // Ended nodes include failures. Only a completed run may contribute 100%.
    ended += Math.min(0.99, fraction);
  }
  const total = ordinaryTasks.length + runs.length;
  return { percent: total ? ended * 100 / total : 0, estimated };
}

export function hasRunningTask(tasks: TeamTask[], runs: WorkflowRun[], sessionActive: boolean): boolean {
  return tasks.some((task) => {
    if (!isRunningTaskStatus(task.status)) return false;
    if (!task.workflow_run_id) return task.task_id.startsWith('application:') || sessionActive;
    const run = runs.find((item) => item.id === task.workflow_run_id);
    const phase = workflowPhaseForTask(task, runs)?.phase;
    return run?.status === 'running' && phase?.status === 'running';
  });
}

/** Run-level counters already include child phases; never sum phase counters here. */
export function getWorkflowAgentCounts(runs: WorkflowRun[]): { ended: number; total: number } | null {
  if (!runs.length || runs.some((run) =>
    run.agent_count == null || run.completed_agent_count == null ||
    !Number.isInteger(run.agent_count) || !Number.isInteger(run.completed_agent_count) ||
    run.agent_count < 0 || run.completed_agent_count < 0 || run.completed_agent_count > run.agent_count
  )) return null;
  return runs.reduce((counts, run) => ({
    total: counts.total + run.agent_count!,
    ended: counts.ended + run.completed_agent_count!,
  }), { total: 0, ended: 0 });
}
