/**
 * Pure state transitions of the diagnosis view.
 *
 * Kept free of React and browser APIs so it can be tested with node --test.
 */

import type {
  DiagnosisProgressEvent,
  DiagnosisRecord,
  DiagnosisStage,
  DiagnosisStatusEvent,
  DiagnosisSummary,
  RootCause,
  RootCauseCategory,
  StepRef,
  TtseTriggerResult,
} from './diagnosisTypes';

export interface EventOutcome {
  /** The record after applying the event; the same reference when ignored. */
  record: DiagnosisRecord;
  /** True when the caller must fetch the record again (seq gap or terminal status). */
  refetch: boolean;
}

/** Identity of a span, matching `TrajectoryDetailRecord.record_id`. */
export function stepRefKey(ref: Pick<StepRef, 'trace_id' | 'span_id'>): string {
  return `${ref.trace_id}:${ref.span_id}`;
}

/**
 * Return the span keys named by a diagnosis.
 *
 * Without `rootCauseIds`, this is every root-cause ref. Lane emphasis does
 * not use that set: see `laneHighlightKeys`.
 */
export function highlightKeysOf(record: DiagnosisRecord | null, rootCauseIds?: readonly string[]): Set<string> {
  const keys = new Set<string>();
  if (record === null) return keys;
  const wanted = rootCauseIds === undefined ? null : new Set(rootCauseIds);
  for (const cause of record.root_causes) {
    if (wanted !== null && !wanted.has(cause.id)) continue;
    for (const ref of cause.refs) keys.add(stepRefKey(ref));
  }
  return keys;
}

/**
 * Span keys to emphasise on the trajectory lanes.
 *
 * Empty until the user focuses root causes. Highlighting every diagnosed
 * step as soon as a run finishes would fade the rest of the lane.
 */
export function laneHighlightKeys(
  record: DiagnosisRecord | null,
  focusedRootCauseIds: readonly string[] | null,
): Set<string> {
  if (record === null || focusedRootCauseIds === null || focusedRootCauseIds.length === 0) {
    return new Set();
  }
  return highlightKeysOf(record, focusedRootCauseIds);
}

function replaceStage(
  stages: readonly DiagnosisStage[],
  stageId: string,
  update: (stage: DiagnosisStage | undefined) => DiagnosisStage,
): DiagnosisStage[] {
  const index = stages.findIndex(stage => stage.stage_id === stageId);
  if (index < 0) return [...stages, update(undefined)];
  const next = [...stages];
  next[index] = update(stages[index]);
  return next;
}

/** Replace the root causes of one signal with its latest set, keeping the rest in order. */
function replaceSignalCauses(
  causes: readonly RootCause[],
  signalId: string | undefined,
  latest: readonly RootCause[],
): RootCause[] {
  const rest = signalId === undefined ? causes : causes.filter(cause => cause.signal?.id !== signalId);
  return [...rest, ...latest];
}

/**
 * Apply one progress event to a record.
 *
 * Events with a `seq` not above the record's are already contained in it and
 * are dropped; a gap means events were lost and the record must be refetched.
 */
export function applyProgressEvent(record: DiagnosisRecord, event: DiagnosisProgressEvent): EventOutcome {
  if (event.diagnosis_id !== record.diagnosis_id) return { record, refetch: false };
  if (event.seq <= record.seq) return { record, refetch: false };
  if (event.seq !== record.seq + 1) return { record, refetch: true };
  if (event.kind === 'root_causes') {
    return {
      record: {
        ...record,
        seq: event.seq,
        root_causes: replaceSignalCauses(record.root_causes, event.signal_id, event.root_causes ?? []),
      },
      refetch: false,
    };
  }
  const stages = replaceStage(record.stages, event.stage_id, (stage) => {
    const base: DiagnosisStage = stage ?? {
      stage_id: event.stage_id,
      title: event.title ?? '',
      status: 'running',
      output: '',
    };
    if (event.kind === 'stage_started') {
      return { ...base, title: event.title ?? base.title, status: 'running' };
    }
    if (event.kind === 'output_delta') {
      return { ...base, output: base.output + (event.text_delta ?? '') };
    }
    return {
      ...base,
      status: event.status ?? 'completed',
      ...(event.summary === undefined ? {} : { summary: event.summary }),
      ...(event.skip_reason === undefined ? {} : { skip_reason: event.skip_reason }),
      ...(event.elapsed_ms === undefined ? {} : { elapsed_ms: event.elapsed_ms }),
    };
  });
  return { record: { ...record, seq: event.seq, stages }, refetch: false };
}

