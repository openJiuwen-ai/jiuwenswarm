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
