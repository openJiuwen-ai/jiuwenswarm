/** Trajectory analysis page: the trajectory and its diagnosis as two sub-pages of one session view. */

import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronLeft } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useChatStore } from '../../stores/chatStore';
import {
  buildDiagnosisScope,
  EMPTY_DIAGNOSIS_SELECTION,
  hasSelection,
  selectionRanges,
  type DiagnosisSelectionState,
} from './diagnosis/diagnosisSelection';
import { canSubmitDiagnosis, highlightKeysOf } from './diagnosis/diagnosisState';
import type { RootCause } from './diagnosis/diagnosisTypes';
import { DiagnosisSelectionBar } from './diagnosis/DiagnosisSelectionBar';
import { DiagnosisSubpage } from './diagnosis/DiagnosisSubpage';
import { useDiagnosisSession } from './diagnosis/useDiagnosisSession';
import { TrajectoryAnalysisToolbar } from './TrajectoryAnalysisToolbar';
import {
  TrajectoryPanel,
  type TrajectoryLaneScale,
  type TrajectoryPanelSubject,
  type TrajectoryPanelSummary,
} from './TrajectoryPanel';
import css from './TrajectoryAnalysisPage.module.css';

export type TrajectoryAnalysisSubpage = 'trajectory' | 'diagnosis';

export interface TrajectoryAnalysisPageProps {
  /** Whether the page is on screen; a hidden page defers its re-projection. */
  active: boolean;
  mode?: string;
  sessionId: string;
  /** Leave the page and return to the conversation. */
  onBack: () => void;
}

interface FocusedCause {
  causeId: string;
  keys: ReadonlySet<string>;
}

