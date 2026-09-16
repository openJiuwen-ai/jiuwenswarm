// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/** Standalone trajectory toolbar dictionary. */

export type TrajectoryKey =
  | 'toolbar.aria'
  | 'toolbar.duration'
  | 'toolbar.useActualDuration'
  | 'toolbar.useEqualWidth'
  | 'toolbar.actualTime'
  | 'toolbar.tokens'
  | 'toolbar.useTokenCost'
  | 'toolbar.turns'
  | 'toolbar.expandTurns'
  | 'toolbar.collapseTurns'
  | 'toolbar.calls'
  | 'toolbar.expandCalls'
  | 'toolbar.collapseCalls'
  | 'toolbar.search'
  | 'toolbar.searchPlaceholder'

export type TrajectoryTranslate = (key: TrajectoryKey) => string

export const en: Readonly<Record<TrajectoryKey, string>> = {
  'toolbar.aria': 'Trajectory toolbar',
  'toolbar.duration': 'Duration',
  'toolbar.useActualDuration': 'Use actual duration',
  'toolbar.useEqualWidth': 'Use equal-width operations',
  'toolbar.actualTime': 'Actual time',
  'toolbar.tokens': 'Tokens',
  'toolbar.useTokenCost': 'Use token cost',
  'toolbar.turns': 'Turns',
  'toolbar.expandTurns': 'Expand turns',
  'toolbar.collapseTurns': 'Collapse turns',
  'toolbar.calls': 'Calls',
  'toolbar.expandCalls': 'Expand calls',
  'toolbar.collapseCalls': 'Collapse calls',
  'toolbar.search': 'Search trajectory',
  'toolbar.searchPlaceholder': 'Search',
}

export const zh: Readonly<Record<TrajectoryKey, string>> = {
  ...en,
  'toolbar.aria': '轨迹工具栏',
  'toolbar.actualTime': '实际时间',
  'toolbar.useTokenCost': '按 token 开销显示',
  'toolbar.search': '搜索轨迹',
  'toolbar.searchPlaceholder': '搜索',
}

/** Create a translator from a complete or partial dictionary. */
export function trajectoryTranslator(
  dictionary: Partial<Record<TrajectoryKey, string>> = en,
): TrajectoryTranslate {
  return key => dictionary[key] ?? en[key]
}
