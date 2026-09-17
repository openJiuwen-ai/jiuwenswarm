/**
 * TeamArea component - cluster mode task overview and member execution detail.
 */

import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { FileCheck2, FileText, Minimize2 } from 'lucide-react';
import type { ReactNode } from 'react';
import { useChatStore, useSessionStore, useTodoStore } from '../../stores';
import type { Message } from '../../types';
import { ArtifactsPanel, useSessionArtifactsCount } from '../ArtifactsPanel';
import { TaskPlanningPanel } from './TaskPlanningPanel';
import { TeamMembersPanel } from './TeamMembersPanel';
import teamProcessIcon from '../../assets/team-process.svg';
import teamIcon from '../../assets/team.svg';
import {
  normalizeTaskStatus,
  type TabType,
  type TeamDetailTab,
  type TeamAreaProps,
  type TeamMember,
} from './shared';
import { getTasksForCurrentProgress } from '../../features/teamTaskProgressBaseline';

function useTaskPlanningMetrics() {
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const todos = useTodoStore((s) => s.runtimes[activeSessionId ?? '']?.todos ?? []);
  const teamTaskEvents = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.teamTaskEvents ?? []);
  const teamTasks = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.teamTasks ?? []);
  const taskProgressBaseline = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.teamTaskProgressBaseline);
  const progressTasks = useMemo(
    () => taskProgressBaseline
      ? getTasksForCurrentProgress(teamTasks, taskProgressBaseline)
      : teamTasks,
    [taskProgressBaseline, teamTasks]
  );

  const totalTasks = useMemo(() => {
    if (teamTasks.length > 0) return teamTasks.length;
    const taskIds = new Set<string>();
    todos.forEach((todo) => taskIds.add(todo.id));
    teamTaskEvents.forEach((event) => {
      if (event.task_id) taskIds.add(event.task_id);
    });
    return taskIds.size;
  }, [teamTaskEvents, teamTasks.length, todos]);

  const completedTasks = useMemo(() => {
    if (teamTasks.length > 0) {
      return teamTasks.filter((task) => task.status === 'completed').length;
    }
    const completed = new Set<string>();
    todos.forEach((todo) => {
      if (normalizeTaskStatus(todo.status) === 'completed') completed.add(todo.id);
    });
    teamTaskEvents.forEach((event) => {
      if (event.task_id && normalizeTaskStatus(event.status, event.type) === 'completed') {
        completed.add(event.task_id);
      }
    });
    return completed.size;
  }, [teamTaskEvents, teamTasks, todos]);

  return { completedTasks, progressTasks, teamTasks, totalTasks };
}

function CompactTeamArea({
  members,
  onExpand,
}: {
  members: TeamMember[];
  onExpand?: (tab: TabType, memberId?: string) => void;
}) {
  const { completedTasks, progressTasks, teamTasks, totalTasks } = useTaskPlanningMetrics();

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
        onMemberClick={(memberId) => onExpand?.('team', memberId)}
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
}) {
  const { t } = useTranslation();
  const { completedTasks, progressTasks, teamTasks, totalTasks } = useTaskPlanningMetrics();
  const artifactsCount = useSessionArtifactsCount();
  const resolvedTab =
    (activeTab === 'artifacts' && artifactsCount === 0) ||
    (activeTab === 'review' && !reviewPanel)
      ? 'planning'
      : activeTab;

  const selectedMember = useMemo(() => {
    if (!externalSelectedMemberId) return null;
    return members.find((member) => member.member_id === externalSelectedMemberId) || null;
  }, [members, externalSelectedMemberId]);

  const handleSelectMember = (memberId: string) => {
    onMemberSelect?.(memberId);
  };

  const tabs = [
    {
      key: 'planning',
      label: t('team.planning.tab'),
      count: completedTasks + '/' + totalTasks,
      icon: <img src={teamProcessIcon} width={16} height={16} />,
    },
    {
      key: 'team',
      label: t('team.membersTab'),
      icon: <img src={teamIcon} width={16} height={16} />,
    },
    ...(artifactsCount > 0
      ? [{
          key: 'artifacts' as const,
          label: t('artifacts.tab'),
          count: artifactsCount,
          icon: <FileText size={16} />,
        }]
      : []),
    ...(reviewPanel ? [{ key: 'review' as const, label: t('codeMode.review'), icon: <FileCheck2 size={16} /> }] : []),
  ];

  return (
    <div className="flex h-full flex-col overflow-hidden bg-card" data-testid="team-area-expanded-root">
      <div className="flex shrink-0 items-center justify-between px-6 py-4 bg-card border-b border-border" data-testid="team-area-tab-bar">
        <div className="flex items-center gap-2">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              data-testid="team-area-tab-button"
              data-variant={tab.key}
              className={`h-9 rounded-lg px-4 text-sm  flex items-center gap-2 ${
                resolvedTab === tab.key
                  ? 'bg-secondary font-medium text-text'
                  : 'text-text-muted hover:bg-secondary/50 hover:text-text'
              }`}
              onClick={() => onTabChange(tab.key as TabType)}
            >
              {tab.icon}
              {tab.label}{'count' in tab ? ' (' + tab.count + ')' : ''}
            </button>
          ))}
        </div>

        <button
          onClick={onCollapse}
          data-testid="team-area-collapse-button"
          className="rounded p-2 text-text-muted  hover:bg-secondary hover:text-text"
          title={t('team.collapse')}
        >
          <Minimize2 size={12} />
        </button>
      </div>

      <div className="flex min-h-0 flex-1 overflow-hidden" data-testid="team-area-expanded-body">
        {resolvedTab === 'planning' ? (
          <TaskPlanningPanel
            variant="expanded"
            tasks={teamTasks}
            progressTasks={progressTasks}
            members={members}
            totalTasks={totalTasks}
            completedTasks={completedTasks}
          />
        ) : resolvedTab === 'artifacts' ? (
          <div className="flex min-w-0 flex-1 overflow-hidden">
            <ArtifactsPanel selectedArtifactId={selectedArtifactId} onSelectArtifact={onArtifactSelect} />
          </div>
        ) : resolvedTab === 'review' && reviewPanel ? (
          <div className="flex min-w-0 flex-1 overflow-hidden">{reviewPanel}</div>
        ) : (
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
      </div>
    </div>
  );
}

export function TeamArea(props: TeamAreaProps) {
  const { members, historyMessages = [], reviewPanel } = props;

  if (props.expanded) {
    return (
      <ExpandedTeamArea
        members={members}
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
      />
    );
  }
  return <CompactTeamArea members={members} onExpand={props.onExpand} />;
}
