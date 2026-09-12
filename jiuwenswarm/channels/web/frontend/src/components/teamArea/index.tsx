/**
 * TeamArea component - cluster mode task overview and member execution detail.
 */

import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import type { ReactNode } from 'react';
import { useChatStore, useSessionStore, useTodoStore } from '../../stores';
import type { Message } from '../../types';
import { TaskPlanningPanel } from './TaskPlanningPanel';
import { TeamMembersPanel } from './TeamMembersPanel';
import teamIcon from '../../assets/team.svg';
import { normalizeTaskStatus, type TabType, type TeamDetailTab, type TeamAreaProps, type TeamMember } from './shared';
import { filterInvokedTeamMembers, isCurrentTurnEvidence, latestUserTurnStartedAtMs } from './invokedTeamMembers';
import { buildCurrentTurnWorkflowRuns } from './teamWorkflowAdapter';
import type { WorkflowRun } from './workflowTypes';
import { ExpandedPanel } from './ExpandedPanel';

function useTaskPlanningMetrics(currentTurnStartedAtMs: number | null) {
  const activeSessionId = useChatStore(s => s.activeSessionId);
  const todos = useTodoStore(s => s.runtimes[activeSessionId ?? '']?.todos ?? []);
  const allTeamTaskEvents = useSessionStore(s => s.runtimes[activeSessionId ?? '']?.teamTaskEvents ?? []);
  const allTeamTasks = useSessionStore(s => s.runtimes[activeSessionId ?? '']?.teamTasks ?? []);
  const teamTaskEvents = useMemo(
    () => allTeamTaskEvents.filter(event => isCurrentTurnEvidence(event, currentTurnStartedAtMs)),
    [allTeamTaskEvents, currentTurnStartedAtMs],
  );
  const teamTasks = useMemo(() => allTeamTasks.filter(task => isCurrentTurnEvidence(task, currentTurnStartedAtMs)), [allTeamTasks, currentTurnStartedAtMs]);
  const progressTasks = teamTasks;

  const totalTasks = useMemo(() => {
    if (teamTasks.length > 0) return teamTasks.length;
    const taskIds = new Set<string>();
    if (currentTurnStartedAtMs === null) todos.forEach(todo => taskIds.add(todo.id));
    teamTaskEvents.forEach(event => {
      if (event.task_id) taskIds.add(event.task_id);
    });
    return taskIds.size;
  }, [currentTurnStartedAtMs, teamTaskEvents, teamTasks.length, todos]);

  const completedTasks = useMemo(() => {
    if (teamTasks.length > 0) {
      return teamTasks.filter(task => task.status === 'completed').length;
    }
    const completed = new Set<string>();
    if (currentTurnStartedAtMs === null) {
      todos.forEach(todo => {
        if (normalizeTaskStatus(todo.status) === 'completed') completed.add(todo.id);
      });
    }
    teamTaskEvents.forEach(event => {
      if (event.task_id && normalizeTaskStatus(event.status, event.type) === 'completed') {
        completed.add(event.task_id);
      }
    });
    return completed.size;
  }, [currentTurnStartedAtMs, teamTaskEvents, teamTasks, todos]);

  return { completedTasks, progressTasks, teamTasks, totalTasks };
}

function CompactTeamArea({
  members,
  currentTurnStartedAtMs,
  onExpand,
}: {
  members: TeamMember[];
  currentTurnStartedAtMs: number | null;
  onExpand?: (tab: TabType, memberId?: string) => void;
}) {
  const { completedTasks, progressTasks, teamTasks, totalTasks } = useTaskPlanningMetrics(currentTurnStartedAtMs);

  return (
    <>
      <TaskPlanningPanel
        variant="compact"
        tasks={teamTasks}
        progressTasks={progressTasks}
        members={members}
        totalTasks={totalTasks}
        completedTasks={completedTasks}
        onExpand={() => onExpand?.('planning')}
      />
      <TeamMembersPanel
        variant="compact"
        members={members}
        tasks={teamTasks}
        onExpand={() => onExpand?.('team')}
        onMemberClick={memberId => onExpand?.('team', memberId)}
      />
    </>
  );
}

