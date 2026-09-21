import {
  forwardRef,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import { createPortal } from 'react-dom';
import './Select.css';

export type SelectOption = {
  value: string;
  label: string;
  disabled?: boolean;
  disabledReason?: string;
};
export type SelectProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'onChange'> & {
  options: readonly SelectOption[];
  invalid?: boolean;
  onChange?: (value: string) => void;
};

const TRIGGER_GAP = 6;
const VIEWPORT_MARGIN = 8;
/** 退出动画时长，需与 Select.css 中 ui-dropdown-menu--closing 的 animation-duration 保持一致 */
const EXIT_DURATION_MS = 120;

/** 自定义下拉：trigger 样式与原生 select 一致，面板门户挂到 body，样式复用 ui-dropdown-menu（参考 DropdownMenu） */
export const Select = forwardRef<HTMLButtonElement, SelectProps>(function Select(
  { options, invalid = false, className, onChange, value, defaultValue, disabled, onBlur, id, ...props },
  forwardedRef,
) {
  const isControlled = value !== undefined;
  const [internalValue, setInternalValue] = useState(() => String(defaultValue ?? ''));
  const selected = isControlled ? String(value) : internalValue;
  const selectedOption = options.find((option) => option.value === selected);
  const [open, setOpen] = useState(false);
  // 三态渲染：open 挂载并播放入场动画；closing 播完出场动画再卸载（同 DropdownMenuContent）
  const [phase, setPhase] = useState<'open' | 'closing' | null>(open ? 'open' : null);
  const [position, setPosition] = useState<{ left: number; top: number; minWidth: number } | null>(null);
  const panelId = useId();
  const innerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const restoreFocusRef = useRef(false);

  const setTrigger = (node: HTMLButtonElement | null) => {
    innerRef.current = node;
    if (typeof forwardedRef === 'function') forwardedRef(node);
    else if (forwardedRef != null) (forwardedRef as { current: HTMLButtonElement | null }).current = node;
  };

  // phase 在渲染期与受控 open 同步，打开首帧即挂载并定位
  if (open && phase !== 'open') setPhase('open');
  else if (!open && phase === 'open') setPhase('closing');

  useEffect(() => {
    if (phase !== 'closing') return;
    const timer = window.setTimeout(() => setPhase(null), EXIT_DURATION_MS);
    return () => window.clearTimeout(timer);
  }, [phase]);

  const close = useCallback(() => {
    if (!open) return;
    restoreFocusRef.current = panelRef.current?.contains(document.activeElement) ?? false;
    setOpen(false);
  }, [open]);

  // 关闭前焦点在面板内（键盘/点选）才把焦点还给 trigger
  useEffect(() => {
    if (open || !restoreFocusRef.current) return;
    restoreFocusRef.current = false;
    const active = document.activeElement;
    if (active === document.body || panelRef.current?.contains(active)) {
      innerRef.current?.focus();
    }
  }, [open]);

  useLayoutEffect(() => {
    if (!open) return;
    const trigger = innerRef.current;
    const panel = panelRef.current;
    if (!trigger || !panel) return;

    const reposition = () => {
      const triggerRect = trigger.getBoundingClientRect();
      const panelRect = panel.getBoundingClientRect();
      const below = triggerRect.bottom + TRIGGER_GAP;
      const top =
        below + panelRect.height <= window.innerHeight - VIEWPORT_MARGIN
          ? below
          : Math.max(VIEWPORT_MARGIN, triggerRect.top - panelRect.height - TRIGGER_GAP);
      const left = Math.min(
        Math.max(triggerRect.left, VIEWPORT_MARGIN),
        Math.max(VIEWPORT_MARGIN, window.innerWidth - panelRect.width - VIEWPORT_MARGIN),
      );
      setPosition({ left, top, minWidth: triggerRect.width });
    };
    reposition();
    window.addEventListener('scroll', reposition, true);
    window.addEventListener('resize', reposition);
    return () => {
      window.removeEventListener('scroll', reposition, true);
      window.removeEventListener('resize', reposition);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (panelRef.current?.contains(target) || innerRef.current?.contains(target)) return;
      close();
    };
    document.addEventListener('mousedown', handlePointerDown);
    return () => document.removeEventListener('mousedown', handlePointerDown);
  }, [open, close]);

  useEffect(() => {
    if (!open) return;
    panelRef.current?.focus({ preventScroll: true });
  }, [open]);

  const openPanel = useCallback(() => {
    if (disabled) return;
    setPosition(null);
    setOpen(true);
  }, [disabled]);

  const select = (option: SelectOption) => {
    if (option.disabled) return;
    // 点击已选中项不重复触发 onChange（与原生 select 一致），仅关闭面板
    if (option.value !== selected) {
      if (!isControlled) setInternalValue(option.value);
      onChange?.(option.value);
    }
    close();
  };

  const handleTriggerKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (open) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      openPanel();
    }
  };

  const handlePanelKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const panel = panelRef.current;
    if (!panel) return;
    const items = Array.from(panel.querySelectorAll<HTMLButtonElement>('[role="option"]:not([disabled])'));
    if (items.length === 0) return;
    const currentIndex = items.indexOf(document.activeElement as HTMLButtonElement);
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        items[currentIndex < 0 ? 0 : (currentIndex + 1) % items.length].focus();
        break;
      case 'ArrowUp':
        event.preventDefault();
        items[currentIndex < 0 ? items.length - 1 : (currentIndex - 1 + items.length) % items.length].focus();
        break;
      case 'Home':
        event.preventDefault();
        items[0].focus();
        break;
      case 'End':
        event.preventDefault();
        items[items.length - 1].focus();
        break;
      case 'Enter':
      case ' ': {
        event.preventDefault();
        const active = document.activeElement as HTMLButtonElement | null;
        if (active && items.includes(active)) active.click();
        break;
      }
      case 'Escape':
        event.preventDefault();
        close();
        break;
      case 'Tab':
        close();
        break;
      default:
        break;
    }
  };

  const style: CSSProperties = {
    position: 'fixed',
    left: position?.left ?? 0,
    top: position?.top ?? 0,
    minWidth: position ? `${Math.max(position.minWidth, 154)}px` : undefined,
    visibility: position ? 'visible' : 'hidden',
  };

  return (
    <>
      <button
        {...props}
        ref={setTrigger}
        id={id}
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-invalid={invalid || undefined}
        data-state={open ? 'open' : 'closed'}
        className={`ui-select${invalid ? ' ui-select--invalid' : ''}${className ? ` ${className}` : ''}`}
        onClick={() => (open ? close() : openPanel())}
        onKeyDown={handleTriggerKeyDown}
        onBlur={onBlur}
      >
        {selectedOption?.label ?? ''}
      </button>
      {phase !== null
        ? createPortal(
            <div
              ref={panelRef}
              role="listbox"
              id={panelId}
              aria-labelledby={id}
              tabIndex={-1}
              data-side="bottom"
              // 门户挂到 body 后不在宿主面板 DOM 子树内：宿主的"点击外部关闭"逻辑
              // 靠该标记把面板内部识别为"内部点击"，避免点选项时误关宿主面板
              data-select-panel=""
              className={`ui-dropdown-menu ui-select__panel${phase === 'closing' ? ' ui-dropdown-menu--closing' : ''}`}
              style={style}
              onKeyDown={handlePanelKeyDown}
            >
              {options.map((option) => {
                const isSelected = option.value === selected;
                return (
                  <button
                    key={option.value}
                    type="button"
                    role="option"
                    aria-selected={isSelected}
                    disabled={option.disabled}
                    title={option.disabledReason}
                    className={`ui-dropdown-menu__item ui-select__option${
                      isSelected ? ' ui-select__option--selected' : ''
                    }`}
                    onClick={() => select(option)}
                  >
                    <span className="ui-dropdown-menu__item-label">{option.label}</span>
                  </button>
                );
              })}
            </div>,
            document.body,
          )
        : null}
    </>
  );
});
