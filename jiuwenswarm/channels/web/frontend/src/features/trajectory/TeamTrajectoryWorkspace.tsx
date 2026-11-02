/** Team member navigation using the native trajectory overview and ledger. */

import {
  memo,
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useTranslation } from 'react-i18next';
import type { TrajectoryViewState } from './client/TrajectoryExplorer';
import { trajectoryTranslator, type TrajectoryKey } from './client/i18n';
import { TrajectoryToolbar } from './client/TrajectoryToolbar';
import {
  buildTeamMemberLanes,
  type TeamMemberLaneModel,
} from './teamTrajectoryLanes';
import {
  buildScheduledTeamLaneSections,
  scheduledMemberDisplayName,
  type ScheduledReviewerLane,
  type ScheduledReviewerTask,
} from './scheduledTrajectoryLanes';
import type { TrajectorySubjectGroups } from './trajectorySubjects';
import {
  ScheduledControlTimeline,
  type ScheduledControlTimelineProps,
} from './ScheduledControlTimeline';
import css from './TeamTrajectoryWorkspace.module.css';

export interface TeamMemberViewContext {
  expanded: boolean;
  viewState: TrajectoryViewState;
  toolbarAddon: ReactNode;
  onOverviewActivate: () => void;
}

export interface TeamTrajectoryWorkspaceProps {
  active: boolean;
  groups: TrajectorySubjectGroups;
  messages?: Partial<Record<TrajectoryKey, string>>;
  /** Currently expanded member subject id, or `null` when every lane is collapsed. */
  selectedSubjectId: string | null;
  onSelectSubject: (subjectId: string | null) => void;
  /** Render one native overview, plus its ledger when the member is expanded. */
  memberView: (subjectId: string, context: TeamMemberViewContext) => ReactNode;
  /** Hide the duration and token switches. Diagnosis locks every lane to wall-clock time. */
  showScaleControls?: boolean;
  /** Drop the workspace toolbar when the host draws its own. */
  hideToolbar?: boolean;
  /** Host-owned scale and fold state. Replaces the toolbar's own switches. */
  externalViewState?: TrajectoryViewState;
  /** Full-width control under the toolbar, used by the diagnosis time axis. */
  toolbarTrailing?: ReactNode;
  /** `main` task events rendered above the lanes on the same shared axis. */
  controlTimeline?: Omit<
    ScheduledControlTimelineProps,
    'reviewRunCount' | 'memberDisplayName' | 'compact' | 'compactKey'
  >;
  /**
   * Diagnosis pick controls. Absent on the raw team trajectory.
   *
   * The include checkbox appears for a shared axis brush, or when this lane
   * already owns a window — uncheck clears that pick. Hidden for a full-
   * trajectory pick and when nothing is selected, so it is not mistaken for
   * "select all". Whole-lane selection is the separate select-all control.
   */
  agentSelection?: {
    isIncluded: (subjectId: string) => boolean;
    onToggle: (subjectId: string) => void;
    /** When false, hide the include checkbox for this agent. */
    showIncludeToggle: (subjectId: string) => boolean;
    /** Select or clear every timed node on this agent's lane. */
    onSelectAll: (subjectId: string) => void;
    /** Whether this agent currently has any timed nodes to select. */
    canSelectAll: (subjectId: string) => boolean;
    /** Whether the pick already covers this agent's whole timed span. */
    isSelectAllActive: (subjectId: string) => boolean;
    /** Checked agents that currently own a time window. */
    isHighlighted?: (subjectId: string) => boolean;
  };
}

