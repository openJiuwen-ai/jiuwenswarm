// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/** Transactional browser window helpers for trajectory list and detail polling. */

import type {
  OtlpExportTraceServiceRequest,
  OtlpSpan,
} from './shared/otlp';
import type { TrajectoryUsage } from './trajectory/model';
import type {
  TrajectoryDetailRecord,
  TrajectorySubjectListResponse,
  TrajectorySubjectRecordsResponse,
  TrajectorySubjectSummary,
} from './trajectoryClient';
import { emptyStreamFrameState } from './trajectoryFrames';
import type { StreamFrameState } from './trajectoryFrames';

export interface TrajectoryChainBucket {
  revision: number;
  records: Map<string, OtlpExportTraceServiceRequest>;
  rawRecords: Map<string, TrajectoryDetailRecord>;
  versions?: Map<string, TrajectoryRecordVersion>;
}

export interface TrajectoryRecordVersion {
  lifecycle: 'running' | 'completed' | 'error';
  recordRevision: number;
}

export interface TrajectoryWindowState {
  /** One bucket per execution subject, keyed by subject id. */
  buckets: Map<string, TrajectoryChainBucket>;
  storeEpoch: string | null;
  /** Highest change_seq the listing has reported, and the next poll's floor. */
  watermark: number;
  /**
   * Frames accumulated for the spans still writing their answer, plus how far
   * along the session's frame stream this reader has read. Frames advance on
   * their own watermark because a streaming span emits many of them without
   * rewriting its record.
   */
  frames: StreamFrameState;
  listWindowInitialized: boolean;
  rawSelection: string;
}

export type SubjectRefreshWindow = {
  reset: true;
  storeEpoch: string;
} | {
  reset: false;
  storeEpoch: string;
  summaries: TrajectorySubjectSummary[];
  watermark: number;
};

export interface TrajectoryOperationCoordinator {
  currentGeneration: () => number;
  invalidate: () => number;
  isCurrent: (generation: number) => boolean;
}

export interface TrajectoryTraceHintCoordinator {
  enqueue: (traceId: string, revision: number) => void;
  drain: (
    loadBatch: (hints: ReadonlyMap<string, number>) => Promise<void>,
    pace?: () => Promise<void>,
  ) => Promise<void>;
}

export interface StagedTrajectoryChain {
  bucket: TrajectoryChainBucket;
  invalidRecordSeen: boolean;
  /**
   * `traceId:spanId` of every record this page brought to a terminal state.
   * Such a span states its own output, so its stream frames can be released.
   */
  finishedSpanKeys: readonly string[];
}

export type TrajectoryContentMode = 'new' | 'loading' | 'blocking-error' | 'empty' | 'data';

export type TrajectoryTerminalEventName =
  | 'chat.final'
  | 'chat.processing_status'
  | 'chat.error'
  | 'execution.error'
  | 'harness.session_finished';

function sameUsage(left: TrajectoryUsage, right: TrajectoryUsage): boolean {
  return left.input === right.input
    && left.cacheRead === right.cacheRead
    && left.cacheWrite === right.cacheWrite
    && left.output === right.output
    && left.reasoning === right.reasoning
    && left.total === right.total;
}

/** Report whether a cumulative-usage refresh changes any projected request fact. */
/**
 * Trace ids whose request usage differs between two session usage maps.
 *
 * Usage is keyed `traceId\u0000inferenceId`; a trace appears when any of its
 * requests gained, lost or changed a cumulative figure.
 */
export function changedTrajectoryUsageTraceIds(
  left: ReadonlyMap<string, TrajectoryUsage>,
  right: ReadonlyMap<string, TrajectoryUsage>,
): Set<string> {
  const traceIds = new Set<string>();
  const traceOf = (identity: string) => identity.slice(0, identity.indexOf('\u0000'));
  for (const [identity, usage] of left) {
    const candidate = right.get(identity);
    if (candidate === undefined || !sameUsage(usage, candidate)) traceIds.add(traceOf(identity));
  }
  for (const identity of right.keys()) {
    if (!left.has(identity)) traceIds.add(traceOf(identity));
  }
  return traceIds;
}

export function sameTrajectoryUsageMap(
  left: ReadonlyMap<string, TrajectoryUsage>,
  right: ReadonlyMap<string, TrajectoryUsage>,
): boolean {
  return left.size === right.size
    && [...left].every(([identity, usage]) => {
      const candidate = right.get(identity);
      return candidate !== undefined && sameUsage(usage, candidate);
    });
}

