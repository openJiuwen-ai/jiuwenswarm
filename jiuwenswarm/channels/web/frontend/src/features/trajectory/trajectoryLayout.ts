// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/** Deterministic sizing rules for the raw OTel inspector. */

export const RAW_INSPECTOR_DEFAULT_HEIGHT = 220;
export const RAW_INSPECTOR_MIN_HEIGHT = 120;
export const RAW_INSPECTOR_MAX_RATIO = 0.6;
export const RAW_INSPECTOR_KEYBOARD_STEP = 16;

export interface RawInspectorHeightBounds {
  min: number;
  max: number;
}

export function shouldInsetTrajectoryForFloatingTasks(
  mode: string,
  activeView: string,
  taskPanelAvailable: boolean,
  taskPanelHidden: boolean,
  taskPanelExpanded: boolean,
): boolean {
  return (mode === 'agent' || mode === 'team')
    && activeView === 'trajectory'
    && taskPanelAvailable
    && !taskPanelHidden
    && !taskPanelExpanded;
}

export function rawInspectorHeightBounds(containerHeight: number): RawInspectorHeightBounds {
  return {
    min: RAW_INSPECTOR_MIN_HEIGHT,
    max: Math.max(
      RAW_INSPECTOR_MIN_HEIGHT,
      Math.floor(Math.max(0, containerHeight) * RAW_INSPECTOR_MAX_RATIO),
    ),
  };
}

export function clampRawInspectorHeight(height: number, containerHeight: number): number {
  const bounds = rawInspectorHeightBounds(containerHeight);
  return Math.min(bounds.max, Math.max(bounds.min, height));
}

export function rawInspectorKeyboardHeight(
  currentHeight: number,
  key: string,
  containerHeight: number,
): number | null {
  const bounds = rawInspectorHeightBounds(containerHeight);
  switch (key) {
    case 'ArrowUp':
      return clampRawInspectorHeight(
        currentHeight + RAW_INSPECTOR_KEYBOARD_STEP,
        containerHeight,
      );
    case 'ArrowDown':
      return clampRawInspectorHeight(
        currentHeight - RAW_INSPECTOR_KEYBOARD_STEP,
        containerHeight,
      );
    case 'Home':
      return bounds.min;
    case 'End':
      return bounds.max;
    default:
      return null;
  }
}