function MemberToolbarControl({
  lane,
  expanded,
  onClick,
  included,
  onToggleIncluded,
  showIncludeToggle,
  onSelectAll,
  canSelectAll,
  selectAllActive,
}: {
  lane: TeamMemberLaneModel;
  expanded: boolean;
  onClick: () => void;
  included?: boolean;
  onToggleIncluded?: () => void;
  showIncludeToggle?: boolean;
  onSelectAll?: () => void;
  canSelectAll?: boolean;
  selectAllActive?: boolean;
}) {
  const { t } = useTranslation();
  const fullLabel = 'fullLabel' in lane && typeof lane.fullLabel === 'string'
    ? lane.fullLabel
    : lane.label;
  const scheduledLane = 'taskId' in lane ? lane as ScheduledReviewerLane : undefined;
  const decision = scheduledLane?.decision;
  const pendingDecisionLabel = scheduledLane?.reviewer.toLowerCase().includes('inspector')
    ? t('trajectory.team.schedule.decision.pendingScore')
    : t('trajectory.team.schedule.decision.pending');
  const decisionLabel = decision === undefined
    ? undefined
    : decision.kind === 'pass'
      ? t('trajectory.team.schedule.decision.pass')
      : decision.kind === 'fail'
        ? t('trajectory.team.schedule.decision.fail')
        : decision.kind === 'score'
          ? t('trajectory.team.schedule.decision.score', {
            value: Number.isInteger(decision.value) ? decision.value : decision.value.toFixed(2),
          })
          : pendingDecisionLabel;
  const button = (
    <button
      type="button"
      className={css.memberControl}
      aria-expanded={expanded}
      aria-label={[
        fullLabel,
        decisionLabel,
        `${lane.traceCount} traces, ${lane.recordCount} records`,
      ].filter(Boolean).join(' — ')}
      data-status={lane.status}
      data-member-lane={lane.subjectId}
      data-review-decision={decision?.kind}
      onClick={onClick}
    >
      <span className={`${css.statusDot} ${css[`status_${lane.status}`]}`} aria-hidden="true" />
      <span className={css.memberName} title={fullLabel}>{lane.label}</span>
      {decisionLabel === undefined ? null : (
        <span
          className={css.reviewDecision}
          data-decision={decision?.kind}
        >
          {decisionLabel}
        </span>
      )}
      <span
        className={`${css.chevron} ${expanded ? css.chevronExpanded : ''}`}
        aria-hidden="true"
      >
        {'>'}
      </span>
    </button>
  );
  if (onSelectAll === undefined) return button;
  const selectAllEnabled = canSelectAll === true;
  const allSelected = selectAllActive === true;
  const selectAllLabel = allSelected
    ? t('trajectory.analysis.selection.clearSelectAllAgent', { name: fullLabel })
    : t('trajectory.analysis.selection.selectAllAgent', { name: fullLabel });
  const includeLabel = t('trajectory.diagnosis.includeAgent', { name: fullLabel });
  const includeTitle = t('trajectory.diagnosis.includeAgentHint', { name: fullLabel });
  const showInclude = showIncludeToggle === true && onToggleIncluded !== undefined;
  return (
    <span className={css.memberCluster}>
      {showInclude ? (
        <input
          type="checkbox"
          className={css.memberCheck}
          checked={included === true}
          aria-label={includeLabel}
          title={includeTitle}
          data-testid="trajectory-analysis-agent-checkbox"
          data-variant={lane.subjectId}
          onChange={() => onToggleIncluded()}
        />
      ) : null}
      {button}
      <button
        type="button"
        className={`${css.selectAll} ${allSelected ? css.selectAllActive : ''}`}
        disabled={!selectAllEnabled}
        aria-pressed={allSelected}
        aria-label={selectAllLabel}
        title={selectAllLabel}
        data-testid="trajectory-analysis-agent-select-all"
        data-variant={lane.subjectId}
        onClick={onSelectAll}
      >
        {allSelected
          ? t('trajectory.analysis.selection.clearSelectAll')
          : t('trajectory.analysis.selection.selectAll')}
      </button>
    </span>
  );
}

/**
 * Keep every member on the same native trajectory surface. Collapsed members
 * show the toolbar and timeline; the selected member appends its record ledger
 * without creating a second overview.
 */
