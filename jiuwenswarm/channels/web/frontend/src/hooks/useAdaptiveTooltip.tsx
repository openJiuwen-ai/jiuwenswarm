import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

const VIEWPORT_MARGIN = 8;
const TOOLTIP_GAP = 6;

type TooltipPlacement = 'top' | 'bottom';

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
  placement?: TooltipPlacement;
}

/**
 * data-tooltip 的自适应定位方案：默认水平居中于触发按钮下方，
 * 右侧空间不足时提示右缘对齐按钮右缘，左侧空间不足时左缘对齐按钮左缘，
 * 仍放不下时收进视口内（VIEWPORT_MARGIN 兜底）。
 *
 * offsetX: 相对触发元素宽度的百分比偏移（负值向左），0 = 居中，-50 = 左移半个触发元素宽度。
 * placement: 'top' 显示在触发元素上方，'bottom'（默认）显示在下方。
 *
 * 用法：
 *   const { tooltip, handlers } = useAdaptiveTooltip();
 *   const { tooltip, handlers } = useAdaptiveTooltip({ offsetX: -50 });
 *   const { tooltip, handlers } = useAdaptiveTooltip({ placement: 'top' });
 *   <button data-tooltip="提示" {...handlers}>...</button>
 *   {tooltip}
 */
export function useAdaptiveTooltip(options?: UseAdaptiveTooltipOptions): { tooltip: ReactNode; handlers: TooltipHandlers } {
  const offsetPct = options?.offsetX ?? 0;
  const placement = options?.placement ?? 'bottom';
  const [state, setState] = useState<TooltipState | null>(null);
  const [position, setPosition] = useState<{ top: number; left: number; visible: boolean } | null>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);

  const show = useCallback((event: { currentTarget: EventTarget | null }) => {
    const el = event.currentTarget as HTMLElement | null;
    const text = el?.getAttribute('data-tooltip') ?? '';
    if (!el || !text) return;
    const rect = el.getBoundingClientRect();
    setPosition(null);
    setState({
      text,
      buttonRect: { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom },
      placement,
    });
  }, [placement]);

  const hide = useCallback(() => {
    setState(null);
    setPosition(null);
  }, []);

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
    const shift = (buttonWidth * offsetPct) / 100;
    const centered = left + buttonWidth / 2 - width / 2 - shift;
    const maxLeft = window.innerWidth - VIEWPORT_MARGIN - width;
    let finalLeft: number;
    if (centered < VIEWPORT_MARGIN) {
      finalLeft = Math.max(VIEWPORT_MARGIN, Math.min(left - shift, maxLeft));
    } else if (centered > maxLeft) {
      finalLeft = Math.min(Math.max(right - width - shift, VIEWPORT_MARGIN), maxLeft);
    } else {
      finalLeft = centered;
    }
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
    setPosition({ top: finalTop, left: finalLeft, visible: true });
  }, [state, offsetPct]);

  useEffect(() => {
    if (!state) return;
    const hideTooltip = () => {
      setState(null);
      setPosition(null);
    };
    window.addEventListener('resize', hideTooltip);
    window.addEventListener('scroll', hideTooltip, true);
    return () => {
      window.removeEventListener('resize', hideTooltip);
      window.removeEventListener('scroll', hideTooltip, true);
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
          }}
          role="tooltip"
        >
          {state.text}
        </div>,
        document.body
      )
    : null;

  const handlers: TooltipHandlers = {
    onMouseEnter: show,
    onMouseLeave: hide,
    onFocus: show,
    onBlur: hide,
  };

  return { tooltip, handlers };
}
