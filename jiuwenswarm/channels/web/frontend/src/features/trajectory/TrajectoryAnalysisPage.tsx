/** Trajectory analysis page: the trajectory and its diagnosis as two sub-pages of one session view. */

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useState } from 'react';
import { ChevronLeft } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useChatStore } from '../../stores/chatStore';
import {
  buildDiagnosisScope,
  DIAGNOSIS_NODE_LIMIT,
  EMPTY_DIAGNOSIS_SELECTION,
  hasSelection,
  selectedNodeCount,
  selectionCoversInterval,
  selectionRanges,
  type DiagnosisSelectionState,
} from './diagnosis/diagnosisSelection';
import { canSubmitDiagnosis, highlightKeysOf } from './diagnosis/diagnosisState';
import { DiagnosisRecordSelect } from './diagnosis/DiagnosisRecordSelect';
import { DiagnosisSelectionBar } from './diagnosis/DiagnosisSelectionBar';
import { DiagnosisSubpage } from './diagnosis/DiagnosisSubpage';
import { useDiagnosisSession } from './diagnosis/useDiagnosisSession';
import { TrajectoryAnalysisToolbar } from './TrajectoryAnalysisToolbar';
import {
  TrajectoryPanel,
  type TrajectoryArchiveActions,
  type TrajectoryLaneScale,
  type TrajectoryDiagnosisLaneNodes,
  type TrajectoryMainAgentLastNode,
  type TrajectoryPanelSubject,
  type TrajectoryPanelSummary,
} from './TrajectoryPanel';
import { trajectoryComposerPolicy, type TrajectoryComposerPolicy } from './trajectoryLayout';
import css from './TrajectoryAnalysisPage.module.css';

export type TrajectoryAnalysisSubpage = 'trajectory' | 'diagnosis';

