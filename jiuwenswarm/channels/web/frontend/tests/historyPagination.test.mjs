import assert from 'node:assert/strict';
import test from 'node:test';

import { canLoadOlderHistory, prefetchHistoryPages, shouldShowHistoryRetry } from '../node_modules/.cache/history-pagination/features/historyPagination.js';

test('offers retry while older history remains after a failed request', () => {
  const state = {
    loadedPages: 1,
    totalPages: 3,
    loadingMore: false,
    prepending: false,
    retryAvailable: true,
  };

  assert.equal(canLoadOlderHistory(state), true);
  assert.equal(shouldShowHistoryRetry(state), true);
});

test('marks background prefetch as failed when page two does not return', async () => {
  const requestedPages = [];
  const appliedPages = [];

  const outcome = await prefetchHistoryPages({
    initialLoadedPages: 1,
    initialTotalPages: 3,
    isCurrent: () => true,
    fetchPage: async pageIdx => {
      requestedPages.push(pageIdx);
      return null;
    },
    applyPage: page => {
      appliedPages.push(page.pageIdx);
    },
    waitForNextPaint: async () => {},
  });

  assert.equal(outcome, 'failed');
  assert.deepEqual(requestedPages, [2]);
  assert.deepEqual(appliedPages, []);
});

test('offers retry when any later page fails before history is complete', async () => {
  const requestedPages = [];
  const appliedPages = [];

  const outcome = await prefetchHistoryPages({
    initialLoadedPages: 1,
    initialTotalPages: 4,
    isCurrent: () => true,
    fetchPage: async pageIdx => {
      requestedPages.push(pageIdx);
      return pageIdx === 3
        ? null
        : {
            pageIdx,
            totalPages: 4,
          };
    },
    applyPage: page => {
      appliedPages.push(page.pageIdx);
    },
    waitForNextPaint: async () => {},
  });

  assert.equal(outcome, 'failed');
  assert.deepEqual(requestedPages, [2, 3]);
  assert.deepEqual(appliedPages, [2]);
  assert.equal(
    shouldShowHistoryRetry({
      loadedPages: 2,
      totalPages: 4,
      loadingMore: false,
      prepending: false,
      retryAvailable: true,
    }),
    true,
  );
});

test('continues automatic prefetch after a successful retry', async () => {
  const appliedPages = [];

  const outcome = await prefetchHistoryPages({
    initialLoadedPages: 2,
    initialTotalPages: 3,
    isCurrent: () => true,
    fetchPage: async pageIdx => ({
      pageIdx,
      totalPages: 3,
    }),
    applyPage: page => {
      appliedPages.push(page.pageIdx);
    },
    waitForNextPaint: async () => {},
  });

  assert.equal(outcome, 'completed');
  assert.deepEqual(appliedPages, [3]);
});

test('does not expose retry while a request is active or history is complete', () => {
  assert.equal(
    shouldShowHistoryRetry({
      loadedPages: 1,
      totalPages: 3,
      loadingMore: true,
      prepending: false,
      retryAvailable: true,
    }),
    false,
  );
  assert.equal(
    shouldShowHistoryRetry({
      loadedPages: 3,
      totalPages: 3,
      loadingMore: false,
      prepending: false,
      retryAvailable: true,
    }),
    false,
  );
});
