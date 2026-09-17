import { useEffect, useState } from 'react';

export function formatDurationPrecise(ms: number): string {
  const clamped = Math.max(0, ms);
  if (clamped < 1000) {
    return `${Math.round(clamped)}ms`;
  }
  const totalSeconds = clamped / 1000;
  if (totalSeconds < 60) {
    return `${totalSeconds.toFixed(2)}s`;
  }
  const totalMinutes = Math.floor(totalSeconds / 60);
  const seconds = Math.floor(totalSeconds % 60);
  if (totalMinutes < 60) {
    return `${totalMinutes}m${seconds.toString().padStart(2, '0')}s`;
  }
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return `${hours}h${minutes.toString().padStart(2, '0')}m`;
}

/** active 时 250ms 刷新；关闭时停走，避免无谓重渲染。 */
export function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    setNow(Date.now());
    if (!active) {
      return;
    }
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}
