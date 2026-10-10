import {
  cloneElement,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type CSSProperties,
  type HTMLAttributes,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type MutableRefObject,
  type ReactElement,
  type ReactNode,
  type Ref,
} from 'react';
import { createPortal } from 'react-dom';
import './DropdownMenu.css';

export type DropdownMenuSide = 'top' | 'bottom' | 'left' | 'right';
export type DropdownMenuAlign = 'start' | 'center' | 'end';

const VIEWPORT_MARGIN = 8;
const TRIGGER_GAP = 6;
/** 退出动画时长，需与 DropdownMenu.css 中 --closing 的 animation-duration 保持一致 */
const EXIT_DURATION_MS = 120;

interface DropdownMenuContextValue {
  open: boolean;
  triggerId: string;
  contentId: string;
  triggerRef: { current: HTMLElement | null };
  contentRef: { current: HTMLDivElement | null };
  toggle: () => void;
  close: () => void;
}

const DropdownMenuContext = createContext<DropdownMenuContextValue | null>(null);

function useDropdownMenuContext(componentName: string): DropdownMenuContextValue {
  const context = useContext(DropdownMenuContext);
  if (!context) {
    throw new Error(`<${componentName}> 必须在 <DropdownMenu> 内使用`);
  }
  return context;
}

export function DropdownMenu({
  open,
  onOpenChange,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: ReactNode;
}) {
  const triggerId = useId();
  const contentId = useId();
  const triggerRef = useRef<HTMLElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  // 关闭前焦点在菜单内（键盘操作或点选菜单项）才把焦点还给 trigger；
  // 外部点击关闭时浏览器已把焦点移给点击目标，不能再抢回来
  const restoreFocusRef = useRef(false);
  // toggle/close 需要在事件监听器里长期持有，经 ref 读取最新受控状态，避免闭包过期
  const stateRef = useRef({ open, onOpenChange });
  useEffect(() => {
    stateRef.current = { open, onOpenChange };
  });

  const toggle = useCallback(() => {
    stateRef.current.onOpenChange(!stateRef.current.open);
  }, []);

  const close = useCallback(() => {
    if (!stateRef.current.open) return;
    restoreFocusRef.current = contentRef.current?.contains(document.activeElement) ?? false;
    stateRef.current.onOpenChange(false);
  }, []);

  useEffect(() => {
    if (open || !restoreFocusRef.current) return;
    restoreFocusRef.current = false;
    const active = document.activeElement;
    // 出场动画期间内容仍未卸载、焦点多半还留在菜单里（卸载后会落到 body）；
    // 焦点已被别处接管（如 onSelect 打开的对话框 autoFocus）时不再抢回
    if (active === document.body || contentRef.current?.contains(active)) {
      triggerRef.current?.focus();
    }
  }, [open]);

  const contextValue: DropdownMenuContextValue = {
    open,
    triggerId,
    contentId,
    triggerRef,
    contentRef,
    toggle,
    close,
  };

  return <DropdownMenuContext.Provider value={contextValue}>{children}</DropdownMenuContext.Provider>;
}

/** Props DropdownMenuTrigger 注入给 asChild 子元素；子元素自己的 onClick/ref 会被链式保留。 */
interface TriggerChildProps {
  id?: string;
  'aria-haspopup'?: string;
  'aria-expanded'?: boolean;
  'aria-controls'?: string;
  'data-state'?: 'open' | 'closed';
  onClick?: (event: ReactMouseEvent<HTMLElement>) => void;
  ref?: Ref<HTMLElement>;
}

