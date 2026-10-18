/**
 * Session trajectory reader of the diagnosis page.
 *
 * A trimmed port of the transport in `TrajectoryPanel`: the same window,
 * sequence, checkpoint and stream-frame pipeline, without archive import and
 * export, replay, or the raw record inspector. The diagnosis page owns its own
 * reader so the raw trajectory page stays untouched.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { webClient } from '../../../services/webClient';
import { projectOtelTrajectory } from '../projector/otel-trajectory-projector';
import { createTrajectoryV2Reducer } from '../projector/trajectory-v2-reducer';
import type { OtlpExportTraceServiceRequest } from '../shared/otlp';
import type { TrajectorySnapshot, TrajectoryUsage } from '../trajectory/model';
import {
  EMPTY_TRAJECTORY_CHECKPOINTS,
  resolveTrajectoryCheckpoints,
  type TrajectoryRetentionCheckpoints,
} from '../trajectoryCheckpoints';
import {
  getTrajectoryCheckpoints,
  getTrajectorySequences,
  getTrajectorySessionUsage,
  getTrajectoryStreamFrames,
  getTrajectorySubjectRecords,
  listTrajectorySubjects,
  MAX_SEQUENCE_REQUEST,
  TrajectoryApiError,
  type TrajectoryDetailRecord,
  type TrajectorySubjectSummary,
} from '../trajectoryClient';
import { shouldCatchUpTrajectory } from '../trajectoryArchive';
import {
  applyStreamFrames,
  forgetStreamFrames,
  withStreamFrames,
} from '../trajectoryFrames';
import {
  absorbSequencePage,
  createSequenceCache,
  rebuildRecord,
  unresolvedAttributesByRecordId,
  unresolvedHeadsOf,
} from '../trajectorySequences';
import {
  createTrajectorySubjectViewCache,
  groupTrajectorySubjects,
  type TrajectorySubjectGroups,
} from '../trajectorySubjects';
import {
  changedTrajectoryUsageTraceIds,
  collectSubjectRefreshWindow,
  createTrajectoryOperationCoordinator,
  createTrajectoryTraceHintCoordinator,
  createTrajectoryWindowState,
  resetTrajectoryWindowState,
  sameTrajectoryUsageMap,
  selectSummariesNeedingLoad,
  shouldCatchUpAfterTrajectoryTerminalEvent,
  shouldCatchUpStreamFrames,
  spansOf,
  stageTrajectoryChainPages,
  trajectoryContentMode,
  type StagedTrajectoryChain,
  type StreamFrameRefresh,
  type TrajectoryContentMode,
  type TrajectoryTerminalEventName,
} from '../trajectoryWindow';

const DETAIL_LIMIT = 1000;
const DETAIL_CONCURRENCY = 6;
const LIVE_HINT_PULL_INTERVAL_MS = 80;
// A reader returning after a long disconnect walks forward a page at a time.
// The cap bounds one refresh; whatever is left is picked up by the next.
const MAX_FRAME_CATCH_UP_PAGES = 20;
// Retention between reading the checkpoints and listing the subjects shows as
// two epochs; reading both again settles it unless retention keeps running.
const MAX_CHECKPOINT_READ_ATTEMPTS = 3;

interface TraceUpdatedPayload {
  session_id?: unknown;
  trace_id?: unknown;
  revision?: unknown;
  frame_seq?: unknown;
  store_epoch?: unknown;
  lifecycle?: unknown;
}

interface PublishedTrajectoryWindow {
  readonly records: OtlpExportTraceServiceRequest[];
  readonly rawRecords: TrajectoryDetailRecord[];
  readonly lifecycleByRecordId: ReadonlyMap<string, 'running' | 'completed' | 'error'>;
  readonly sessionCumulativeUsageByRequestIdentity: ReadonlyMap<string, TrajectoryUsage>;
  readonly checkpoints: TrajectoryRetentionCheckpoints;
}

const EMPTY_PUBLISHED_WINDOW: PublishedTrajectoryWindow = {
  records: [],
  rawRecords: [],
  lifecycleByRecordId: new Map(),
  sessionCumulativeUsageByRequestIdentity: new Map(),
  checkpoints: EMPTY_TRAJECTORY_CHECKPOINTS,
};

/** Why the trajectory could not be read, for the page to word in the UI language. */
export type DiagnosisLoadErrorCode =
  | 'TRAJECTORY_DISABLED'
  | 'UNSUPPORTED_SESSION_MODE'
  | 'RETENTION_RUNNING'
  | 'FAILED';

