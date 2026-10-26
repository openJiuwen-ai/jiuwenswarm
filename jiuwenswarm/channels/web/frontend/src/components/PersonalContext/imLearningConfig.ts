/**
 * ImLearningPanel 纯函数：preset ↔ since_ms 换算、targets 语义签名脏检查、
 * 白名单校验/合并去重、discover diff 标记、批量粘贴解析。
 *
 * 语义对齐后端（openjiuwen ImLearningConfig，harness/personal_context/config.py）：
 * - enabled=true 必须有 targets；
 * - (channel_id, kind, external_id) 三元组唯一；
 * - since_ms: int | None（ge=0，null=不限）；
 * - fetch_interval_seconds: gt=0 le=31_536_000；
 * - fetch_top_n: int ge=1 le=200；
 * - channel_id/external_id 仅要求非空文本（无字符集限制）。
 *
 * title 是纯展示元数据：不参与脏检查签名与去重键
 * （对齐后端 _semantic_config_dump 对 title 的剔除——仅改标题不触发运行时重启）。
 */

import type {
  ImLearningChannelId,
  ImLearningConfig,
  ImLearningDiscoverItem,
  ImLearningTarget,
  ImLearningTargetKind,
} from '../../services/personalContextApi';

/** 时间范围 preset（天）；null 表示不限（全量回填）。 */
export const SINCE_PRESET_DAYS = [30, 90, 180, 365] as const;
export type SincePresetDays = (typeof SINCE_PRESET_DAYS)[number];

/** 与后端 fetch_interval_seconds 的 le 上限对齐（365 天）。 */
export const IM_LEARNING_INTERVAL_MAX_SECONDS = 31_536_000;
/** 与后端 fetch_top_n 的 ge/le 对齐。 */
export const IM_LEARNING_TOP_N_MIN = 1;
export const IM_LEARNING_TOP_N_MAX = 200;

const DAY_MS = 86_400_000;
/** preset 回读漂移容忍（半天）：保存后即时回读不至于直接显示「自定义」。 */
const PRESET_DRIFT_TOLERANCE_MS = DAY_MS / 2;

/** 目标唯一键（与后端三元组唯一性校验一致；title 不参与）。 */
export function targetKey(target: {
  channel_id: string;
  kind: ImLearningTargetKind;
  external_id: string;
}): string {
  return `${target.channel_id}:${target.kind}:${target.external_id}`;
}

/** preset 天数 → since_ms（以 now 为锚点向前取整天数）。 */
export function presetDaysToSinceMs(days: SincePresetDays, now: number = Date.now()): number {
  return now - days * DAY_MS;
}

/**
 * since_ms → 回显用的 preset。
 * 返回 null = 不限；'custom' = 非 preset 值（UI 显示「自定义」）。
 */
export function sinceMsToPreset(
  sinceMs: number | null,
  now: number = Date.now(),
): SincePresetDays | null | 'custom' {
  if (sinceMs == null) return null;
  const elapsed = now - sinceMs;
  for (const days of SINCE_PRESET_DAYS) {
    if (Math.abs(elapsed - days * DAY_MS) <= PRESET_DRIFT_TOLERANCE_MS) return days;
  }
  return 'custom';
}

/** targets 语义签名（排序稳定；title 不参与——仅改标题不算脏）。 */
export function targetsSignature(targets: ImLearningTarget[]): string {
  return [...targets]
    .map((target) => targetKey(target))
    .sort()
    .join('|');
}

/** 配置语义签名：enabled + targets 集合 + 标量字段。 */
export function imLearningSignature(config: ImLearningConfig): string {
  return JSON.stringify([
    config.enabled,
    targetsSignature(config.targets),
    config.since_ms,
    config.fetch_interval_seconds,
    config.fetch_top_n,
  ]);
}

/** 草稿是否与已保存配置存在语义差异（决定「保存」按钮可用态）。 */
export function isImLearningDirty(saved: ImLearningConfig, draft: ImLearningConfig): boolean {
  return imLearningSignature(saved) !== imLearningSignature(draft);
}

/** 按三元组键合并去重（已存在者保留原条目，包括其 title）。 */
export function mergeTargets(
  existing: ImLearningTarget[],
  additions: ImLearningTarget[],
): ImLearningTarget[] {
  const byKey = new Map(existing.map((target) => [targetKey(target), target]));
  for (const target of additions) {
    const key = targetKey(target);
    if (!byKey.has(key)) byKey.set(key, target);
  }
  return [...byKey.values()];
}

/** discover 会话 → 学习白名单目标（target_kind → kind 字段名转换）。 */
export function discoverItemToTarget(item: ImLearningDiscoverItem): ImLearningTarget {
  return {
    channel_id: item.channel_id,
    kind: item.target_kind,
    external_id: item.external_id,
    title: item.title ?? null,
  };
}

/** discover 结果与白名单 diff：learning=true 表示该会话已在学习名单中。 */
export function markDiscoveredItems(
  discoverItems: ImLearningDiscoverItem[],
  targets: ImLearningTarget[],
): Array<ImLearningDiscoverItem & { learning: boolean }> {
  const learningKeys = new Set(targets.map((target) => targetKey(target)));
  return discoverItems.map((item) => ({
    ...item,
    learning: learningKeys.has(
      targetKey({
        channel_id: item.channel_id,
        kind: item.target_kind,
        external_id: item.external_id,
      }),
    ),
  }));
}

/**
 * 批量粘贴解析：每行一个 external_id。
 * 空白行忽略；含内部空白的行视为粘贴错误记入 invalidLines（行号 1 起）。
 * 返回去重后的目标列表（channelId/kind 由当前选择器决定）。
 */
export function parseBulkExternalIds(
  text: string,
  channelId: ImLearningChannelId,
  kind: ImLearningTargetKind,
): { targets: ImLearningTarget[]; invalidLines: number[] } {
  const targets: ImLearningTarget[] = [];
  const invalidLines: number[] = [];
  const seen = new Set<string>();
  text.split(/\r?\n/).forEach((raw, index) => {
    const id = raw.trim();
    if (!id) return;
    if (/\s/.test(id)) {
      invalidLines.push(index + 1);
      return;
    }
    const key = targetKey({ channel_id: channelId, kind, external_id: id });
    if (seen.has(key)) return;
    seen.add(key);
    targets.push({ channel_id: channelId, kind, external_id: id, title: null });
  });
  return { targets, invalidLines };
}

/** 学习配置草稿校验（对齐后端 Config.from_dict 写前校验）；返回错误 key 或 null。 */
export function validateImLearningDraft(config: ImLearningConfig): string | null {
  if (config.enabled && config.targets.length === 0) return 'targetsRequired';
  const keys = new Set(config.targets.map((target) => targetKey(target)));
  if (keys.size !== config.targets.length) return 'targetsDuplicated';
  if (
    !Number.isFinite(config.fetch_interval_seconds) ||
    config.fetch_interval_seconds <= 0 ||
    config.fetch_interval_seconds > IM_LEARNING_INTERVAL_MAX_SECONDS
  ) {
    return 'intervalRange';
  }
  if (
    !Number.isInteger(config.fetch_top_n) ||
    config.fetch_top_n < IM_LEARNING_TOP_N_MIN ||
    config.fetch_top_n > IM_LEARNING_TOP_N_MAX
  ) {
    return 'topNRange';
  }
  if (config.since_ms != null && (!Number.isFinite(config.since_ms) || config.since_ms < 0)) {
    return 'sinceInvalid';
  }
  return null;
}