export const TrajectoryAnalysisPage = memo(function TrajectoryAnalysisPage({
  active,
  mode = 'agent',
  sessionId,
  onBack,
}: TrajectoryAnalysisPageProps) {
  const { t } = useTranslation();
  const [subpage, setSubpage] = useState<TrajectoryAnalysisSubpage>('trajectory');
  const [subjects, setSubjects] = useState<ReadonlyArray<TrajectoryPanelSubject>>([]);
  const [timelineAvailable, setTimelineAvailable] = useState(false);
  const [focused, setFocused] = useState<FocusedCause | null>(null);
  // Selection mode: the trajectory turns into a picker and a bar asks how to run.
  const [selecting, setSelecting] = useState(false);
  const [selectionState, setSelectionState] = useState<DiagnosisSelectionState>(EMPTY_DIAGNOSIS_SELECTION);
  const [fullScope, setFullScope] = useState(false);
  const [note, setNote] = useState('');
  const [scale, setScale] = useState<TrajectoryLaneScale>('duration');
  const [turnsCollapsed, setTurnsCollapsed] = useState(false);
  const [callsCollapsed, setCallsCollapsed] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [summary, setSummary] = useState<TrajectoryPanelSummary | null>(null);
  const exportTrajectoryRef = useRef<(() => void) | null>(null);
  const [pageSessionId, setPageSessionId] = useState(sessionId);
  if (pageSessionId !== sessionId) {
    setPageSessionId(sessionId);
    setSubpage('trajectory');
    setFocused(null);
    setSelecting(false);
    setScale('duration');
    setSearchQuery('');
    setSummary(null);
  }
  const session = useDiagnosisSession(sessionId);
  const { record, start, dismissNotice } = session;
  const recordId = record?.diagnosis_id ?? null;
  // Emphasised steps belong to one record; switching records drops them.
  const [focusRecordId, setFocusRecordId] = useState(recordId);
  if (focusRecordId !== recordId) {
    setFocusRecordId(recordId);
    setFocused(null);
  }
  const busy = useChatStore((state) => {
    const runtime = state.runtimes[sessionId];
    return runtime !== undefined
      && (Boolean(runtime.isProcessing) || Boolean(runtime.isPaused) || runtime.pendingQuestions.length > 0);
  });
  const subjectLabels = useMemo(
    () => new Map(subjects.map(subject => [subject.id, subject.label] as const)),
    [subjects],
  );
  const subjectChoices = useMemo(
    () => subjects.map(subject => ({ subjectId: subject.id, subjectLabel: subject.label })),
    [subjects],
  );

  const exitSelection = useCallback(() => {
    setSelecting(false);
    setSelectionState(EMPTY_DIAGNOSIS_SELECTION);
    setFullScope(false);
    setNote('');
  }, []);
  // A hidden page is not a place to keep an unsent pick.
  useEffect(() => {
    if (!active) exitSelection();
  }, [active, exitSelection]);
  useEffect(() => {
    if (!selecting) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      // A lane uses Esc to clear its own window first; a text field keeps Esc for itself.
      if (event.key !== 'Escape' || event.defaultPrevented) return;
      if (event.target instanceof HTMLInputElement && event.target.type === 'text') return;
      exitSelection();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [exitSelection, selecting]);

  const enterSelection = useCallback(() => {
    dismissNotice();
    // A root-cause emphasis would dim the lanes the user is about to pick from.
    setFocused(null);
    setSelectionState(EMPTY_DIAGNOSIS_SELECTION);
    setFullScope(false);
    setNote('');
    setScale('duration');
    setSubpage('trajectory');
    setSelecting(true);
  }, [dismissNotice]);
  const registerExport = useCallback((exportArchive: () => void) => {
    exportTrajectoryRef.current = exportArchive;
  }, []);

  const pickedAgents = useMemo(
    () => selectionRanges(selectionState, subjectChoices).length,
    [selectionState, subjectChoices],
  );
  const canStart = canSubmitDiagnosis({
    busy,
    submitting: session.submitting,
    running: session.running,
    fullScope,
    hasSelection: hasSelection(selectionState, subjectChoices),
  });
  const startDiagnosis = useCallback(() => {
    const scope = buildDiagnosisScope(selectionState, subjectChoices, fullScope);
    if (scope === null) return;
    void start(scope, note).then((started) => {
      if (!started) return;
      exitSelection();
      setSubpage('diagnosis');
    });
  }, [exitSelection, fullScope, note, selectionState, start, subjectChoices]);

  const focusCause = useCallback((cause: RootCause | null) => {
    if (cause === null) {
      setFocused(null);
      return;
    }
    setFocused({ causeId: cause.id, keys: highlightKeysOf(record, [cause.id]) });
    setSubpage('trajectory');
  }, [record]);

  let diagnoseBlockedReason: string | null = null;
  if (session.running) diagnoseBlockedReason = t('trajectory.analysis.selection.running');
  else if (!timelineAvailable) diagnoseBlockedReason = t('trajectory.analysis.selection.noData');

  const tabs: ReadonlyArray<{ id: TrajectoryAnalysisSubpage; label: string }> = [
    { id: 'trajectory', label: t('trajectory.analysis.tabs.trajectory') },
    { id: 'diagnosis', label: t('trajectory.analysis.tabs.diagnosis') },
  ];
  const errorNotice = selecting && session.notice !== null && session.notice.kind === 'error'
    ? session.notice.text
    : null;

  return (
    <div
      className={css.root}
      data-selecting={selecting ? 'true' : 'false'}
      data-testid="trajectory-analysis-page"
    >
      <header className={css.header}>
        <button
          type="button"
          className={css.back}
          aria-label={t('trajectory.analysis.back')}
          title={t('trajectory.analysis.back')}
          onClick={onBack}
          data-testid="trajectory-analysis-back"
        >
          <ChevronLeft size={16} strokeWidth={2} aria-hidden />
        </button>
        <h2 className={css.title}>{t('trajectory.diagnosis.entry')}</h2>
        {summary === null ? null : (
          <span className={css.summary} data-testid="trajectory-analysis-summary">
            {t('trajectory.analysis.summary', { traces: summary.traces, spans: summary.spans })}
          </span>
        )}
        <span className={css.spacer} />
        <button
          type="button"
          className={css.diagnose}
          disabled={selecting || diagnoseBlockedReason !== null}
          title={diagnoseBlockedReason ?? undefined}
          onClick={enterSelection}
          data-testid="trajectory-analysis-diagnose"
        >
          {t('trajectory.analysis.diagnose')}
        </button>
      </header>
      <div className={css.tabBar}>
        <div
          className={css.tabs}
          role="tablist"
          aria-label={t('trajectory.analysis.tabs.aria')}
          data-testid="trajectory-analysis-tabs"
        >
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={subpage === tab.id}
              className={`${css.tab} ${subpage === tab.id ? css.tabActive : ''}`}
              disabled={selecting}
              onClick={() => setSubpage(tab.id)}
              data-testid={`trajectory-analysis-tab-${tab.id}`}
            >
              {tab.label}
            </button>
          ))}
        </div>
        {subpage === 'trajectory' ? (
          <TrajectoryAnalysisToolbar
            scale={scale}
            scaleLocked={selecting}
            onScaleChange={setScale}
            turnsCollapsed={turnsCollapsed}
            onToggleTurns={() => setTurnsCollapsed(value => !value)}
            callsCollapsed={callsCollapsed}
            onToggleCalls={() => setCallsCollapsed(value => !value)}
            searchQuery={searchQuery}
            onSearchQueryChange={setSearchQuery}
            onDownload={() => { exportTrajectoryRef.current?.(); }}
          />
        ) : null}
      </div>
      <div
        className={css.panel}
        role="tabpanel"
        hidden={subpage !== 'trajectory'}
        data-testid="trajectory-analysis-trajectory"
      >
        <TrajectoryPanel
          active={active && subpage === 'trajectory'}
          mode={mode}
          sessionId={sessionId}
          sharedTimeAxis
          laneScale={scale}
          turnsCollapsed={turnsCollapsed}
          callsCollapsed={callsCollapsed}
          searchQuery={searchQuery}
          onSummaryChange={setSummary}
          onExportReady={registerExport}
          onSubjectsChange={setSubjects}
          onTimelineAvailableChange={setTimelineAvailable}
          {...(focused === null ? {} : { highlightStepKeys: focused.keys })}
          {...(selecting ? { selection: { state: selectionState, onChange: setSelectionState } } : {})}
        />
      </div>
      <div
        className={css.panel}
        role="tabpanel"
        hidden={subpage !== 'diagnosis'}
        data-testid="trajectory-analysis-diagnosis"
      >
        <DiagnosisSubpage
          session={session}
          subjectLabels={subjectLabels}
          focusedCauseId={focused?.causeId ?? null}
          onFocusCause={focusCause}
        />
      </div>
      {selecting ? (
        <DiagnosisSelectionBar
          fullScope={fullScope}
          fullScopeBlocked={busy}
          onFullScopeChange={(value) => {
            setFullScope(value);
            if (value) {
              setSelectionState(current => ({
                ...EMPTY_DIAGNOSIS_SELECTION,
                excluded: current.excluded,
              }));
            }
          }}
          note={note}
          onNoteChange={setNote}
          pickedAgents={pickedAgents}
          canStart={canStart}
          submitting={session.submitting}
          error={errorNotice}
          onCancel={exitSelection}
          onStart={startDiagnosis}
        />
      ) : null}
    </div>
  );
});