function trajectoryEventSessionId(payload: Record<string, unknown>): string | null {
  if (typeof payload.session_id === 'string' && payload.session_id.trim()) {
    return payload.session_id;
  }
  for (const key of ['payload', 'event'] as const) {
    const nested = payload[key];
    if (typeof nested !== 'object' || nested === null || Array.isArray(nested)) continue;
    const nestedSessionId = trajectoryEventSessionId(nested as Record<string, unknown>);
    if (nestedSessionId !== null) return nestedSessionId;
  }
  return null;
}

type SubjectListLoader = (
  afterRevision: number,
  signal: AbortSignal,
) => Promise<TrajectorySubjectListResponse>;

type ChainPageLoader = (
  sinceRevision: number,
  signal: AbortSignal,
) => Promise<TrajectorySubjectRecordsResponse>;

type ChainPagePublisher = (staged: StagedTrajectoryChain) => void;

function invalidPagination(message: string): Error {
  const error = new Error(message);
  error.name = 'TrajectoryPaginationError';
  return error;
}

export function createTrajectoryWindowState(): TrajectoryWindowState {
  return {
    buckets: new Map<string, TrajectoryChainBucket>(),
    storeEpoch: null,
    watermark: 0,
    frames: emptyStreamFrameState,
    listWindowInitialized: false,
    rawSelection: '',
  };
}

/** Recognize session terminal events that must repair a missed trajectory hint. */
export function shouldCatchUpAfterTrajectoryTerminalEvent(
  eventName: TrajectoryTerminalEventName,
  payload: Record<string, unknown>,
  sessionId: string,
): boolean {
  if (trajectoryEventSessionId(payload) !== sessionId) return false;
  if (eventName === 'chat.processing_status') {
    return payload.is_processing === false;
  }
  return true;
}

export function resetTrajectoryWindowState(state: TrajectoryWindowState): void {
  state.buckets = new Map<string, TrajectoryChainBucket>();
  state.storeEpoch = null;
  state.watermark = 0;
  state.frames = emptyStreamFrameState;
  state.listWindowInitialized = false;
  state.rawSelection = '';
}

export function createTrajectoryOperationCoordinator(): TrajectoryOperationCoordinator {
  let generation = 0;
  return {
    currentGeneration: () => generation,
    invalidate: () => {
      generation += 1;
      return generation;
    },
    isCurrent: candidate => candidate === generation,
  };
}

/** Coalesce trace hints while making one flight chase every later watermark. */
export function createTrajectoryTraceHintCoordinator(): TrajectoryTraceHintCoordinator {
  const pending = new Map<string, number>();
  let flight: Promise<void> | null = null;

  const enqueue = (traceId: string, revision: number): void => {
    const prior = pending.get(traceId) ?? -1;
    if (revision > prior) pending.set(traceId, revision);
  };
  const drain = (
    loadBatch: (hints: ReadonlyMap<string, number>) => Promise<void>,
    pace?: () => Promise<void>,
  ): Promise<void> => {
    if (flight !== null) return flight;
    const operation = (async () => {
      let firstBatch = true;
      while (pending.size > 0) {
        if (!firstBatch && pace !== undefined) await pace();
        const hints = new Map(pending);
        pending.clear();
        try {
          await loadBatch(hints);
        } catch (error) {
          for (const [traceId, revision] of hints) enqueue(traceId, revision);
          throw error;
        }
        firstBatch = false;
      }
    })();
    flight = operation;
    const clearFlight = () => {
      if (flight === operation) flight = null;
    };
    void operation.then(clearFlight, clearFlight);
    return operation;
  };
  return { enqueue, drain };
}

export function spansOf(record: OtlpExportTraceServiceRequest): OtlpSpan[] {
  return record.resourceSpans.flatMap(resource => (
    resource.scopeSpans ?? []
  ).flatMap(scope => scope.spans ?? []));
}

export function recordIdentity(record: OtlpExportTraceServiceRequest): string | null {
  const spans = spansOf(record);
  if (spans.length !== 1 || spans[0] === undefined) return null;
  return `${spans[0].traceId}:${spans[0].spanId}`;
}

