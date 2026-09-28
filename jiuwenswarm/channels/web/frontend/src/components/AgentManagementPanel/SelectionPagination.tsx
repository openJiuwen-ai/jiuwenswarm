import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

export const SELECTION_PAGE_SIZE = 10;

type SelectionPaginationState<T> = {
  pageItems: T[];
  page: number;
  totalPages: number;
  setPage: (page: number) => void;
};

export function useSelectionPagination<T>(items: T[], resetKey: string): SelectionPaginationState<T> {
  const [pageState, setPageState] = useState(1);
  const totalPages = Math.max(1, Math.ceil(items.length / SELECTION_PAGE_SIZE));
  const page = Math.min(pageState, totalPages);

  useEffect(() => {
    setPageState(1);
  }, [resetKey]);

  return {
    pageItems: items.slice((page - 1) * SELECTION_PAGE_SIZE, page * SELECTION_PAGE_SIZE),
    page,
    totalPages,
    setPage: (nextPage) => setPageState(Math.min(Math.max(nextPage, 1), totalPages)),
  };
}

type SelectionPaginationProps = {
  page: number;
  totalPages: number;
  totalItems: number;
  onPageChange: (page: number) => void;
  testId: string;
  previousTestId?: string;
  nextTestId?: string;
};

export function SelectionPagination({
  page,
  totalPages,
  totalItems,
  onPageChange,
  testId,
  previousTestId,
  nextTestId,
}: SelectionPaginationProps) {
  const { t } = useTranslation();
  if (totalPages <= 1) return null;

  return (
    <div
      className="agent-management-pagination"
      aria-label={t('agentManagement.form.selectionPaginationLabel')}
      data-testid={testId}
    >
      <span>
        {t('agentManagement.pagination.range', {
          start: (page - 1) * SELECTION_PAGE_SIZE + 1,
          end: Math.min(page * SELECTION_PAGE_SIZE, totalItems),
          total: totalItems,
        })}
      </span>
      <div className="agent-management-pagination__buttons">
        <button
          type="button"
          data-testid={previousTestId || `${testId}-previous`}
          disabled={page <= 1}
          onClick={() => onPageChange(page - 1)}
          aria-label={t('agentManagement.pagination.previous')}
        >
          <ChevronLeft size={16} aria-hidden="true" />
        </button>
        <span>{t('agentManagement.pagination.page', { page, total: totalPages })}</span>
        <button
          type="button"
          data-testid={nextTestId || `${testId}-next`}
          disabled={page >= totalPages}
          onClick={() => onPageChange(page + 1)}
          aria-label={t('agentManagement.pagination.next')}
        >
          <ChevronRight size={16} aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}
