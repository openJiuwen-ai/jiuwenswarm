import { useEffect, useRef, useState } from 'react';

import type { ContextCompressionRuntime } from '../types';

const ELAPSED_TICK_MS = 1000;

/**
 * 从 `startedAt` 起每秒刷新已等待秒数，`active` 为 false 时归零。
 *
 * 用时间戳差值而不是自增计数，因此后台标签页被浏览器节流 setInterval 之后，
 * 回到前台仍然显示正确的时长。`startedAt` 缺失时退化为「本次进入 running 的
 * 时刻」，保证任何情况下都有计时可显示。
 */
export function useElapsedSeconds(
  startedAt: number | undefined,
  active: boolean,
): number | undefined {
  const fallbackStartRef = useRef<number | undefined>(undefined);
  const [elapsedSeconds, setElapsedSeconds] = useState<number | undefined>(undefined);

  useEffect(() => {
    if (!active) {
      fallbackStartRef.current = undefined;
      setElapsedSeconds(undefined);
      return;
    }
    const normalizedStart = startedAt ?? fallbackStartRef.current ?? Date.now();
    fallbackStartRef.current = normalizedStart;
    const tick = () => {
      setElapsedSeconds(Math.max(0, Math.floor((Date.now() - normalizedStart) / 1000)));
    };
    tick();
    const timer = window.setInterval(tick, ELAPSED_TICK_MS);
    return () => window.clearInterval(timer);
  }, [active, startedAt]);

  return elapsedSeconds;
}

/**
 * 当前这一轮压缩的已等待秒数。
 *
 * 每一轮压缩的 `started` 事件都会带入新的 `startedAt`，依赖变化后计时自动
 * 从头开始；压缩结束（状态不再是 running）时计时停止并归零。
 */
export function useContextCompressionElapsed(
  runtime?: ContextCompressionRuntime,
): number | undefined {
  return useElapsedSeconds(runtime?.startedAt, runtime?.status === 'running');
}
