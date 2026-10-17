/** Diagnosis view: every agent lane of the session beside the diagnosis panel. */

import { memo, useCallback, useRef, useState } from 'react';
import { useChatStore } from '../../../stores/chatStore';
import { TrajectoryExplorer, type TrajectoryWallClockSelection } from '../client/TrajectoryExplorer';
import type { TrajectoryKey } from '../client/i18n';
import type { TrajectorySnapshot } from '../trajectory/model';
import { TeamTrajectoryWorkspace } from '../TeamTrajectoryWorkspace';
import type { TrajectorySubjectGroups } from '../trajectorySubjects';
import { DiagnosisPanel, type DiagnosisSelection } from './DiagnosisPanel';
import css from './DiagnosisWorkspace.module.css';

const EMPTY_KEYS: ReadonlySet<string> = new Set();
const EMPTY_SNAPSHOT: TrajectorySnapshot = { turns: [] };

export interface DiagnosisWorkspaceProps {
  active: boolean;
  sessionId: string;
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
  groups,
  snapshots,
  messages,
  selectedSubjectId,
  onSelectSubject,
}: DiagnosisWorkspaceProps) {
  const busy = useChatStore((state) => {
    const runtime = state.runtimes[sessionId];
    return runtime !== undefined
      && (Boolean(runtime.isProcessing) || Boolean(runtime.isPaused) || runtime.pendingQuestions.length > 0);
  });
  const [selection, setSelection] = useState<DiagnosisSelection | null>(null);
  const [highlightKeys, setHighlightKeys] = useState<ReadonlySet<string>>(EMPTY_KEYS);
  const [clearTokens, setClearTokens] = useState<Readonly<Record<string, number>>>({});
  const selectionOwnerRef = useRef<string | null>(null);

  const reportSelection = useCallback((
    subjectId: string,
    subjectLabel: string,
    reported: TrajectoryWallClockSelection | null,
  ) => {
    if (reported === null) {
      // Only the lane that owns the selection may clear it.
      if (selectionOwnerRef.current === subjectId) {
        selectionOwnerRef.current = null;
        setSelection(null);
      }
      return;
    }
    const previousOwner = selectionOwnerRef.current;
    selectionOwnerRef.current = subjectId;
    if (previousOwner !== null && previousOwner !== subjectId) {
      // A new lane takes over; the earlier lane forgets its own drag range.
      setClearTokens(tokens => ({ ...tokens, [previousOwner]: (tokens[previousOwner] ?? 0) + 1 }));
    }
    setSelection({ subjectId, subjectLabel, startMs: reported.startMs, endMs: reported.endMs });
  }, []);

  const clearSelection = useCallback(() => {
    const owner = selectionOwnerRef.current;
    selectionOwnerRef.current = null;
    setSelection(null);
    if (owner !== null) {
      setClearTokens(tokens => ({ ...tokens, [owner]: (tokens[owner] ?? 0) + 1 }));
    }
  }, []);

  return (
    <div className={css.root} data-testid="trajectory-diagnosis-workspace">
      <div className={css.lanes}>
        <TeamTrajectoryWorkspace
          active={active}
          groups={groups}
          {...(messages === undefined ? {} : { messages })}
          selectedSubjectId={selectedSubjectId}
          onSelectSubject={onSelectSubject}
          memberView={(subjectId, { expanded, viewState, toolbarAddon, onOverviewActivate }) => {
            const group = groups.byId.get(subjectId);
            const label = group?.label ?? subjectId;
            return (
              <TrajectoryExplorer
                active={active}
                snapshot={snapshots.get(subjectId) ?? EMPTY_SNAPSHOT}
                {...(messages === undefined ? {} : { messages })}
                colorMode="light"
                displayMode={expanded ? 'full' : 'overview'}
                showToolbarViewControls={false}
                toolbarAddon={toolbarAddon}
                viewState={viewState}
                onOverviewActivate={onOverviewActivate}
                highlightStepKeys={highlightKeys}
                onTimelineSelectionChange={reported => reportSelection(subjectId, label, reported)}
                clearSelectionToken={clearTokens[subjectId] ?? 0}
                {...(expanded ? { className: css.explorer } : {})}
              />
            );
          }}
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
