import { Chevron, StatusIcon, type TaskStatus } from './shared';

export interface MemberTaskListItem {
  id: string;
  title: string;
  detail?: string;
  status: TaskStatus | string;
  raw?: Record<string, unknown>;
  statusHistory?: Array<{ status: string; atMs?: number; source?: string }>;
}

function asTaskStatus(status: string): TaskStatus {
  if (
    status === 'pending' ||
    status === 'in_progress' ||
    status === 'completed' ||
    status === 'cancelled' ||
    status === 'error'
  ) {
    return status;
  }
  return 'pending';
}

export function MemberTaskListBar({
  tasks,
  expanded,
  onToggle,
}: {
  tasks: MemberTaskListItem[];
  expanded: boolean;
  onToggle: () => void;
}) {
  const completed = tasks.filter((task) => task.status === 'completed').length;
  return (
    <button
      type="button"
      onClick={onToggle}
      className="flex w-full items-center gap-2 border-t border-border px-4 py-2 text-left text-xs text-text"
      data-testid="member-task-list-bar"
    >
      <Chevron expanded={expanded} />
      <span>Tasks {completed}/{tasks.length}</span>
    </button>
  );
}

export function MemberTaskListPanel({
  tasks,
}: {
  tasks: MemberTaskListItem[];
}) {
  return (
    <ul className="flex flex-col gap-2 border-t border-border px-4 py-3" data-testid="member-task-list-panel">
      {tasks.map((task) => (
        <li key={task.id} className="flex items-start gap-2">
          <StatusIcon status={asTaskStatus(String(task.status))} />
          <div className="min-w-0">
            <div className="truncate text-xs text-text">{task.title}</div>
            {task.detail ? (
              <div className="truncate text-[11px] text-text-muted">{task.detail}</div>
            ) : null}
          </div>
        </li>
      ))}
    </ul>
  );
}
