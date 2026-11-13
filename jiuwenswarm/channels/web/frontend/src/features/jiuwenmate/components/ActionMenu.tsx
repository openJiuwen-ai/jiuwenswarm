import { Ellipsis, type LucideIcon } from 'lucide-react';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { computeMenuPosition, type MenuAlign } from './menuPosition';

export interface JiuwenMateMenuItem {
  key: string;
  label: string;
  icon?: LucideIcon;
  iconSrc?: string;
  iconHue?: number;
  disabled?: boolean;
  onSelect: () => void;
}

interface ActionMenuProps {
  label: string;
  items: readonly JiuwenMateMenuItem[];
  emptyLabel?: string;
  trigger?: ReactNode;
  className?: string;
  align?: MenuAlign;
  testId?: string;
  /** Anchor the list to the viewport so a scrolling container cannot clip it. */
  floating?: boolean;
}

export function ActionMenu({
  label,
  items,
  emptyLabel,
  trigger,
  className,
  align = 'end',
  testId,
  floating = false,
}: ActionMenuProps) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open || !floating) {
      setPosition(null);
      return undefined;
    }
    const place = () => {
      const anchor = triggerRef.current?.getBoundingClientRect();
      const list = listRef.current;
      if (!anchor || !list) return;
      const next = computeMenuPosition(
        anchor,
        { width: list.offsetWidth, height: list.offsetHeight },
        { width: window.innerWidth, height: window.innerHeight },
        align,
      );
      setPosition((current) =>
        current && current.left === next.left && current.top === next.top
          ? current
          : { left: next.left, top: next.top },
      );
    };
    // Measure the hidden panel first, including its viewport height limit.
    place();
    const observer =
      typeof ResizeObserver === 'undefined' || !triggerRef.current
        ? null
        : new ResizeObserver(place);
    observer?.observe(triggerRef.current as HTMLButtonElement);
    return () => observer?.disconnect();
  }, [open, floating, align]);

  useEffect(() => {
    if (!open) return undefined;
    // resize targets Window (no nodeType); close unless the event is from inside the menu.
    const close = (event: Event) => {
      const target = event.target;
      if (
        target != null
        && typeof (target as Node).nodeType === 'number'
        && rootRef.current?.contains(target as Node)
      ) {
        return;
      }
      setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', escape);
    // A viewport-anchored list would drift away from its trigger once anything scrolls or resizes.
    if (floating) {
      window.addEventListener('scroll', close, true);
      window.addEventListener('resize', close);
    }
    return () => {
      document.removeEventListener('mousedown', close);
      document.removeEventListener('keydown', escape);
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('resize', close);
    };
  }, [open, floating]);

  return (
    <div
      ref={rootRef}
      className={`jiuwenmate-menu${open ? ' jiuwenmate-menu--open' : ''}${className ? ` ${className}` : ''}`}
      data-align={align}
    >
      <button
        ref={triggerRef}
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
        <div
          ref={listRef}
          className="jiuwenmate-menu__list"
          role="menu"
          style={floating ? {
            position: 'fixed',
            right: 'auto',
            left: position?.left ?? 0,
            top: position?.top ?? 0,
            visibility: position ? undefined : 'hidden',
          } : undefined}
          data-testid="jiuwenmate-menu-list"
        >
          {items.length === 0 ? <p className="jiuwenmate-menu__empty">{emptyLabel}</p> : null}
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
                {item.iconSrc ? (
                  <img
                    src={item.iconSrc}
                    alt=""
                    aria-hidden
                    data-hue={item.iconHue}
                    style={
                      item.iconHue === undefined
                        ? undefined
                        : { ['--jm-avatar-hue' as string]: `${item.iconHue}deg` }
                    }
                  />
                ) : null}
                <span>{item.label}</span>
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
