/**
 * 企业版 cron「立即执行」会话的 processing 守卫。
 *
 * 对话里的「任务执行 Xs」依赖本地 isProcessing。
 * 企业版执行态走 live，不写 web channel busy 表，metadata 常误报 false。
 * 守卫只在「乐观置位宽限期」内挡住误报；权威 idle / 超时后必须放行。
 */

/** 乐观标 processing 后，挡住 metadata 误报 false 的最长时间。 */
export const CRON_OPTIMISTIC_PROCESSING_GRACE_MS = 120_000;

const optimisticUntilBySession = new Map<string, number>();

export function isCronExecutionSession(sessionId: string): boolean {
  return sessionId.startsWith('cron_') || sessionId.startsWith('__cron__');
}

/** 「立即执行」跳进会话时调用：开启宽限期，避免紧跟着的 metadata 误报盖掉提示。 */
export function markCronOptimisticProcessing(
  sessionId: string,
  graceMs: number = CRON_OPTIMISTIC_PROCESSING_GRACE_MS,
): void {
  const id = String(sessionId || '').trim();
  if (!id) return;
  optimisticUntilBySession.set(id, Date.now() + Math.max(0, graceMs));
}

export function clearCronOptimisticProcessing(sessionId: string): void {
  optimisticUntilBySession.delete(String(sessionId || '').trim());
}

/** 测试用：清空乐观宽限表。 */
export function resetCronOptimisticProcessingForTests(): void {
  optimisticUntilBySession.clear();
}

export type KeepLocalCronProcessingOptions = {
  /**
   * 统一会话未必带 cron_ 前缀，靠 cron_id / lastRun 等元数据识别。
   * 与 isCronExecutionSession(sessionId) 任一成立即可。
   */
  hasCronMetadata?: boolean;
  /**
   * 权威空闲（如组织负责人已停止）。必须放行 metadata 的 false，不能吞掉。
   */
  authoritativeIdle?: boolean;
  /** 注入当前时间，便于单测；默认 Date.now()。 */
  nowMs?: number;
};

/**
 * metadata 的 false 是否应被忽略，以免盖掉本地已拉起的「任务执行」提示。
 * 仅在 cron 会话 + 乐观宽限期内返回 true；权威 idle / 宽限过期一律放行。
 */
export function shouldKeepLocalCronProcessing(
  sessionId: string,
  metadataIsProcessing: boolean,
  localIsProcessing: boolean,
  options?: KeepLocalCronProcessingOptions,
): boolean {
  if (metadataIsProcessing || !localIsProcessing) return false;
  if (options?.authoritativeIdle) return false;

  const isCron =
    isCronExecutionSession(sessionId) || Boolean(options?.hasCronMetadata);
  if (!isCron) return false;

  const now = options?.nowMs ?? Date.now();
  const until = optimisticUntilBySession.get(sessionId);
  if (until == null) return false;
  if (now > until) {
    optimisticUntilBySession.delete(sessionId);
    return false;
  }
  return true;
}
