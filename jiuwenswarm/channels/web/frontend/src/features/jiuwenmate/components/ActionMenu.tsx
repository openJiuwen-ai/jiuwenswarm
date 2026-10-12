import { Ellipsis, type LucideIcon } from 'lucide-react';
import { useEffect, useRef, useState, type ReactNode } from 'react';

export interface JiuwenMateMenuItem {
  key: string;
  label: string;
  icon?: LucideIcon;
  iconSrc?: string;
  disabled?: boolean;
  onSelect: () => void;
}

interface ActionMenuProps {
  label: string;
  items: readonly JiuwenMateMenuItem[];
  trigger?: ReactNode;
  className?: string;
  align?: 'start' | 'end';
  testId?: string;
}

export function ActionMenu({ label, items, trigger, className, align = 'end', testId }: ActionMenuProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return undefined;
    const close = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('mousedown', close);
      document.removeEventListener('keydown', escape);
    };
  }, [open]);

  return (
    <div
      ref={rootRef}
      className={`jiuwenmate-menu${open ? ' jiuwenmate-menu--open' : ''}${className ? ` ${className}` : ''}`}
      data-align={align}
    >
      <button
        type="button"
        className="jiuwenmate-menu__trigger"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={(event) => {
          event.stopPropagation();
          setOpen((current) => !current);
        }}
        data-testid={testId}
      >
        {trigger ?? <Ellipsis aria-hidden />}
      </button>
      {open ? (
        <div className="jiuwenmate-menu__list" role="menu">
          {items.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.key}
                type="button"
                role="menuitem"
                disabled={item.disabled}
                onClick={(event) => {
                  event.stopPropagation();
                  setOpen(false);
                  item.onSelect();
                }}
                data-testid="jiuwenmate-menu-item"
                data-variant={item.key}
              >
                {Icon ? <Icon aria-hidden /> : null}
                {item.iconSrc ? <img src={item.iconSrc} alt="" aria-hidden /> : null}
                <span>{item.label}</span>
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
