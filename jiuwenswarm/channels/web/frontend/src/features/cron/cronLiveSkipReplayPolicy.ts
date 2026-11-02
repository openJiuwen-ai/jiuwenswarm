/**
 * Cron live SSE：何时禁止 full-replay → skip_replay 中途升级。
 * 纯函数，便于单测；实现见 cronLiveWatch.upgradeCronLiveWatchToSkipReplay。
 */

export type CronLiveSkipReplayRuntimeHint = {
  isProcessing?: boolean;
  currentStreamId?: string | null;
  pendingQuestion?: unknown;
};

/**
 * 出字中 / 本轮仍在跑 / 挂审批：禁止切连（字级 CJK delta 易丢/乱序）。
 * 冷刷新等审批应走「首连 skip_replay」，不走 upgrade。
 */
export function shouldDeferCronLiveSkipReplayUpgrade(
  runtime?: CronLiveSkipReplayRuntimeHint | null,
): boolean {
  if (!runtime) return false;
  return Boolean(
    runtime.isProcessing || runtime.currentStreamId || runtime.pendingQuestion,
  );
}
