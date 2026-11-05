/**
 * 外观宿主：把 Gateway 连接上的外观贡献写入文档，撤销或卸载时移除（Gateway S_08）。
 * 主题切换时按新主题重新计算半透明基色；不改变 data-theme 与 data-color-mode。
 */

import { useEffect, useLayoutEffect, useSyncExternalStore } from 'react';
import { applyAppearance } from '../appearance/applyAppearance.ts';
import type { WebUIAppearanceFrame } from '../appearance/appearance.ts';
import type { WebUIRuntime } from '../runtime/types.ts';
import { useOptionalWebUIAppContext } from './WebUIProvider.tsx';

const noopSubscribe = (): (() => void) => () => undefined;
const noAppearance = (): WebUIAppearanceFrame | null => null;

export interface WebUIAppearanceHostProps {
  /** 测试或特殊宿主的注入点；生产环境由 WebUIProvider 提供。 */
  runtime?: WebUIRuntime;
}

export function WebUIAppearanceHost({ runtime: runtimeOverride }: WebUIAppearanceHostProps) {
  const context = useOptionalWebUIAppContext();
  const runtime = runtimeOverride ?? context?.runtime;
  const frame = useSyncExternalStore(
    runtime ? runtime.subscribeAppearance : noopSubscribe,
    runtime ? runtime.getAppearance : noAppearance,
    noAppearance,
  );
  const appearance = frame?.appearance ?? null;

  useLayoutEffect(() => {
    applyAppearance(document, appearance);
    if (!appearance) return undefined;
    const observer = new MutationObserver(() => applyAppearance(document, appearance));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'data-color-mode'] });
    return () => observer.disconnect();
  }, [appearance]);

  useEffect(() => () => applyAppearance(document, null), []);
  return null;
}