export interface DiagnosisLoadError {
  code: DiagnosisLoadErrorCode;
  /** The underlying message, only meaningful for `FAILED`. */
  message: string;
}

class RetentionRunningError extends Error {}

function loadErrorOf(error: unknown): DiagnosisLoadError {
  if (error instanceof RetentionRunningError) return { code: 'RETENTION_RUNNING', message: '' };
  if (error instanceof TrajectoryApiError && error.code === 'TRAJECTORY_DISABLED') {
    return { code: 'TRAJECTORY_DISABLED', message: '' };
  }
  if (error instanceof TrajectoryApiError && error.code === 'UNSUPPORTED_SESSION_MODE') {
    return { code: 'UNSUPPORTED_SESSION_MODE', message: '' };
  }
  return { code: 'FAILED', message: error instanceof Error ? error.message : '' };
}

export interface DiagnosisTrajectory {
  contentMode: TrajectoryContentMode;
  groups: TrajectorySubjectGroups;
  snapshots: ReadonlyMap<string, TrajectorySnapshot>;
  error: DiagnosisLoadError | null;
  /** Read the whole trajectory again from its head. */
  retry: () => void;
}

/**
 * Keep one session's trajectory loaded and live.
 *
 * @param active Whether the page is on screen. Publishing is deferred while
 *   hidden so a background page does not re-project on every update.
 * @param sessionId Session whose trajectory is read.
 * @param teamMode Whether the session groups its subjects as a Team.
 */
