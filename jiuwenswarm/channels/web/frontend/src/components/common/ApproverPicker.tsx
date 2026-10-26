import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, Search } from 'lucide-react';
import type { ApprovalApprover } from '../../features/approvals/approvalsApi';

function matchesQuery(item: ApprovalApprover, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  const userId = (item.user_id || '').toLowerCase();
  const displayName = (item.display_name || '').toLowerCase();
  return userId.includes(q) || displayName.includes(q);
}

function formatSelected(item: ApprovalApprover | undefined): string {
  if (!item) return '';
  const name = (item.display_name || '').trim();
  const id = (item.user_id || '').trim();
  if (name && id && name !== id) return `${name} (${id})`;
  return name || id;
}

interface ApproverPickerProps {
  value: string;
  options: ApprovalApprover[];
  onChange: (userId: string) => void;
  disabled?: boolean;
  loading?: boolean;
  className?: string;
}

/** 审批人可搜索下拉：展示 display_name + user_id，支持按二者搜索。 */
export function ApproverPicker({
  value,
  options,
  onChange,
  disabled = false,
  loading = false,
  className = '',
}: ApproverPickerProps) {
  const { t } = useTranslation();
  const listId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');

  const selected = useMemo(
    () => options.find((item) => item.user_id === value),
    [options, value],
  );
  const filtered = useMemo(
    () => options.filter((item) => matchesQuery(item, query)),
    [options, query],
  );

  useEffect(() => {
    if (!open) {
      setQuery('');
      return;
    }
    const timer = window.setTimeout(() => searchRef.current?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const emptyHint = loading
    ? t('common.loading')
    : options.length === 0
      ? t('agent.quota.expandApproverEmpty')
      : t('agent.quota.expandApproverPlaceholder');

  return (
    <div className={`relative mb-3 ${className}`} ref={rootRef}>
      <button
        type="button"
        className="flex w-full items-center justify-between gap-2 rounded-md border border-border bg-background px-3 py-2 text-left text-sm disabled:cursor-not-allowed disabled:opacity-60"
        disabled={disabled || loading || options.length === 0}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => setOpen((current) => !current)}
      >
        <span className={selected ? 'truncate text-text' : 'truncate text-text-muted'}>
          {selected ? formatSelected(selected) : emptyHint}
        </span>
        <ChevronDown className={`h-4 w-4 shrink-0 text-text-muted transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && !disabled && options.length > 0 ? (
        <div
          className="absolute left-0 right-0 top-[calc(100%+4px)] z-50 overflow-hidden rounded-md border border-border bg-card shadow-lg"
          role="listbox"
          id={listId}
        >
          <div className="flex items-center gap-2 border-b border-border px-3 py-2">
            <Search className="h-3.5 w-3.5 shrink-0 text-text-muted" aria-hidden />
            <input
              ref={searchRef}
              type="text"
              className="w-full bg-transparent text-sm text-text outline-none placeholder:text-text-muted"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t('agent.quota.expandApproverSearch')}
              aria-label={t('agent.quota.expandApproverSearch')}
            />
          </div>
          <div className="max-h-52 overflow-y-auto py-1">
            {filtered.length === 0 ? (
              <div className="px-3 py-2 text-sm text-text-muted">
                {t('agent.quota.expandApproverNoMatch')}
              </div>
            ) : (
              filtered.map((item) => {
                const active = item.user_id === value;
                const name = (item.display_name || '').trim() || item.user_id;
                return (
                  <button
                    key={item.user_id}
                    type="button"
                    role="option"
                    aria-selected={active}
                    className={`flex w-full flex-col items-start gap-0.5 px-3 py-2 text-left hover:bg-secondary/60 ${
                      active ? 'bg-secondary/40' : ''
                    }`}
                    onClick={() => {
                      onChange(item.user_id);
                      setOpen(false);
                    }}
                  >
                    <span className="text-sm font-medium text-text-strong break-all">{name}</span>
                    <span className="font-mono text-xs text-text-muted break-all">{item.user_id}</span>
                  </button>
                );
              })
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
