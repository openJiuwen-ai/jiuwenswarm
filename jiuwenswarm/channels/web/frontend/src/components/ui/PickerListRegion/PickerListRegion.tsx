import { Fragment, useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
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
  /** testid 基名（通常与外层 PickerDrawer 的 testId 一致）；
      派生规则：`-loading`/`-error`/`-retry`/`-empty`/`-list`/`-load-more`。 */
  testId?: string;
  loadMoreLabel?: string;
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
  testId,
  loadMoreLabel,
}: PickerListRegionProps<T>) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const [visibleCount, setVisibleCount] = useState(pageSize);
  const hasMore = visibleCount < items.length;

  useEffect(() => {
    setVisibleCount(pageSize);
  }, [items, pageSize]);

  const appendNextBatch = useCallback(() => {
    setVisibleCount((count) => (count < items.length ? Math.min(count + pageSize, items.length) : count));
  }, [items.length, pageSize]);

  // 时间戳节流：滚动事件/IntersectionObserver 高频触发，100ms 内只允许追加一次，
  // 避免一帧内连续追加多批；一次惯性滑动受限于实际渲染节奏，不会再"几百条"地连发。
  const lastAppendAtRef = useRef(0);
  const tryAppend = useCallback(() => {
    const now = performance.now();
    if (now - lastAppendAtRef.current < 100) return;
    lastAppendAtRef.current = now;
    appendNextBatch();
  }, [appendNextBatch]);

  // 滚动触底兜底：IntersectionObserver 为主路径，这里覆盖快速滑动越过哨兵的情形。
  const handleScroll = useCallback(() => {
    if (!hasMore) return;
    const el = scrollRef.current;
    if (!el) return;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - LOAD_MORE_THRESHOLD_PX) tryAppend();
  }, [hasMore, tryAppend]);

  // 哨兵进入视口即追加下一批；rootMargin 提前 LOAD_MORE_THRESHOLD_PX 触发，体验更平滑。
  // jsdom 等无布局环境没有 IntersectionObserver，跳过（由 onScroll + 兜底 effect 覆盖）。
  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return;
    const root = scrollRef.current;
    const sentinel = sentinelRef.current;
    if (!root || !sentinel) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) tryAppend();
      },
      { root, rootMargin: `0px 0px ${LOAD_MORE_THRESHOLD_PX}px 0px` },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [tryAppend]);

  // 兜底：首批没撑出滚动条（clientHeight 可测且 scrollHeight <= clientHeight）时持续追加，
  // 直到出现滚动条或加载完。否则哨兵恒在视口内但 IntersectionObserver 不会再回调（已处于相交态），
  // 后续批次永远加载不出来。clientHeight === 0（无布局，如 jsdom）时跳过，避免误判一次性全量加载。
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !hasMore) return;
    if (el.clientHeight === 0 || el.scrollHeight > el.clientHeight) return;
    appendNextBatch();
  }, [hasMore, appendNextBatch, visibleCount]);

  const visibleItems = items.slice(0, visibleCount);
  const isEmpty = status === 'success' && items.length === 0;

  return (
    <div className="form-drawer__scroll-area" ref={scrollRef} onScroll={handleScroll}>
      {status === 'loading' ? (
        <DrawerStateMessage
          variant="loading"
          message={loadingMessage}
          testId={testId ? `${testId}-loading` : undefined}
        />
      ) : status === 'error' ? (
        <DrawerStateMessage
          variant="error"
          message={errorMessage ?? ''}
          retryLabel={retryLabel}
          onRetry={onRetry}
          testId={testId ? `${testId}-error` : undefined}
          retryTestId={testId ? `${testId}-retry` : undefined}
        />
      ) : isEmpty ? (
        <DrawerStateMessage variant="empty" message={emptyMessage} testId={testId ? `${testId}-empty` : undefined} />
      ) : status === 'success' ? (
        <>
          <div className="grid grid-cols-2 gap-4" data-testid={testId ? `${testId}-list` : undefined}>
            {visibleItems.map((item, index) => (
              <Fragment key={getItemKey ? getItemKey(item) : index}>{renderItem(item)}</Fragment>
            ))}
          </div>
          {hasMore ? (
            <div
              ref={sentinelRef}
              className="flex items-center justify-center gap-2 py-4"
              role="status"
              aria-label={loadMoreLabel}
              data-testid={testId ? `${testId}-load-more` : undefined}
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
