import { useEffect, useRef, useState } from 'react';

/** Smooth live chunks without replaying history or splitting Unicode characters. */
export function useProgressiveText(text: string, animate: boolean): string {
  const [shown, setShown] = useState(() => animate ? '' : text);
  const target = useRef(text);
  target.current = text;
  const reducedMotion = typeof window !== 'undefined'
    && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  const enabled = animate && !reducedMotion;
  const complete = shown === text;
  useEffect(() => {
    if (!enabled || complete) return;
    const timer = setInterval(() => {
      setShown((previous) => {
        const value = target.current;
        const prefix = value.startsWith(previous) ? previous : '';
        const remaining = Array.from(value.slice(prefix.length));
        return prefix + remaining.slice(0, Math.max(1, Math.ceil(remaining.length / 60))).join('');
      });
    }, 16);
    return () => clearInterval(timer);
  }, [enabled, complete]);
  if (!enabled) return text;
  return text.startsWith(shown) ? shown : '';
}
