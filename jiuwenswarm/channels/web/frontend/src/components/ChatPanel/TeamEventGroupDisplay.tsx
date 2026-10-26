import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ListChecks, MessageSquareText, Wrench } from 'lucide-react';
import { Message, type TodoItem } from '../../types';
import {
  type ParsedTeamEvent,
  parseTeamEventMessage,
} from './teamEventUtils';
import { isTeamLeaderMember } from '../../utils/teamMemberAvatar';
import { openTeamPanel } from '../../features/teamPanelState';
import TeamProcessIcon from '../../assets/team-process.svg?react';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import { useChatStore, useSessionStore } from '../../stores';
import type {
  TeamMemberExecutionEvent,
  TeamTask,
  TeamTaskEvent,
} from '../../stores/sessionStore';

type ActivityStatus = TeamTask['status'] | TodoItem['status'];
type Translate = (key: string, options?: Record<string, unknown>) => string;

interface AgentTeamActivityCardProps {
  messages: Message[];
  isProcessing: boolean;
  tasks: TeamTask[];
  taskEvents: TeamTaskEvent[];
  todos: TodoItem[];
  executionEvents: TeamMemberExecutionEvent[];
}

interface TeamMemberLike {
  member_id: string;
  name?: string;
  status?: string;
}

interface MemberActivity {
  memberId: string;
  displayName: string;
  statusLabel: string;
  summary: string;
  timestamp: number;
  counts?: MemberCounts;
}

interface ActivityCandidate {
  summary: string;
  statusLabel: string;
  timestamp: number;
}

interface TaskCandidate {
  title: string;
  status: ActivityStatus;
  timestamp: number;
}

interface MemberCounts {
  taskCount: number;
  messageCount: number;
  toolCount: number;
}

type TeamEventItem = { event: ParsedTeamEvent; message: Message };

interface AgentTeamHeaderProps {
  memberCount: number;
  expanded: boolean;
  isProcessing: boolean;
  currentActivity?: MemberActivity;
  onToggle: () => void;
  onOpenGroupChat: () => void;
}

function compactText(value: string, max = 54): string {
  const normalized = value.replace(/\s+/g, ' ').trim();
  if (normalized.length <= max) {
    return normalized;
  }
  return `${normalized.slice(0, max)}...`;
}

function toEventTime(event: ParsedTeamEvent, message?: Message): number {
  return event.timestamp || (message ? Date.parse(message.timestamp) : 0) || 0;
}

function toTaskTime(task: TeamTask): number {
  return task.timestamp || 0;
}

function toTodoTime(value?: string): number {
  return value ? Date.parse(value) || 0 : 0;
}

function getTaskTitle(task: Pick<TeamTask, 'title' | 'content' | 'task_id'>): string {
  return task.title?.trim() || task.content?.trim() || task.task_id;
}

function getTaskStatusLabel(status: ActivityStatus, t: Translate): string {
  if (status === 'completed') return t('chatUi.teamActivity.status.completed');
  if (status === 'cancelled') return t('chatUi.teamActivity.status.cancelled');
  return t('chatUi.teamActivity.status.thinking');
}

function isRunningTaskStatus(status: ActivityStatus): boolean {
  return (
    status === 'in_progress' ||
    status === 'planning' ||
    status === 'in_review'
  );
}

function getMemberName(memberId: string, members: TeamMemberLike[]): string {
  return members.find((member) => member.member_id === memberId)?.name?.trim() || memberId;
}

function isVisibleTeamMember(memberId?: string): memberId is string {
  return Boolean(memberId) && memberId !== 'user' && !isTeamLeaderMember(memberId);
}

function isActiveTeamMember(member: TeamMemberLike): boolean {
  const status = `${member.status || ''}`.toLowerCase();
  return isVisibleTeamMember(member.member_id) && !status.includes('shutdown') && !status.includes('down');
}

function pickTaskActivity(
  memberId: string,
  tasks: TeamTask[],
  todos: TodoItem[],
  t: Translate
): ActivityCandidate | null {
  const taskCandidates: TaskCandidate[] = [
    ...tasks
      .filter((task) => task.assignee === memberId)
      .map((task) => ({
        title: getTaskTitle(task),
        status: task.status,
        timestamp: toTaskTime(task),
      })),
    ...todos
      .filter((todo) => todo.claimedBy === memberId)
      .map((todo) => ({
        title: todo.content || todo.activeForm || todo.id,
        status: todo.status,
        timestamp: toTodoTime(todo.updatedAt),
      })),
  ];

  const runningTask = getLatestTask(taskCandidates.filter((task) => isRunningTaskStatus(task.status)));
  if (runningTask) {
    return buildTaskActivity(runningTask, t);
  }

  const latestTask = getLatestTask(taskCandidates);
  if (!latestTask) {
    return null;
  }

  return buildTaskActivity(latestTask, t);
}