export function DropdownMenuTrigger({
  asChild = false,
  children,
  id,
  onClick,
  ...props
}: {
  asChild?: boolean;
  children: ReactNode;
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  const { open, triggerId, contentId, triggerRef, toggle } = useDropdownMenuContext('DropdownMenuTrigger');

  // data-state 供调用方在菜单打开期间维持 trigger 的激活/hover 样式（门户化后 :hover 不会延伸到浮层）
  const ariaProps = {
    'aria-haspopup': 'menu' as const,
    'aria-expanded': open,
    'aria-controls': open ? contentId : undefined,
    'data-state': (open ? 'open' : 'closed') as 'open' | 'closed',
  };

  if (asChild) {
    if (!isTriggerElement(children)) {
      throw new Error('<DropdownMenuTrigger asChild> 需要单个可注入 props 的 React 元素作为子节点');
    }
    // 与 trajectory Tooltip 相同的锚点克隆方式：不包裹额外节点，避免嵌套 button
    const child = children as ReactElement<TriggerChildProps> & { ref?: Ref<HTMLElement> };
    const childRef = child.ref;
    return cloneElement(child, {
      ...ariaProps,
      id: child.props.id ?? triggerId,
      onClick: (event: ReactMouseEvent<HTMLElement>) => {
        child.props.onClick?.(event);
        if (!event.defaultPrevented) toggle();
      },
      ref: (node: HTMLElement | null) => {
        triggerRef.current = node;
        if (typeof childRef === 'function') childRef(node);
        else if (childRef != null) (childRef as MutableRefObject<HTMLElement | null>).current = node;
      },
    });
  }

  return (
    <button
      {...props}
      {...ariaProps}
      ref={(node: HTMLButtonElement | null) => {
        triggerRef.current = node;
      }}
      type="button"
      id={id ?? triggerId}
      onClick={(event) => {
        onClick?.(event);
        if (!event.defaultPrevented) toggle();
      }}
    >
      {children}
    </button>
  );
}

function isTriggerElement(children: ReactNode): boolean {
  return children != null && typeof children === 'object' && 'props' in children;
}

function getViewportPosition(
  side: DropdownMenuSide,
  align: DropdownMenuAlign,
  triggerRect: DOMRect,
  contentRect: DOMRect,
): { left: number; top: number } {
  let left: number;
  let top: number;

  if (side === 'top' || side === 'bottom') {
    top = side === 'bottom' ? triggerRect.bottom + TRIGGER_GAP : triggerRect.top - contentRect.height - TRIGGER_GAP;
    if (align === 'start') left = triggerRect.left;
    else if (align === 'end') left = triggerRect.right - contentRect.width;
    else left = triggerRect.left + triggerRect.width / 2 - contentRect.width / 2;
  } else {
    left = side === 'right' ? triggerRect.right + TRIGGER_GAP : triggerRect.left - contentRect.width - TRIGGER_GAP;
    if (align === 'start') top = triggerRect.top;
    else if (align === 'end') top = triggerRect.bottom - contentRect.height;
    else top = triggerRect.top + triggerRect.height / 2 - contentRect.height / 2;
  }

  // 视口夹取：溢出时贴边，保证菜单始终完整可见
  const maxLeft = Math.max(VIEWPORT_MARGIN, window.innerWidth - contentRect.width - VIEWPORT_MARGIN);
  const maxTop = Math.max(VIEWPORT_MARGIN, window.innerHeight - contentRect.height - VIEWPORT_MARGIN);
  left = Math.min(Math.max(left, VIEWPORT_MARGIN), maxLeft);
  top = Math.min(Math.max(top, VIEWPORT_MARGIN), maxTop);
  return { left, top };
}

export function DropdownMenuContent({
  side = 'bottom',
  align = 'start',
  className,
  children,
  ...props
}: {
  side?: DropdownMenuSide;
  align?: DropdownMenuAlign;
  className?: string;
  children: ReactNode;
} & Omit<HTMLAttributes<HTMLDivElement>, 'children'>) {
  const { open, triggerId, contentId, triggerRef, contentRef, close } = useDropdownMenuContext('DropdownMenuContent');
  // 三态渲染：open 挂载并播放入场动画；closing 保留节点播完出场动画再卸载，期间冻结定位。
  // phase 在渲染期与受控 open 同步（而非 useEffect），保证打开的首个 commit 即挂载，
  // 定位/焦点/入场动画都落在同一帧，不会晚一个 effect 周期。
  const [phase, setPhase] = useState<'open' | 'closing' | null>(open ? 'open' : null);
  if (open && phase !== 'open') {
    setPhase('open');
  } else if (!open && phase === 'open') {
    setPhase('closing');
  }
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);

  useEffect(() => {
    if (phase !== 'closing') return;
    const timer = window.setTimeout(() => setPhase(null), EXIT_DURATION_MS);
    return () => window.clearTimeout(timer);
  }, [phase]);

  useLayoutEffect(() => {
    if (!open) return;
    const content = contentRef.current;
    const trigger = triggerRef.current;
    if (!content || !trigger) return;

    const reposition = () => {
      setPosition(
        getViewportPosition(side, align, trigger.getBoundingClientRect(), content.getBoundingClientRect()),
      );
    };
    reposition();
    // 捕获阶段监听 scroll：侧边栏等滚动容器的 scroll 事件本身不冒泡
    window.addEventListener('scroll', reposition, true);
    window.addEventListener('resize', reposition);
    return () => {
      window.removeEventListener('scroll', reposition, true);
      window.removeEventListener('resize', reposition);
    };
  }, [open, side, align, contentRef, triggerRef]);

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (contentRef.current?.contains(target) || triggerRef.current?.contains(target)) return;
      close();
    };
    document.addEventListener('mousedown', handlePointerDown);
    return () => document.removeEventListener('mousedown', handlePointerDown);
  }, [open, close, contentRef, triggerRef]);

  // 打开后把焦点移入菜单容器，Esc/方向键立即生效；关闭时由 DropdownMenu 还焦给 trigger
  useEffect(() => {
    if (!open) return;
    contentRef.current?.focus({ preventScroll: true });
  }, [open, contentRef]);

  if (phase === null) return null;

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!open) return;
    const content = contentRef.current;
    if (!content) return;
    const items = Array.from(
      content.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not([disabled])'),
    );
    if (items.length === 0) return;
    const currentIndex = items.indexOf(document.activeElement as HTMLButtonElement);
    let nextIndex: number;

    switch (event.key) {
      case 'ArrowDown':
        nextIndex = (currentIndex + 1 + items.length) % items.length;
        break;
      case 'ArrowUp':
        nextIndex = (currentIndex - 1 + items.length) % items.length;
        break;
      case 'Home':
        nextIndex = 0;
        break;
      case 'End':
        nextIndex = items.length - 1;
        break;
      case 'Escape':
        event.preventDefault();
        close();
        return;
      case 'Tab':
        close();
        return;
      default:
        return;
    }

    event.preventDefault();
    items[nextIndex].focus();
  };

  const style: CSSProperties = {
    position: 'fixed',
    left: position?.left ?? 0,
    top: position?.top ?? 0,
    // 首帧先隐藏，定位算好后再显示，避免菜单在左上角闪烁
    visibility: position ? 'visible' : 'hidden',
  };

  return createPortal(
    <div
      {...props}
      ref={contentRef}
      role="menu"
      id={contentId}
      aria-labelledby={triggerId}
      tabIndex={-1}
      data-side={side}
      className={`ui-dropdown-menu${phase === 'closing' ? ' ui-dropdown-menu--closing' : ''}${className ? ` ${className}` : ''}`}
      style={style}
      onKeyDown={handleKeyDown}
    >
      {children}
    </div>,
    document.body,
  );
}

export function DropdownMenuItem({
  icon,
  danger = false,
  disabled = false,
  onSelect,
  className,
  children,
  ...props
}: {
  icon?: ReactNode;
  danger?: boolean;
  disabled?: boolean;
  onSelect?: () => void;
  className?: string;
  children: ReactNode;
} & Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children' | 'onSelect'>) {
  const { open, close } = useDropdownMenuContext('DropdownMenuItem');

  return (
    <button
      {...props}
      type="button"
      role="menuitem"
      disabled={disabled}
      className={`ui-dropdown-menu__item${danger ? ' ui-dropdown-menu__item--danger' : ''}${className ? ` ${className}` : ''}`}
      onClick={() => {
        // 出场动画期间节点仍挂载，逻辑上已关闭的菜单不再触发动作
        if (disabled || !open) return;
        onSelect?.();
        close();
      }}
    >
      {icon ? <span className="ui-dropdown-menu__item-icon" aria-hidden="true">{icon}</span> : null}
      <span className="ui-dropdown-menu__item-label">{children}</span>
    </button>
  );
}
