import { Check, ChevronRight, Circle } from 'lucide-react';
import i18n from '../../i18n';
import { ParsedTeamEvent, parseTeamEventMessage } from '../ChatPanel/teamEventUtils';
import type { Message, TodoItem } from '../../types';
import type { ReactNode } from 'react';
import type {
  TeamTask as SessionTeamTask,
  TeamMemberExecutionEvent,
  TeamTaskStatus,
} from '../../stores/sessionStore';

type Translate = (key: string, options?: Record<string, unknown>) => string;

export interface TeamMember {
  id: string;
  member_id: string;
  status: string;
  timestamp: number;
  name?: string;
  execution_status?: string | null;
  mode?: string;
}

export interface TeamTaskEvent {
  id: string;
  type: string;
  team_id: string;
  task_id: string;
  status: string;
  timestamp: number;
  member_id?: string;
  assignee?: string;
  team_name?: string;
  title?: string;
  content?: string;
  // Truncation observability flags — kept aligned with TeamTaskEvent in
  // stores/sessionStore.ts and components/TeamTaskEvents.tsx so the event
  // panel can surface them if needed (currently passthrough/observability only).
  title_truncated?: boolean;
  title_original_size?: number;
  content_truncated?: boolean;
  content_original_size?: number;
  updated_at?: number | string | null;
}

export interface MemberTask {
  id: string;
  title: string;
  detail: string;
  status: TaskStatus;
  assignee?: string;
  updatedAt?: number | string | null;
  source: 'snapshot' | 'todo' | 'event';
  raw?: Record<string, unknown>;
}

export interface ProcessItem {
  id: string;
  type: 'execution' | 'message' | 'task';
  timestamp: number;
  title: string;
  subtitle?: string;
  status: TaskStatus | 'execution' | 'message';
  kind?: TeamMemberExecutionEvent['kind'];
  event?: ParsedTeamEvent;
  execution?: TeamMemberExecutionEvent;
  linkedResult?: TeamMemberExecutionEvent;
  raw?: TeamTaskEvent;
}

interface BaseTeamAreaProps {
  members: TeamMember[];
  historyMessages?: Message[];
  reviewPanel?: ReactNode;
}

export type TeamAreaProps = BaseTeamAreaProps & (
  | {
    expanded?: false;
    onExpand?: (tab: TabType, memberId?: string) => void;
  }
  | {
    expanded: true;
    activeTab: TabType;
    activeDetailTab: TeamDetailTab;
    selectedMemberId?: string;
    selectedArtifactId?: string;
    onTabChange: (tab: TabType) => void;
    onDetailTabChange: (tab: TeamDetailTab) => void;
    onMemberSelect?: (memberId: string) => void;
    onArtifactSelect?: (artifactId: string) => void;
    onCollapse?: () => void;
  }
);

export type TabType = 'planning' | 'team' | 'artifacts' | 'review';
export type TeamDetailTab = 'members' | 'group';
export type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'cancelled' | 'error';
export type TaskColumnKey = 'waiting' | 'running' | 'completed' | 'cancelled';

const GENERIC_TASK_TITLES = new Set(['task', '任务', '任务 task']);

export const BOARD_COLUMNS: Array<{
  key: TaskColumnKey;
  labelKey: string;
  pillClassName: string;
  dotClassName: string;
}> = [
  {
    key: 'waiting',
    labelKey: 'team.planning.columns.waiting',
    pillClassName: 'bg-card text-[var(--color-team-status-waiting)]',
    dotClassName: 'bg-[var(--color-team-status-waiting)]',
  },
  {
    key: 'running',
    labelKey: 'team.planning.columns.running',
    pillClassName: 'bg-[var(--color-team-status-running-surface)] text-[var(--color-team-status-running)]',
    dotClassName: 'bg-[var(--color-team-status-running)]',
  },
  {
    key: 'completed',
    labelKey: 'team.planning.columns.completed',
    pillClassName: 'bg-[var(--color-team-status-completed-surface)] text-[var(--color-team-status-completed)]',
    dotClassName: 'bg-[var(--color-team-status-completed)]',
  },
  {
    key: 'cancelled',
    labelKey: 'team.planning.columns.cancelled',
    pillClassName: 'bg-[var(--color-team-status-cancelled-surface)] text-[var(--color-team-status-cancelled)]',
    dotClassName: 'bg-[var(--color-team-status-cancelled)]',
  },
];

const TASK_STATUS_TO_COLUMN: Record<TeamTaskStatus, TaskColumnKey> = {
  pending: 'waiting',
  blocked: 'waiting',
  // Both optional gates (planning / in_review) and the execution state fold
  // into the single "running" column per product decision.
  planning: 'running',
  in_progress: 'running',
  in_review: 'running',
  completed: 'completed',
  cancelled: 'cancelled',
};

