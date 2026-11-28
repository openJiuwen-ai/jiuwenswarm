/**
 * 主页品牌位置读取外观部件（Gateway S_08 第 3.3 节）：标识、欢迎语与吉祥物。
 * 没有 WebUI 宿主或没有提供者时返回 null，调用方显示宿主默认；图片加载失败时退回宿主图片。
 */

import { createContext, useCallback, useContext, useEffect, useState, useSyncExternalStore } from 'react';
import type { WebUIAppearanceParts, WebUIIdentity } from '../appearance/appearance.ts';
import { readIdentityRecord, type AppearancePreparation } from '../appearance/preparation.ts';
import { useOptionalWebUIAppContext } from './WebUIProvider.tsx';

export const DEFAULT_BRAND_NAME = 'WorkSwarm';
export const DEFAULT_BRAND_LOGO = '/logo.svg';

/** Provided by the startup gate, which owns the appearance preparation. */
export const AppearanceBrandContext = createContext<AppearancePreparation | null>(null);

const noSubscribe = (): (() => void) => () => undefined;
const nothing = (): null => null;

/** The identity part shown by the host: the last record until the gate's preparation ends. */
export function useBrandIdentity(): WebUIIdentity | null {
  const preparation = useContext(AppearanceBrandContext);
  return useSyncExternalStore(
    preparation ? preparation.subscribe : noSubscribe,
    preparation ? preparation.getIdentity : nothing,
    nothing,
  );
}

/** The current value of one appearance part, or null for the host default. */
export function useAppearancePart<P extends keyof WebUIAppearanceParts>(part: P): WebUIAppearanceParts[P] | null {
  const runtime = useOptionalWebUIAppContext()?.runtime;
  const getPart = useCallback(
    () => (runtime?.getAppearance()?.parts[part]?.value ?? null) as WebUIAppearanceParts[P] | null,
    [runtime, part],
  );
  return useSyncExternalStore(runtime ? runtime.subscribeAppearance : noSubscribe, getPart, nothing);
}

/** An image source that falls back to the host image once the package image fails to load. */
export function useBrandImage(src: string | undefined, fallback: string): { src: string; onError: () => void } {
  const [failed, setFailed] = useState<string | null>(null);
  useEffect(() => setFailed(null), [src]);
  const onError = useCallback(() => { if (src) setFailed(src); }, [src]);
  return { src: src && failed !== src ? src : fallback, onError };
}

/** Shared WorkSwarm logo. The standalone login page can use the last displayed brand before connecting. */
export function useWorkSwarmLogo(useRecord = false): ReturnType<typeof useBrandImage> {
  const identity = useBrandIdentity();
  const [recorded] = useState(() => useRecord ? readIdentityRecord()?.logo : undefined);
  return useBrandImage(useRecord ? recorded : identity?.logo, DEFAULT_BRAND_LOGO);
}
