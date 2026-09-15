import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';
import { useChatStore, useSessionStore, useTodoStore } from '../../stores';
import type { Message, TeamMemberContextCompressionState } from '../../types';
import type { TeamMemberExecutionEvent, TeamTask as SessionTeamTask } from '../../stores/sessionStore';
import { MarkdownMessageBody } from '../ChatPanel/MessageItem';
import { parseTeamEventMessage, type ParsedTeamEvent } from '../ChatPanel/teamEventUtils';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import { isTeamLeaderMember, isUserMember } from '../../utils/teamMemberAvatar';
import { contextCompressionRunningText } from '../../utils/contextCompression';
import teamIcon from '../../assets/team.svg';
import PendingIcon from '../../assets/pending.svg?react';

import BackIcon from '../../assets/back.svg?react';
import { MemberListItem } from './MemberListItem';
import { MemberOverviewCard } from './MemberOverviewCard';
import { ProcessListCard } from './ProcessListCard';
import { MemberTaskListBar, MemberTaskListPanel } from './MemberTaskList';
import {
  buildProcessItems,
  buildTaskMap,
  getMemberDisplayName,
  getMemberStatusKey,
  latestUserPrompt,
  mergeUniqueMessages,
  type TeamDetailTab,
  type TeamMember,
} from './shared';
import { AlertTriangle, CircleAlert, LoaderCircle, X } from 'lucide-react';

type TeamMembersPanelProps = {
  variant: 'compact' | 'expanded';
  members: TeamMember[];
  tasks?: SessionTeamTask[];
  selectedMemberId?: string;
  selectedMember?: TeamMember | null;
  activeDetailTab?: TeamDetailTab;
  historyMessages?: Message[];
  onSelectMember?: (memberId: string) => void;
  onMemberClick?: (memberId: string) => void;
  onDetailTabChange?: (tab: TeamDetailTab) => void;
  onExpand?: () => void;
};

type GroupMessageItem = { message: Message; event: ParsedTeamEvent };

const GROUP_LEADER_MEMBER_ID = 'team_leader';

function getGroupMemberIds(members: TeamMember[]): string[] {
  return members.map((member) => member.member_id).filter((memberId) => !isTeamLeaderMember(memberId));
}

function isGroupMessageItem(item: { message: Message; event: ParsedTeamEvent | null }): item is GroupMessageItem {
  return item.event !== null && !item.event.isLeaderToUser;
}

function getGroupMessageTime(item: GroupMessageItem): number {
  return item.event.timestamp || Date.parse(item.message.timestamp) || 0;
}

function buildGroupMessageItems(historyMessages: Message[], messages: Message[]): GroupMessageItem[] {
  return mergeUniqueMessages(historyMessages.concat(messages))
    .map((message) => ({ message, event: parseTeamEventMessage(message) }))
    .filter(isGroupMessageItem)
    .sort((a, b) => getGroupMessageTime(a) - getGroupMessageTime(b));
}

function normalizeMemberKey(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[\s_-]+/g, '');
}

function isLeaderMember(member: TeamMember, leaderIds: string[]): boolean {
  const memberKeys = [member.member_id, member.name || ''].map(normalizeMemberKey);
  return (
    isTeamLeaderMember(member.member_id) ||
    member.mode === 'leader' ||
    member.mode === 'team_leader' ||
    leaderIds.some((leaderId) => memberKeys.includes(normalizeMemberKey(leaderId)))
  );
}

function normalizeFinalEventContent(content?: string): string {
  return (content || '').replace(/\s+/g, ' ').trim();
}

function dedupeFinalEvents(events: TeamMemberExecutionEvent[]): TeamMemberExecutionEvent[] {
  const deduped: TeamMemberExecutionEvent[] = [];
  for (const event of events) {
    const normalizedContent = normalizeFinalEventContent(event.content);
    const duplicate = deduped.some(
      (item) =>
        item.member_id === event.member_id &&
        normalizeFinalEventContent(item.content) === normalizedContent &&
        Math.abs((item.timestamp || 0) - (event.timestamp || 0)) <= 60_000,
    );
    if (!duplicate) {
      deduped.push(event);
    }
  }
  return deduped;
}