function getLatestTask(candidates: TaskCandidate[]): TaskCandidate | undefined {
  return getLatestByTimestamp(candidates, (candidate) => candidate.timestamp);
}

function buildTaskActivity(task: TaskCandidate, t: Translate): ActivityCandidate {
  const statusLabel = getTaskStatusLabel(task.status, t);
  return {
    summary: compactText(task.title),
    statusLabel,
    timestamp: task.timestamp,
  };
}

function pickMessageActivity(
  memberId: string,
  eventItems: TeamEventItem[],
  t: Translate
): ActivityCandidate | null {
  const latest = getLatestByTimestamp(
    eventItems.filter(({ event }) => event.fromMember === memberId || event.toMember === memberId),
    ({ event, message }) => toEventTime(event, message)
  );
  if (!latest) {
    return null;
  }

  const isReceived = latest.event.toMember === memberId;
  return {
    summary: compactText(latest.event.content || latest.event.type),
    statusLabel: isReceived
      ? t('chatUi.teamActivity.status.receivedMessage')
      : t('chatUi.teamActivity.status.message'),
    timestamp: toEventTime(latest.event, latest.message),
  };
}

function pickToolActivity(
  memberId: string,
  executionEvents: TeamMemberExecutionEvent[],
  t: Translate
): ActivityCandidate | null {
  const latest = getLatestByTimestamp(
    executionEvents.filter((event) => event.member_id === memberId && event.kind !== 'final'),
    (event) => event.timestamp
  );
  if (!latest) {
    return null;
  }

  const activityLabel = getExecutionActivityLabel(latest, t);
  return {
    summary: activityLabel.summary,
    statusLabel: activityLabel.statusLabel,
    timestamp: latest.timestamp,
  };
}

function getExecutionActivityLabel(event: TeamMemberExecutionEvent, t: Translate): Pick<ActivityCandidate, 'summary' | 'statusLabel'> {
  const content = event.content || event.tool_name || event.title;
  if (event.kind === 'file') {
    return {
      summary: compactText(content),
      statusLabel: t('chatUi.teamActivity.status.file'),
    };
  }
  return {
    summary: compactText(content),
    statusLabel: event.kind === 'tool_call'
      ? t('chatUi.teamActivity.status.executingTool')
      : t('chatUi.teamActivity.status.toolCall'),
  };
}

function getLatestByTimestamp<T>(
  items: T[],
  getTimestamp: (item: T) => number
): T | undefined {
  return items.reduce<T | undefined>((latest, item) => {
    if (!latest || getTimestamp(item) > getTimestamp(latest)) {
      return item;
    }
    return latest;
  }, undefined);
}

function parseTeamEventItems(messages: Message[]): TeamEventItem[] {
  return messages
    .map((message) => ({ message, event: parseTeamEventMessage(message) }))
    .filter((item): item is TeamEventItem => Boolean(item.event));
}

function compareRecentActivity(a: MemberActivity, b: MemberActivity): number {
  return b.timestamp - a.timestamp
    || a.displayName.localeCompare(b.displayName, undefined, { numeric: true });
}

function buildMemberActivities(
  messages: Message[],
  members: TeamMemberLike[],
  tasks: TeamTask[],
  todos: TodoItem[],
  executionEvents: TeamMemberExecutionEvent[],
  t: Translate
): MemberActivity[] {
  const eventItems = parseTeamEventItems(messages);
  const memberIds = members
    .filter(isActiveTeamMember)
    .map((member) => member.member_id);

  return memberIds
    .map((memberId) => {
      const taskActivity = pickTaskActivity(memberId, tasks, todos, t);
      const messageActivity = pickMessageActivity(memberId, eventItems, t);
      const toolActivity = pickToolActivity(memberId, executionEvents, t);
      const selected = pickLatestActivity([toolActivity, messageActivity, taskActivity]);
      if (!selected) {
        return {
          memberId,
          displayName: getMemberName(memberId, members),
          statusLabel: t('chatUi.teamActivity.status.idle'),
          summary: '',
          timestamp: 0,
        };
      }
      return {
        memberId,
        displayName: getMemberName(memberId, members),
        statusLabel: selected.statusLabel,
        summary: selected.summary,
        timestamp: selected.timestamp,
      };
    })
    .sort(compareRecentActivity);
}

interface MemberActivityWithCounts extends MemberActivity {
  counts: MemberCounts;
}

