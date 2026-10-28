import { useSyncExternalStore } from 'react';

// Project the saved browser setting; do not persist a second preference locally.
let headless = true;
let revision = 0;
const listeners = new Set<() => void>();

export function isDesktopBrowserHeadless(): boolean {
  return headless;
}

export function browserDisplayRevision(): number {
  return revision;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function publish(next: boolean): void {
  if (headless === next) return;
  headless = next;
  listeners.forEach((listener) => listener());
}

export function useDesktopBrowserHeadless(): boolean {
  return useSyncExternalStore(subscribe, isDesktopBrowserHeadless, isDesktopBrowserHeadless);
}

export async function applyBrowserDisplaySettings(
  settings: { headless?: unknown },
  expectedRevision?: number,
): Promise<void> {
  const desktop = window.jiuwenDesktop;
  if (!desktop?.isElectron) return;
  if (expectedRevision !== undefined && expectedRevision !== revision) return;
  const next = settings.headless !== false;
  const current = ++revision;
  // Hide immediately; enable rendering only after the native guard is updated.
  if (next) publish(true);
  try {
    await desktop.browser.setHeadless?.(next);
  } catch (error) {
    console.warn('[desktop.browser] display mode sync failed:', error);
    return;
  }
  if (current === revision) publish(next);
}