export function useDiagnosisTrajectory(
  active: boolean,
  sessionId: string,
  teamMode: boolean,
): DiagnosisTrajectory {
  const windowStateRef = useRef(createTrajectoryWindowState());
  const operationCoordinatorRef = useRef(createTrajectoryOperationCoordinator());
  const loadedSessionRef = useRef<string | null>(null);
  const requestControllerRef = useRef<AbortController | null>(null);
  const refreshPromiseRef = useRef<Promise<void> | null>(null);
  const terminalCatchUpPromiseRef = useRef<Promise<void> | null>(null);
  const terminalCatchUpAgainRef = useRef(false);
  const terminalSettleTimerRef = useRef<number | null>(null);
  const rebuildPromiseRef = useRef<Promise<boolean> | null>(null);
  const hintFlushScheduledRef = useRef(false);
  const hintCoordinatorRef = useRef(createTrajectoryTraceHintCoordinator());
  const activeRef = useRef(active);
  const deferredPublishRef = useRef(false);
  activeRef.current = active;
  // Content is addressed by the hash of itself, so a cached element can never
  // go stale and is reusable for the life of the session.
  const sequenceCacheRef = useRef(createSequenceCache());
  const subjectViewCacheRef = useRef(
    createTrajectorySubjectViewCache<ReturnType<typeof projectOtelTrajectory>>(),
  );
  const trajectoryV2ReducerRef = useRef(createTrajectoryV2Reducer());
  const sessionCumulativeUsageRef = useRef(new Map<string, TrajectoryUsage>());
  // What retention left for the session's removed turns, read before any of
  // its records so every view starts from it.
  const checkpointsRef = useRef<TrajectoryRetentionCheckpoints>(EMPTY_TRAJECTORY_CHECKPOINTS);
  // Highest frame watermark any trace hint has stated for this session.
  const hintedFrameSeqRef = useRef(0);
  const [publishedWindow, setPublishedWindow] = useState<PublishedTrajectoryWindow>(
    EMPTY_PUBLISHED_WINDOW,
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<DiagnosisLoadError | null>(null);

  const publish = useCallback((generation: number) => {
    if (!operationCoordinatorRef.current.isCurrent(generation)) return;
    if (!activeRef.current) {
      deferredPublishRef.current = true;
      return;
    }
    deferredPublishRef.current = false;
    // A span still writing its answer states nothing about it yet, so the
    // frames it has produced stand in until its own record supersedes them.
    const nextRecords = withStreamFrames(
      [...windowStateRef.current.buckets.values()].flatMap(bucket => (
        [...bucket.records.values()]
      )),
      windowStateRef.current.frames,
    ) as OtlpExportTraceServiceRequest[];
    const nextRawRecords = [...windowStateRef.current.buckets.values()].flatMap(bucket => (
      [...bucket.rawRecords.values()]
    ));
    setPublishedWindow({
      records: nextRecords,
      rawRecords: nextRawRecords,
      lifecycleByRecordId: new Map(
        [...windowStateRef.current.buckets.values()].flatMap(bucket => (
          [...(bucket.versions ?? [])].map(([identity, version]) => (
            [identity, version.lifecycle] as const
          ))
        )),
      ),
      sessionCumulativeUsageByRequestIdentity: new Map(sessionCumulativeUsageRef.current),
      checkpoints: checkpointsRef.current,
    });
  }, []);

  const refreshSessionUsage = useCallback(async (
    signal: AbortSignal,
    generation: number,
  ) => {
    const usage = await getTrajectorySessionUsage(sessionId, { signal });
    if (signal.aborted || !operationCoordinatorRef.current.isCurrent(generation)) return;
    const expectedEpoch = windowStateRef.current.storeEpoch;
    if (expectedEpoch !== null && usage.store_epoch !== expectedEpoch) return;
    const nextUsage = new Map(usage.items.map(item => (
      [`${item.trace_id}\u0000${item.inference_id}`, item.cumulative_usage]
    )));
    if (sameTrajectoryUsageMap(sessionCumulativeUsageRef.current, nextUsage)) return;
    const changedTraceIds = changedTrajectoryUsageTraceIds(
      sessionCumulativeUsageRef.current,
      nextUsage,
    );
    sessionCumulativeUsageRef.current = nextUsage;
    // The view cache does not compare usage, so it would keep a stale figure;
    // only subjects with a record in a changed trace need projecting again.
    subjectViewCacheRef.current.invalidate(group => group.records.some(record => (
      spansOf(record).some(span => changedTraceIds.has(span.traceId))
    )));
    publish(generation);
  }, [publish, sessionId]);

  const catchUpStreamFrames = useCallback(async (
    signal: AbortSignal,
    generation: number,
  ) => {
    // Walk forward from the cursor this reader holds until it is level with
    // the store. A reader that was disconnected closes the whole gap here
    // rather than waiting for the answer to finish.
    let pagesLeft = MAX_FRAME_CATCH_UP_PAGES;
    let changed = false;
    while (pagesLeft > 0) {
      pagesLeft -= 1;
      const expectedEpoch = windowStateRef.current.storeEpoch;
      const page = await getTrajectoryStreamFrames(sessionId, {
        signal,
        sinceFrameSeq: windowStateRef.current.frames.frameSeq,
      });
      if (signal.aborted
        || !operationCoordinatorRef.current.isCurrent(generation)
        || windowStateRef.current.storeEpoch !== expectedEpoch) return;
      if (page.frames.length === 0 && !page.reset) break;
      windowStateRef.current.frames = applyStreamFrames(
        windowStateRef.current.frames,
        page,
      );
      changed = true;
      if (!page.has_more) break;
    }
    if (!changed) return;
    // No cache reset: overlaying frames replaces only the records of spans
    // that have them, and the view cache compares records by identity, so it
    // re-projects just the subjects that are streaming.
    publish(generation);
  }, [publish, sessionId]);

  const clearPublishedWindow = useCallback(() => {
    resetTrajectoryWindowState(windowStateRef.current);
    deferredPublishRef.current = false;
    subjectViewCacheRef.current.clear();
    trajectoryV2ReducerRef.current.clear();
    checkpointsRef.current = EMPTY_TRAJECTORY_CHECKPOINTS;
    sessionCumulativeUsageRef.current.clear();
    hintedFrameSeqRef.current = 0;
    setPublishedWindow(EMPTY_PUBLISHED_WINDOW);
    setError(null);
  }, []);

  useEffect(() => {
    if (!active || !deferredPublishRef.current) return;
    publish(operationCoordinatorRef.current.currentGeneration());
  }, [active, publish]);

  const recoverSequences = useCallback(async (
    heads: readonly string[],
    signal: AbortSignal,
  ) => {
    for (let offset = 0; offset < heads.length; offset += MAX_SEQUENCE_REQUEST) {
      const batch = heads.slice(offset, offset + MAX_SEQUENCE_REQUEST);
      try {
        const page = await getTrajectorySequences(sessionId, batch, { signal });
        absorbSequencePage(sequenceCacheRef.current, page);
      } catch {
        // Recovering content must not cost the reader the page that needed
        // it. What could not be rebuilt stays marked on its record, and the
        // next page that states the same chain asks again.
        return;
      }
    }
  }, [sessionId]);

  const loadChain = useCallback(async (
    subjectId: string,
    targetRevision: number,
    signal: AbortSignal,
    generation: number,
  ) => {
    if (!operationCoordinatorRef.current.isCurrent(generation)) return;
    const current = windowStateRef.current.buckets.get(subjectId);
    if (current !== undefined && current.revision >= targetRevision) return;
    const publishPage = (staged: StagedTrajectoryChain) => {
      if (signal.aborted || !operationCoordinatorRef.current.isCurrent(generation)) return;
      // A finished span is final whichever page carries it, so its frames go
      // even when a newer concurrent page already holds the bucket.
      windowStateRef.current.frames = forgetStreamFrames(
        windowStateRef.current.frames,
        staged.finishedSpanKeys,
      );
      const latest = windowStateRef.current.buckets.get(subjectId);
      // A consumed revision uniquely identifies the trace state visible to
      // this coalesced detail feed. Equal or older concurrent pages cannot add
      // facts and would only trigger another full presentation publish.
      if (latest !== undefined && latest.revision >= staged.bucket.revision) return;
      windowStateRef.current.buckets.set(subjectId, staged.bucket);
      publish(generation);
    };
    const staged = await stageTrajectoryChainPages(
      current,
      signal,
      async (sinceRevision, pageSignal) => {
        const page = await getTrajectorySubjectRecords(sessionId, subjectId, {
          signal: pageSignal,
          sinceRevision,
          limit: DETAIL_LIMIT,
        });
        // Records state their restated attributes by reference. Take in what
        // this page delivered, then rebuild them from what is now held: the
        // server sends content only when it was not assumed to be cached.
        absorbSequencePage(sequenceCacheRef.current, page);
        let rebuilt = page.records.map(
          record => rebuildRecord(record, sequenceCacheRef.current),
        );
        // That assumption can be wrong -- a reload, a second device, an entry
        // the browser dropped. Ask for what is still missing by hash, which
        // makes the answer conclusive: content absent after this is content
        // the store no longer has, not content still on its way.
        const unresolved = unresolvedHeadsOf(rebuilt);
        if (unresolved.length > 0) {
          await recoverSequences(unresolved, pageSignal);
          rebuilt = page.records.map(
            record => rebuildRecord(record, sequenceCacheRef.current),
          );
        }
        return { ...page, records: rebuilt };
      },
      publishPage,
    );
    if (staged === null
      || signal.aborted
      || !operationCoordinatorRef.current.isCurrent(generation)) return;
    setError(null);
  }, [publish, recoverSequences, sessionId]);

  const loadSummaries = useCallback(async (
    summaries: readonly TrajectorySubjectSummary[],
    signal: AbortSignal,
    generation: number,
  ) => {
    for (let offset = 0; offset < summaries.length; offset += DETAIL_CONCURRENCY) {
      const batch = summaries.slice(offset, offset + DETAIL_CONCURRENCY);
      await Promise.all(batch.map(summary => loadChain(
        summary.subject_id,
        summary.revision,
        signal,
        generation,
      )));
      if (signal.aborted
        || !operationCoordinatorRef.current.isCurrent(generation)) return;
    }
  }, [loadChain]);

  const loadCheckpoints = useCallback(async (signal: AbortSignal) => {
    const response = await getTrajectoryCheckpoints(sessionId, { signal });
    absorbSequencePage(sequenceCacheRef.current, response);
    return {
      storeEpoch: response.store_epoch,
      checkpoints: resolveTrajectoryCheckpoints(response.checkpoints, sequenceCacheRef.current),
    };
  }, [sessionId]);

  const rebuildFromHead = useCallback((signal: AbortSignal): Promise<boolean> => {
    if (rebuildPromiseRef.current !== null) return rebuildPromiseRef.current;
    const coordinator = operationCoordinatorRef.current;
    const generation = coordinator.invalidate();
    clearPublishedWindow();
    setLoading(true);
    const operation = (async () => {
      try {
        // Checkpoints and listing must describe one store epoch: records
        // listed after a retention pass need the checkpoints it wrote.
        let seeded = await loadCheckpoints(signal);
        let page = await listTrajectorySubjects(sessionId, { signal });
        for (
          let attempt = 1;
          page.store_epoch !== seeded.storeEpoch && attempt < MAX_CHECKPOINT_READ_ATTEMPTS;
          attempt += 1
        ) {
          if (signal.aborted || !coordinator.isCurrent(generation)) return false;
          seeded = await loadCheckpoints(signal);
          page = await listTrajectorySubjects(sessionId, { signal });
        }
        if (signal.aborted || !coordinator.isCurrent(generation)) return false;
        if (page.store_epoch !== seeded.storeEpoch) throw new RetentionRunningError();
        checkpointsRef.current = seeded.checkpoints;
        trajectoryV2ReducerRef.current.seed(seeded.checkpoints.v2Seeds);
        await loadSummaries(page.items, signal, generation);
        if (signal.aborted || !coordinator.isCurrent(generation)) return false;
        const windowState = windowStateRef.current;
        windowState.storeEpoch = page.store_epoch;
        windowState.watermark = page.watermark;
        windowState.listWindowInitialized = true;
        // Resetting the window zeroed the frame cursor, so a page opened while
        // an answer streams must page the frames too, or its running spans wait
        // for the next hint before showing any text.
        await Promise.all([
          refreshSessionUsage(signal, generation),
          catchUpStreamFrames(signal, generation),
        ]);
        if (signal.aborted || !coordinator.isCurrent(generation)) return false;
        setError(null);
        return true;
      } catch (rebuildError) {
        if (!signal.aborted && coordinator.isCurrent(generation)) {
          setError(loadErrorOf(rebuildError));
        }
        return false;
      } finally {
        if (coordinator.isCurrent(generation)) setLoading(false);
      }
    })();
    rebuildPromiseRef.current = operation;
    void operation.finally(() => {
      if (rebuildPromiseRef.current === operation) rebuildPromiseRef.current = null;
    });
    return operation;
  }, [
    catchUpStreamFrames,
    clearPublishedWindow,
    loadCheckpoints,
    loadSummaries,
    refreshSessionUsage,
    sessionId,
  ]);

  const refreshLatest = useCallback(async (frames: StreamFrameRefresh = 'always') => {
    if (sessionId === 'new' || requestControllerRef.current?.signal.aborted) return;
    if (refreshPromiseRef.current !== null) return refreshPromiseRef.current;
    const signal = requestControllerRef.current?.signal;
    if (signal === undefined) return;
    const operation = (async () => {
      try {
        const coordinator = operationCoordinatorRef.current;
        const generation = coordinator.currentGeneration();
        if (signal.aborted || !coordinator.isCurrent(generation)) return;
        const expectedStoreEpoch = windowStateRef.current.storeEpoch;
        if (expectedStoreEpoch === null) {
          await rebuildFromHead(signal);
          return;
        }
        const loadedRevisions = new Map<string, number>(
          [...windowStateRef.current.buckets]
            .map(([subjectId, bucket]): [string, number] => [subjectId, bucket.revision]),
        );
        // One listing covers both the head and the incremental feed: the
        // watermark is the floor, and a rotated epoch means restart.
        const subjectWindow = await collectSubjectRefreshWindow(
          windowStateRef.current.watermark,
          expectedStoreEpoch,
          signal,
          (afterRevision, pageSignal) => listTrajectorySubjects(sessionId, {
            signal: pageSignal,
            afterRevision,
          }),
        );
        if (subjectWindow === null
          || signal.aborted
          || !coordinator.isCurrent(generation)) return;
        if (subjectWindow.reset) {
          await rebuildFromHead(signal);
          return;
        }
        const summaries = selectSummariesNeedingLoad(
          loadedRevisions,
          subjectWindow.summaries,
        );
        // Detail, usage and frames answer three questions of their own and
        // resume from three cursors of their own, so they travel together
        // instead of one round trip after another. Frames are what a reader
        // watches in real time; they no longer wait behind the other two.
        await Promise.all([
          (async () => {
            await loadSummaries(summaries, signal, generation);
            if (signal.aborted
              || !coordinator.isCurrent(generation)
              || windowStateRef.current.storeEpoch !== expectedStoreEpoch) return;
            // The listing watermark advances only once the detail behind it
            // is held: a subject skipped here is one the next listing would
            // not name again.
            windowStateRef.current.listWindowInitialized = true;
            windowStateRef.current.watermark = subjectWindow.watermark;
          })(),
          refreshSessionUsage(signal, generation),
          shouldCatchUpStreamFrames(
            frames,
            windowStateRef.current.frames.frameSeq,
            hintedFrameSeqRef.current,
          )
            ? catchUpStreamFrames(signal, generation)
            : undefined,
        ]);
        if (signal.aborted || !coordinator.isCurrent(generation)) return;
        setError(null);
      } catch (refreshError) {
        if (!signal.aborted) setError(loadErrorOf(refreshError));
      }
    })();
    refreshPromiseRef.current = operation;
    try {
      await operation;
    } finally {
      if (refreshPromiseRef.current === operation) {
        refreshPromiseRef.current = null;
      }
    }
  }, [
    catchUpStreamFrames,
    loadSummaries,
    rebuildFromHead,
    refreshSessionUsage,
    sessionId,
  ]);

  const catchUpAfterTerminalEvent = useCallback((): Promise<void> => {
    terminalCatchUpAgainRef.current = true;
    if (terminalCatchUpPromiseRef.current !== null) {
      return terminalCatchUpPromiseRef.current;
    }
    const operation = (async () => {
      while (terminalCatchUpAgainRef.current) {
        terminalCatchUpAgainRef.current = false;
        const inFlight = refreshPromiseRef.current;
        if (inFlight !== null) await inFlight;
        await refreshLatest();
      }
    })();
    terminalCatchUpPromiseRef.current = operation;
    void operation.finally(() => {
      if (terminalCatchUpPromiseRef.current === operation) {
        terminalCatchUpPromiseRef.current = null;
      }
    });
    return operation;
  }, [refreshLatest]);

  const flushTraceHints = useCallback(async () => {
    const signal = requestControllerRef.current?.signal;
    if (signal === undefined || signal.aborted) return;
    await hintCoordinatorRef.current.drain(async () => {
      // A hint names a trace, which no longer maps onto one chain: a trace can
      // carry records for several subjects. It is enough as a signal that
      // something advanced - the listing decides which chains to reload. The
      // refresh already pulls usage; frames only when a hint is ahead of them.
      await refreshLatest('ifBehind');
    }, () => new Promise<void>((resolve) => {
      window.setTimeout(resolve, LIVE_HINT_PULL_INTERVAL_MS);
    }));
  }, [refreshLatest]);

  useEffect(() => {
    requestControllerRef.current?.abort();
    refreshPromiseRef.current = null;
    terminalCatchUpPromiseRef.current = null;
    terminalCatchUpAgainRef.current = false;
    if (terminalSettleTimerRef.current !== null) {
      window.clearTimeout(terminalSettleTimerRef.current);
      terminalSettleTimerRef.current = null;
    }
    rebuildPromiseRef.current = null;
    operationCoordinatorRef.current.invalidate();
    const controller = new AbortController();
    requestControllerRef.current = controller;
    hintCoordinatorRef.current = createTrajectoryTraceHintCoordinator();
    hintFlushScheduledRef.current = false;
    const sessionChanged = loadedSessionRef.current !== sessionId;
    if (sessionChanged) {
      loadedSessionRef.current = sessionId;
      clearPublishedWindow();
      // Content hashes stay valid across epochs of one session, but another
      // session's content is only memory this reader will not use again.
      sequenceCacheRef.current = createSequenceCache();
    }
    if (sessionId === 'new') {
      setLoading(false);
      return () => controller.abort();
    }
    if (!sessionChanged && windowStateRef.current.storeEpoch !== null) {
      setLoading(false);
      void refreshLatest();
      return () => controller.abort();
    }
    const operation = rebuildFromHead(controller.signal).then(() => undefined);
    refreshPromiseRef.current = operation;
    void operation.finally(() => {
      if (refreshPromiseRef.current === operation) refreshPromiseRef.current = null;
    });
    return () => controller.abort();
  }, [clearPublishedWindow, rebuildFromHead, refreshLatest, sessionId]);

  useEffect(() => {
    if (sessionId === 'new') return undefined;
    const unsubscribe = webClient.on<TraceUpdatedPayload>('trace.updated', (event) => {
      if (event.payload.session_id !== sessionId) return;
      const traceId = event.payload.trace_id;
      const frameSeq = event.payload.frame_seq;
      if (typeof frameSeq === 'number'
        && Number.isSafeInteger(frameSeq)
        && frameSeq > hintedFrameSeqRef.current) {
        hintedFrameSeqRef.current = frameSeq;
      }
      const revisionValue = event.payload.revision;
      const revision = typeof revisionValue === 'number'
        ? revisionValue
        : typeof revisionValue === 'string' && /^\d+$/.test(revisionValue)
          ? Number(revisionValue)
          : Number.NaN;
      const eventEpoch = event.payload.store_epoch;
      const currentEpoch = windowStateRef.current.storeEpoch;
      if (typeof eventEpoch === 'string'
        && currentEpoch !== null
        && eventEpoch !== currentEpoch) {
        const signal = requestControllerRef.current?.signal;
        if (signal !== undefined && !signal.aborted) void rebuildFromHead(signal);
        return;
      }
      if (typeof traceId !== 'string'
        || !/^[0-9a-f]{32}$/.test(traceId)
        || !Number.isSafeInteger(revision)
        || revision < 0) {
        void refreshLatest();
        return;
      }
      hintCoordinatorRef.current.enqueue(traceId, revision);
      if (hintFlushScheduledRef.current) return;
      hintFlushScheduledRef.current = true;
      queueMicrotask(() => {
        hintFlushScheduledRef.current = false;
        void flushTraceHints().catch(() => {
          // Detail hints are only watermarks. A transient detail fetch must not
          // strand the highest revision; the coordinator requeues it and the
          // revision feed repairs the view without waiting for chat completion.
          void refreshLatest();
        });
      });
    });
    let previousConnectionState = webClient.getState();
    const unsubscribeState = webClient.onStateChange((nextConnectionState) => {
      if (shouldCatchUpTrajectory(previousConnectionState, nextConnectionState)) {
        void refreshLatest();
      }
      previousConnectionState = nextConnectionState;
    });
    const terminalEventNames: TrajectoryTerminalEventName[] = [
      'chat.final',
      'chat.processing_status',
      'chat.error',
      'execution.error',
      'harness.session_finished',
    ];
    const unsubscribeTerminalEvents = terminalEventNames.map(eventName => (
      webClient.on<Record<string, unknown>>(eventName, (event) => {
        if (!shouldCatchUpAfterTrajectoryTerminalEvent(
          eventName,
          event.payload,
          sessionId,
        )) return;
        if (terminalSettleTimerRef.current !== null) {
          window.clearTimeout(terminalSettleTimerRef.current);
        }
        terminalSettleTimerRef.current = window.setTimeout(() => {
          terminalSettleTimerRef.current = null;
          void catchUpAfterTerminalEvent();
        }, 300);
      })
    ));
    return () => {
      unsubscribe();
      unsubscribeState();
      unsubscribeTerminalEvents.forEach(unsubscribeTerminal => unsubscribeTerminal());
      if (terminalSettleTimerRef.current !== null) {
        window.clearTimeout(terminalSettleTimerRef.current);
        terminalSettleTimerRef.current = null;
      }
      hintFlushScheduledRef.current = false;
    };
  }, [catchUpAfterTerminalEvent, flushTraceHints, rebuildFromHead, refreshLatest, sessionId]);

  const subjectView = useMemo(() => subjectViewCacheRef.current.update(
    groupTrajectorySubjects(
      publishedWindow.records,
      publishedWindow.rawRecords,
      publishedWindow.lifecycleByRecordId,
      sessionId,
      { teamMode, retiredSubjects: publishedWindow.checkpoints.retiredSubjects },
    ),
    group => projectOtelTrajectory(group.records, {
      checkpoint: publishedWindow.checkpoints.projections.get(group.subject.id),
      lifecycleByRecordId: group.lifecycleByRecordId,
      sessionCumulativeUsageByRequestIdentity: publishedWindow.sessionCumulativeUsageByRequestIdentity,
      unresolvedAttributesByRecordId: unresolvedAttributesByRecordId(group.rawRecords),
      v2Reducer: trajectoryV2ReducerRef.current,
    }),
  ), [publishedWindow, sessionId, teamMode]);

  const contentMode = trajectoryContentMode({
    sessionId,
    loading,
    error: error === null ? null : (error.message === '' ? error.code : error.message),
    projectedCount: publishedWindow.records.length,
    rawCount: publishedWindow.rawRecords.length,
  });

  const retry = useCallback(() => {
    const signal = requestControllerRef.current?.signal;
    if (signal !== undefined && !signal.aborted) void rebuildFromHead(signal);
  }, [rebuildFromHead]);

  return {
    contentMode,
    groups: subjectView.groups,
    snapshots: subjectView.snapshots,
    error,
    retry,
  };
}
