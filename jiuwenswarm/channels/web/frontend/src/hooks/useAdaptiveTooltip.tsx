import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react';
import { createPortal } from 'react-dom';

const VIEWPORT_MARGIN = 8;
const TOOLTIP_GAP = 6;

type TooltipPlacement = 'top' | 'bottom';

type TooltipAlign = 'center' | 'left' | 'right';

type TooltipState = {
  text: string;
  buttonRect: { left: number; right: number; top: number; bottom: number };
  placement: TooltipPlacement;
};

type TooltipHandlers = {
  onMouseEnter: (event: { currentTarget: EventTarget | null }) => void;
  onMouseLeave: () => void;
  onFocus: (event: { currentTarget: EventTarget | null }) => void;
  onBlur: () => void;
};

interface UseAdaptiveTooltipOptions {
  offsetX?: number;
  /** 垂直偏移（px），正值为向下，默认 0 */
  offsetY?: number;
  /** 水平对齐：默认 'center'（居中于触发元素），'left'（tooltip 左缘对齐锚点左缘），'right'（tooltip 右缘对齐锚点右缘）。
   *  仅 'center' 时 offsetX 生效；left/right 忽略 offsetX，仍受视口边界约束。 */
  align?: TooltipAlign;
  placement?: TooltipPlacement;
  /** 最大宽度（px），不传时用 .adaptive-tooltip 的默认 320px */
  maxWidth?: number;
  /** 用此 ref 指向的元素的 rect 作为定位锚点（替代事件触发元素）。
   *  典型场景：hover 子元素（被截断的标题 span）时，tooltip 相对于稳定行容器定位。
   *  data-tooltip 文本仍然从事件触发元素读取。 */
  anchorRef?: RefObject<HTMLElement | null>;
}

/**
 * data-tooltip 的自适应定位方案：默认水平居中于触发按钮下方，
 * 右侧空间不足时提示右缘对齐按钮右缘，左侧空间不足时左缘对齐按钮左缘，
 * 仍放不下时收进视口内（VIEWPORT_MARGIN 兜底）。
 *
 * offsetX: 相对触发元素宽度的百分比偏移（负值向左），0 = 居中，-50 = 左移半个触发元素宽度。
 * placement: 'top' 显示在触发元素上方，'bottom'（默认）显示在下方。
 *
 * 自动隐藏时机：hover/focus 离开、点击任意位置、焦点移到其他元素、
 * 触发元素被卸载（如弹出菜单关闭）或页面滚动/缩放。
 *
 * 用法：
 *   const { tooltip, handlers } = useAdaptiveTooltip();
 *   const { tooltip, handlers } = useAdaptiveTooltip({ offsetX: -50 });
 *   const { tooltip, handlers } = useAdaptiveTooltip({ placement: 'top' });
 *   const { tooltip, handlers } = useAdaptiveTooltip({ maxWidth: 400 });
 *   <button data-tooltip="提示" {...handlers}>...</button>
 *   {tooltip}
 */