export const getMemberDisplayName = (member: TeamMember | string): string => {
  if (typeof member === 'string') {
    return member;
  }
  return member.name?.trim() || member.member_id;
};

export const normalizeTaskStatus = (status?: string, type?: string): TaskStatus => {
  const raw = `${status || ''} ${type || ''}`.toLowerCase();
  if (raw.includes('completed') || raw.includes('done') || raw.includes('success') || raw.includes('verified')) return 'completed';
  // Running family: the execution state plus the planning / in_review gates.
  // Matches on either the status value or the event type substring (e.g. the
  // "team.task.claimed" / "team.task.started" event types).
  if (
    raw.includes('claimed') ||
    raw.includes('progress') ||
    raw.includes('running') ||
    raw.includes('busy') ||
    raw.includes('planning') ||
    raw.includes('review') ||
    raw.includes('started')
  ) {
    return 'in_progress';
  }
  if (raw.includes('cancel')) return 'cancelled';
  if (raw.includes('error') || raw.includes('fail')) return 'error';
  return 'pending';
};

const normalizeTeamTaskStatus = (status?: string): TeamTaskStatus => {
  if (
    status === 'blocked' ||
    status === 'planning' ||
    status === 'in_progress' ||
    status === 'in_review' ||
    status === 'completed' ||
    status === 'cancelled'
  ) {
    return status;
  }
  return 'pending';
};

export const getTaskColumnKey = (task: SessionTeamTask): TaskColumnKey => {
  return TASK_STATUS_TO_COLUMN[normalizeTeamTaskStatus(task.status)];
};

export const getBoardTaskTitle = (task: SessionTeamTask): string => {
  return task.title?.trim() || task.task_id;
};

export const getBoardTaskContent = (task: SessionTeamTask): string => {
  const content = task.content?.trim();
  if (!content) return '';
  return content === getBoardTaskTitle(task) ? '' : content;
};

const normalizeMemberRuntimeState = (member: TeamMember): string => {
  return `${member.status || ''}`.toLowerCase();
};

export const getMemberStatusLabel = (member: TeamMember): string => {
  const key = getMemberStatusKey(member);
  if (key === 'unknown') return member.status || i18n.t('team.memberStatus.unknown');
  return i18n.t(`team.memberStatus.${key}`);
};

export const getMemberStatusKey = (member: TeamMember): string => {
  const status = normalizeMemberRuntimeState(member);
  if (status.includes('execut') || status.includes('running') || status.includes('busy') || status.includes('working')) return 'running';
  if (status.includes('ready') || status.includes('idle')) return 'idle';
  if (status.includes('restart')) return 'restarting';
  if (status.includes('shutdown') || status.includes('down')) return 'shutdown';
  if (status.includes('error') || status.includes('fail')) return 'error';
  if (status.includes('unstarted')) return 'unstarted';
  return 'unknown';
};

export const getMemberStatusDotClass = (member: TeamMember): string => {
  const key = getMemberStatusKey(member);
  if (key === 'running') return 'bg-blue-500';
  if (key === 'idle') return 'bg-emerald-500';
  if (key === 'error') return 'bg-red-500';
  if (key === 'restarting') return 'bg-amber-500';
  if (key === 'shutdown') return 'bg-gray-400';
  return 'bg-slate-300';
};

export const getTaskStatusLabel = (status: TaskStatus): string => {
  return i18n.t(`team.taskStatus.${status}`);
};

const getTaskStatusIconClass = (status: TaskStatus): string => {
  switch (status) {
    case 'completed':
      return 'bg-emerald-500 text-text-inverse';
    case 'in_progress':
      return 'bg-blue-500 text-text-inverse';
    case 'cancelled':
      return 'bg-slate-300 text-text-inverse';
    case 'error':
      return 'bg-red-500 text-text-inverse';
    case 'pending':
    default:
      return 'bg-card text-slate-400 ring-1 ring-slate-300';
  }
};

const getTaskEventTitle = (event: TeamTaskEvent): string => {
  const type = event.type.toLowerCase();
  if (type.includes('completed')) return i18n.t('team.taskEvents.completed');
  if (type.includes('claimed')) return i18n.t('team.taskEvents.claimed');
  if (type.includes('created')) return i18n.t('team.taskEvents.created');
  if (type.includes('cancelled')) return i18n.t('team.taskEvents.cancelled');
  if (type.includes('unblocked')) return i18n.t('team.taskEvents.unblocked');
  return i18n.t('team.taskEvents.updated');
};

export const formatTime = (timestamp: number): string => {
  if (!Number.isFinite(timestamp) || timestamp <= 0) return '';
  return new Date(timestamp).toLocaleTimeString(i18n.language.startsWith('zh') ? 'zh-CN' : 'en-US', {
    hour: '2-digit',
    minute: '2-digit',
  });
};

