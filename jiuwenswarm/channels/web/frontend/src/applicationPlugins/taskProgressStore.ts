import { create } from 'zustand';
import type { TeamTask } from '../stores/sessionStore';

export type ApplicationTaskStatus = 'queued' | 'running' | 'cancelling' | 'cancelled' | 'completed' | 'failed';
export type ApplicationTaskAction = 'cancel' | 'next' | 'before' | 'preempt';
type TaskController = (
  task: ApplicationTaskProgress,
  action: ApplicationTaskAction,
  beforeId?: string,
) => Promise<void>;
const controllers = new Map<string, TaskController>();
export function registerApplicationTaskController(pluginId: string, controller: TaskController): () => void {
  controllers.set(pluginId, controller);
  return () => {
    if (controllers.get(pluginId) === controller) controllers.delete(pluginId);
  };
}
export async function controlApplicationTask(
  task: ApplicationTaskProgress,
  action: ApplicationTaskAction,
  beforeId?: string,
): Promise<void> {
  const controller = controllers.get(task.pluginId);
  if (!controller) throw new Error('任务控制尚未就绪');
  await controller(task, action, beforeId);
}
export interface ApplicationTaskProgress {
  id: string;
  pluginId: string;
  title: string;
  status: ApplicationTaskStatus;
  sequence: number;
  detail: string;
  createdAt: number;
  steps?: Array<{ id: string; content: string; status: string }>;
  queuePosition?: number;
  queueVersion?: number;
  searchSessionId?: string;
}

const terminal = (status: ApplicationTaskStatus) => ['completed', 'failed', 'cancelled'].includes(status);
export const EMPTY_APPLICATION_TASKS: ApplicationTaskProgress[] = [];

/** Plugin tasks are display state, never items in the composer's send queue. */
export const useApplicationTaskStore = create<{
  sessions: Record<string, ApplicationTaskProgress[]>;
  upsert: (sessionId: string, task: ApplicationTaskProgress) => void;
}>((set) => ({
  sessions: {},
  upsert: (sessionId, task) =>
    set((state) => {
      if (!sessionId || sessionId === 'new' || !task.id) return state;
      const tasks = state.sessions[sessionId] || EMPTY_APPLICATION_TASKS;
      const existing = tasks.find((item) => item.id === task.id && item.pluginId === task.pluginId);
      if (
        existing &&
        ((existing.status === 'cancelled' && task.status !== 'cancelled') ||
          task.sequence < existing.sequence ||
          (terminal(existing.status) && !terminal(task.status)) ||
          (existing.status === 'running' && task.status === 'queued'))
      ) {
        return state;
      }
      const updated = {
        ...existing,
        ...task,
        createdAt: existing?.createdAt ?? task.createdAt,
        steps: task.steps ?? existing?.steps,
      };
      const next = existing ? tasks.map((item) => (item === existing ? updated : item)) : [...tasks, updated];
      return { sessions: { ...state.sessions, [sessionId]: next } };
    }),
}));

export function applicationTasksToTeamTasks(
  tasks: ApplicationTaskProgress[],
  labels: Record<ApplicationTaskStatus, string>,
): TeamTask[] {
  const priority = { running: 0, cancelling: 0, queued: 1, failed: 2, cancelled: 2, completed: 3 };
  return [...tasks]
    .sort(
      (a, b) =>
        priority[a.status] - priority[b.status] ||
        (a.queuePosition ?? 0) - (b.queuePosition ?? 0) ||
        a.createdAt - b.createdAt,
    )
    .map((task) => ({
      task_id: `application:${task.pluginId}:${task.id}`,
      title: `${labels[task.status]}${task.status === 'queued' && task.queuePosition ? ` ${task.queuePosition}` : ''} · ${task.title}`,
      content: [
        task.detail,
        ...(task.steps || []).map(
          (step) => `${step.status === 'completed' ? '✓' : step.status === 'in_progress' ? '◉' : '○'} ${step.content}`,
        ),
      ]
        .filter(Boolean)
        .join('\n'),
      status: (
        {
          queued: 'pending',
          running: 'in_progress',
          cancelling: 'in_progress',
          cancelled: 'cancelled',
          completed: 'completed',
          failed: 'cancelled',
        } as const
      )[task.status],
      timestamp: task.createdAt,
    }));
}
