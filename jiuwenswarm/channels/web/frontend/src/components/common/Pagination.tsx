import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import SimpleSelect from '../CronPanel/SimpleSelect';

const PAGE_SIZE_OPTIONS = [10, 20, 50];

export interface PaginationProps {
  page: number;
  totalPages: number;
  total: number;
  pageSize: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
  className?: string;
}

/** 通用分页条：每页条数 + 总数 + 上一页/下一页。 */
export function Pagination({
  page,
  totalPages,
  total,
  pageSize,
  onPageChange,
  onPageSizeChange,
  className,
}: PaginationProps) {
  const { t } = useTranslation();
  const pageSizeOptions = useMemo(
    () => PAGE_SIZE_OPTIONS.map(n => ({ value: String(n), label: String(n) })),
    [],
  );

  return (
    <div
      className={`flex flex-wrap items-center justify-between gap-x-3 gap-y-2 text-xs text-text-muted ${className ?? ''}`}
      data-testid="common-pagination"
    >
      <div className="flex items-center gap-2">
        <span>{t('heartbeat.pagination.pageSize')}</span>
        <SimpleSelect
          value={String(pageSize)}
          onChange={v => onPageSizeChange(Number(v))}
          options={pageSizeOptions}
          className="w-16"
          menuPlacement="up"
        />
        <span>{t('heartbeat.pagination.total', { total })}</span>
      </div>
      {totalPages > 1 && (
        <div className="flex items-center gap-1">
          <button
            type="button"
            disabled={page <= 1}
            onClick={() => onPageChange(page - 1)}
            aria-label={t('heartbeat.pagination.prev') ?? undefined}
            className="flex h-6 w-6 items-center justify-center rounded-md border border-border text-text hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
            data-testid="common-pagination-prev"
          >
            <ChevronLeft size={13} />
          </button>
          <span className="min-w-[2.5rem] text-center tabular-nums text-text">
            {page} / {totalPages}
          </span>
          <button
            type="button"
            disabled={page >= totalPages}
            onClick={() => onPageChange(page + 1)}
            aria-label={t('heartbeat.pagination.next') ?? undefined}
            className="flex h-6 w-6 items-center justify-center rounded-md border border-border text-text hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
            data-testid="common-pagination-next"
          >
            <ChevronRight size={13} />
          </button>
        </div>
      )}
    </div>
  );
}
