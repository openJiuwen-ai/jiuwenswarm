/**
 * 表头列筛选：标签 + 漏斗图标，点击弹出单选菜单。
 * value 为空串表示「全部 / 未筛选」。
 * 供列表表格在 thead 内复用（对齐管理面 TableColumnFilter 交互）。
 */

import { useCallback, useRef, useState, type ReactNode } from 'react';
import { useClickOutside } from '../CronPanel/useClickOutside';

export type TableColumnFilterOption = {
  value: string;
  label: ReactNode;
};

export type TableColumnFilterProps = {
  label: string;
  value: string;
  options: TableColumnFilterOption[];
  onChange: (value: string) => void;
  /** 仅展示筛选图标，不展示 label 文本（仍用于 aria-label） */
  iconOnly?: boolean;
  /** 下拉对齐：默认 left */
  menuAlign?: 'left' | 'right';
  className?: string;
};

export function TableColumnFilter({
  label,
  value,
  options,
  onChange,
  iconOnly = false,
  menuAlign = 'left',
  className = '',
}: TableColumnFilterProps) {
  const [open, setOpen] = useState(false);
  const hostRef = useRef<HTMLDivElement>(null);
  const active = value !== '';
  const dismiss = useCallback(() => setOpen(false), []);
  useClickOutside(hostRef, open, dismiss);

  return (
    <div
      ref={hostRef}
      className={`relative inline-flex max-w-full items-center gap-1 ${open ? 'z-[2]' : ''} ${className}`}
      data-testid="table-column-filter"
    >
      {!iconOnly ? <span className="truncate font-medium">{label}</span> : null}
      <button
        type="button"
        aria-label={label}
        aria-expanded={open}
        className={`inline-flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-md border-0 bg-transparent transition-colors ${
          active ? 'text-accent' : 'text-text-muted'
        } ${open ? 'bg-bg-hover text-text' : 'hover:bg-bg-hover hover:text-text'}`}
        onClick={() => setOpen((prev) => !prev)}
      >
        <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M3 4.5h18M7 9.75h10M10.5 15h3" />
        </svg>
      </button>
      {open ? (
        <div
          role="menu"
          className={`absolute top-[calc(100%+6px)] z-30 min-w-[148px] rounded-lg border border-border bg-card py-1.5 shadow-lg ${
            menuAlign === 'right' ? 'right-0' : 'left-0'
          }`}
        >
          {options.map((opt) => {
            const selected = value === opt.value;
            return (
              <button
                key={opt.value || '__all__'}
                type="button"
                role="menuitemradio"
                aria-checked={selected}
                className={`block w-full px-3 py-1.5 text-left text-sm hover:bg-bg-hover ${
                  selected ? 'bg-accent-subtle font-medium text-accent' : 'text-text'
                }`}
                onClick={() => {
                  onChange(opt.value);
                  setOpen(false);
                }}
              >
                {opt.label}
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