export function TeamMembersPanel({
  variant,
  members,
  tasks = [],
  selectedMemberId = '',
  selectedMember = null,
  activeDetailTab = 'members',
  historyMessages = [],
  onSelectMember,
  onMemberClick,
  onDetailTabChange,
}: TeamMembersPanelProps) {
  const { t } = useTranslation();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const messages = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.messages ?? []);
  const teamLeaderMemberIds = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.teamLeaderMemberIds ?? []);
  const groupMessages = useMemo(() => buildGroupMessageItems(historyMessages, messages), [historyMessages, messages]);
  const visibleMembers = useMemo(
    () => members.filter((member) => !isLeaderMember(member, teamLeaderMemberIds)),
    [members, teamLeaderMemberIds],
  );
  const visibleSelectedMember = useMemo(() => {
    const id = selectedMember?.member_id || selectedMemberId;
    if (!id) return null;
    return visibleMembers.find((member) => member.member_id === id) || null;
  }, [selectedMember?.member_id, selectedMemberId, visibleMembers]);
  const memberTaskProgress = useMemo(() => {
    const progress: Record<string, { completed: number; total: number }> = {};
    visibleMembers.forEach((member) => {
      const memberTasks = tasks.filter((task) => task.assignee === member.member_id);
      const completed = memberTasks.filter((task) => task.status === 'completed').length;
      progress[member.member_id] = { completed, total: memberTasks.length };
    });
    return progress;
  }, [tasks, visibleMembers]);

  if (variant === 'compact') {
    return (
      <div
        className="flex flex-1 flex-col overflow-hidden rounded-b-lg bg-card min-h-0 px-3"
        data-testid="team-area-members-panel"
        data-variant="compact"
      >
        <div
          className="flex w-full shrink-0 items-center justify-between bg-card px-4 py-3 border-border"
          data-testid="team-area-members-header"
        >
          <div className="flex items-center gap-2">
            <img src={teamIcon} alt="" className="h-4 w-4 text-text-muted" />
            <span className="text-sm font-medium text-text" data-testid="team-area-members-count">
              {t('team.members')} ({visibleMembers.length})
            </span>
          </div>
        </div>
        <div className="flex-1 space-y-2 overflow-y-auto px-4 py-3" data-testid="team-area-members-list">
          {visibleMembers.length === 0 ? (
            <div className="py-8 text-center text-xs text-text-muted" data-testid="team-area-members-empty">
              {t('team.noMemberData')}
            </div>
          ) : (
            visibleMembers.map((member) => (
              <MemberListItem
                key={member.member_id}
                member={member}
                compact
                taskProgress={memberTaskProgress[member.member_id]}
                onClick={() => onMemberClick?.(member.member_id)}
              />
            ))
          )}
        </div>
      </div>
    );
  }

  return (
    <div
      className="flex min-w-0 flex-1 overflow-x-auto overflow-y-hidden"
      data-testid="team-area-members-panel"
      data-variant="expanded"
    >
      {activeDetailTab === 'members' && (
        <aside
          className="w-[240px] shrink-0 overflow-y-auto border-r border-border bg-card"
          data-testid="team-area-members-sidebar"
        >
          <div className="px-[24px] pt-[24px]">
            <DetailTabSwitch activeTab={activeDetailTab} onChange={onDetailTabChange} />
          </div>

          <div className="space-y-3 px-[24px] py-4" data-testid="team-area-members-sidebar-list">
            {visibleMembers.length === 0 ? (
              <div className="py-10 text-center text-sm text-text-muted" data-testid="team-area-members-sidebar-empty">
                {t('team.noMemberData')}
              </div>
            ) : (
              visibleMembers.map((member) => (
                <MemberListItem
                  key={member.member_id}
                  member={member}
                  selected={visibleSelectedMember?.member_id === member.member_id}
                  onClick={() => onSelectMember?.(member.member_id)}
                />
              ))
            )}
          </div>
        </aside>
      )}

      {activeDetailTab === 'group' ? (
        <GroupChatDetail
          items={groupMessages}
          members={members}
          activeTab={activeDetailTab}
          onTabChange={onDetailTabChange}
        />
      ) : visibleSelectedMember ? (
        <MemberTaskDetail
          member={visibleSelectedMember}
          tasks={tasks}
          historyMessages={historyMessages}
          onBack={() => onSelectMember?.('')}
        />
      ) : (
        <MemberOverviewPanel
          members={visibleMembers}
          tasks={tasks}
          historyMessages={historyMessages}
          onMemberClick={(memberId) => onSelectMember?.(memberId)}
        />
      )}
    </div>
  );
}

