/** Diagnosis & attribution side panel of the trajectory tab. */

import { memo, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { webClient } from '../../../services/webClient';
import { useChatStore } from '../../../stores/chatStore';
import { shouldCatchUpTrajectory } from '../trajectoryArchive';
import { TrajectoryApiError } from '../trajectoryClient';
import {
  triggerDiagnosisTtse,
  cancelDiagnosis,
  deleteDiagnosis,
  getDiagnosis,
  listDiagnoses,
  runDiagnosis,
} from './diagnosisClient';
import {
  applyProgressEvent,
  applyStatusEvent,
  canSubmitDiagnosis,
  laneHighlightKeys,
  isRunning,
  mergeTtseResults,
  msRangeToUnixNano,
  summaryOf,
  upsertSummary,
} from './diagnosisState';
import { formatElapsed } from './diagnosisTimeSelection';
import {
  DIAGNOSIS_PROGRESS_EVENT,
  DIAGNOSIS_STATUS_EVENT,
  type DiagnosisProgressEvent,
  type DiagnosisRecord,
  type DiagnosisScope,
  type SelectionRange,
  type DiagnosisStatusEvent,
  type DiagnosisSummary,
} from './diagnosisTypes';
import css from './DiagnosisPanel.module.css';

/** One checked agent's share of the shared diagnosis window, in Unix milliseconds. */
export interface DiagnosisSelectionRange {
  subjectId: string;
  subjectLabel: string;
  startMs: number;
  endMs: number;
}

/** The shared brush plus the agents still included in it. */
export interface DiagnosisSelection {
  domainStartMs: number;
  startMs: number;
  endMs: number;
  ranges: DiagnosisSelectionRange[];
}

export interface DiagnosisPanelProps {
  sessionId: string;
  /** Whether the session is executing; the full trajectory cannot be diagnosed then. */
  busy: boolean;
  selection: DiagnosisSelection | null;
  onClearSelection: () => void;
  /** Span anchors of a root cause the user asked to emphasise. Empty otherwise. */
  onHighlightChange: (keys: ReadonlySet<string>) => void;
}

interface Notice {
  kind: 'error' | 'info';
  text: string;
}

const EMPTY_KEYS: ReadonlySet<string> = new Set();

export const DiagnosisPanel = memo(function DiagnosisPanel({
  sessionId,
  busy,
  selection,
  onClearSelection,
  onHighlightChange,
}: DiagnosisPanelProps) {
  const { t, i18n } = useTranslation();
  const locale: 'zh' | 'en' = (i18n.resolvedLanguage ?? i18n.language).toLowerCase().startsWith('zh') ? 'zh' : 'en';
  const [summaries, setSummaries] = useState<DiagnosisSummary[]>([]);
  const [record, setRecord] = useState<DiagnosisRecord | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [note, setNote] = useState('');
  const [fullScope, setFullScope] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [triggeringTtse, setTriggeringTtse] = useState<ReadonlySet<string>>(EMPTY_KEYS);
  const [focusCauseIds, setFocusCauseIds] = useState<string[] | null>(null);
  const recordRef = useRef<DiagnosisRecord | null>(null);
  recordRef.current = record;
  const sessionRef = useRef(sessionId);
  sessionRef.current = sessionId;

  const errorText = useCallback((error: unknown): string => {
    if (error instanceof TrajectoryApiError && error.code !== undefined) {
      const key = `trajectory.diagnosis.errors.${error.code}`;
      return i18n.exists(key) ? t(key) : error.message;
    }
    return error instanceof Error && error.message !== '' ? error.message : t('trajectory.diagnosis.errors.default');
  }, [i18n, t]);

  const fail = useCallback((error: unknown) => {
    setNotice({ kind: 'error', text: errorText(error) });
  }, [errorText]);

  const refetchRecord = useCallback(async (diagnosisId: string) => {
    const requestedSession = sessionRef.current;
    try {
      const next = await getDiagnosis(requestedSession, diagnosisId);
      if (sessionRef.current !== requestedSession) return;
      setSummaries(list => upsertSummary(list, summaryOf(next)));
      if (recordRef.current?.diagnosis_id === diagnosisId) {
        // Never step back to an older snapshot than the one already on screen.
        setRecord(current => (current !== null && current.seq > next.seq ? current : next));
      }
    } catch (error) {
      if (sessionRef.current === requestedSession) fail(error);
    }
  }, [fail]);

  const reloadList = useCallback(async (signal?: AbortSignal) => {
    const requestedSession = sessionRef.current;
    try {
      const items = await listDiagnoses(requestedSession, signal === undefined ? {} : { signal });
      if (sessionRef.current !== requestedSession) return;
      setSummaries([...items].sort((left, right) => right.ordinal - left.ordinal));
    } catch (error) {
      if (signal?.aborted === true || sessionRef.current !== requestedSession) return;
      fail(error);
    }
  }, [fail]);

  // Switching sessions drops everything that belongs to the previous one.
  useEffect(() => {
    setSummaries([]);
    setRecord(null);
    setNotice(null);
    setFocusCauseIds(null);
    const controller = new AbortController();
    void reloadList(controller.signal);
    return () => controller.abort();
  }, [reloadList, sessionId]);

  useEffect(() => {
    const onProgress = webClient.on<DiagnosisProgressEvent>(DIAGNOSIS_PROGRESS_EVENT, (event) => {
      const payload = event.payload;
      if (payload.session_id !== sessionRef.current) return;
      const current = recordRef.current;
      if (current === null || current.diagnosis_id !== payload.diagnosis_id) return;
      const outcome = applyProgressEvent(current, payload);
      if (outcome.record !== current) setRecord(outcome.record);
      if (outcome.refetch) void refetchRecord(payload.diagnosis_id);
    });
    const onStatus = webClient.on<DiagnosisStatusEvent>(DIAGNOSIS_STATUS_EVENT, (event) => {
      const payload = event.payload;
      if (payload.session_id !== sessionRef.current) return;
      setSummaries(list => upsertSummary(list, payload.summary));
      const current = recordRef.current;
      if (current === null || current.diagnosis_id !== payload.diagnosis_id) return;
      const outcome = applyStatusEvent(current, payload);
      if (outcome.record !== current) setRecord(outcome.record);
      if (outcome.refetch) void refetchRecord(payload.diagnosis_id);
    });
    let previousState = webClient.getState();
    const onState = webClient.onStateChange((nextState) => {
      if (shouldCatchUpTrajectory(previousState, nextState)) {
        void reloadList();
        const current = recordRef.current;
        if (current !== null) void refetchRecord(current.diagnosis_id);
      }
      previousState = nextState;
    });
    return () => {
      onProgress();
      onStatus();
      onState();
    };
  }, [refetchRecord, reloadList]);

  const highlightKeys = useMemo(
    () => laneHighlightKeys(record, focusCauseIds),
    [focusCauseIds, record],
  );
  useEffect(() => {
    onHighlightChange(highlightKeys);
  }, [highlightKeys, onHighlightChange]);
  useEffect(() => () => onHighlightChange(EMPTY_KEYS), [onHighlightChange]);

  const running = summaries.some(isRunning) || isRunning(record);
  const canSubmit = canSubmitDiagnosis({
    busy,
    submitting,
    running,
    fullScope,
    hasSelection: selection !== null && selection.ranges.length > 0,
  });

  const submit = useCallback(async () => {
    let scope: DiagnosisScope;
    if (fullScope) {
      scope = { kind: 'full' };
    } else {
      if (selection === null || selection.ranges.length === 0) return;
      const ranges: SelectionRange[] = [];
      for (const item of selection.ranges) {
        const range = msRangeToUnixNano(item.startMs, item.endMs);
        if (range === null) return;
        ranges.push({ subject_id: item.subjectId, ...range });
      }
      scope = { kind: 'selection', ranges };
    }
    setSubmitting(true);
    setNotice(null);
    // The drag range has been copied into the request. Leave the bars at
    // their normal colors instead of keeping the selection fade.
    onClearSelection();
    try {
      const trimmed = note.trim();
      const started = await runDiagnosis(sessionRef.current, {
        scope,
        ...(trimmed === '' ? {} : { user_note: trimmed }),
        locale,
      });
      setRecord(started);
      setFocusCauseIds(null);
      setSummaries(list => upsertSummary(list, summaryOf(started)));
    } catch (error) {
      fail(error);
    } finally {
      setSubmitting(false);
    }
  }, [fail, fullScope, locale, note, onClearSelection, selection]);

  const cancel = useCallback(async () => {
    const current = recordRef.current;
    const target = current !== null && isRunning(current)
      ? current.diagnosis_id
      : summaries.find(isRunning)?.diagnosis_id;
    if (target === undefined) return;
    try {
      const next = await cancelDiagnosis(sessionRef.current, target);
      setSummaries(list => upsertSummary(list, summaryOf(next)));
      if (recordRef.current?.diagnosis_id === target) setRecord(next);
    } catch (error) {
      fail(error);
    }
  }, [fail, summaries]);

  const open = useCallback(async (diagnosisId: string) => {
    setNotice(null);
    setFocusCauseIds(null);
    try {
      setRecord(await getDiagnosis(sessionRef.current, diagnosisId));
    } catch (error) {
      fail(error);
    }
  }, [fail]);

  // The list stays on screen, so open the newest record until the user picks one.
  useEffect(() => {
    if (recordRef.current !== null || summaries.length === 0) return;
    void open(summaries[0].diagnosis_id);
  }, [open, summaries]);

  const remove = useCallback(async (diagnosisId: string) => {
    if (!window.confirm(t('trajectory.diagnosis.deleteConfirm'))) return;
    try {
      await deleteDiagnosis(sessionRef.current, diagnosisId);
      setSummaries(list => list.filter(item => item.diagnosis_id !== diagnosisId));
      if (recordRef.current?.diagnosis_id === diagnosisId) setRecord(null);
    } catch (error) {
      fail(error);
    }
  }, [fail, t]);

  const fillComposer = useCallback((text: string) => {
    const sid = sessionRef.current;
    useChatStore.getState().setInputValue(sid, text);
    window.dispatchEvent(new CustomEvent('chat-input-sync', { detail: { sessionId: sid, value: text } }));
    setNotice({ kind: 'info', text: t('trajectory.diagnosis.filled') });
  }, [t]);

  const triggerTtse = useCallback(async (signalId: string) => {
    const current = recordRef.current;
    if (current === null) return;
    setTriggeringTtse(keys => new Set(keys).add(signalId));
    try {
      const results = await triggerDiagnosisTtse(sessionRef.current, current.diagnosis_id, [signalId]);
      setRecord(latest => (latest !== null && latest.diagnosis_id === current.diagnosis_id
        ? mergeTtseResults(latest, results)
        : latest));
      const result = results[signalId];
      if (result !== undefined) {
        setNotice({
          kind: result.result === 'failed' ? 'error' : 'info',
          text: t(`trajectory.diagnosis.ttseResult.${result.result}`),
        });
      }
    } catch (error) {
      fail(error);
    } finally {
      setTriggeringTtse((keys) => {
        const next = new Set(keys);
        next.delete(signalId);
        return next;
      });
    }
  }, [fail, t]);

  const statusBadge = (status: DiagnosisSummary['status']) => (
    <span className={`${css.badge} ${css[`badge_${status}`] ?? ''}`}>
      {t(`trajectory.diagnosis.status.${status}`)}
    </span>
  );

  const stampOf = (value: string | undefined): string => {
    if (value === undefined || value === '') return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    const pad = (part: number) => String(part).padStart(2, '0');
    return `${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  };

  const controls = (
    <div className={css.toolbar} data-testid="trajectory-diagnosis-controls">
      <span className={css.brand}>{t('trajectory.diagnosis.panelTitle')}</span>
      <input
        id="trajectory-diagnosis-note"
        className={css.note}
        type="text"
        value={note}
        maxLength={2000}
        placeholder={t('trajectory.diagnosis.notePlaceholder')}
        aria-label={t('trajectory.diagnosis.noteLabel')}
        onChange={event => setNote(event.target.value)}
        data-testid="trajectory-diagnosis-note"
      />
      <label className={css.check} title={busy ? t('trajectory.diagnosis.fullScopeBusy') : undefined}>
        <input
          type="checkbox"
          checked={fullScope}
          disabled={busy}
          onChange={event => setFullScope(event.target.checked)}
          data-testid="trajectory-diagnosis-full-scope"
        />
        <span>{t('trajectory.diagnosis.fullScope')}</span>
      </label>
      {fullScope ? null : (
        <span className={css.selection} data-testid="trajectory-diagnosis-selection">
          {selection === null
            ? t('trajectory.diagnosis.selectionHint')
            : t('trajectory.diagnosis.selectionActive', {
              count: selection.ranges.length,
              start: formatElapsed(selection.startMs - selection.domainStartMs),
              end: formatElapsed(selection.endMs - selection.domainStartMs),
            })}
          {selection === null ? null : (
            <button
              type="button"
              className={css.selectionClear}
              onClick={onClearSelection}
              data-testid="trajectory-diagnosis-clear-selection"
            >
              {t('trajectory.diagnosis.clearSelection')}
            </button>
          )}
        </span>
      )}
      <span className={css.spacer} />
      {running ? (
        <button
          type="button"
          className={css.button}
          onClick={() => { void cancel(); }}
          data-testid="trajectory-diagnosis-cancel"
        >
          {t('trajectory.diagnosis.cancel')}
        </button>
      ) : null}
      <button
        type="button"
        className={`${css.button} ${css.primary}`}
        disabled={!canSubmit}
        onClick={() => { void submit(); }}
        data-testid="trajectory-diagnosis-run"
      >
        {submitting ? t('trajectory.diagnosis.starting') : t('trajectory.diagnosis.run')}
      </button>
    </div>
  );

  const recordList = (
    <section className={css.column} data-testid="trajectory-diagnosis-records">
      <header className={css.columnHead}>
        <h3 className={css.columnTitle}>{t('trajectory.diagnosis.records')}</h3>
        <span className={css.count}>{summaries.length}</span>
      </header>
      <div className={css.columnBody}>
        {summaries.length === 0 ? (
          <p className={css.hint}>{t('trajectory.diagnosis.noRecords')}</p>
        ) : (
          <ul className={css.list}>
            {summaries.map(item => {
              const selected = record?.diagnosis_id === item.diagnosis_id;
              return (
                <li
                  className={`${css.card} ${selected ? css.cardSelected : ''}`}
                  key={item.diagnosis_id}
                  data-testid="trajectory-diagnosis-record-item"
                  data-selected={selected ? 'true' : 'false'}
                >
                  <button
                    type="button"
                    className={css.cardMain}
                    onClick={() => { void open(item.diagnosis_id); }}
                    data-testid="trajectory-diagnosis-record-view"
                  >
                    <span className={css.cardTitle}>
                      <strong>{t('trajectory.diagnosis.recordTitle', { ordinal: item.ordinal })}</strong>
                      <span className={css.cardTime}>{stampOf(item.created_at)}</span>
                      {statusBadge(item.status)}
                    </span>
                    <span className={css.cardMeta} title={item.headline ?? ''}>
                      {t(`trajectory.diagnosis.scopeKind.${item.scope_kind}`)}
                      {item.headline === undefined || item.headline === '' ? '' : ` · ${item.headline}`}
                    </span>
                  </button>
                  <button
                    type="button"
                    className={`${css.button} ${css.danger} ${css.cardDelete}`}
                    disabled={isRunning(item)}
                    onClick={() => { void remove(item.diagnosis_id); }}
                    data-testid="trajectory-diagnosis-record-delete"
                  >
                    {t('trajectory.diagnosis.delete')}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );

  const detail = record === null ? null : (
    <DiagnosisRecordView
      record={record}
      focusCauseIds={focusCauseIds}
      triggeringTtse={triggeringTtse}
      statusBadge={statusBadge}
      onFocusCause={(id) => {
        const turningOff = focusCauseIds?.length === 1 && focusCauseIds[0] === id;
        setFocusCauseIds(turningOff ? null : [id]);
        if (!turningOff) onClearSelection();
      }}
      onFill={fillComposer}
      onTriggerTtse={(id) => { void triggerTtse(id); }}
    />
  );

  const emptyColumn = (title: string, hint: string, testId: string) => (
    <section className={css.column} data-testid={testId}>
      <header className={css.columnHead}>
        <h3 className={css.columnTitle}>{title}</h3>
      </header>
      <div className={css.columnBody}>
        <p className={css.hint}>{hint}</p>
      </div>
    </section>
  );

  return (
    <div className={css.root} aria-label={t('trajectory.diagnosis.panelTitle')} data-testid="trajectory-diagnosis-panel">
      {controls}
      {notice === null ? null : (
        <p
          className={`${css.notice} ${notice.kind === 'error' ? css.noticeError : ''}`}
          role={notice.kind === 'error' ? 'alert' : 'status'}
          data-testid="trajectory-diagnosis-notice"
        >
          {notice.text}
        </p>
      )}
      <div className={css.columns}>
        {recordList}
        {detail ?? (
          <>
            {emptyColumn(
              t('trajectory.diagnosis.stages'),
              t('trajectory.diagnosis.selectRecord'),
              'trajectory-diagnosis-record',
            )}
            {emptyColumn(
              t('trajectory.diagnosis.suggestions'),
              t('trajectory.diagnosis.selectRecord'),
              'trajectory-diagnosis-suggestions',
            )}
          </>
        )}
      </div>
    </div>
  );
});

interface DiagnosisRecordViewProps {
  record: DiagnosisRecord;
  focusCauseIds: string[] | null;
  triggeringTtse: ReadonlySet<string>;
  statusBadge: (status: DiagnosisSummary['status']) => ReactNode;
  onFocusCause: (causeId: string) => void;
  onFill: (text: string) => void;
  onTriggerTtse: (signalId: string) => void;
}

function DiagnosisRecordView({
  record,
  focusCauseIds,
  triggeringTtse,
  statusBadge,
  onFocusCause,
  onFill,
  onTriggerTtse,
}: DiagnosisRecordViewProps) {
  const { t } = useTranslation();
  const { suggestions } = record;
  const done = record.status !== 'running';
  return (
    <>
      <section className={css.column} data-testid="trajectory-diagnosis-record">
        <header className={css.columnHead}>
          <h3 className={css.columnTitle}>{t('trajectory.diagnosis.stages')}</h3>
          {statusBadge(record.status)}
        </header>
        <div className={css.columnBody}>
        {record.headline === undefined || record.headline === '' ? null : <p className={css.hint}>{record.headline}</p>}
        {record.error === undefined ? null : (
          <p className={`${css.notice} ${css.noticeError}`} role="alert">
            {record.error.message}
          </p>
        )}
        <div data-testid="trajectory-diagnosis-stages">
        {record.stages.map(stage => (
          <div
            key={stage.stage_id}
            className={`${css.stage} ${stage.status === 'running'
              ? css.stageRunning
              : stage.status === 'completed' ? css.stageCompleted : stage.status === 'failed' ? css.stageFailed : ''}`}
            data-testid="trajectory-diagnosis-stage"
            data-stage-status={stage.status}
          >
            <div className={css.stageHead}>
              <span>{stage.title}</span>
              <span className={css.badge}>{t(`trajectory.diagnosis.stageStatus.${stage.status}`)}</span>
            </div>
            {stage.summary === undefined || stage.summary === '' ? null : <p className={css.hint}>{stage.summary}</p>}
            {stage.skip_reason === undefined ? null : <p className={css.hint}>{stage.skip_reason}</p>}
            {stage.output === '' ? null : <pre className={css.output}>{stage.output}</pre>}
          </div>
        ))}
        </div>

      {done ? (
        <div data-testid="trajectory-diagnosis-root-causes">
          <h3 className={css.blockTitle}>{t('trajectory.diagnosis.rootCauses')}</h3>
          {record.root_causes.length === 0 ? <p className={css.hint}>{t('trajectory.diagnosis.noRootCause')}</p> : null}
          {record.root_causes.map(cause => (
            <div className={css.cause} key={cause.id} data-testid="trajectory-diagnosis-root-cause">
              <div className={css.causeHead}>
                <span className={css.badge}>{t(`trajectory.diagnosis.category.${cause.category}`)}</span>
                <span className={css.hint}>
                  {t('trajectory.diagnosis.confidence', { value: Math.round(cause.confidence * 100) })}
                </span>
                <span className={css.spacer} />
                <button
                  type="button"
                  className={css.button}
                  aria-pressed={focusCauseIds?.length === 1 && focusCauseIds[0] === cause.id}
                  onClick={() => onFocusCause(cause.id)}
                  data-testid="trajectory-diagnosis-highlight"
                >
                  {t('trajectory.diagnosis.highlight')}
                </button>
              </div>
              <strong>{cause.summary}</strong>
              {cause.evidence === '' ? null : <p className={css.hint}>{cause.evidence}</p>}
            </div>
          ))}
        </div>
      ) : null}
        </div>
      </section>

      <section className={css.column} data-testid="trajectory-diagnosis-suggestions">
        <header className={css.columnHead}>
          <h3 className={css.columnTitle}>{t('trajectory.diagnosis.suggestions')}</h3>
        </header>
        <div className={css.columnBody}>
      {done ? (
        <>
          {suggestions.artifact_fix === undefined ? null : (
            <div className={css.suggestion} data-testid="trajectory-diagnosis-artifact-fix">
              <strong>{t('trajectory.diagnosis.artifactFix')}</strong>
              <p className={css.suggestionText}>{suggestions.artifact_fix.instruction}</p>
              <div className={css.row}>
                <button
                  type="button"
                  className={css.button}
                  onClick={() => onFill(suggestions.artifact_fix?.instruction ?? '')}
                  data-testid="trajectory-diagnosis-artifact-fix-fill"
                >
                  {t('trajectory.diagnosis.fillInput')}
                </button>
              </div>
            </div>
          )}
          {suggestions.skills.map(skill => (
            <div className={css.suggestion} key={skill.id} data-testid="trajectory-diagnosis-skill">
              <strong>{t('trajectory.diagnosis.skills')}: {skill.skill_name}</strong>
              <p className={css.suggestionText}>{skill.change_summary}</p>
              {skill.rationale === '' ? null : <p className={css.hint}>{skill.rationale}</p>}
              {skill.selectable ? (
                <div className={css.row}>
                  <button
                    type="button"
                    className={css.button}
                    onClick={() => onFill(skill.message)}
                    data-testid="trajectory-diagnosis-skill-fill"
                  >
                    {t('trajectory.diagnosis.fillInput')}
                  </button>
                </div>
              ) : (
                <p className={css.hint}>{t('trajectory.diagnosis.blocked', { reason: skill.blocked_reason ?? '' })}</p>
              )}
            </div>
          ))}
          {suggestions.ttse.map((signal) => {
            const triggered = record.ttse_trigger[signal.id];
            const triggering = triggeringTtse.has(signal.id);
            return (
              <div className={css.suggestion} key={signal.id} data-testid="trajectory-diagnosis-ttse">
                <strong>{t('trajectory.diagnosis.ttse')} · {t(`trajectory.diagnosis.ttseOutcome.${signal.outcome}`)}</strong>
                {signal.reason === '' ? null : <p className={css.suggestionText}>{signal.reason}</p>}
                <div className={css.row}>
                  <button
                    type="button"
                    className={css.button}
                    disabled={triggering || (triggered !== undefined && triggered.result !== 'failed')}
                    onClick={() => onTriggerTtse(signal.id)}
                    data-testid="trajectory-diagnosis-ttse-trigger"
                  >
                    {triggering ? t('trajectory.diagnosis.triggeringTtse') : t('trajectory.diagnosis.triggerTtse')}
                  </button>
                  {triggered === undefined ? null : (
                    <span className={css.hint} data-testid="trajectory-diagnosis-ttse-result">
                      {t(`trajectory.diagnosis.ttseResult.${triggered.result}`)}
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </>
      ) : (
        <p className={css.hint}>{t('trajectory.diagnosis.outletsPending')}</p>
      )}
        </div>
      </section>
    </>
  );
}
