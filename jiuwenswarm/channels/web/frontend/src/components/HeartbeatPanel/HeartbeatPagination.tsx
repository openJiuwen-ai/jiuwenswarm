import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import SimpleSelect from '../CronPanel/SimpleSelect';

// 心跳任务列表分页：纯前端本地分页（后端 heartbeat.job.list 一次性返回全部）。
// 面板宽度只有 420px，不复用 CronPanel 的全宽版分页条：这里用"当前页 / 总页数 + 前后箭头"
// 的紧凑样式，不铺可点页码（心跳任务通常没几页，铺页码 + 省略号折叠是过度设计）。
export const HEARTBEAT_PAGE_SIZE_OPTIONS = [5, 10, 20];
export const HEARTBEAT_PAGE_SIZE_DEFAULT = 10;

interface HeartbeatPaginationProps {
  currentPage: number;
  totalPages: number;
  pageSize: number;
  totalCount: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
}

export default function HeartbeatPagination({
  currentPage,
  totalPages,
  pageSize,
  totalCount,
  onPageChange,
  onPageSizeChange,
}: HeartbeatPaginationProps) {
  const { t } = useTranslation();
  const pageSizeOptions = useMemo(
    () => HEARTBEAT_PAGE_SIZE_OPTIONS.map((n) => ({ value: String(n), label: String(n) })),
    [],
  );

  return (
    <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-t border-border px-4 py-2 text-xs text-text-muted" data-testid="heartbeat-panel-pagination">
      <div className="flex items-center gap-2" data-testid="heartbeat-panel-page-size-field">
        <span data-testid="heartbeat-panel-page-size-label">{t('heartbeat.pagination.pageSize')}</span>
        <SimpleSelect
          value={String(pageSize)}
          onChange={(v) => onPageSizeChange(Number(v))}
          options={pageSizeOptions}
          className="w-16"
          menuPlacement="up"
        />
      </div>
      <div className="flex items-center gap-2">
        <span data-testid="heartbeat-panel-pagination-total">{t('heartbeat.pagination.total', { total: totalCount })}</span>
        {totalPages > 1 && (
          <div className="flex items-center gap-1" data-testid="heartbeat-panel-pagination-controls">
            <button
              type="button"
              disabled={currentPage <= 1}
              onClick={() => onPageChange(currentPage - 1)}
              aria-label={t('heartbeat.pagination.prev') ?? undefined}
              className="flex h-6 w-6 items-center justify-center rounded-md border border-border text-text hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
              data-testid="heartbeat-panel-pagination-prev-btn"
            >
              <ChevronLeft size={13} />
            </button>
            <span className="min-w-[2.5rem] text-center tabular-nums text-text" data-testid="heartbeat-panel-pagination-page-info">
              {currentPage} / {totalPages}
            </span>
            <button
              type="button"
              disabled={currentPage >= totalPages}
              onClick={() => onPageChange(currentPage + 1)}
              aria-label={t('heartbeat.pagination.next') ?? undefined}
              className="flex h-6 w-6 items-center justify-center rounded-md border border-border text-text hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
              data-testid="heartbeat-panel-pagination-next-btn"
            >
              <ChevronRight size={13} />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