export interface TrajectoryAnalysisPageProps {
  /** Whether the page is on screen; a hidden page defers its re-projection. */
  active: boolean;
  mode?: string;
  sessionId: string;
  /** Leave the page and return to the conversation. */
  onBack: () => void;
  /**
   * The docked composer follows the sub-page: closed and not reopenable while
   * a range is being chosen or the diagnosis sub-page is showing.
   */
  onComposerPolicyChange?: (policy: TrajectoryComposerPolicy) => void;
  /** Return to the conversation after a suggestion is written into the composer. */
  onComposerFilled?: () => void;
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
  onComposerPolicyChange,
  onComposerFilled,
}: TrajectoryAnalysisPageProps) {
  const { t } = useTranslation();
  const [subpage, setSubpage] = useState<TrajectoryAnalysisSubpage>('trajectory');
  const [subjects, setSubjects] = useState<ReadonlyArray<TrajectoryPanelSubject>>([]);
  const [mainAgentLastNode, setMainAgentLastNode] = useState<TrajectoryMainAgentLastNode | null>(null);
  const [diagnosisLanes, setDiagnosisLanes] = useState<ReadonlyArray<TrajectoryDiagnosisLaneNodes>>([]);
  const [timelineAvailable, setTimelineAvailable] = useState(false);
  const [focused, setFocused] = useState<FocusedCause | null>(null);
  // Selection mode: the trajectory turns into a picker and a bar asks how to run.
  const [selecting, setSelecting] = useState(false);
  const [selectionState, setSelectionState] = useState<DiagnosisSelectionState>(EMPTY_DIAGNOSIS_SELECTION);
  const [fullScope, setFullScope] = useState(false);
  const [note, setNote] = useState('');
  const [emptyNodesHint, setEmptyNodesHint] = useState(false);
  const [scale, setScale] = useState<TrajectoryLaneScale>('time');
  const [turnsCollapsed, setTurnsCollapsed] = useState(false);
  const [callsCollapsed, setCallsCollapsed] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [summary, setSummary] = useState<TrajectoryPanelSummary | null>(null);
  const [archiveActions, setArchiveActions] = useState<TrajectoryArchiveActions | null>(null);
  const [pageSessionId, setPageSessionId] = useState(sessionId);
  if (pageSessionId !== sessionId) {
    setPageSessionId(sessionId);
    setSubpage('trajectory');
    setFocused(null);
    setSelecting(false);
    setScale('time');
    setSearchQuery('');
    setSummary(null);
    setMainAgentLastNode(null);
    setDiagnosisLanes([]);
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
    setEmptyNodesHint(false);
  }, []);
  const replayActive = archiveActions?.replayActive === true;
  // A hidden page is not a place to keep an unsent pick.
  useEffect(() => {
    if (!active) exitSelection();
  }, [active, exitSelection]);
  // Imported archives are read-only replays; leave selection mode as soon as one opens.
  useEffect(() => {
    if (replayActive) exitSelection();
  }, [exitSelection, replayActive]);
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

  useLayoutEffect(() => {
    onComposerPolicyChange?.(active ? trajectoryComposerPolicy(subpage, selecting) : 'available');
  }, [active, onComposerPolicyChange, selecting, subpage]);
  useLayoutEffect(() => () => {
    onComposerPolicyChange?.('available');
  }, [onComposerPolicyChange]);

  const enterSelection = useCallback(() => {
    dismissNotice();
    // A root-cause emphasis would dim the lanes the user is about to pick from.
    setFocused(null);
    setSelectionState(EMPTY_DIAGNOSIS_SELECTION);
    setFullScope(false);
    setNote('');
    setScale('time');
    setSubpage('trajectory');
    setSelecting(true);
  }, [dismissNotice]);
  const registerArchiveActions = useCallback((actions: TrajectoryArchiveActions) => {
    setArchiveActions(actions);
  }, []);

  const pickedAgents = useMemo(
    () => selectionRanges(selectionState, subjectChoices).length,
    [selectionState, subjectChoices],
  );
  const coversMainLastTurn = mainAgentLastNode !== null && selectionCoversInterval(
    selectionState,
    mainAgentLastNode.subjectId,
    mainAgentLastNode,
    fullScope,
  );
  const selectedNodes = useMemo(
    () => selectedNodeCount(selectionState, diagnosisLanes, fullScope),
    [diagnosisLanes, fullScope, selectionState],
  );
  const nodeLimitExceeded = selectedNodes > DIAGNOSIS_NODE_LIMIT;
  if (emptyNodesHint && (selectedNodes > 0 || fullScope)) setEmptyNodesHint(false);
  const canStart = canSubmitDiagnosis({
    busy,
    submitting: session.submitting,
    running: session.running,
    fullScope,
    hasSelection: hasSelection(selectionState, subjectChoices),
    nodeLimitExceeded,
  });
  const startDiagnosis = useCallback(() => {
    if (nodeLimitExceeded) return;
    if (!fullScope && selectedNodes === 0) {
      setEmptyNodesHint(true);
      return;
    }
    const scope = buildDiagnosisScope(selectionState, subjectChoices, fullScope);
    if (scope === null) return;
    void start(scope, note).then((started) => {
      if (!started) return;
      exitSelection();
      setSubpage('diagnosis');
    });
  }, [exitSelection, fullScope, nodeLimitExceeded, note, selectedNodes, selectionState, start, subjectChoices]);

  const focusCause = useCallback((findingId: string | null) => {
    if (findingId === null) {
      setFocused(null);
      return;
    }
    setFocused({ causeId: findingId, keys: highlightKeysOf(record, [findingId]) });
    setSubpage('trajectory');
  }, [record]);

  let diagnoseBlockedReason: string | null = null;
  if (replayActive) diagnoseBlockedReason = t('trajectory.analysis.selection.replay');
  else if (session.running) diagnoseBlockedReason = t('trajectory.analysis.selection.running');
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
            {t('trajectory.analysis.summary', { agents: summary.agents, spans: summary.spans })}
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
            archiveActions={archiveActions}
          />
        ) : (
          <div className={css.tabBarEnd}>
            <DiagnosisRecordSelect
              items={session.summaries}
              currentId={recordId}
              onSelect={(id) => { void session.open(id); }}
              onDelete={(id) => { void session.remove(id); }}
            />
          </div>
        )}
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
          onArchiveActionsReady={registerArchiveActions}
          onSubjectsChange={setSubjects}
          onMainAgentLastNodeChange={setMainAgentLastNode}
          onDiagnosisNodesChange={setDiagnosisLanes}
          onTimelineAvailableChange={setTimelineAvailable}
          {...(focused === null ? {} : { highlightStepKeys: focused.keys })}
          {...(selecting
            ? {
              selection: { state: selectionState, onChange: setSelectionState },
              fullScopeSelection: fullScope,
              onFullScopeChange: setFullScope,
            }
            : {})}
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
          focusedFindingId={focused?.causeId ?? null}
          onFocusFinding={focusCause}
          onComposerFilled={onComposerFilled ?? onBack}
        />
      </div>
      {selecting ? (
        <DiagnosisSelectionBar
          fullScope={fullScope}
          fullScopeBlocked={busy}
          onFullScopeChange={(value) => {
            setFullScope(value);
            if (value) setSelectionState(EMPTY_DIAGNOSIS_SELECTION);
          }}
          note={note}
          onNoteChange={setNote}
          pickedAgents={fullScope ? subjectChoices.length : pickedAgents}
          pickedNodes={selectedNodes}
          lastTurnNotice={coversMainLastTurn ? t('trajectory.analysis.selection.lastTurn') : null}
          nodeLimitNotice={nodeLimitExceeded
            ? t('trajectory.analysis.selection.nodeLimit', { count: selectedNodes, limit: DIAGNOSIS_NODE_LIMIT })
            : null}
          emptyNodesNotice={emptyNodesHint ? t('trajectory.analysis.selection.emptyNodes') : null}
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
