import { useEffect, useRef, useState } from 'react';

const DEFAULT_DURATION_MS = 500;

/**
 * Eases a percentage toward its target so status changes read as motion
 * instead of a jump. Purely cosmetic: the target is always the real value and
 * the hook never advances it on its own.
 *
 * Users who prefer reduced motion get the target immediately.
 */
export function useTweenedPercent(target: number, durationMs = DEFAULT_DURATION_MS): number {
  const [displayed, setDisplayed] = useState(target);
  const displayedRef = useRef(target);

  useEffect(() => {
    const media = typeof window.matchMedia === 'function' ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    const from = displayedRef.current;
    if (from === target) {
      return undefined;
    }
    if (media?.matches || durationMs <= 0) {
      displayedRef.current = target;
      setDisplayed(target);
      return undefined;
    }

    let frame = 0;
    const onMotionChange = (event: MediaQueryListEvent) => {
      if (!event.matches) return;
      window.cancelAnimationFrame(frame);
      displayedRef.current = target;
      setDisplayed(target);
    };
    media?.addEventListener?.('change', onMotionChange);
    const start = performance.now();
    const tick = (now: number) => {
      const elapsed = Math.min(1, (now - start) / durationMs);
      const eased = 1 - Math.pow(1 - elapsed, 3);
      const value = from + (target - from) * eased;
      displayedRef.current = value;
      setDisplayed(value);
      if (elapsed < 1) {
        frame = window.requestAnimationFrame(tick);
      }
    };
    frame = window.requestAnimationFrame(tick);
    return () => {
      window.cancelAnimationFrame(frame);
      media?.removeEventListener?.('change', onMotionChange);
    };
  }, [durationMs, target]);

  return displayed;
}