const truncate = (text: string, max = 72): string => {
  const normalized = text.replace(/\s+/g, ' ').trim();
  if (normalized.length <= max) return normalized;
  return `${normalized.slice(0, max)}...`;
};

const isGenericTaskTitle = (title: string): boolean => {
  return GENERIC_TASK_TITLES.has(title.trim().toLowerCase());
};

const buildTaskDetail = ({
  title,
  content,
  fallback,
}: {
  title: string;
  content?: string;
  fallback?: string;
}): string => {
  const normalizedContent = (content || '').trim();
  if (normalizedContent && normalizedContent !== title) return normalizedContent;
  if (fallback) return i18n.t('team.taskDetail.sourcePrompt', { prompt: truncate(fallback, 96) });
  return i18n.t('team.taskDetail.noDetail');
};

export function StatusIcon({ status }: { status: TaskStatus }) {
  const completed = status === 'completed';
  const inProgress = status === 'in_progress';

  return (
    <span className={`inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full ${getTaskStatusIconClass(status)}`}>
      {completed ? (
        <Check size={10} strokeWidth={2.5} />
      ) : inProgress ? (
        <Circle size={6} strokeWidth={2} />
      ) : (
        <Circle size={8} strokeWidth={1.5} />
      )}
    </span>
  );
}

export function Chevron({ expanded }: { expanded?: boolean }) {
  return (
    <ChevronRight
      size={16}
      className={` ${expanded ? 'rotate-90' : ''}`}
    />
  );
}

export function buildTaskMap(
  memberId: string,
  todos: TodoItem[],
  teamTaskEvents: TeamTaskEvent[],
  fallbackPrompt: string,
  teamTasks: SessionTeamTask[] = [],
): MemberTask[] {
  const taskMap = new Map<string, MemberTask>();

  todos.forEach((todo) => {
    if (todo.claimedBy !== memberId) return;
    const rawTitle = todo.content || todo.activeForm || i18n.t('team.taskFallback', { id: todo.id.slice(-4) });
    const fallback = isGenericTaskTitle(rawTitle) ? fallbackPrompt : undefined;
    taskMap.set(todo.id, {
      id: todo.id,
      title: rawTitle,
      detail: buildTaskDetail({ title: rawTitle, content: todo.activeForm, fallback }),
      status: normalizeTaskStatus(todo.status),
      assignee: todo.claimedBy,
      updatedAt: todo.updatedAt,
      source: 'todo',
      raw: todo as unknown as Record<string, unknown>,
    });
  });

  teamTasks.forEach((task) => {
    if (!task.task_id) return;
    if (task.assignee !== memberId) return;
    const rawTitle = task.title || task.task_id;
    const fallback = isGenericTaskTitle(rawTitle) ? fallbackPrompt : undefined;
    taskMap.set(task.task_id, {
      id: task.task_id,
      title: rawTitle,
      detail: buildTaskDetail({ title: rawTitle, content: task.content, fallback }),
      status: normalizeTaskStatus(task.status),
      assignee: task.assignee,
      updatedAt: task.timestamp,
      source: 'snapshot',
      raw: task as unknown as Record<string, unknown>,
    });
  });

  teamTaskEvents.forEach((event) => {
    if (!event.task_id) return;
    const owner = event.assignee || event.member_id;
    const existing = taskMap.get(event.task_id);
    if (owner !== memberId && !existing) return;
    const rawTitle = event.title || existing?.title || event.task_id;
    const fallback = isGenericTaskTitle(rawTitle) ? fallbackPrompt : undefined;
    taskMap.set(event.task_id, {
      id: event.task_id,
      title: rawTitle,
      detail: buildTaskDetail({ title: rawTitle, content: event.content || existing?.detail, fallback }),
      status: normalizeTaskStatus(event.status, event.type),
      assignee: owner || existing?.assignee,
      updatedAt: event.updated_at ?? event.timestamp ?? existing?.updatedAt,
      source: event.type === 'team.task.snapshot' ? 'snapshot' : 'event',
      raw: event as unknown as Record<string, unknown>,
    });
  });

  return Array.from(taskMap.values()).sort((a, b) => {
    const statusOrder: Record<TaskStatus, number> = {
      in_progress: 0,
      pending: 1,
      error: 2,
      cancelled: 3,
      completed: 4,
    };
    return statusOrder[a.status] - statusOrder[b.status] || a.title.localeCompare(b.title);
  });
}

