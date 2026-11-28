import type { DropdownMenuAlign, DropdownMenuSide } from '../../../components/ui/DropdownMenu/DropdownMenu.tsx';
import { isRecord } from '../protocol/utils.ts';

export const LAUNCHER_POSITION_STORAGE_KEY = 'workswarm.webui.presentation.launcherPosition';
export const LAUNCHER_VIEWPORT_MARGIN = 12;
export const LAUNCHER_DRAG_THRESHOLD_PX = 5;

export interface LauncherPosition {
  x: number;
  y: number;
}

export interface LauncherSize {
  width: number;
  height: number;
}

export interface LauncherViewport {
  width: number;
  height: number;
}

export function getLauncherStorage(): Storage | undefined {
  try {
    return (globalThis as { window?: Window }).window?.localStorage ?? globalThis.localStorage;
  } catch {
    return undefined;
  }
}

export function clampLauncherPosition(
  position: LauncherPosition,
  size: LauncherSize,
  viewport: LauncherViewport,
  margin = LAUNCHER_VIEWPORT_MARGIN,
): LauncherPosition {
  const maxX = Math.max(margin, viewport.width - size.width - margin);
  const maxY = Math.max(margin, viewport.height - size.height - margin);
  return {
    x: Math.min(Math.max(position.x, margin), maxX),
    y: Math.min(Math.max(position.y, margin), maxY),
  };
}

export function readStoredLauncherPosition(storage = getLauncherStorage()): LauncherPosition | null {
  try {
    const raw = storage?.getItem(LAUNCHER_POSITION_STORAGE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!isRecord(parsed) || typeof parsed.x !== 'number' || typeof parsed.y !== 'number') return null;
    if (!Number.isFinite(parsed.x) || !Number.isFinite(parsed.y)) return null;
    return { x: parsed.x, y: parsed.y };
  } catch {
    return null;
  }
}

export function writeStoredLauncherPosition(position: LauncherPosition, storage = getLauncherStorage()): void {
  try {
    storage?.setItem(
      LAUNCHER_POSITION_STORAGE_KEY,
      JSON.stringify({ x: Math.round(position.x), y: Math.round(position.y) }),
    );
  } catch {
    // Storage failures must not block dragging or opening application presentations.
  }
}

export function getLauncherMenuPlacement(
  rect: Pick<DOMRect, 'left' | 'top' | 'width' | 'height'>,
  viewport: LauncherViewport,
): { side: DropdownMenuSide; align: DropdownMenuAlign } {
  const centerX = rect.left + rect.width / 2;
  const centerY = rect.top + rect.height / 2;
  return {
    side: centerY >= viewport.height / 2 ? 'top' : 'bottom',
    align: centerX >= viewport.width / 2 ? 'end' : 'start',
  };
}

/**
 * Keep dismissed candidates hidden across normal frame updates, while revealing the launcher
 * immediately when any brand-new manual candidate joins or a previously released one returns.
 */
export function reconcileDismissedKeys(
  dismissedKeys: ReadonlySet<string>,
  candidateKeys: readonly string[],
): Set<string> {
  if (dismissedKeys.size === 0) return new Set();
  if (candidateKeys.some((key) => !dismissedKeys.has(key))) return new Set();
  return new Set(candidateKeys.filter((key) => dismissedKeys.has(key)));
}

export function sameKeySet(left: ReadonlySet<string>, right: ReadonlySet<string>): boolean {
  if (left.size !== right.size) return false;
  for (const key of left) {
    if (!right.has(key)) return false;
  }
  return true;
}
