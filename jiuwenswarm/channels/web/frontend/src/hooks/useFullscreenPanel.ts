import { useCallback, useEffect, useRef, useState } from 'react';

export interface FullscreenPanelResult<T extends HTMLElement> {
  ref: React.RefObject<T>;
  isFullscreen: boolean;
  toggle: () => void;
  enter: () => void;
  exit: () => void;
}

export function useFullscreenPanel<T extends HTMLElement = HTMLDivElement>(): FullscreenPanelResult<T> {
  const ref = useRef<T>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);

  const enter = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    setIsFullscreen(true);
  }, []);

  const exit = useCallback(() => {
    setIsFullscreen(false);
  }, []);

  const toggle = useCallback(() => {
    setIsFullscreen((v) => !v);
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el || !isFullscreen) return;

    const prevPosition = el.style.position;
    const prevInset = el.style.inset;
    const prevZIndex = el.style.zIndex;
    const prevWidth = el.style.width;
    const prevHeight = el.style.height;
    const prevMaxHeight = el.style.maxHeight;

    el.style.position = 'fixed';
    el.style.inset = '0';
    el.style.zIndex = '9999';
    el.style.width = '100vw';
    el.style.height = '100vh';
    el.style.maxHeight = 'none';

    return () => {
      el.style.position = prevPosition;
      el.style.inset = prevInset;
      el.style.zIndex = prevZIndex;
      el.style.width = prevWidth;
      el.style.height = prevHeight;
      el.style.maxHeight = prevMaxHeight;
    };
  }, [isFullscreen]);

  return { ref, isFullscreen, toggle, enter, exit };
}