function DetailTabSwitch({
  activeTab,
  onChange,
}: {
  activeTab: TeamDetailTab;
  onChange?: (tab: TeamDetailTab) => void;
}) {
  const { t } = useTranslation();

  return (
    <div
      className="grid grid-cols-2 rounded-md bg-[var(--color-tag-surface)] p-1 text-sm"
      data-testid="team-area-detail-tab-switch"
    >
      <button
        type="button"
        data-testid="team-area-detail-tab-members"
        className={`h-8 rounded text-center  ${activeTab === 'members' ? 'bg-card font-semibold text-text shadow-sm' : 'font-semibold text-[var(--color-text-secondary)] hover:text-text'}`}
        onClick={() => onChange?.('members')}
      >
        {t('team.detailTabs.members')}
      </button>
      <button
        type="button"
        data-testid="team-area-detail-tab-group"
        className={`h-8 rounded text-center  ${activeTab === 'group' ? 'bg-card font-semibold text-text shadow-sm' : 'font-semibold text-[var(--color-text-secondary)] hover:text-text'}`}
        onClick={() => onChange?.('group')}
      >
        {t('team.detailTabs.group')}
      </button>
    </div>
  );
}

function GroupChatDetail({
  items,
  members,
  activeTab,
  onTabChange,
}: {
  items: GroupMessageItem[];
  members: TeamMember[];
  activeTab: TeamDetailTab;
  onTabChange?: (tab: TeamDetailTab) => void;
}) {
  const { t } = useTranslation();
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const userScrolledUpRef = useRef(false);
  const groupMemberIds = getGroupMemberIds(members);
  const memberNames = [t('team.leader'), ...groupMemberIds.map(getMemberDisplayName)].join(t('team.memberSeparator'));
  const avatarMemberIds = [GROUP_LEADER_MEMBER_ID, ...groupMemberIds];
  const memberNamesRef = useRef<HTMLDivElement>(null);
  const [memberNamesTruncated, setMemberNamesTruncated] = useState(false);
  const { tooltip: memberNamesTooltip, handlers: memberNamesTooltipHandlers } = useAdaptiveTooltip();

  useLayoutEffect(() => {
    const element = memberNamesRef.current;
    if (!element) return;
    const checkOverflow = () => setMemberNamesTruncated(element.scrollWidth > element.clientWidth);
    checkOverflow();
    const observer = new ResizeObserver(checkOverflow);
    observer.observe(element);
    return () => observer.disconnect();
  }, [memberNames]);

  useEffect(() => {
    const el = scrollContainerRef.current;
    if (!el || userScrolledUpRef.current) {
      return;
    }
    el.scrollTop = el.scrollHeight;
  }, [items.length]);

  const handleScroll = () => {
    const el = scrollContainerRef.current;
    if (!el) return;
    userScrolledUpRef.current = el.scrollHeight - el.scrollTop - el.clientHeight >= 40;
  };

  return (
    <section className="flex min-w-0 flex-1 flex-col bg-card" data-testid="team-area-group-chat">
      <div
        className="flex shrink-0 items-center justify-between gap-5 border-b border-border bg-card px-[24px] pt-[22px] pb-[24px]"
        data-testid="team-area-group-chat-section"
      >
        <div className="w-[192px] shrink-0">
          <DetailTabSwitch activeTab={activeTab} onChange={onTabChange} />
        </div>
        <div className="flex min-w-0 items-center justify-end gap-3">
          <div className="min-w-0 text-right">
            <div className="text-base font-semibold text-text" data-testid="team-area-group-chat-title">
              {t('team.groupChat')}
            </div>
            <div
              ref={memberNamesRef}
              className="mt-1 truncate text-xs text-text-muted"
              data-testid="team-area-group-chat-member-names"
              data-tooltip={memberNamesTruncated ? memberNames : undefined}
              tabIndex={memberNamesTruncated ? 0 : undefined}
              {...memberNamesTooltipHandlers}
            >
              {memberNames}
            </div>
          </div>
          <div className="flex -space-x-2" data-testid="team-area-group-chat-avatar-stack">
            <GroupAvatarStack memberIds={avatarMemberIds} />
          </div>
        </div>
      </div>

      {memberNamesTooltip}
      <div
        ref={scrollContainerRef}
        className="team-group-chat-message-list min-h-0 flex-1 overflow-y-auto px-7 py-6"
        onScroll={handleScroll}
        data-testid="team-area-group-chat-message-list"
      >
        {items.length === 0 ? (
          <div
            className="flex h-full items-center justify-center text-sm text-text-muted"
            data-testid="team-area-group-chat-empty"
          >
            {t('team.noGroupMessages')}
          </div>
        ) : (
          <div className="mx-auto max-w-[820px] space-y-5">
            {items.map(({ message, event }, index) => (
              <GroupChatMessage key={`${message.id}-${event.timestamp ?? index}`} event={event} />
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function GroupAvatarStack({ memberIds }: { memberIds: string[] }) {
  const visibleMemberIds = memberIds.length > 3 ? memberIds.slice(0, 2) : memberIds;
  const hiddenCount = memberIds.length - visibleMemberIds.length;

  return (
    <>
      {visibleMemberIds.map((memberId) => (
        <TeamMemberAvatar key={memberId} member={memberId} className="!h-7 !w-7 ring-2 ring-card" />
      ))}
      {hiddenCount > 0 && (
        <span
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-[var(--color-team-overflow-surface)] text-xs font-medium text-accent ring-2 ring-card"
          data-testid="team-area-group-chat-avatar-overflow"
        >
          +{hiddenCount}
        </span>
      )}
    </>
  );
}

function GroupChatMessage({ event }: { event: ParsedTeamEvent }) {
  const { t } = useTranslation();
  const displayName = getMemberDisplayName(event.fromMember);
  const isUser = isUserMember(event.fromMember);

  return (
    <div className={`flex items-start gap-3 ${isUser ? 'justify-end' : ''}`}>
      {!isUser && <TeamMemberAvatar member={event.fromMember} className="h-8 w-8" />}
      <div className={`min-w-0 ${isUser ? 'max-w-[72%] text-right' : 'flex-1'}`}>
        <div
          className="pb-2 text-base font-semibold leading-7 text-text"
          data-testid="team-area-group-chat-message-sender"
        >
          {displayName}
        </div>
        <div
          className={`text-sm leading-6 text-text ${isUser ? 'inline-block rounded-lg bg-accent-subtle px-3 py-2 text-left' : ''}`}
        >
          {event.isP2P && event.toMember && (
            <span
              className="team-event-group-chip team-event-group-chip--p2p"
              data-testid="team-area-group-chat-message-p2p-chip"
            >
              @{getMemberDisplayName(event.toMember)}
            </span>
          )}
          {event.isBroadcast && (
            <span
              className="team-event-group-chip team-event-group-chip--broadcast"
              data-testid="team-area-group-chat-message-broadcast-chip"
            >
              @{t('team.allMembers')}
            </span>
          )}
          <MarkdownMessageBody
            content={event.content}
            className="team-message-markdown team-message-markdown--inline"
          />
        </div>
      </div>
      {isUser && <TeamMemberAvatar member={event.fromMember} className="h-8 w-8" />}
    </div>
  );
}

function MemberOverviewPanel({
  members,
  tasks,
  historyMessages,
  onMemberClick,
}: {
  members: TeamMember[];
  tasks: SessionTeamTask[];
  historyMessages?: Message[];
  onMemberClick: (memberId: string) => void;
}) {
  const { t } = useTranslation();

  return (
    <section className="flex min-w-0 flex-1 flex-col bg-card" data-testid="team-area-member-overview">
      <div
        className="min-h-0 flex-1 overflow-y-auto px-6 pt-6 pb-[48px] [scrollbar-gutter:stable]"
        data-testid="team-area-member-overview-body"
      >
        {members.length === 0 ? (
          <div className="py-12 text-center text-sm text-text-muted" data-testid="team-area-member-overview-empty">
            {t('team.noMemberData')}
          </div>
        ) : (
          <div className="flex flex-col gap-4" data-testid="team-area-member-overview-grid">
            {members.map((member, index) => (
              <TeamMemberOverviewCard
                key={member.member_id}
                member={member}
                sequence={index + 1}
                tasks={tasks}
                historyMessages={historyMessages}
                onClick={() => onMemberClick(member.member_id)}
              />
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

const TeamMemberOverviewCard = memo(function TeamMemberOverviewCard({
  member,
  sequence,
  tasks,
  historyMessages = [],
  onClick,
}: {
  member: TeamMember;
  sequence: number;
  tasks?: SessionTeamTask[];
  historyMessages?: Message[];
  onClick?: () => void;
}) {
  const { t } = useTranslation();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const todos = useTodoStore((s) => s.runtimes[activeSessionId ?? '']?.todos ?? []);
  const teamTaskEvents = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.teamTaskEvents ?? []);
  const teamMemberExecutionEvents = useSessionStore(
    (s) => s.runtimes[activeSessionId ?? '']?.teamMemberExecutionEvents ?? [],
  );
  const messages = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.messages ?? []);

  const processMessages = useMemo(
    () => mergeUniqueMessages([...historyMessages, ...messages]),
    [historyMessages, messages],
  );
  const prompt = useMemo(() => latestUserPrompt(messages), [messages]);
  const memberTasks = useMemo(
    () => buildTaskMap(member.member_id, todos, teamTaskEvents, prompt, tasks ?? []),
    [member.member_id, prompt, tasks, teamTaskEvents, todos],
  );
  const processItems = useMemo(
    () =>
      buildProcessItems(member.member_id, memberTasks, teamTaskEvents, processMessages, teamMemberExecutionEvents, t),
    [member.member_id, memberTasks, processMessages, t, teamMemberExecutionEvents, teamTaskEvents],
  );

  const displayName = getMemberDisplayName(member);
  const statusKey = getMemberStatusKey(member);
  const isRunning = statusKey === 'running';
  const statusIcon = isRunning ? (
    <svg className="w-4 h-4 text-info animate-spin shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 2v4" />
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="m16.2 7.8 2.9-2.9" />
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M18 12h4" />
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="m16.2 16.2 2.9 2.9" />
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 18v4" />
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="m4.9 19.1 2.9-2.9" />
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M2 12h4" />
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="m4.9 4.9 2.9 2.9" />
    </svg>
  ) : (
    <PendingIcon className="w-4 h-4 shrink-0 text-text-muted" />
  );

  return (
    <MemberOverviewCard
      memberId={member.member_id}
      displayName={displayName}
      sequence={sequence}
      statusIcon={statusIcon}
      onClick={onClick}
      items={processItems}
    />
  );
});

function MemberTaskDetail({
  member,
  tasks = [],
  historyMessages = [],
  onBack,
}: {
  member: TeamMember;
  tasks?: SessionTeamTask[];
  historyMessages?: Message[];
  onBack?: () => void;
}) {
  const { t } = useTranslation();
  const [taskListExpanded, setTaskListExpanded] = useState(false);
  const [expandedProcessIds, setExpandedProcessIds] = useState<Set<string>>(new Set());
  const footerRef = useRef<HTMLDivElement>(null);
  // 任务列表卡片（popup）展开时：点击外部收起
  useEffect(() => {
    if (!taskListExpanded) return;
    const handlePointerDown = (event: MouseEvent) => {
      if (!footerRef.current?.contains(event.target as Node)) setTaskListExpanded(false);
    };
    document.addEventListener('mousedown', handlePointerDown);
    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
    };
  }, [taskListExpanded]);
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const todos = useTodoStore((s) => s.runtimes[activeSessionId ?? '']?.todos ?? []);
  const teamTaskEvents = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.teamTaskEvents ?? []);
  const teamMemberExecutionEvents = useSessionStore(
    (s) => s.runtimes[activeSessionId ?? '']?.teamMemberExecutionEvents ?? [],
  );
  const teamMemberContextCompression = useSessionStore(
    (s) => s.runtimes[activeSessionId ?? '']?.teamMemberContextCompression ?? {},
  );
  const clearTeamMemberContextCompressionStatus = useSessionStore((s) => s.clearTeamMemberContextCompressionStatus);
  const messages = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.messages ?? []);
  const processMessages = useMemo(
    () => mergeUniqueMessages([...historyMessages, ...messages]),
    [historyMessages, messages],
  );
  const prompt = useMemo(() => latestUserPrompt(messages), [messages]);
  const memberTasks = useMemo(
    () => buildTaskMap(member.member_id, todos, teamTaskEvents, prompt, tasks),
    [member.member_id, prompt, tasks, teamTaskEvents, todos],
  );
  const processItems = useMemo(
    () =>
      buildProcessItems(member.member_id, memberTasks, teamTaskEvents, processMessages, teamMemberExecutionEvents, t),
    [member.member_id, memberTasks, processMessages, t, teamMemberExecutionEvents, teamTaskEvents],
  );
  const finalEvents = useMemo(
    () =>
      dedupeFinalEvents(
        teamMemberExecutionEvents.filter(
          (event) => event.member_id === member.member_id && event.kind === 'final' && event.title !== '成员回复',
        ),
      ).sort((a, b) => a.timestamp - b.timestamp),
    [member.member_id, teamMemberExecutionEvents],
  );
  const displayName = getMemberDisplayName(member);
  const contextCompressionState = teamMemberContextCompression[member.member_id];

  useEffect(() => {
    setTaskListExpanded(false);
    setExpandedProcessIds(new Set());
  }, [member.member_id]);

  const toggleProcess = (itemId: string) => {
    setExpandedProcessIds((prev) => {
      const next = new Set(prev);
      if (next.has(itemId)) next.delete(itemId);
      else next.add(itemId);
      return next;
    });
  };

  return (
    <section className="flex min-w-[320px] flex-1 flex-col bg-card" data-testid="team-area-member-task-detail">
      <div className="flex shrink-0 items-center gap-2 bg-card pl-4 pt-6" data-testid="team-area-member-detail-section">
        {onBack && (
          <button
            type="button"
            onClick={onBack}
            className="flex items-center text-sm text-text-muted hover:text-text"
            data-testid="team-area-member-detail-back"
          >
            <BackIcon className="text-text" />
          </button>
        )}
        <div className="text-sm font-semibold text-text" data-testid="team-area-member-detail-title">
          {t('team.memberTasksTitle', { member: displayName })}
        </div>
      </div>

      <div
        className="member-detail-body min-h-0 flex-1 overflow-y-auto px-12 pt-[26px] pb-7"
        data-testid="team-area-member-detail-body"
      >
        <ProcessListCard items={processItems} expandedIds={expandedProcessIds} onToggle={toggleProcess} />
        <FinalSummaryList events={finalEvents} />
      </div>

      <div ref={footerRef} className="relative shrink-0 border-t border-border bg-card">
        <TeamMemberContextCompressionBar
          state={contextCompressionState}
          onClose={() => {
            if (activeSessionId) {
              clearTeamMemberContextCompressionStatus(activeSessionId, member.member_id);
            }
          }}
        />
        {memberTasks.length > 0 ? (
          <div
            data-testid="team-area-member-detail-footer"
          >
            <MemberTaskListBar
              tasks={memberTasks}
              expanded={taskListExpanded}
              onToggle={() => setTaskListExpanded((expanded) => !expanded)}
            />
            {taskListExpanded && <MemberTaskListPanel tasks={memberTasks} />}
          </div>
        ) : null}
      </div>
    </section>
  );
}

function TeamMemberContextCompressionBar({
  state,
  onClose,
}: {
  state?: TeamMemberContextCompressionState;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const runtime = state?.runtime;
  const summary = state?.summary;
  const summaryItems = (summary?.summaries ?? []).filter(Boolean);
  const showSummaryDetails = summaryItems.length > 0;

  if (!runtime?.summary && !showSummaryDetails) {
    return null;
  }

  const status = runtime?.status;
  const isRunning = status === 'running';
  const isFailed = status === 'failed';
  let statusTitle = t('team.contextCompression.completed', { count: summary?.count || 1 });
  if (isRunning) {
    statusTitle = t('team.contextCompression.running');
  } else if (isFailed) {
    statusTitle = t('team.contextCompression.failed');
  }
  const detailsTitle = showSummaryDetails
    ? summaryItems.map((item, index) => `${index + 1}. ${item}`).join('\n')
    : undefined;

  const isComplete = !isRunning && !isFailed;
  let stateClass = 'is-complete';
  if (isFailed) {
    stateClass = 'is-failed';
  } else if (isRunning) {
    stateClass = 'is-running';
  }
  const statusIcon = isFailed ? <AlertTriangle size={14} /> : <CircleAlert size={14} />;
  const statusIconTitle = showSummaryDetails && !isRunning ? detailsTitle : undefined;
  const activityClassName = isRunning
    ? 'team-event-group-summary__activity context-compression-running-text'
    : 'team-event-group-summary__activity';

  return (
    <div
      className="team-event-group team-event-group--context-compression w-[auto]"
      data-testid="team-area-context-compression"
    >
      <div
        className={`team-event-group-summary team-event-group-summary--context-compression ${stateClass}`}
        data-testid="team-area-context-compression-summary"
        data-variant={stateClass}
      >
        <span className="team-event-group-summary__main">
          <span
            className="team-event-group-summary__icon team-event-group-summary__icon--status"
            title={statusIconTitle}
            aria-hidden="true"
          >
            {statusIcon}
          </span>
          <span className="team-event-group-summary__title" data-testid="team-area-context-compression-status-title">
            {statusTitle}
          </span>
          {isRunning && (
            <span className="team-event-group-summary__icon team-event-group-summary__icon--status" aria-hidden="true">
              <LoaderCircle size={14} className="animate-spin" />
            </span>
          )}
        </span>
        {runtime?.summary && !isComplete && (
          <span className={activityClassName} data-testid="team-area-context-compression-activity">
            {isRunning ? contextCompressionRunningText(t, runtime?.processor, runtime.summary) : runtime.summary}
          </span>
        )}
        {!isRunning && (
          <button
            type="button"
            className="team-event-group-summary__icon team-event-group-summary__icon--close"
            onClick={onClose}
            data-testid="team-area-context-compression-close-button"
            title={t('team.contextCompression.close')}
            aria-label={t('team.contextCompression.close')}
          >
            <X size={14} />
          </button>
        )}
      </div>
    </div>
  );
}

function FinalSummaryList({ events }: { events: TeamMemberExecutionEvent[] }) {
  if (events.length === 0) {
    return null;
  }

  return (
    <div className="mt-5 border-t border-[var(--color-team-detail-divider)] pt-4" data-testid="team-area-final-summary">
      <div className="mt-4 space-y-6">
        {events.map((event) => (
          <section
            key={event.id}
            className="space-y-3"
            data-testid="team-area-final-summary-item"
            data-variant={event.id}
          >
            <div
              className="whitespace-pre-wrap break-words text-sm leading-7 text-text"
              data-testid="team-area-final-summary-content"
            >
              {event.content || '-'}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}

