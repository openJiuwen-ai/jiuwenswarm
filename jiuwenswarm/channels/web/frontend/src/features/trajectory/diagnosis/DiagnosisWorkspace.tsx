/** Diagnosis view: every agent lane of the session beside the diagnosis panel. */

import { memo, useCallback, useMemo, useRef, useState } from 'react';
import { useChatStore } from '../../../stores/chatStore';
import { TrajectoryExplorer } from '../client/TrajectoryExplorer';
import type { TrajectoryKey } from '../client/i18n';
import type { TrajectorySnapshot } from '../trajectory/model';
import type { TrajectoryTimeRange } from '../trajectory/timeline';
import { TeamTrajectoryWorkspace } from '../TeamTrajectoryWorkspace';
import type { TrajectorySubjectGroups } from '../trajectorySubjects';
import { DiagnosisPanel, type DiagnosisSelection } from './DiagnosisPanel';
import { DiagnosisTimeAxis } from './DiagnosisTimeAxis';
import {
  clampSharedViewport,
  collectWallClockSamples,
  compressedToWall,
  compressSharedTimeline,
  diagnosisSelectionRanges,
  wallToCompressed,
  type CompressedTimeline,
  type WallClockSample,
} from './diagnosisTimeSelection';
import css from './DiagnosisWorkspace.module.css';

const EMPTY_KEYS: ReadonlySet<string> = new Set();
const EMPTY_EXCLUDED: ReadonlySet<string> = new Set();
const EMPTY_SNAPSHOT: TrajectorySnapshot = { turns: [] };

export interface DiagnosisWorkspaceProps {
  active: boolean;
  sessionId: string;
  /** Team sessions share lane expansion with the trajectory view. */
  teamMode: boolean;
  groups: TrajectorySubjectGroups;
  snapshots: ReadonlyMap<string, TrajectorySnapshot>;
  messages?: Partial<Record<TrajectoryKey, string>>;
  /** Expanded lane, or `null` when every lane is collapsed. */
  selectedSubjectId: string | null;
  onSelectSubject: (subjectId: string | null) => void;
}

/**
 * Swimlanes fill the top half. The diagnosis bar and the three columns
 * (records, progress, outlets) fill the bottom half.
 */
