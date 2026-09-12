import { useCallback, useLayoutEffect, useRef, useState } from 'react';

export interface HorizontalScrollEdgesResult<T extends HTMLElement> {
  ref: React.RefObject<T>;
  canScrollLeft: boolean;
  canScrollRight: boolean;
  update: () => void;
}

export function useHorizontalScrollEdges<T extends HTMLElement = HTMLDivElement>(threshold = 1): HorizontalScrollEdgesResult<T> {
  const ref = useRef<T>(null);
  const [state, setState] = useState({ canScrollLeft: false, canScrollRight: false });

  const update = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const next = {
      canScrollLeft: el.scrollLeft > threshold,
      canScrollRight: el.scrollLeft < el.scrollWidth - el.clientWidth - threshold,
    };
    setState(prev =>
      prev.canScrollLeft === next.canScrollLeft && prev.canScrollRight === next.canScrollRight ? prev : next,
    );
  }, [threshold]);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    update();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, [update]);

  return { ref, ...state, update };
}
