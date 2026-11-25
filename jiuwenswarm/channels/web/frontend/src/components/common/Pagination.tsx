/**
 * 通用分页条：上一页/下一页 + 页码指示 + 可选总数。
 * 支持可选页大小选择器（pageSizeOptions/onPageSizeChange）与跳页输入（onPageChange 到指定页）。
 * 供「我的技能」列表、企业技能源面板等列表复用。
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";

interface PaginationProps {
  page: number;
  totalPages: number;
  total?: number;
  onPageChange: (page: number) => void;
  /** 当前页大小；提供时渲染页大小选择器 */
  pageSize?: number;
  /** 页大小变化回调 */
  onPageSizeChange?: (pageSize: number) => void;
  /** 可选的页大小选项，默认 [10, 20, 50] */
  pageSizeOptions?: number[];
  className?: string;
}

export function Pagination({
  page,
  totalPages,
  total,
  onPageChange,
  pageSize,
  onPageSizeChange,
  pageSizeOptions = [10, 20, 50],
  className = "",
}: PaginationProps) {
  const { t } = useTranslation();
  const [jumpValue, setJumpValue] = useState("");

  if (totalPages <= 1 && pageSize === undefined) return null;

  const goToPage = (target: number) => {
    const clamped = Math.max(1, Math.min(totalPages, target));
    if (clamped !== page) onPageChange(clamped);
    setJumpValue("");
  };

  const handleJumpKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "Enter") return;
    const parsed = Number.parseInt(jumpValue, 10);
    if (Number.isNaN(parsed)) {
      setJumpValue("");
      return;
    }
    goToPage(parsed);
  };

  return (
    <div className={`flex items-center justify-end gap-3 text-sm text-text-muted flex-shrink-0 ${className}`}>
      {total != null && <span>{t("common.pagination.total", { count: total })}</span>}
      {pageSize !== undefined && onPageSizeChange && (
        <label className="flex items-center gap-1.5">
          <span className="whitespace-nowrap">{t("common.pagination.pageSize")}</span>
          <select
            value={pageSize}
            onChange={event => {
              onPageSizeChange(Number.parseInt(event.target.value, 10));
              setJumpValue("");
            }}
            className="px-2 py-1 rounded-md border border-border bg-panel text-sm text-text focus:outline-none"
          >
            {pageSizeOptions.map(size => (
              <option key={size} value={size}>
                {size}
              </option>
            ))}
          </select>
        </label>
      )}
      <button
        type="button"
        onClick={() => onPageChange(page - 1)}
        disabled={page <= 1}
        aria-label={t("common.pagination.prev")}
        className="px-3 py-1 rounded-md border border-border hover:bg-secondary/50 disabled:text-text-muted disabled:cursor-not-allowed"
      >
        ‹
      </button>
      <span className="whitespace-nowrap">
        {page} / {totalPages}
      </span>
      <button
        type="button"
        onClick={() => onPageChange(page + 1)}
        disabled={page >= totalPages}
        aria-label={t("common.pagination.next")}
        className="px-3 py-1 rounded-md border border-border hover:bg-secondary/50 disabled:text-text-muted disabled:cursor-not-allowed"
      >
        ›
      </button>
      {totalPages > 1 && (
        <label className="flex items-center gap-1.5">
          <span className="whitespace-nowrap">{t("common.pagination.jumpTo")}</span>
          <input
            type="number"
            min={1}
            max={totalPages}
            value={jumpValue}
            onChange={event => setJumpValue(event.target.value)}
            onKeyDown={handleJumpKeyDown}
            onBlur={() => jumpValue !== "" && goToPage(Number.parseInt(jumpValue, 10))}
            className="w-16 px-2 py-1 rounded-md border border-border bg-panel text-sm text-text placeholder:text-text-muted focus:outline-none"
            placeholder={String(totalPages)}
          />
          <button
            type="button"
            onClick={() => goToPage(Number.parseInt(jumpValue, 10))}
            disabled={jumpValue === ""}
            className="px-3 py-1 rounded-md border border-border hover:bg-secondary/50 disabled:text-text-muted disabled:cursor-not-allowed"
          >
            {t("common.pagination.go")}
          </button>
        </label>
      )}
    </div>
  );
}