export const DiagnosisWorkspace = memo(function DiagnosisWorkspace({
  active,
  sessionId,
  teamMode,
  groups,
  snapshots,
  messages,
  selectedSubjectId,
  onSelectSubject,
}: DiagnosisWorkspaceProps) {
  // Single-agent raw view pins `selectedSubjectId` to Main Agent and repairs a
  // null selection immediately, so the diagnosis diagram cannot reuse it: the
  // ledger would stay open and a click on Main Agent could not fold it.
  const [singleAgentSubjectId, setSingleAgentSubjectId] = useState<string | null>(null);
  const [singleAgentSessionId, setSingleAgentSessionId] = useState(sessionId);
  const singleAgentSessionChanged = singleAgentSessionId !== sessionId;
  if (singleAgentSessionChanged) {
    setSingleAgentSessionId(sessionId);
    setSingleAgentSubjectId(null);
  }
  const laneSubjectId = teamMode
    ? selectedSubjectId
    : (singleAgentSessionChanged ? null : singleAgentSubjectId);
  const selectLane = useCallback((subjectId: string | null) => {
    if (teamMode) {
      onSelectSubject(subjectId);
      return;
    }
    setSingleAgentSubjectId(subjectId);
  }, [onSelectSubject, teamMode]);
  const busy = useChatStore((state) => {
    const runtime = state.runtimes[sessionId];
    return runtime !== undefined
      && (Boolean(runtime.isProcessing) || Boolean(runtime.isPaused) || runtime.pendingQuestions.length > 0);
  });
  const [highlightKeys, setHighlightKeys] = useState<ReadonlySet<string>>(EMPTY_KEYS);
  const [sharedRange, setSharedRange] = useState<TrajectoryTimeRange | null>(null);
  const [sharedViewport, setSharedViewport] = useState<TrajectoryTimeRange | null>(null);
  const sharedViewportRef = useRef<TrajectoryTimeRange | null>(null);
  const renderedViewportRef = useRef<TrajectoryTimeRange | null>(null);
  const [excludedSubjectIds, setExcludedSubjectIds] = useState<ReadonlySet<string>>(EMPTY_EXCLUDED);
  const [scopeSessionId, setScopeSessionId] = useState(sessionId);
  const scopeSessionChanged = scopeSessionId !== sessionId;
  if (scopeSessionChanged) {
    setScopeSessionId(sessionId);
    setSharedRange(null);
    setSharedViewport(null);
    sharedViewportRef.current = null;
    renderedViewportRef.current = null;
    setExcludedSubjectIds(EMPTY_EXCLUDED);
  }
  const activeRange = scopeSessionChanged ? null : sharedRange;
  const activeViewport = scopeSessionChanged ? null : sharedViewport;
  const excluded = scopeSessionChanged ? EMPTY_EXCLUDED : excludedSubjectIds;
  const compressed = useMemo(() => {
    const samples: WallClockSample[] = [];
    for (const snapshot of snapshots.values()) {
      samples.push(...collectWallClockSamples(snapshot.turns));
    }
    return compressSharedTimeline(samples, Date.now());
  }, [snapshots]);
  const domain = compressed === null
    ? null
    : { startMs: 0, endMs: compressed.durationMs };
  const mapSharedTime = useMemo(() => {
    if (compressed === null) return undefined;
    const timeline: CompressedTimeline = compressed;
    return (time: number) => wallToCompressed(timeline, time);
  }, [compressed]);
  const subjects = useMemo(() => groups.groups.map(group => ({
    subjectId: group.subject.id,
    subjectLabel: group.label,
  })), [groups]);
  const visualRange = useMemo(() => {
    if (activeRange === null || compressed === null) return null;
    return {
      start: wallToCompressed(compressed, activeRange.start),
      end: wallToCompressed(compressed, activeRange.end),
    };
  }, [activeRange, compressed]);
  const selection = useMemo<DiagnosisSelection | null>(() => {
    if (activeRange === null || compressed === null || visualRange === null) return null;
    return {
      domainStartMs: 0,
      startMs: visualRange.start,
      endMs: visualRange.end,
      ranges: diagnosisSelectionRanges({
        range: { startMs: activeRange.start, endMs: activeRange.end },
        subjects,
        excludedSubjectIds: excluded,
      }),
    };
  }, [activeRange, compressed, excluded, subjects, visualRange]);
  const sharedTimeDomain = domain === null ? null : { start: domain.startMs, end: domain.endMs };
  const visibleViewport = useMemo(
    () => clampSharedViewport(activeViewport, 0, compressed?.durationMs ?? 0),
    [activeViewport, compressed],
  );
  if (sharedViewportRef.current === renderedViewportRef.current) {
    sharedViewportRef.current = visibleViewport;
  }
  renderedViewportRef.current = visibleViewport;
  const updateSharedViewport = useCallback((next: TrajectoryTimeRange | null) => {
    const clamped = clampSharedViewport(next, 0, compressed?.durationMs ?? 0);
    sharedViewportRef.current = clamped;
    setSharedViewport(clamped);
  }, [compressed]);
  const updateSharedRange = useCallback((range: TrajectoryTimeRange | null) => {
    if (range === null || compressed === null) {
      setSharedRange(null);
      return;
    }
    const start = compressedToWall(compressed, range.start);
    const end = compressedToWall(compressed, range.end);
    setSharedRange(end < start ? null : { start, end });
  }, [compressed]);
  const toggleAgent = useCallback((subjectId: string) => {
    setExcludedSubjectIds((current) => {
      const next = new Set(current);
      if (next.has(subjectId)) next.delete(subjectId);
      else next.add(subjectId);
      return next;
    });
  }, []);
  const isAgentIncluded = useCallback(
    (subjectId: string) => !excluded.has(subjectId),
    [excluded],
  );
  const clearSelection = useCallback(() => {
    setSharedRange(null);
  }, []);

  return (
    <div className={css.root} data-testid="trajectory-diagnosis-workspace">
      <div className={css.lanes}>
        <TeamTrajectoryWorkspace
          active={active}
          groups={groups}
          {...(messages === undefined ? {} : { messages })}
          selectedSubjectId={laneSubjectId}
          onSelectSubject={selectLane}
          showScaleControls={false}
          toolbarTrailing={(
            <DiagnosisTimeAxis
              domain={domain}
              viewport={visibleViewport}
              range={visualRange}
              onRangeChange={updateSharedRange}
            />
          )}
          agentSelection={{ isIncluded: isAgentIncluded, onToggle: toggleAgent }}
          memberView={(subjectId, { expanded, viewState, toolbarAddon, onOverviewActivate }) => (
            <TrajectoryExplorer
              active={active}
              snapshot={snapshots.get(subjectId) ?? EMPTY_SNAPSHOT}
              {...(messages === undefined ? {} : { messages })}
              colorMode="light"
              displayMode={expanded ? 'full' : 'overview'}
              showToolbarViewControls={false}
              toolbarAddon={toolbarAddon}
              viewState={{
                ...viewState,
                actualDuration: true,
                actualTime: true,
                tokenView: false,
              }}
              onOverviewActivate={onOverviewActivate}
              highlightStepKeys={highlightKeys}
              sharedTimeDomain={sharedTimeDomain}
              {...(mapSharedTime === undefined ? {} : { mapSharedTime })}
              timelineRange={excluded.has(subjectId) ? null : visualRange}
              onTimelineRangeChange={updateSharedRange}
              lockTimelineViewport
              sharedViewport={visibleViewport}
              onSharedViewportChange={updateSharedViewport}
              sharedViewportRef={sharedViewportRef}
              {...(expanded ? { className: css.explorer } : {})}
            />
          )}
        />
      </div>
      <div className={css.panel}>
        <DiagnosisPanel
          sessionId={sessionId}
          busy={busy}
          selection={selection}
          onClearSelection={clearSelection}
          onHighlightChange={setHighlightKeys}
        />
      </div>
    </div>
  );
});
