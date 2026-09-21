// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/** Team member navigation using the native trajectory overview and ledger. */

import { memo, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import type { TrajectoryViewState } from './client/TrajectoryExplorer';
import { trajectoryTranslator, type TrajectoryKey } from './client/i18n';
import { TrajectoryToolbar } from './client/TrajectoryToolbar';
import { buildTeamMemberLanes, type TeamMemberLaneModel } from './teamTrajectoryLanes';
import type { TrajectorySubjectGroups } from './trajectorySubjects';
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
}

function MemberToolbarControl({
  lane,
  expanded,
  onClick,
}: {
  lane: TeamMemberLaneModel;
  expanded: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={css.memberControl}
      aria-expanded={expanded}
      aria-label={`${lane.label} — ${lane.traceCount} traces, ${lane.recordCount} records`}
      data-status={lane.status}
      data-member-lane={lane.subjectId}
      onClick={onClick}
    >
      <span className={`${css.statusDot} ${css[`status_${lane.status}`]}`} aria-hidden="true" />
      <span className={css.memberName}>{lane.label}</span>
      <span className={css.chevron} aria-hidden="true">{expanded ? '▾' : '▸'}</span>
    </button>
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
}: TeamTrajectoryWorkspaceProps) {
  const { t } = useTranslation();
  const lanes = buildTeamMemberLanes(groups.groups);
  const translate = useMemo(() => trajectoryTranslator(messages), [messages]);
  const [actualDuration, setActualDuration] = useState(false);
  const [actualTime, setActualTime] = useState(false);
  const [tokenView, setTokenView] = useState(false);
  const [turnsCollapsed, setTurnsCollapsed] = useState(false);
  const [callsCollapsed, setCallsCollapsed] = useState(false);
  const lanesRef = useRef<HTMLDivElement>(null);
  const laneRowsRef = useRef(new Map<string, HTMLDivElement>());
  const viewState = useMemo<TrajectoryViewState>(() => ({
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
  useLayoutEffect(() => {
    if (selectedSubjectId === null) return;
    const lanesElement = lanesRef.current;
    const rowElement = laneRowsRef.current.get(selectedSubjectId);
    if (lanesElement === null || rowElement === undefined) return;
    const lanesBounds = lanesElement.getBoundingClientRect();
    const rowBounds = rowElement.getBoundingClientRect();
    lanesElement.scrollTop += rowBounds.top - lanesBounds.top;
  }, [selectedSubjectId]);

  return (
    <div
      className={`${css.root} jiuwenTrajectoryTheme`}
      data-active={active ? 'true' : 'false'}
      data-trajectory-theme="light"
      data-testid="team-trajectory-workspace"
    >
      <TrajectoryToolbar
        showSearch={false}
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
      />
      <div
        ref={lanesRef}
        className={css.lanes}
        role="list"
        aria-label={t('trajectory.team.memberLanes')}
        data-testid="team-trajectory-lanes"
      >
        {lanes.map((lane) => {
          const expanded = lane.subjectId === selectedSubjectId;
          const select = () => onSelectSubject(expanded ? null : lane.subjectId);
          const expand = () => {
            if (!expanded) onSelectSubject(lane.subjectId);
          };
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
              data-testid="team-trajectory-lane-row"
            >
              {memberView(lane.subjectId, {
                expanded,
                viewState,
                toolbarAddon: (
                  <MemberToolbarControl lane={lane} expanded={expanded} onClick={select} />
                ),
                onOverviewActivate: expand,
              })}
            </div>
          );
        })}
      </div>
    </div>
  );
});
