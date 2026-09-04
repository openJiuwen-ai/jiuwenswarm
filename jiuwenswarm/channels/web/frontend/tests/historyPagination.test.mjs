import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canApplyHistoryCursorBatch,
  canLoadOlderHistory,
  filterPublishedHistoryBatch,
  prefetchHistoryBatches,
  resolveHistoryPrependScrollTop,
  shiftBoundedTimelineRange,
  shouldShowHistoryRetry,
} from '../node_modules/.cache/history-pagination/features/historyPagination.js';

function batch(requestCursor, nextCursor, batchSeq) {
  return {
    requestCursor,
    nextCursor,
    hasMore: nextCursor !== null,
    batchSeq,
  };
}

test('prefetches one immutable cursor chain until hasMore is false', async () => {
  const requested = [];
  const applied = [];
  const pages = new Map([
    ['cursor-1', batch('cursor-1', 'cursor-2', 2)],
    ['cursor-2', batch('cursor-2', null, 3)],
  ]);

  const outcome = await prefetchHistoryBatches({
    initialCursor: 'cursor-1',
    initialHasMore: true,
    initialBatchSeq: 1,
    isCurrent: () => true,
    fetchBatch: async (cursor, batchSeq) => {
      requested.push([cursor, batchSeq]);
      return pages.get(cursor) ?? null;
    },
    applyBatch: value => applied.push(value.batchSeq),
    waitForNextPaint: async () => {},
  });

  assert.equal(outcome, 'completed');
  assert.deepEqual(requested, [['cursor-1', 2], ['cursor-2', 3]]);
  assert.deepEqual(applied, [2, 3]);
});

test('fails without applying when a cursor batch does not return', async () => {
  const applied = [];
  const outcome = await prefetchHistoryBatches({
    initialCursor: 'cursor-1',
    initialHasMore: true,
    initialBatchSeq: 1,
    isCurrent: () => true,
    fetchBatch: async () => null,
    applyBatch: value => applied.push(value),
    waitForNextPaint: async () => {},
  });

  assert.equal(outcome, 'failed');
  assert.deepEqual(applied, []);
});

test('rejects a response for another cursor or out-of-order batch sequence', async () => {
  for (const invalid of [
    batch('another-cursor', null, 2),
    batch('cursor-1', null, 3),
  ]) {
    const outcome = await prefetchHistoryBatches({
      initialCursor: 'cursor-1',
      initialHasMore: true,
      initialBatchSeq: 1,
      isCurrent: () => true,
      fetchBatch: async () => invalid,
      applyBatch: () => assert.fail('invalid batch must not be applied'),
      waitForNextPaint: async () => {},
    });
    assert.equal(outcome, 'failed');
  }
});

test('rejects inconsistent hasMore and nextCursor metadata', async () => {
  const invalid = {
    requestCursor: 'cursor-1',
    nextCursor: null,
    hasMore: true,
    batchSeq: 2,
  };
  const outcome = await prefetchHistoryBatches({
    initialCursor: 'cursor-1',
    initialHasMore: true,
    initialBatchSeq: 1,
    isCurrent: () => true,
    fetchBatch: async () => invalid,
    applyBatch: () => assert.fail('invalid batch must not be applied'),
    waitForNextPaint: async () => {},
  });
  assert.equal(outcome, 'failed');
});

test('stops the cursor chain when the current state rejects a batch', async () => {
  const requested = [];
  const outcome = await prefetchHistoryBatches({
    initialCursor: 'cursor-1',
    initialHasMore: true,
    initialBatchSeq: 1,
    isCurrent: () => true,
    fetchBatch: async (cursor, batchSeq) => {
      requested.push([cursor, batchSeq]);
      return batch(cursor, 'cursor-2', batchSeq);
    },
    applyBatch: () => false,
    waitForNextPaint: async () => {},
  });

  assert.equal(outcome, 'failed');
  assert.deepEqual(requested, [['cursor-1', 2]]);
});

test('applies a batch only once at the exact cursor and snapshot boundary', () => {
  const current = {
    nextCursor: 'cursor-1',
    snapshotId: 'snapshot-a',
    snapshotEnd: 50_000_000,
    loadedBatchSeq: 1,
  };
  const candidate = {
    ...batch('cursor-1', 'cursor-2', 2),
    snapshotId: 'snapshot-a',
    snapshotEnd: 50_000_000,
  };
  assert.equal(canApplyHistoryCursorBatch(current, candidate), true);
  assert.equal(canApplyHistoryCursorBatch({ ...current, loadedBatchSeq: 2 }, candidate), false);
  assert.equal(canApplyHistoryCursorBatch(current, { ...candidate, snapshotId: 'snapshot-b' }), false);
  assert.equal(canApplyHistoryCursorBatch(current, { ...candidate, requestCursor: 'cursor-x' }), false);
});