export function detailRecordIdentity(record: TrajectoryDetailRecord): string | null {
  if (record.record_id !== undefined) return record.record_id;
  if (record.trace_id !== undefined && record.span_id !== undefined) {
    return `${record.trace_id}:${record.span_id}`;
  }
  return record.otlp === null ? null : recordIdentity(record.otlp);
}

export function detailRecordLifecycle(
  record: TrajectoryDetailRecord,
): TrajectoryRecordVersion['lifecycle'] {
  if (record.lifecycle === 'provisional' || record.lifecycle === 'running') return 'running';
  if (record.lifecycle === 'abandoned' || record.lifecycle === 'error') return 'error';
  return 'completed';
}

function detailRecordRevision(record: TrajectoryDetailRecord): number {
  return record.record_revision ?? record.change_seq ?? record.ingest_seq;
}

function terminal(lifecycle: TrajectoryRecordVersion['lifecycle']): boolean {
  return lifecycle === 'completed' || lifecycle === 'error';
}

/** Apply one revision page with per-record latest-wins and terminal absorption. */
export function applyTrajectoryDetailRecords(
  current: TrajectoryChainBucket | undefined,
  detail: TrajectorySubjectRecordsResponse,
): StagedTrajectoryChain {
  let records = new Map<string, OtlpExportTraceServiceRequest>(current?.records ?? []);
  let rawRecords = new Map<string, TrajectoryDetailRecord>(current?.rawRecords ?? []);
  let versions = new Map<string, TrajectoryRecordVersion>(current?.versions ?? []);
  let invalidRecordSeen = false;
  const finishedSpanKeys: string[] = [];
  if (detail.reset) {
    records = new Map();
    rawRecords = new Map();
    versions = new Map();
  }
  for (const item of detail.records) {
    const identity = detailRecordIdentity(item);
    if (identity === null) {
      invalidRecordSeen = true;
      continue;
    }
    const incoming = {
      lifecycle: detailRecordLifecycle(item),
      recordRevision: detailRecordRevision(item),
    };
    const prior = versions.get(identity);
    if (prior !== undefined) {
      if (terminal(prior.lifecycle) && incoming.lifecycle === 'running') continue;
      const incomingTerminal = terminal(incoming.lifecycle);
      if (!incomingTerminal || terminal(prior.lifecycle)) {
        if (incoming.recordRevision < prior.recordRevision) continue;
        if (incoming.recordRevision === prior.recordRevision) continue;
      }
    }
    if (item.operation === 'delete') {
      records.delete(identity);
      rawRecords.delete(identity);
      versions.delete(identity);
      continue;
    }
    versions.set(identity, incoming);
    rawRecords.set(identity, item);
    if (terminal(incoming.lifecycle)) finishedSpanKeys.push(identity);
    if (!item.raw_valid || item.otlp === null) {
      invalidRecordSeen = true;
      records.delete(identity);
      continue;
    }
    const otlpIdentity = recordIdentity(item.otlp);
    if (otlpIdentity !== identity) {
      invalidRecordSeen = true;
      continue;
    }
    records.set(identity, item.otlp);
  }
  return {
    bucket: {
      revision: detail.revision,
      records,
      rawRecords,
      versions,
    },
    invalidRecordSeen,
    finishedSpanKeys,
  };
}

export function dedupeSubjectSummaries(
  summaries: readonly TrajectorySubjectSummary[],
): TrajectorySubjectSummary[] {
  const bySubjectId = new Map<string, TrajectorySubjectSummary>();
  for (const summary of summaries) {
    const current = bySubjectId.get(summary.subject_id);
    if (current === undefined || summary.revision >= current.revision) {
      bySubjectId.set(summary.subject_id, summary);
    }
  }
  return [...bySubjectId.values()];
}

/** Which frame catch-up a refresh performs. */
export type StreamFrameRefresh = 'always' | 'ifBehind';

/**
 * Whether a refresh should page stream frames.
 *
 * A trace hint states the frame watermark it was committed at. When the frames
 * this reader already holds reach it, paging would only confirm there is
 * nothing new -- one round trip per hint while an answer streams. Reconnects,
 * terminal events and rebuilds pass 'always': they cannot trust a hint.
 */
export function shouldCatchUpStreamFrames(
  mode: StreamFrameRefresh,
  heldFrameSeq: number,
  hintedFrameSeq: number,
): boolean {
  return mode === 'always' || heldFrameSeq < hintedFrameSeq;
}