export const TeamTrajectoryWorkspace = memo(function TeamTrajectoryWorkspace({
  active,
  groups,
  messages,
  selectedSubjectId,
  onSelectSubject,
  memberView,
  showScaleControls = true,
  hideToolbar = false,
  externalViewState,
  toolbarTrailing,
  controlTimeline,
  agentSelection,
}: TeamTrajectoryWorkspaceProps) {
  const { t } = useTranslation();
  const flatLanes = useMemo(() => buildTeamMemberLanes(groups.groups), [groups.groups]);
  const scheduledMode = controlTimeline !== undefined && controlTimeline.model.tasks.length > 0;
  const laneSections = useMemo(
    () => (scheduledMode
      ? buildScheduledTeamLaneSections(groups.groups, controlTimeline?.model)
      : null),
    [controlTimeline?.model, groups.groups, scheduledMode],
  );
  const reviewRunCount = laneSections?.reviewRunCount ?? flatLanes.filter(
    lane => lane.subjectId.startsWith('team-reviewer:'),
  ).length;
  const memberDisplayName = useCallback(
    (memberName: string) => scheduledMemberDisplayName(
      memberName,
      laneSections?.members ?? flatLanes,
    ),
    [flatLanes, laneSections],
  );
  const translate = useMemo(() => trajectoryTranslator(messages), [messages]);
  const [actualDuration, setActualDuration] = useState(false);
  const [actualTime, setActualTime] = useState(false);
  const [tokenView, setTokenView] = useState(false);
  const [turnsCollapsed, setTurnsCollapsed] = useState(false);
  const [callsCollapsed, setCallsCollapsed] = useState(false);
  const lanesRef = useRef<HTMLDivElement>(null);
  const laneRowsRef = useRef(new Map<string, HTMLDivElement>());
  const [pinToken, setPinToken] = useState(0);
  const [collapsedTasks, setCollapsedTasks] = useState<ReadonlySet<string>>(() => new Set());
  const localViewState = useMemo<TrajectoryViewState>(() => ({
    actualDuration,
    actualTime,
    tokenView,
    turnsCollapsed,
    callsCollapsed,
  }), [
    actualDuration,
    actualTime,
    callsCollapsed,
    tokenView,
    turnsCollapsed,
  ]);
  const viewState = externalViewState ?? localViewState;
  useLayoutEffect(() => {
    if (selectedSubjectId === null) return;
    const lanesElement = lanesRef.current;
    const rowElement = laneRowsRef.current.get(selectedSubjectId);
    if (lanesElement === null || rowElement === undefined) return;
    const lanesBounds = lanesElement.getBoundingClientRect();
    const rowBounds = rowElement.getBoundingClientRect();
    lanesElement.scrollTop += rowBounds.top - lanesBounds.top;
  }, [pinToken, selectedSubjectId]);

  const renderLane = (lane: TeamMemberLaneModel) => {
    const expanded = lane.subjectId === selectedSubjectId;
    const select = () => onSelectSubject(expanded ? null : lane.subjectId);
    const expand = () => {
      if (!expanded) onSelectSubject(lane.subjectId);
      setPinToken(value => value + 1);
    };
    const scheduled = 'taskId' in lane ? lane as ScheduledReviewerLane : undefined;
    return (
      <div
        ref={(element) => {
          if (element === null) laneRowsRef.current.delete(lane.subjectId);
          else laneRowsRef.current.set(lane.subjectId, element);
        }}
        key={lane.subjectId}
        role="listitem"
        className={`${css.laneRow} ${expanded ? css.laneRowExpanded : ''}`}
        data-member-subject={lane.subjectId}
        data-review-task={scheduled?.taskId}
        data-review-round={scheduled?.round}
        data-review-attempt={scheduled?.attempt}
        data-highlighted={agentSelection?.isHighlighted?.(lane.subjectId) === true ? 'true' : 'false'}
        data-testid="team-trajectory-lane-row"
      >
        {memberView(lane.subjectId, {
          expanded,
          viewState,
          toolbarAddon: (
            <MemberToolbarControl
              lane={lane}
              expanded={expanded}
              onClick={select}
              {...(agentSelection === undefined
                ? {}
                : {
                  included: agentSelection.isIncluded(lane.subjectId),
                  onToggleIncluded: () => agentSelection.onToggle(lane.subjectId),
                  showIncludeToggle: agentSelection.showIncludeToggle(lane.subjectId),
                  onSelectAll: () => agentSelection.onSelectAll(lane.subjectId),
                  canSelectAll: agentSelection.canSelectAll(lane.subjectId),
                  selectAllActive: agentSelection.isSelectAllActive(lane.subjectId),
                })}
            />
          ),
          onOverviewActivate: expand,
        })}
      </div>
    );
  };

  const renderTask = (task: ScheduledReviewerTask) => {
    const collapsed = collapsedTasks.has(task.taskId);
    const taskPhase = task.phase === undefined
      ? null
      : t(`trajectory.team.schedule.phase.${task.phase}`);
    const taskAssignee = task.assignee === undefined
      ? null
      : `${t('trajectory.team.schedule.assignee')}: ${memberDisplayName(task.assignee)}`;
    return (
      <div
        className={css.laneSection}
        key={`task:${task.taskId}`}
        data-testid="team-trajectory-review-task-group"
        data-variant={task.taskId}
        data-task-index={task.taskIndex}
      >
        <button
          type="button"
          className={css.sectionHeader}
          aria-expanded={!collapsed}
          onClick={() => setCollapsedTasks((current) => {
            const next = new Set(current);
            if (next.has(task.taskId)) next.delete(task.taskId);
            else next.add(task.taskId);
            return next;
          })}
        >
          <span className={`${css.sectionChevron} ${collapsed ? '' : css.sectionChevronOpen}`}>
            {'>'}
          </span>
          <span className={css.sectionTitle}>
            <span className={css.taskIndex}>T{task.taskIndex}</span>
            <strong className={css.taskName}>{task.taskId}</strong>
          </span>
          <span className={css.sectionMeta}>
            {[
              taskPhase,
              taskAssignee,
              t('trajectory.team.schedule.reviewCount', { count: task.runCount }),
            ].filter(Boolean).join(' · ')}
          </span>
        </button>
        {collapsed ? null : task.rounds.map(round => (
          <div className={css.roundSection} key={`${task.taskId}:r${round.round}`}>
            <div className={css.roundHeader} data-testid="team-trajectory-review-round">
              <span>R{round.round}</span>
              <span>
                {t('trajectory.team.schedule.reviewCount', { count: round.lanes.length })}
              </span>
            </div>
            {round.lanes.map(renderLane)}
          </div>
        ))}
      </div>
    );
  };

  return (
    <div
      className={`${css.root} jiuwenTrajectoryTheme`}
      data-active={active ? 'true' : 'false'}
      data-trajectory-theme="light"
      data-scheduled-control={scheduledMode ? 'true' : 'false'}
      data-testid="team-trajectory-workspace"
    >
      {hideToolbar ? null : <TrajectoryToolbar
        showSearch={false}
        showScaleControls={showScaleControls}
        actualDuration={actualDuration}
        onActualDurationChange={(value) => {
          setActualDuration(value);
          setTokenView(false);
        }}
        actualTime={actualTime}
        onActualTimeChange={setActualTime}
        tokenView={tokenView}
        onTokenViewChange={setTokenView}
        allTurnsCollapsed={turnsCollapsed}
        onToggleAllTurns={() => setTurnsCollapsed(value => !value)}
        allAssistantsCollapsed={callsCollapsed}
        onToggleAllAssistants={() => setCallsCollapsed(value => !value)}
        searchQuery=""
        onSearchQueryChange={() => {}}
        t={translate}
      />}
      {toolbarTrailing === undefined || toolbarTrailing === null
        ? null
        : <div className={css.axisHost}>{toolbarTrailing}</div>}
      {controlTimeline === undefined ? null : (
        <div className={css.controlTimeline}>
          <ScheduledControlTimeline
            {...controlTimeline}
            reviewRunCount={reviewRunCount}
            memberDisplayName={memberDisplayName}
            compact={selectedSubjectId !== null}
            compactKey={selectedSubjectId}
          />
        </div>
      )}
      <div
        ref={lanesRef}
        className={css.lanes}
        role="list"
        aria-label={t('trajectory.team.memberLanes')}
        data-testid="team-trajectory-lanes"
      >
        {scheduledMode && laneSections !== null ? (
          <>
            {laneSections.members.length === 0 ? null : (
              <div className={css.laneSection} data-testid="team-trajectory-member-group">
                <div className={css.memberSectionHeader}>
                  <strong>{t('trajectory.team.members')}</strong>
                  <span>{laneSections.members.length}</span>
                </div>
                {laneSections.members.map(renderLane)}
              </div>
            )}
            {laneSections.tasks.map(renderTask)}
            {laneSections.ungroupedReviewers.length === 0 ? null : (
              <div className={css.laneSection}>
                {laneSections.ungroupedReviewers.map(renderLane)}
              </div>
            )}
          </>
        ) : (
          flatLanes.map(renderLane)
        )}
      </div>
    </div>
  );
});
