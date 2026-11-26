/**
 * 外观宿主：把 Gateway 连接上快照的配色部件写入文档，无提供者时移除（Gateway S_08）；
 * 浏览器标签的标题与图标跟随标识部件。主题切换时按新主题重新计算半透明基色；不改变 data-theme 与 data-color-mode。
 */

import { useContext, useEffect, useLayoutEffect, useSyncExternalStore } from 'react';
import { applyAppearance } from '../appearance/applyAppearance.ts';
import { APPEARANCE_PROVIDER_KEY, writeItem } from '../splash/launch.ts';
import type { WebUIAppearanceSnapshot } from '../appearance/appearance.ts';
import type { WebUIRuntime } from '../runtime/types.ts';
import { useOptionalWebUIAppContext } from './WebUIProvider.tsx';
import { AppearanceBrandContext, DEFAULT_BRAND_NAME, useBrandIdentity } from './appearanceBrand.tsx';

const noopSubscribe = (): (() => void) => () => undefined;
const noAppearance = (): WebUIAppearanceSnapshot | null => null;
const DEFAULT_FAVICON = '/favicon.png';

export interface WebUIAppearanceHostProps {
  /** 测试或特殊宿主的注入点；生产环境由 WebUIProvider 提供。 */
  runtime?: WebUIRuntime;
}

/** Point the tab icon at `href`, or at the host icon when the image cannot be loaded. */
export function setFavicon(doc: Document, href: string): () => void {
  let current = true;
  const use = (value: string) => {
    if (!current) return;
    const previous = doc.querySelector<HTMLLinkElement>('link[rel="icon"]');
    const suffix = value.split(/[?#]/, 1)[0].toLowerCase().match(/\.(png|svg|webp|gif)$/)?.[1];
    const type = suffix === 'svg' ? 'image/svg+xml' : suffix ? `image/${suffix}` : '';
    if (previous?.getAttribute('href') === value && previous.type === type) return;
    const link = doc.createElement('link');
    link.rel = 'icon';
    if (type) link.type = type;
    link.href = value;
    if (previous) previous.replaceWith(link);
    else doc.head.append(link);
  };
  if (href === DEFAULT_FAVICON || typeof Image === 'undefined') {
    use(href);
    return () => undefined;
  }
  const probe = new Image();
  probe.onload = () => use(href);
  probe.onerror = () => use(DEFAULT_FAVICON);
  probe.src = href;
  return () => { current = false; };
}

/** Show the host brand in the tab; index.html may have applied a recorded identity. */
export function resetTabBrand(doc: Document): void {
  doc.title = DEFAULT_BRAND_NAME;
  setFavicon(doc, DEFAULT_FAVICON);
}

export function WebUIAppearanceHost({ runtime: runtimeOverride }: WebUIAppearanceHostProps) {
  const context = useOptionalWebUIAppContext();
  const runtime = runtimeOverride ?? context?.runtime;
  const snapshot = useSyncExternalStore(
    runtime ? runtime.subscribeAppearance : noopSubscribe,
    runtime ? runtime.getAppearance : noAppearance,
    noAppearance,
  );
  const colors = snapshot?.parts.colors ?? null;
  const identity = useBrandIdentity();
  // Until the gate's preparation exists, the tab keeps what index.html applied from the record.
  const branded = useContext(AppearanceBrandContext) !== null;

  useEffect(() => {
    // The current colors provider plays its splash first on the next start (Gateway S_09 section 5).
    if (snapshot) writeItem(APPEARANCE_PROVIDER_KEY, snapshot.parts.colors?.packageId ?? null);
  }, [snapshot]);

  useLayoutEffect(() => {
    const value = colors?.value ?? null;
    applyAppearance(document, value);
    if (!value) return undefined;
    const observer = new MutationObserver(() => applyAppearance(document, value));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'data-color-mode'] });
    return () => observer.disconnect();
  }, [colors]);

  useEffect(() => {
    if (branded) document.title = identity?.name ?? DEFAULT_BRAND_NAME;
  }, [branded, identity?.name]);
  useEffect(
    () => (branded ? setFavicon(document, identity?.favicon ?? DEFAULT_FAVICON) : undefined),
    [branded, identity?.favicon],
  );

  useEffect(() => () => {
    applyAppearance(document, null);
    resetTabBrand(document);
  }, []);
  return null;
}