export function buildProcessItems(
  memberId: string,
  memberTasks: MemberTask[],
  teamTaskEvents: TeamTaskEvent[],
  messages: Message[],
  executionEvents: TeamMemberExecutionEvent[] = [],
  t: Translate = i18n.t.bind(i18n),
): ProcessItem[] {
  const memberTaskIds = new Set(memberTasks.map((task) => task.id));
  const taskItems = teamTaskEvents
    .filter((event) => event.type !== 'team.task.snapshot')
    .filter((event) => {
      const owner = event.assignee || event.member_id;
      return owner === memberId || (!!event.task_id && memberTaskIds.has(event.task_id));
    })
    .map((event): ProcessItem => {
      const relatedTask = memberTasks.find((task) => task.id === event.task_id);
      const timestamp = typeof event.timestamp === 'number' ? event.timestamp : Date.now();
      return {
        id: `task-${event.id}-${event.task_id}`,
        type: 'task',
        timestamp,
        title: getTaskEventTitle(event),
        subtitle: relatedTask?.title || event.title || event.content || event.task_id,
        status: normalizeTaskStatus(event.status, event.type),
        raw: event,
      };
    });

  const messageItems = messages
    .map((message): ProcessItem | null => {
      const event = parseTeamEventMessage(message);
      if (!event) return null;
      if (event.fromMember !== memberId && event.toMember !== memberId) return null;
      const timestamp = event.timestamp || Date.parse(message.timestamp) || Date.now();
      const target = event.isBroadcast
        ? t('team.process.broadcastMessage')
        : event.fromMember === memberId
          ? t('team.process.sentMessage')
          : t('team.process.receivedMessage');

      return {
        id: `message-${message.id}`,
        type: 'message',
        timestamp,
        title: target,
        subtitle: truncate(event.content || t('team.process.memberMessage')),
        status: 'message',
        event,
      };
    })
    .filter((item): item is ProcessItem => item !== null);

  // 将 tool_call 和 tool_result 配对合并
  const pairedExecutionItems: ProcessItem[] = [];
  const toolResultsByCallId = new Map<string, TeamMemberExecutionEvent>();

  // 先收集所有 tool_result，按 tool_call_id 分组
  executionEvents
    .filter((e) => e.kind === 'tool_result' && e.member_id === memberId)
    .forEach((e) => {
      if (e.tool_call_id) {
        toolResultsByCallId.set(e.tool_call_id, e);
      }
    });

  // 处理所有 execution 事件
  executionEvents
    .filter((event) => event.member_id === memberId && event.kind !== 'final')
    .forEach((event) => {
      // 如果是 tool_call，尝试关联其 tool_result
      if (event.kind === 'tool_call' && event.tool_call_id) {
        const linkedResult = toolResultsByCallId.get(event.tool_call_id);
        pairedExecutionItems.push({
          id: `execution-${event.id}`,
          type: 'execution',
          timestamp: event.timestamp,
          title: getExecutionEventTitle(event, t),
          subtitle: truncate(event.content || ''),
          status: 'execution',
          kind: event.kind,
          execution: event,
          linkedResult: linkedResult || undefined,
        });
        return;
      }

      // 其他类型（file 等）单独添加
      if (event.kind !== 'tool_result') {
        pairedExecutionItems.push({
          id: `execution-${event.id}`,
          type: 'execution',
          timestamp: event.timestamp,
          title: getExecutionEventTitle(event, t),
          subtitle: truncate(event.content || event.files?.map((file) => file.name).join(', ') || ''),
          status: 'execution',
          kind: event.kind,
          execution: event,
        });
      }
    });

  return [...taskItems, ...messageItems, ...pairedExecutionItems]
    .sort((a, b) => a.timestamp - b.timestamp)
    .slice(0, 80);
}

function getExecutionEventTitle(event: TeamMemberExecutionEvent, t: Translate): string {
  if (event.kind === 'tool_call' && event.tool_name) {
    return t('team.process.execution.toolCallTitle', { tool: event.tool_name });
  }
  if (event.kind === 'tool_result' && event.tool_name) {
    return t('team.process.execution.toolResultTitle', { tool: event.tool_name });
  }
  if (event.kind === 'file') {
    return t('team.process.execution.sentFile');
  }
  if (event.kind === 'final') {
    return t('team.process.execution.final');
  }
  return event.title || t('team.process.execution.event');
}

export function mergeUniqueMessages(messages: Message[]): Message[] {
  const seen = new Set<string>();
  const merged: Message[] = [];
  for (const message of messages) {
    const event = parseTeamEventMessage(message);
    const key = event
      ? [
          'team',
          event.type,
          event.messageId,
          event.fromMember,
          event.toMember || '',
          event.timestamp || '',
          event.content,
        ].join(':')
      : `${message.id}:${message.content}`;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    merged.push(message);
  }
  return merged;
}

export function latestUserPrompt(messages: Message[]): string {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message.role === 'user' && message.content.trim()) {
      return message.content.trim();
    }
  }
  return '';
}
