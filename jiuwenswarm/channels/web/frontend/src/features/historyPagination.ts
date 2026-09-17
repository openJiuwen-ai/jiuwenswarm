export interface HistoryPageDescriptor {
  pageIdx: number;
  totalPages: number;
}

export type HistoryPrefetchOutcome = 'completed' | 'failed' | 'cancelled';

interface PrefetchHistoryPagesOptions<Page extends HistoryPageDescriptor> {
  initialLoadedPages: number;
  initialTotalPages: number;
  isCurrent: () => boolean;
  fetchPage: (pageIdx: number, totalPages: number) => Promise<Page | null>;
  applyPage: (page: Page) => void;
  waitForNextPaint: () => Promise<void>;
}

export async function prefetchHistoryPages<Page extends HistoryPageDescriptor>({
  initialLoadedPages,
  initialTotalPages,
  isCurrent,
  fetchPage,
  applyPage,
  waitForNextPaint,
}: PrefetchHistoryPagesOptions<Page>): Promise<HistoryPrefetchOutcome> {
  let loadedPages = initialLoadedPages;
  let totalPages = initialTotalPages;

  while (loadedPages < totalPages) {
    if (!isCurrent()) {
      return 'cancelled';
    }

    const nextPage = loadedPages + 1;
    const page = await fetchPage(nextPage, totalPages);

    if (!isCurrent()) {
      return 'cancelled';
    }
    if (!page || page.pageIdx !== nextPage) {
      return 'failed';
    }

    applyPage(page);
    loadedPages = page.pageIdx;
    totalPages = page.totalPages;
    await waitForNextPaint();
  }

  return 'completed';
}

interface HistoryLoadMoreState {
  loadedPages: number;
  totalPages: number;
  loadingMore: boolean;
  prepending: boolean;
}

export function canLoadOlderHistory({ loadedPages, totalPages, loadingMore, prepending }: HistoryLoadMoreState): boolean {
  return loadedPages < totalPages && !loadingMore && !prepending;
}

export function shouldShowHistoryRetry(state: HistoryLoadMoreState & { retryAvailable: boolean }): boolean {
  return state.retryAvailable && canLoadOlderHistory(state);
}
