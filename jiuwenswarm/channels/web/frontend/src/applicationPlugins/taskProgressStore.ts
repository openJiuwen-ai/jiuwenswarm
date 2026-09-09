import { create } from 'zustand';
import type { TeamTask } from '../stores/sessionStore';

export type ApplicationTaskStatus = 'queued' | 'running' | 'completed' | 'failed';
export interface ApplicationTaskProgress {
  id: string;
  pluginId: string;
  title: string;
  status: ApplicationTaskStatus;
  sequence: number;
  detail: string;
  createdAt: number;
  steps?: Array<{ id: string; content: string; status: string }>;
}

const terminal = (status: ApplicationTaskStatus) => status === 'completed' || status === 'failed';
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
        (task.sequence < existing.sequence ||
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
  const priority = { running: 0, queued: 1, failed: 2, completed: 3 };
  return [...tasks]
    .sort((a, b) => priority[a.status] - priority[b.status] || a.createdAt - b.createdAt)
    .map((task) => ({
      task_id: `application:${task.pluginId}:${task.id}`,
      title: `${labels[task.status]} · ${task.title}`,
      content: [
        task.detail,
        ...(task.steps || []).map(
          (step) => `${step.status === 'completed' ? '✓' : step.status === 'in_progress' ? '◉' : '○'} ${step.content}`,
        ),
      ]
        .filter(Boolean)
        .join('\n'),
      status: ({ queued: 'pending', running: 'in_progress', completed: 'completed', failed: 'cancelled' } as const)[
        task.status
      ],
      timestamp: task.createdAt,
    }));
}