function buildMemberCompletionSummaries(
  messages: Message[],
  members: TeamMemberLike[],
  tasks: TeamTask[],
  todos: TodoItem[],
  taskEvents: TeamTaskEvent[],
  executionEvents: TeamMemberExecutionEvent[]
): MemberActivityWithCounts[] {
  const eventItems = parseTeamEventItems(messages);
  const memberIds = members
    .filter(isActiveTeamMember)
    .map((member) => member.member_id);

  return memberIds
    .map((memberId) => {
      const counts = countMemberActivity(memberId, eventItems, tasks, todos, taskEvents, executionEvents);
      const latestTimestamp = getLatestMemberTimestamp(memberId, eventItems, tasks, todos, taskEvents, executionEvents);

      return {
        memberId,
        displayName: getMemberName(memberId, members),
        statusLabel: '',
        summary: '',
        timestamp: latestTimestamp,
        counts,
      };
    })
    .sort(compareRecentActivity);
}

function countMemberActivity(
  memberId: string,
  eventItems: TeamEventItem[],
  tasks: TeamTask[],
  todos: TodoItem[],
  taskEvents: TeamTaskEvent[],
  executionEvents: TeamMemberExecutionEvent[]
): MemberCounts {
  return {
    taskCount: countMemberTasks(memberId, tasks, todos, taskEvents),
    messageCount: eventItems.filter(({ event }) => event.fromMember === memberId).length,
    toolCount: executionEvents.filter((event) => event.member_id === memberId && event.kind === 'tool_call').length,
  };
}

function countMemberTasks(
  memberId: string,
  tasks: TeamTask[],
  todos: TodoItem[],
  taskEvents: TeamTaskEvent[]
): number {
  const taskIds = new Set<string>();
  tasks.forEach((task) => {
    if (task.assignee === memberId) {
      taskIds.add(task.task_id);
    }
  });
  todos.forEach((todo) => {
    if (todo.claimedBy === memberId) {
      taskIds.add(todo.id);
    }
  });
  taskEvents.forEach((event) => {
    if ((event.assignee === memberId || event.member_id === memberId) && event.task_id) {
      taskIds.add(event.task_id);
    }
  });
  return taskIds.size;
}

function getLatestMemberTimestamp(
  memberId: string,
  eventItems: TeamEventItem[],
  tasks: TeamTask[],
  todos: TodoItem[],
  taskEvents: TeamTaskEvent[],
  executionEvents: TeamMemberExecutionEvent[]
): number {
  const timestamps = [
    ...eventItems
      .filter(({ event }) => event.fromMember === memberId)
      .map(({ event, message }) => toEventTime(event, message)),
    ...tasks
      .filter((task) => task.assignee === memberId)
      .map(toTaskTime),
    ...todos
      .filter((todo) => todo.claimedBy === memberId)
      .map((todo) => toTodoTime(todo.updatedAt)),
    ...taskEvents
      .filter((event) => event.assignee === memberId || event.member_id === memberId)
      .map((event) => event.timestamp || 0),
    ...executionEvents
      .filter((event) => event.member_id === memberId)
      .map((event) => event.timestamp || 0),
  ];

  return Math.max(0, ...timestamps);
}

function pickLatestActivity(activities: Array<ActivityCandidate | null>): ActivityCandidate | null {
  const latest = getLatestByTimestamp(
    activities.filter((activity): activity is ActivityCandidate => activity !== null),
    (activity) => activity.timestamp
  );
  return latest || null;
}

function sortActivitiesByMember<T extends MemberActivity>(
  activities: T[],
  members: TeamMemberLike[]
): T[] {
  const orderedIds = members
    .map((member) => member.member_id)
    .filter(isVisibleTeamMember);
  const order = new Map(orderedIds.map((memberId, index) => [memberId, index]));
  return [...activities].sort((a, b) => {
    const aOrder = order.get(a.memberId) ?? Number.MAX_SAFE_INTEGER;
    const bOrder = order.get(b.memberId) ?? Number.MAX_SAFE_INTEGER;
    return aOrder - bOrder
      || a.displayName.localeCompare(b.displayName, undefined, { numeric: true })
      || a.timestamp - b.timestamp;
  });
}

function buildProgressLabel(memberCount: number, isProcessing: boolean, t: Translate): string {
  if (!isProcessing) {
    return t('chatUi.teamActivity.progress.inactive');
  }
  return t('chatUi.teamActivity.progress.activeMembers', { count: memberCount });
}