test('publishes requested history batches while always retaining the live tail', () => {
  const items = [
    { id: 'old-hidden', historyBatchSeq: 3 },
    { id: 'old-visible', historyBatchSeq: 1 },
    { id: 'live-tool' },
    { id: 'live-message' },
  ];
  assert.deepEqual(
    filterPublishedHistoryBatch(items, 1).map(item => item.id),
    ['old-visible', 'live-tool', 'live-message'],
  );
  assert.deepEqual(
    filterPublishedHistoryBatch(items, 3).map(item => item.id),
    ['old-hidden', 'old-visible', 'live-tool', 'live-message'],
  );
});

test('moves through the full timeline while keeping a hard DOM window bound', () => {
  const itemCount = 1_000;
  const maxSize = 120;
  let range = { start: 920, end: itemCount };

  while (range.start > 0) {
    range = shiftBoundedTimelineRange(range, itemCount, 'older', 40, maxSize);
    assert.ok(range.end - range.start <= maxSize);
  }
  assert.deepEqual(range, { start: 0, end: 120 });

  while (range.end < itemCount) {
    range = shiftBoundedTimelineRange(range, itemCount, 'newer', 40, maxSize);
    assert.ok(range.end - range.start <= maxSize);
  }
  assert.deepEqual(range, { start: 880, end: 1_000 });
});

test('cancels before or after a request when generation is stale', async () => {
  let current = false;
  assert.equal(await prefetchHistoryBatches({
    initialCursor: 'cursor-1',
    initialHasMore: true,
    initialBatchSeq: 1,
    isCurrent: () => current,
    fetchBatch: async () => assert.fail('stale generation must not fetch'),
    applyBatch: () => {},
    waitForNextPaint: async () => {},
  }), 'cancelled');

  current = true;
  assert.equal(await prefetchHistoryBatches({
    initialCursor: 'cursor-1',
    initialHasMore: true,
    initialBatchSeq: 1,
    isCurrent: () => current,
    fetchBatch: async () => {
      current = false;
      return batch('cursor-1', null, 2);
    },
    applyBatch: () => assert.fail('stale response must not be applied'),
    waitForNextPaint: async () => {},
  }), 'cancelled');
});

test('allows reading already loaded batches after backend loading completes', () => {
  const state = {
    loadedBatchSeq: 10,
    publishedBatchSeq: 1,
    hasMore: false,
    loadingMore: false,
    prepending: false,
  };
  assert.equal(canLoadOlderHistory(state), true);
});

test('allows revealing an arrived batch while the next cursor request is still running', () => {
  const state = {
    loadedBatchSeq: 3,
    publishedBatchSeq: 2,
    hasMore: true,
    loadingMore: true,
    prepending: false,
  };
  assert.equal(canLoadOlderHistory(state), true);
  assert.equal(canLoadOlderHistory({
    ...state,
    publishedBatchSeq: state.loadedBatchSeq,
  }), false);
  assert.equal(canLoadOlderHistory({
    ...state,
    prepending: true,
  }), false);
});

test('offers retry only when an older cursor remains and no request is active', () => {
  const state = {
    loadedBatchSeq: 1,
    publishedBatchSeq: 1,
    hasMore: true,
    loadingMore: false,
    prepending: false,
    retryAvailable: true,
  };
  assert.equal(shouldShowHistoryRetry(state), true);
  assert.equal(shouldShowHistoryRetry({ ...state, loadingMore: true }), false);
  assert.equal(shouldShowHistoryRetry({ ...state, hasMore: false }), false);
});

test('preserves the visible anchor only when a new batch is published', () => {
  assert.equal(resolveHistoryPrependScrollTop({
    previousPublishedBatchSeq: 4,
    publishedBatchSeq: 5,
    previousScrollHeight: 4_000,
    scrollHeight: 5_000,
    previousScrollTop: 900,
  }), 1_900);
  assert.equal(resolveHistoryPrependScrollTop({
    previousPublishedBatchSeq: 5,
    publishedBatchSeq: 5,
    previousScrollHeight: 5_000,
    scrollHeight: 5_600,
    previousScrollTop: 900,
  }), null);
});