function ExpandedTeamArea({
  members,
  historyMessages = [],
  activeTab,
  activeDetailTab,
  selectedMemberId: externalSelectedMemberId,
  selectedArtifactId,
  onTabChange,
  onDetailTabChange,
  onMemberSelect,
  onArtifactSelect,
  onCollapse,
  reviewPanel,
  currentTurnStartedAtMs,
  workflowRuns,
  activeSessionId,
}: {
  members: TeamMember[];
  historyMessages?: Message[];
  activeTab: TabType;
  activeDetailTab: TeamDetailTab;
  selectedMemberId?: string;
  selectedArtifactId?: string;
  onTabChange: (tab: TabType) => void;
  onDetailTabChange: (tab: TeamDetailTab) => void;
  onMemberSelect?: (memberId: string) => void;
  onArtifactSelect?: (artifactId: string) => void;
  onCollapse?: () => void;
  reviewPanel?: ReactNode;
  currentTurnStartedAtMs: number | null;
  workflowRuns: WorkflowRun[];
  activeSessionId: string;
}) {
  const { t } = useTranslation();
  const { completedTasks, progressTasks, teamTasks, totalTasks } = useTaskPlanningMetrics(currentTurnStartedAtMs);

  const selectedMember = useMemo(() => {
    if (!externalSelectedMemberId) return null;
    return members.find(member => member.member_id === externalSelectedMemberId) || null;
  }, [members, externalSelectedMemberId]);

  const handleSelectMember = (memberId: string) => {
    onMemberSelect?.(memberId);
  };

  return (
    <ExpandedPanel
      activeTab={activeTab}
      onTabChange={tab => onTabChange(tab as TabType)}
      onCollapse={onCollapse ?? (() => {})}
      reviewPanel={reviewPanel}
      selectedArtifactId={selectedArtifactId}
      onArtifactSelect={onArtifactSelect}
      middleTab={{ key: 'team', label: t('team.membersTab'), icon: <img src={teamIcon} width={16} height={16} aria-hidden="true" /> }}
      showMiddleTab
      resolveActiveTab={(tab, artifactsCount, panel) => ((tab === 'artifacts' && artifactsCount === 0) || (tab === 'review' && !panel) ? 'planning' : tab)}
      renderPlanningContent={() => (
        <TaskPlanningPanel
          variant="expanded"
          tasks={teamTasks}
          progressTasks={progressTasks}
          members={members}
          totalTasks={totalTasks}
          completedTasks={completedTasks}
          workflowRuns={workflowRuns}
          sessionId={activeSessionId}
        />
      )}
      renderMiddleTabContent={() => (
        <TeamMembersPanel
          variant="expanded"
          members={members}
          selectedMemberId={selectedMember?.member_id || ''}
          selectedMember={selectedMember}
          activeDetailTab={activeDetailTab}
          historyMessages={historyMessages}
          onSelectMember={handleSelectMember}
          onDetailTabChange={onDetailTabChange}
        />
      )}
      testIdPrefix="team-area"
    />
  );
}

export function TeamArea(props: TeamAreaProps) {
  const { members, historyMessages = [], reviewPanel } = props;
  const activeSessionId = useChatStore(state => state.activeSessionId);
  const teamTasks = useSessionStore(state => state.runtimes[activeSessionId ?? '']?.teamTasks ?? []);
  const teamTaskEvents = useSessionStore(state => state.runtimes[activeSessionId ?? '']?.teamTaskEvents ?? []);
  const teamMemberExecutionEvents = useSessionStore(state => state.runtimes[activeSessionId ?? '']?.teamMemberExecutionEvents ?? []);
  const teamLeaderMemberIds = useSessionStore(state => state.runtimes[activeSessionId ?? '']?.teamLeaderMemberIds ?? []);
  const messages = useChatStore(state => state.runtimes[activeSessionId ?? '']?.messages ?? []);
  const isProcessing = useChatStore(state => state.runtimes[activeSessionId ?? '']?.isProcessing ?? false);
  const currentTurnStartedAtMs = useMemo(() => latestUserTurnStartedAtMs(messages), [messages]);
  const currentQuery = useMemo(() => [...messages].reverse().find(message => message.role === 'user')?.content ?? '', [messages]);
  const invokedMembers = useMemo(
    () => filterInvokedTeamMembers(members, teamTasks, teamTaskEvents, teamMemberExecutionEvents, teamLeaderMemberIds, currentTurnStartedAtMs),
    [currentTurnStartedAtMs, members, teamLeaderMemberIds, teamMemberExecutionEvents, teamTaskEvents, teamTasks],
  );
  const workflowRuns = useMemo(
    () =>
      buildCurrentTurnWorkflowRuns({
        members: invokedMembers,
        tasks: teamTasks,
        taskEvents: teamTaskEvents,
        executionEvents: teamMemberExecutionEvents,
        leaderMemberIds: teamLeaderMemberIds,
        currentTurnStartedAtMs,
        query: currentQuery,
        isProcessing,
      }),
    [currentQuery, currentTurnStartedAtMs, invokedMembers, isProcessing, teamLeaderMemberIds, teamMemberExecutionEvents, teamTaskEvents, teamTasks],
  );

  if (props.expanded) {
    return (
      <ExpandedTeamArea
        members={invokedMembers}
        historyMessages={historyMessages}
        activeTab={props.activeTab}
        activeDetailTab={props.activeDetailTab}
        selectedMemberId={props.selectedMemberId}
        selectedArtifactId={props.selectedArtifactId}
        onTabChange={props.onTabChange}
        onDetailTabChange={props.onDetailTabChange}
        onMemberSelect={props.onMemberSelect}
        onArtifactSelect={props.onArtifactSelect}
        onCollapse={props.onCollapse}
        reviewPanel={reviewPanel}
        currentTurnStartedAtMs={currentTurnStartedAtMs}
        workflowRuns={workflowRuns}
        activeSessionId={activeSessionId ?? ''}
      />
    );
  }
  return <CompactTeamArea members={invokedMembers} currentTurnStartedAtMs={currentTurnStartedAtMs} onExpand={props.onExpand} />;
}
