import { Fragment, useEffect, useRef, useState, type ReactNode } from 'react';
import { DrawerStateMessage } from '../DrawerStateMessage/DrawerStateMessage';
import { LoadingSpinner } from '../LoadingSpinner/LoadingSpinner';

export type PickerListStatus = 'loading' | 'error' | 'success' | 'idle';

const DEFAULT_PAGE_SIZE = 30;
const LOAD_MORE_THRESHOLD_PX = 40;

export interface PickerListRegionProps<T> {
  status: PickerListStatus;
  /** 全量数据（已按页签/搜索词过滤）；分页切片在本组件内部完成 */
  items: T[];
  renderItem: (item: T) => ReactNode;
  /** 列表 key；缺省回退为索引 */
  getItemKey?: (item: T) => string;
  /** 每批渲染数量，默认 30；滚动触底追加下一批 */
  pageSize?: number;
  loadingMessage: string;
  errorMessage?: string;
  emptyMessage: string;
  retryLabel?: string;
  onRetry?: () => void;
  loadingTestId?: string;
  errorTestId?: string;
  retryTestId?: string;
  emptyTestId?: string;
  listTestId?: string;
  loadMoreLabel?: string;
  loadMoreTestId?: string;
}

/** 抽屉滚动区（form-drawer__scroll-area）：loading/error/empty 三态 + 两列卡片网格 +
    客户端分页（首批 pageSize 条，触底追加下一批；items 引用变化后回到首批）。
    status='idle' 时渲染空滚动区（列表尚未开始加载）；success 且 items 为空时渲染空态。 */
export function PickerListRegion<T>({
  status,
  items,
  renderItem,
  getItemKey,
  pageSize = DEFAULT_PAGE_SIZE,
  loadingMessage,
  errorMessage,
  emptyMessage,
  retryLabel,
  onRetry,
  loadingTestId,
  errorTestId,
  retryTestId,
  emptyTestId,
  listTestId,
  loadMoreLabel,
  loadMoreTestId,
}: PickerListRegionProps<T>) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [visibleCount, setVisibleCount] = useState(pageSize);
  const hasMore = visibleCount < items.length;

  useEffect(() => {
    setVisibleCount(pageSize);
  }, [items, pageSize]);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el || !hasMore) return;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - LOAD_MORE_THRESHOLD_PX) {
      setVisibleCount((count) => Math.min(count + pageSize, items.length));
    }
  };

  const visibleItems = items.slice(0, visibleCount);
  const isEmpty = status === 'success' && items.length === 0;

  return (
    <div className="form-drawer__scroll-area" ref={scrollRef} onScroll={handleScroll}>
      {status === 'loading' ? (
        <DrawerStateMessage variant="loading" message={loadingMessage} testId={loadingTestId} />
      ) : status === 'error' ? (
        <DrawerStateMessage
          variant="error"
          message={errorMessage ?? ''}
          retryLabel={retryLabel}
          onRetry={onRetry}
          testId={errorTestId}
          retryTestId={retryTestId}
        />
      ) : isEmpty ? (
        <DrawerStateMessage variant="empty" message={emptyMessage} testId={emptyTestId} />
      ) : status === 'success' ? (
        <>
          <div className="grid grid-cols-2 gap-4" data-testid={listTestId}>
            {visibleItems.map((item, index) => (
              <Fragment key={getItemKey ? getItemKey(item) : index}>{renderItem(item)}</Fragment>
            ))}
          </div>
          {hasMore ? (
            <div
              className="flex items-center justify-center gap-2 py-4"
              role="status"
              aria-label={loadMoreLabel}
              data-testid={loadMoreTestId}
            >
              <LoadingSpinner size={16} />
              {loadMoreLabel ? <span className="text-sm text-text-muted">{loadMoreLabel}</span> : null}
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