export function useAdaptiveTooltip(options?: UseAdaptiveTooltipOptions): { tooltip: ReactNode; handlers: TooltipHandlers } {
  const offsetPct = options?.offsetX ?? 0;
  const offsetY = options?.offsetY ?? 0;
  const align: TooltipAlign = options?.align ?? 'center';
  const placement = options?.placement ?? 'bottom';
  const maxWidth = options?.maxWidth;
  const anchorRef = options?.anchorRef;
  const [state, setState] = useState<TooltipState | null>(null);
  const [position, setPosition] = useState<{ top: number; left: number; visible: boolean } | null>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);

  const hide = useCallback(() => {
    triggerRef.current = null;
    setState(null);
    setPosition(null);
  }, []);

  const show = useCallback(
    (event: { currentTarget: EventTarget | null }) => {
      const el = event.currentTarget as HTMLElement | null;
      const text = el?.getAttribute('data-tooltip') ?? '';
      if (!el) return;
      if (!text) {
        hide();
        return;
      }
      const anchor = anchorRef?.current ?? el;
      const rect = anchor.getBoundingClientRect();
      triggerRef.current = el;
      setPosition(null);
      setState({
        text,
        buttonRect: { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom },
        placement,
      });
    },
    [placement, anchorRef, hide],
  );

  useLayoutEffect(() => {
    if (!state) {
      setPosition(null);
      return;
    }
    const el = tooltipRef.current;
    if (!el) return;
    const width = el.offsetWidth;
    const height = el.offsetHeight;
    const { left, right, top, bottom } = state.buttonRect;
    const buttonWidth = right - left;
    const maxLeft = window.innerWidth - VIEWPORT_MARGIN - width;
    const viewportHeight = window.innerHeight;
    let finalTop: number;
    if (state.placement === 'top') {
      const topPos = top - TOOLTIP_GAP - height;
      const spaceBelow = viewportHeight - bottom - TOOLTIP_GAP;
      finalTop = topPos >= VIEWPORT_MARGIN ? topPos : (spaceBelow >= height ? bottom + TOOLTIP_GAP : topPos);
    } else {
      const bottomPos = bottom + TOOLTIP_GAP;
      const spaceAbove = top - TOOLTIP_GAP;
      finalTop = bottomPos + height <= viewportHeight - VIEWPORT_MARGIN ? bottomPos : (spaceAbove >= height ? top - TOOLTIP_GAP - height : bottomPos);
    }

    let finalLeft: number;
    if (align === 'left') {
      finalLeft = Math.max(VIEWPORT_MARGIN, Math.min(left, maxLeft));
    } else if (align === 'right') {
      finalLeft = Math.max(VIEWPORT_MARGIN, Math.min(right - width, maxLeft));
    } else {
      const shift = (buttonWidth * offsetPct) / 100;
      const centered = left + buttonWidth / 2 - width / 2 - shift;
      if (centered < VIEWPORT_MARGIN) {
        finalLeft = Math.max(VIEWPORT_MARGIN, Math.min(left - shift, maxLeft));
      } else if (centered > maxLeft) {
        finalLeft = Math.min(Math.max(right - width - shift, VIEWPORT_MARGIN), maxLeft);
      } else {
        finalLeft = centered;
      }
    }
    setPosition({ top: finalTop + offsetY, left: finalLeft, visible: true });
  }, [state, offsetPct, offsetY, align]);

  useEffect(() => {
    if (!state) return;
    const hideTooltip = () => {
      triggerRef.current = null;
      setState(null);
      setPosition(null);
    };
    const onPointerDown = () => hideTooltip();
    const onFocusIn = (event: FocusEvent) => {
      if (event.target !== triggerRef.current) hideTooltip();
    };
    // 触发元素可能在 hover/focus 期间被整体卸载（如弹出菜单关闭），
    // 此时 mouseleave/blur 永远不会触发，state 会残留并在容器重新挂载时复活旧 tooltip。
    // 仅监听触发元素父节点的 childList，避免 document.body subtree 在流式输出时反复回调。
    const trigger = triggerRef.current;
    const parentNode = trigger?.parentNode as Node | null;
    const observer = new MutationObserver(() => {
      if (triggerRef.current && !triggerRef.current.isConnected) hideTooltip();
    });
    if (parentNode) {
      observer.observe(parentNode, { childList: true });
    }
    window.addEventListener('resize', hideTooltip);
    window.addEventListener('scroll', hideTooltip, true);
    document.addEventListener('pointerdown', onPointerDown, true);
    document.addEventListener('focusin', onFocusIn);
    return () => {
      observer.disconnect();
      window.removeEventListener('resize', hideTooltip);
      window.removeEventListener('scroll', hideTooltip, true);
      document.removeEventListener('pointerdown', onPointerDown, true);
      document.removeEventListener('focusin', onFocusIn);
    };
  }, [state]);

  const tooltip = state
    ? createPortal(
        <div
          ref={tooltipRef}
          className="adaptive-tooltip"
          style={{
            position: 'fixed',
            top: position ? position.top : -9999,
            left: position ? position.left : -9999,
            visibility: position?.visible ? 'visible' : 'hidden',
            zIndex: 10000,
            maxWidth: maxWidth !== undefined ? `${maxWidth}px` : undefined,
          }}
          role="tooltip"
        >
          {state.text}
        </div>,
        document.body
      )
    : null;

  // focus 只认键盘聚焦（:focus-visible）：点击触发的 focus（含菜单关闭后框架把焦点
  // 还给触发按钮的程序性回焦）一律不弹 tooltip——保证"任何点击后 tooltip 必定消失"，
  // 否则 pointerdown 隐藏后会被紧随的 refocus 重新拉起且再无 mouseleave 能关掉它。
  const showOnFocus = useCallback(
    (event: { currentTarget: EventTarget | null }) => {
      const el = event.currentTarget as HTMLElement | null;
      if (!el || !el.matches(':focus-visible')) return;
      show(event);
    },
    [show],
  );

  const handlers: TooltipHandlers = {
    onMouseEnter: show,
    onMouseLeave: hide,
    onFocus: showOnFocus,
    onBlur: hide,
  };

  return { tooltip, handlers };
}
