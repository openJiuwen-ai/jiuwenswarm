/**
 * Distill 配置纯函数：语义签名脏检查与可写字段校验（WEB-03 改版）。
 * 脏检查只比较 interval_seconds；保存时由 UI 固定写 enabled:true。
 * message_threshold 不在 UI 编辑（agent-core 默认语义）。
 */

import type { DistillConfig } from '../../services/personalContextApi';

/** 与后端 DistillScheduleSettings.interval_seconds 常见上界对齐（365 天）。 */
export const DISTILL_INTERVAL_MAX_SECONDS = 31_536_000;
export const DISTILL_INTERVAL_MIN_SECONDS = 60;

/** 快捷档位：只填充秒数，不另发枚举字段。 */
export const DISTILL_INTERVAL_PRESETS = [
  { id: '1h', seconds: 3_600 },
  { id: '6h', seconds: 21_600 },
  { id: '1day', seconds: 86_400 },
  { id: '3day', seconds: 259_200 },
  { id: '7day', seconds: 604_800 },
] as const;

export type DistillIntervalPresetId = (typeof DISTILL_INTERVAL_PRESETS)[number]['id'];

export const DEFAULT_DISTILL_CONFIG: DistillConfig = {
  enabled: false,
  interval_seconds: 86400,
  message_threshold: 50,
  max_messages: 800,
  learning_since_ms: null,
};

export function matchDistillIntervalPreset(
  seconds: number,
): DistillIntervalPresetId | null {
  const found = DISTILL_INTERVAL_PRESETS.find((p) => p.seconds === Number(seconds));
  return found ? found.id : null;
}

export function distillSignature(config: DistillConfig): string {
  return JSON.stringify([Number(config.interval_seconds)]);
}

export function isDistillDirty(saved: DistillConfig, draft: DistillConfig): boolean {
  // 服务端 enabled=false 时也视为需保存（保存会写 enabled:true）。
  return distillSignature(saved) !== distillSignature(draft) || !saved.enabled;
}

export type DistillValidationError = 'intervalRange';

export function validateDistillDraft(draft: DistillConfig): DistillValidationError | null {
  const interval = Number(draft.interval_seconds);
  if (
    !Number.isFinite(interval) ||
    interval < DISTILL_INTERVAL_MIN_SECONDS ||
    interval > DISTILL_INTERVAL_MAX_SECONDS
  ) {
    return 'intervalRange';
  }
  return null;
}
