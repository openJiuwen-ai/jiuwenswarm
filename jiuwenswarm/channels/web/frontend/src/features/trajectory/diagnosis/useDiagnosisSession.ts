/**
 * Diagnosis records of one session: the list, the record on screen, live
 * progress, and the actions on them (start, cancel, delete, TTSE, fill input).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { webClient } from '../../../services/webClient';
import { useChatStore } from '../../../stores/chatStore';
import { shouldCatchUpTrajectory } from '../trajectoryArchive';
import { TrajectoryApiError } from '../trajectoryClient';
import {
  cancelDiagnosis,
  deleteDiagnosis,
  getDiagnosis,
  listDiagnoses,
  runDiagnosis,
  triggerDiagnosisTtse,
} from './diagnosisClient';
import {
  applyProgressEvent,
  applyStatusEvent,
  isRunning,
  mergeTtseResults,
  summaryOf,
  upsertSummary,
} from './diagnosisState';
import {
  DIAGNOSIS_PROGRESS_EVENT,
  DIAGNOSIS_STATUS_EVENT,
  type DiagnosisProgressEvent,
  type DiagnosisRecord,
  type DiagnosisScope,
  type DiagnosisStatusEvent,
  type DiagnosisSummary,
} from './diagnosisTypes';

export interface DiagnosisNotice {
  kind: 'error' | 'info';
  text: string;
}

export interface DiagnosisSession {
  /** Records of the session, newest first. */
  summaries: DiagnosisSummary[];
  /** The record on screen, kept live while it runs. */
  record: DiagnosisRecord | null;
  notice: DiagnosisNotice | null;
  /** Some diagnosis of the session is still producing output. */
  running: boolean;
  /** A start request is in flight. */
  submitting: boolean;
  /** Root causes whose TTSE trigger request is in flight. */
  triggeringTtse: ReadonlySet<string>;
  /** Start a diagnosis. Resolves to whether it started. */
  start: (scope: DiagnosisScope, note: string) => Promise<boolean>;
  cancel: () => Promise<void>;
  open: (diagnosisId: string) => Promise<void>;
  remove: (diagnosisId: string) => Promise<void>;
  triggerTtse: (rootCauseId: string) => Promise<void>;
  /** Put text into the chat composer of the session, replacing what is there. */
  fillComposer: (text: string) => void;
  dismissNotice: () => void;
}

const EMPTY_KEYS: ReadonlySet<string> = new Set();

type RecordUpdate = DiagnosisRecord | null | ((current: DiagnosisRecord | null) => DiagnosisRecord | null);

export function useDiagnosisSession(sessionId: string): DiagnosisSession {
  const { t, i18n } = useTranslation();
  const locale: 'zh' | 'en' = (i18n.resolvedLanguage ?? i18n.language).toLowerCase().startsWith('zh') ? 'zh' : 'en';
  const [summaries, setSummaries] = useState<DiagnosisSummary[]>([]);
  const [record, setRecordState] = useState<DiagnosisRecord | null>(null);
  const [notice, setNotice] = useState<DiagnosisNotice | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [triggeringTtse, setTriggeringTtse] = useState<ReadonlySet<string>>(EMPTY_KEYS);
  const recordRef = useRef<DiagnosisRecord | null>(null);
  // The ref moves together with the state: push events arrive faster than React re-renders, and an event applied
  // to a stale ref would be mistaken for a gap (or dropped when the record has just been created).
  const setRecord = useCallback((next: RecordUpdate) => {
    const resolved = typeof next === 'function' ? next(recordRef.current) : next;
    recordRef.current = resolved;
    setRecordState(resolved);
  }, []);
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
  }, [fail, setRecord]);

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
    const controller = new AbortController();
    void reloadList(controller.signal);
    return () => controller.abort();
  }, [reloadList, sessionId, setRecord]);

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

  const open = useCallback(async (diagnosisId: string) => {
    setNotice(null);
    try {
      const next = await getDiagnosis(sessionRef.current, diagnosisId);
      // Live events may have advanced the same record while the request was in flight.
      setRecord(current => (current !== null && current.diagnosis_id === next.diagnosis_id && current.seq > next.seq
        ? current
        : next));
    } catch (error) {
      fail(error);
    }
  }, [fail, setRecord]);

  // Show the newest record until the user picks one.
  useEffect(() => {
    if (recordRef.current !== null || summaries.length === 0) return;
    void open(summaries[0].diagnosis_id);
  }, [open, summaries]);

  const start = useCallback(async (scope: DiagnosisScope, note: string): Promise<boolean> => {
    setSubmitting(true);
    setNotice(null);
    try {
      const trimmed = note.trim();
      const started = await runDiagnosis(sessionRef.current, {
        scope,
        ...(trimmed === '' ? {} : { user_note: trimmed }),
        locale,
      });
      setRecord(started);
      setSummaries(list => upsertSummary(list, summaryOf(started)));
      // The engine starts pushing before this response lands; those events found no record to apply to.
      void refetchRecord(started.diagnosis_id);
      return true;
    } catch (error) {
      fail(error);
      return false;
    } finally {
      setSubmitting(false);
    }
  }, [fail, locale, refetchRecord, setRecord]);

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

  const remove = useCallback(async (diagnosisId: string) => {
    try {
      await deleteDiagnosis(sessionRef.current, diagnosisId);
      setSummaries(list => list.filter(item => item.diagnosis_id !== diagnosisId));
      // Dropping the open record lets the newest remaining one take its place.
      if (recordRef.current?.diagnosis_id === diagnosisId) setRecord(null);
    } catch (error) {
      fail(error);
    }
  }, [fail]);

  const fillComposer = useCallback((text: string) => {
    const sid = sessionRef.current;
    useChatStore.getState().setInputValue(sid, text);
    window.dispatchEvent(new CustomEvent('chat-input-sync', { detail: { sessionId: sid, value: text } }));
  }, []);

  const triggerTtse = useCallback(async (causeId: string) => {
    const current = recordRef.current;
    if (current === null) return;
    setTriggeringTtse(keys => new Set(keys).add(causeId));
    try {
      const results = await triggerDiagnosisTtse(sessionRef.current, current.diagnosis_id, [causeId]);
      setRecord(latest => (latest !== null && latest.diagnosis_id === current.diagnosis_id
        ? mergeTtseResults(latest, results)
        : latest));
      const result = results[causeId];
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
        next.delete(causeId);
        return next;
      });
    }
  }, [fail, t]);

  const dismissNotice = useCallback(() => setNotice(null), []);

  return {
    summaries,
    record,
    notice,
    running: summaries.some(isRunning) || isRunning(record),
    submitting,
    triggeringTtse,
    start,
    cancel,
    open,
    remove,
    triggerTtse,
    fillComposer,
    dismissNotice,
  };
}
