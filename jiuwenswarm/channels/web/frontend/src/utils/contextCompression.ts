/**
 * started/running 阶段的压缩提示文案，按压缩器类型区分：
 * - DialogueCompressor：压缩过去轮消息
 * - CurrentRoundCompressor：压缩当前轮
 * - RoundLevelCompressor：全量压缩
 * 其余压缩器（如 SessionMemoryCompressor）回退到后端 summary 原文。
 */
export function contextCompressionRunningText(
  t: (key: string) => string,
  processor: string | undefined,
  fallback: string,
): string {
  const key =
    processor === 'DialogueCompressor'
      ? 'dialogue'
      : processor === 'CurrentRoundCompressor'
        ? 'currentRound'
        : processor === 'RoundLevelCompressor'
          ? 'roundLevel'
          : '';
  return key ? t(`chat.contextCompressionStarted.${key}`) : fallback;
}

/**
 * 压缩进行中的计时文案，按秒/分/时三档渲染。
 *
 * 压缩调用是非流式的，后端在整段压缩期间只发 `started` 一个事件，前端拿不到
 * 任何进度数据，因此只能用「已等待时长」让用户确认它还在跑。
 */
export function contextCompressionElapsedText(
  t: (key: string, options?: Record<string, unknown>) => string,
  elapsedSeconds: number,
): string {
  const safeSeconds = Math.max(0, Math.floor(elapsedSeconds));
  const minutes = Math.floor(safeSeconds / 60);
  const hours = Math.floor(minutes / 60);
  if (hours > 0) {
    return t('chat.contextCompressionElapsed.hoursMinutes', { hours, minutes: minutes % 60 });
  }
  if (minutes > 0) {
    return t('chat.contextCompressionElapsed.minutesSeconds', { minutes, seconds: safeSeconds % 60 });
  }
  return t('chat.contextCompressionElapsed.seconds', { seconds: safeSeconds });
}
