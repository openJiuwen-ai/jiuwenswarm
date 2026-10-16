import { useCallback, useEffect, useRef, useState, type FocusEvent } from 'react';

export type SecondarySidebar = 'chat' | 'experiments';
const COLLAPSED_KEY = 'jiuwenswarm.secondarySidebarCollapsed';

export function useSecondarySidebarPreference() {
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(COLLAPSED_KEY) === 'true');

  useEffect(() => {
    localStorage.setItem(COLLAPSED_KEY, String(collapsed));
  }, [collapsed]);

  return [collapsed, setCollapsed] as const;
}

export function useSecondarySidebarPeek() {
  const [target, setTarget] = useState<SecondarySidebar | null>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hovered = useRef(false);
  const focused = useRef<HTMLElement | null>(null);
  const pointerInside = useRef(false);

  const cancelClose = useCallback(() => {
    if (closeTimer.current !== null) {
      clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  }, []);
  const close = useCallback(() => {
    cancelClose();
    hovered.current = false;
    focused.current = null;
    setTarget(null);
  }, [cancelClose]);
  const scheduleClose = useCallback(() => {
    cancelClose();
    closeTimer.current = setTimeout(() => {
      closeTimer.current = null;
      const hasFocus = focused.current !== null && focused.current === document.activeElement;
      if (!hovered.current && !hasFocus) close();
    }, 150);
  }, [cancelClose, close]);
  const open = useCallback((next: SecondarySidebar) => {
    cancelClose();
    if (target !== next) focused.current = null;
    setTarget(next);
  }, [cancelClose, target]);
  const hover = useCallback((next: SecondarySidebar | null) => {
    hovered.current = next !== null;
    if (next) open(next);
    else scheduleClose();
  }, [open, scheduleClose]);

  useEffect(() => cancelClose, [cancelClose]);
  useEffect(() => {
    if (!target) return;
    const resetPointer = () => { pointerInside.current = false; };
    const onPointerDown = () => {
      if (!pointerInside.current) close();
      pointerInside.current = false;
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.defaultPrevented) close();
    };
    window.addEventListener('pointerdown', resetPointer, true);
    window.addEventListener('pointerdown', onPointerDown);
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('pointerdown', resetPointer, true);
      window.removeEventListener('pointerdown', onPointerDown);
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [target, close]);

  // React events include portaled menus and dialogs in the same sidebar subtree.
  const surfaceProps = {
    onMouseEnter: () => {
      hovered.current = true;
      cancelClose();
    },
    onMouseLeave: () => {
      hovered.current = false;
      scheduleClose();
    },
    onFocusCapture: (event: FocusEvent<HTMLElement>) => {
      focused.current = event.target;
      cancelClose();
    },
    onBlurCapture: () => {
      focused.current = null;
      scheduleClose();
    },
    onPointerDownCapture: () => { pointerInside.current = true; },
  };

  return { target, open, hover, close, surfaceProps };
}