export function selectSummariesNeedingLoad(
  loadedRevisions: ReadonlyMap<string, number>,
  ...summaryGroups: Array<readonly TrajectorySubjectSummary[]>
): TrajectorySubjectSummary[] {
  const summaries = dedupeSubjectSummaries(summaryGroups.flatMap(group => [...group]));
  return summaries.filter((summary) => {
    const loadedRevision = loadedRevisions.get(summary.subject_id);
    return loadedRevision === undefined || summary.revision > loadedRevision;
  });
}

/**
 * Collect the subjects that own a chain in one session.
 *
 * A session holds few subjects, so the listing is a single request rather
 * than a paginated window: first load and polling differ only in the
 * revision floor they pass. A rotated store epoch means the caller's
 * revisions describe a database that no longer exists, so it must restart.
 */
export async function collectSubjectRefreshWindow(
  afterRevision: number,
  expectedStoreEpoch: string | null,
  signal: AbortSignal,
  loadList: SubjectListLoader,
): Promise<SubjectRefreshWindow | null> {
  if (signal.aborted) return null;
  const page = await loadList(afterRevision, signal);
  if (signal.aborted) return null;
  if (expectedStoreEpoch !== null && page.store_epoch !== expectedStoreEpoch) {
    return { reset: true, storeEpoch: page.store_epoch };
  }
  return {
    reset: false,
    storeEpoch: page.store_epoch,
    summaries: dedupeSubjectSummaries(page.items),
    watermark: page.watermark,
  };
}

export async function stageTrajectoryChainPages(
  current: TrajectoryChainBucket | undefined,
  signal: AbortSignal,
  loadPage: ChainPageLoader,
  publishPage?: ChainPagePublisher,
): Promise<StagedTrajectoryChain | null> {
  let sinceRevision = current?.revision ?? 0;
  let stagedRecords = new Map<string, OtlpExportTraceServiceRequest>(
    current?.records ?? [],
  );
  let stagedRawRecords = new Map<string, TrajectoryDetailRecord>(
    current?.rawRecords ?? [],
  );
  let stagedVersions = new Map<string, TrajectoryRecordVersion>(current?.versions ?? []);
  let stagedRevision = current?.revision ?? 0;
  let invalidRecordSeen = false;
  while (true) {
    if (signal.aborted) return null;
    const detail = await loadPage(sinceRevision, signal);
    if (signal.aborted) return null;
    if (detail.reset) {
      stagedRecords = new Map<string, OtlpExportTraceServiceRequest>();
      stagedRawRecords = new Map<string, TrajectoryDetailRecord>();
      stagedVersions = new Map<string, TrajectoryRecordVersion>();
      sinceRevision = 0;
    }
    const applied = applyTrajectoryDetailRecords({
      revision: stagedRevision,
      records: stagedRecords,
      rawRecords: stagedRawRecords,
      versions: stagedVersions,
    }, detail);
    stagedRecords = applied.bucket.records;
    stagedRawRecords = applied.bucket.rawRecords;
    stagedVersions = applied.bucket.versions ?? new Map();
    invalidRecordSeen = invalidRecordSeen || applied.invalidRecordSeen;
    const consumedRevision = detail.has_more
      ? detail.next_since_revision
      : detail.revision;
    if (detail.has_more && consumedRevision <= sinceRevision) {
      throw invalidPagination('Trajectory detail pagination did not advance');
    }
    stagedRevision = consumedRevision;
    const progress = {
      bucket: {
        revision: stagedRevision,
        records: stagedRecords,
        rawRecords: stagedRawRecords,
        versions: stagedVersions,
      },
      invalidRecordSeen,
      finishedSpanKeys: applied.finishedSpanKeys,
    };
    publishPage?.(progress);
    if (!detail.has_more) return progress;
    sinceRevision = consumedRevision;
  }
}

export function trajectoryContentMode(input: {
  sessionId: string;
  loading: boolean;
  error: string | null;
  projectedCount: number;
  rawCount: number;
}): TrajectoryContentMode {
  if (input.sessionId === 'new') return 'new';
  const hasData = input.projectedCount > 0 || input.rawCount > 0;
  if (input.loading && !hasData) return 'loading';
  if (input.error !== null && !hasData) return 'blocking-error';
  if (!hasData) return 'empty';
  return 'data';
}
