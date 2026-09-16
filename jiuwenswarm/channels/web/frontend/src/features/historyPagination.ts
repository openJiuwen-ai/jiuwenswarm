export interface HistoryCursorBatchDescriptor {
  requestCursor: string | null;
  nextCursor: string | null;
  hasMore: boolean;
  batchSeq: number;
}

export interface HistoryCursorApplyState {
  nextCursor: string | null;
  snapshotId: string | null;
  snapshotEnd: number;
  loadedBatchSeq: number;
}

export interface HistoryCursorApplyCandidate extends HistoryCursorBatchDescriptor {
  snapshotId: string | null;
  snapshotEnd: number;
}

export function canApplyHistoryCursorBatch(
  current: HistoryCursorApplyState | null,
  batch: HistoryCursorApplyCandidate,
): boolean {
  return Boolean(
    current
    && current.nextCursor === batch.requestCursor
    && current.loadedBatchSeq + 1 === batch.batchSeq
    && current.snapshotId === batch.snapshotId
    && current.snapshotEnd === batch.snapshotEnd
  );
}

export function filterPublishedHistoryBatch<T extends { historyBatchSeq?: number }>(
  items: T[],
  publishedBatchSeq: number,
): T[] {
  return items.filter(
    (item) => item.historyBatchSeq === undefined || item.historyBatchSeq <= publishedBatchSeq,
  );
}

export interface BoundedTimelineRange {
  start: number;
  end: number;
}

/** Move a fixed-size render window without letting previously visited history accumulate in the DOM. */
export function shiftBoundedTimelineRange(
  range: BoundedTimelineRange,
  itemCount: number,
  direction: 'older' | 'newer',
  batchSize: number,
  maxSize: number,
): BoundedTimelineRange {
  if (direction === 'older') {
    const start = Math.max(0, range.start - batchSize);
    return {
      start,
      end: Math.min(range.end, start + maxSize),
    };
  }

  const end = Math.min(itemCount, range.end + batchSize);
  return {
    start: Math.max(range.start, end - maxSize),
    end,
  };
}

export type HistoryPrefetchOutcome = 'completed' | 'failed' | 'cancelled';

interface PrefetchHistoryBatchesOptions<Batch extends HistoryCursorBatchDescriptor> {
  initialCursor: string | null;
  initialHasMore: boolean;
  initialBatchSeq: number;
  isCurrent: () => boolean;
  fetchBatch: (cursor: string, batchSeq: number) => Promise<Batch | null>;
  applyBatch: (batch: Batch) => boolean | void;
  waitForNextPaint: () => Promise<void>;
}

/** Serially fetch every batch in one immutable server snapshot. */
export async function prefetchHistoryBatches<Batch extends HistoryCursorBatchDescriptor>({
  initialCursor,
  initialHasMore,
  initialBatchSeq,
  isCurrent,
  fetchBatch,
  applyBatch,
  waitForNextPaint,
}: PrefetchHistoryBatchesOptions<Batch>): Promise<HistoryPrefetchOutcome> {
  let cursor = initialCursor;
  let hasMore = initialHasMore;
  let batchSeq = initialBatchSeq;

  while (hasMore) {
    if (!isCurrent()) return 'cancelled';
    if (!cursor) return 'failed';

    const expectedCursor = cursor;
    const expectedBatchSeq = batchSeq + 1;
    const batch = await fetchBatch(expectedCursor, expectedBatchSeq);

    if (!isCurrent()) return 'cancelled';
    if (
      !batch
      || batch.requestCursor !== expectedCursor
      || batch.batchSeq !== expectedBatchSeq
      || (batch.hasMore && !batch.nextCursor)
      || (!batch.hasMore && batch.nextCursor !== null)
    ) {
      return 'failed';
    }

    if (applyBatch(batch) === false) return 'failed';
    cursor = batch.nextCursor;
    hasMore = batch.hasMore;
    batchSeq = batch.batchSeq;
    await waitForNextPaint();
  }

  return 'completed';
}

export interface HistoryLoadMoreState {
  loadedBatchSeq: number;
  publishedBatchSeq: number;
  hasMore: boolean;
  loadingMore: boolean;
  prepending: boolean;
}

export function canLoadOlderHistory({
  loadedBatchSeq,
  publishedBatchSeq,
  hasMore,
  loadingMore,
  prepending,
}: HistoryLoadMoreState): boolean {
  if (prepending) return false;
  if (publishedBatchSeq < loadedBatchSeq) return true;
  return hasMore && !loadingMore;
}

export function shouldShowHistoryRetry(
  state: HistoryLoadMoreState & { retryAvailable: boolean },
): boolean {
  return state.retryAvailable && canLoadOlderHistory(state);
}

interface HistoryPrependScrollState {
  previousPublishedBatchSeq: number;
  publishedBatchSeq: number;
  previousScrollHeight: number;
  scrollHeight: number;
  previousScrollTop: number;
}

/** Preserve the viewport after a newly published history batch is prepended. */
export function resolveHistoryPrependScrollTop({
  previousPublishedBatchSeq,
  publishedBatchSeq,
  previousScrollHeight,
  scrollHeight,
  previousScrollTop,
}: HistoryPrependScrollState): number | null {
  if (
    previousPublishedBatchSeq <= 0
    || publishedBatchSeq <= previousPublishedBatchSeq
  ) {
    return null;
  }

  const prependedHeight = scrollHeight - previousScrollHeight;
  return prependedHeight === 0 ? null : previousScrollTop + prependedHeight;
}