function buildActivityText(currentActivity: MemberActivity | undefined, t: Translate): string {
  if (!currentActivity) {
    return t('chatUi.teamActivity.preparing');
  }
  return t('chatUi.teamActivity.currentActivity', {
    member: currentActivity.displayName,
    summary: currentActivity.summary,
  });
}

function MemberCountSummary({ counts }: { counts: MemberCounts }) {
  const { t } = useTranslation();

  return (
    <span className="team-event-member-counts">
      <span className="team-event-member-count" title={t('chatUi.teamActivity.counts.tasks')}>
        <ListChecks aria-hidden="true" />
        {counts.taskCount}
      </span>
      <span className="team-event-member-count" title={t('chatUi.teamActivity.counts.messages')}>
        <MessageSquareText aria-hidden="true" />
        {counts.messageCount}
      </span>
      <span className="team-event-member-count" title={t('chatUi.teamActivity.counts.tools')}>
        <Wrench aria-hidden="true" />
        {counts.toolCount}
      </span>
    </span>
  );
}

function openMemberDetail(memberId: string): void {
  openTeamPanel('team', 'members', memberId);
}

function AgentTeamHeader({
  memberCount,
  expanded,
  isProcessing,
  currentActivity,
  onToggle,
  onOpenGroupChat,
}: AgentTeamHeaderProps) {
  const { t } = useTranslation();

  return (
    <button
      type="button"
      className="team-event-group-summary"
      onClick={onToggle}
      aria-expanded={expanded}
    >
      <span className="team-event-group-summary__main">
        <span className="team-event-group-summary__icon" aria-hidden="true">
          <TeamProcessIcon aria-hidden />
        </span>
        <span className="team-event-group-summary__title">
          {buildProgressLabel(memberCount, isProcessing, t)}
        </span>
      </span>
      {isProcessing && (
        <span className="team-event-group-summary__activity">
          ｜ {buildActivityText(currentActivity, t)}
        </span>
      )}
      <span
        className="team-event-group-summary__chevron"
        aria-hidden="true"
        title={t('chatUi.teamActivity.openGroupChatTitle')}
        onClick={(event) => {
          event.stopPropagation();
          onOpenGroupChat();
        }}
      >
        {t('chatUi.teamActivity.openGroupChat')}
      </span>
    </button>
  );
}

export function AgentTeamActivityCard({
  messages,
  isProcessing,
  tasks,
  taskEvents,
  todos,
  executionEvents,
}: AgentTeamActivityCardProps) {
  const [expanded, setExpanded] = useState(false);
  const { t } = useTranslation();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const teamMembers = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.teamMembers ?? []);
  const members = teamMembers as TeamMemberLike[];
  const { activities, activeCount } = useMemo(() => {
    const count = members.filter(isActiveTeamMember).length;
    if (!isProcessing) {
      const acts = buildMemberCompletionSummaries(
        messages, members, tasks, todos, taskEvents, executionEvents
      );
      return { activities: acts, activeCount: count };
    }
    const acts = buildMemberActivities(
      messages, members, tasks, todos, executionEvents, t
    );
    return { activities: acts, activeCount: count };
  }, [messages, members, tasks, todos, taskEvents, executionEvents, isProcessing, t]);

  if (activities.length === 0) {
    return null;
  }

  const currentActivity = activities.find((a) => a.timestamp > 0);

  return (
    <div className="chat-active-team-group animate-rise">
      <div className="team-event-group team-event-group--activity">
        <AgentTeamHeader
          memberCount={activeCount}
          expanded={expanded}
          isProcessing={isProcessing}
          currentActivity={currentActivity}
          onToggle={() => setExpanded((current) => !current)}
          onOpenGroupChat={() => openTeamPanel('team', 'group')}
        />
        {expanded && (
          <div className="team-event-group-list team-event-group-list--activity">
            {sortActivitiesByMember(activities, members).map((activity) => (
              <button
                key={activity.memberId}
                type="button"
                className="team-event-group-row team-event-group-row--activity"
                onClick={() => openMemberDetail(activity.memberId)}
              >
                <div className="team-event-group-row__avatar">
                  <TeamMemberAvatar member={activity.memberId} className="h-7 w-7" />
                </div>
                <div className="team-event-group-row__main">
                  <div className="team-event-group-row__meta">
                    <span className="team-event-group-row__member">{activity.displayName}</span>
                    {activity.counts ? (
                      <MemberCountSummary counts={activity.counts} />
                    ) : (
                      <span className="team-event-group-chip team-event-group-chip--status">
                        {activity.statusLabel}
                      </span>
                    )}
                  </div>
                  {!activity.counts && (
                    <div className="team-event-group-row__content">
                      {activity.summary}
                    </div>
                  )}
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
