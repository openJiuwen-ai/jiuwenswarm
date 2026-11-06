/**
 * 企业版 cron「立即执行」会话的 processing 守卫。
 *
 * 对话里的「任务执行 Xs」依赖本地 isProcessing。
 * 企业版执行态走 live，不写 web channel busy 表，metadata 常误报 false。
 */

export function isCronExecutionSession(sessionId: string): boolean {
  return sessionId.startsWith('cron_') || sessionId.startsWith('__cron__');
}

/**
 * metadata 的 false 是否应被忽略，以免盖掉本地已拉起的「任务执行」提示。
 */
export function shouldKeepLocalCronProcessing(
  sessionId: string,
  metadataIsProcessing: boolean,
  localIsProcessing: boolean,
): boolean {
  return (
    !metadataIsProcessing
    && localIsProcessing
    && isCronExecutionSession(sessionId)
  );
}