/** Apply one status event; terminal states always ask for a refetch. */
export function applyStatusEvent(record: DiagnosisRecord, event: DiagnosisStatusEvent): EventOutcome {
  if (event.diagnosis_id !== record.diagnosis_id) return { record, refetch: false };
  if (event.seq <= record.seq) return { record, refetch: false };
  const contiguous = event.seq === record.seq + 1;
  return {
    record: contiguous
      ? { ...record, ...event.summary, seq: event.seq, status: event.status }
      : record,
    refetch: !contiguous || event.status !== 'running',
  };
}

/** Insert or replace a summary and keep the list ordered newest first. */
export function upsertSummary(
  list: readonly DiagnosisSummary[],
  summary: DiagnosisSummary,
): DiagnosisSummary[] {
  const rest = list.filter(item => item.diagnosis_id !== summary.diagnosis_id);
  return [summary, ...rest].sort((left, right) => right.ordinal - left.ordinal);
}

/** Project a full record onto its list summary. */
export function summaryOf(record: DiagnosisRecord): DiagnosisSummary {
  return {
    diagnosis_id: record.diagnosis_id,
    ordinal: record.ordinal,
    status: record.status,
    created_at: record.created_at,
    finished_at: record.finished_at,
    scope_kind: record.scope_kind,
    evaluate_artifact: record.evaluate_artifact,
    artifact_eval_applied: record.artifact_eval_applied,
    headline: record.headline,
  };
}

/** Whether a root cause category reports the TTSE experience verdict. */
export function isTtseCategory(category: RootCauseCategory): boolean {
  return category === 'ttse_success' || category === 'ttse_partial' || category === 'ttse_fail';
}

/** The experience root causes of a record, the ones TTSE can be triggered with. */
export function ttseRootCauses(record: DiagnosisRecord): RootCause[] {
  return record.root_causes.filter(cause => isTtseCategory(cause.category));
}

/** Merge TTSE trigger results into a record without touching other fields. */
export function mergeTtseResults(
  record: DiagnosisRecord,
  results: Record<string, TtseTriggerResult>,
): DiagnosisRecord {
  return { ...record, ttse_trigger: { ...record.ttse_trigger, ...results } };
}

/** Whether a diagnosis is still producing output. */
export function isRunning(record: Pick<DiagnosisSummary, 'status'> | null): boolean {
  return record !== null && record.status === 'running';
}

/** Split step references into those still present in the trajectory and those that are gone. */
export function resolveStepIds(
  refs: readonly StepRef[],
  known: ReadonlySet<string>,
): { found: StepRef[]; missing: StepRef[] } {
  const found: StepRef[] = [];
  const missing: StepRef[] = [];
  for (const ref of refs) {
    (known.has(stepRefKey(ref)) ? found : missing).push(ref);
  }
  return { found, missing };
}

/** Convert a wall-clock hull in Unix milliseconds to an inclusive Unix-nanosecond range. */
export function msRangeToUnixNano(
  startMs: number,
  endMs: number,
): { start_unix_nano: string; end_unix_nano: string } | null {
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || startMs < 0 || endMs < startMs) return null;
  const start = BigInt(Math.floor(startMs)) * 1_000_000n;
  // The projected clock has millisecond precision, so widen the end to the whole last millisecond.
  const end = BigInt(Math.ceil(endMs)) * 1_000_000n + 999_999n;
  return { start_unix_nano: start.toString(), end_unix_nano: end.toString() };
}

/** Whether a diagnosis run may be submitted with the current form. */
export function canSubmitDiagnosis(options: {
  busy: boolean;
  submitting: boolean;
  running: boolean;
  fullScope: boolean;
  hasSelection: boolean;
  /** The pick contains more nodes than a diagnosis may cover. */
  nodeLimitExceeded?: boolean;
}): boolean {
  if (options.submitting || options.running || options.nodeLimitExceeded === true) return false;
  if (options.fullScope) return !options.busy;
  return options.hasSelection;
}
