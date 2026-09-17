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
