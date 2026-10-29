import type { ContextCompressionSummary } from '../types/message';

/**
 * 成员压缩 turn 级状态的 ref 键：`sessionId\u0000memberId`。
 *
 * NUL 不可能出现在正常 id 里，作分隔符可无歧义地按会话反解前缀，
 * 也避免 "s1" 前缀误命中 "s10\u0000…"。
 */
export function memberCompressionKey(sessionId: string, memberId: string): string {
  return `${sessionId}\u0000${memberId}`;
}

/**
 * 从 map 中按会话前缀清掉该会话的全部成员压缩条目。
 *
 * 必须按 key 前缀清、不能按 store 现存成员枚举：用户在 turn 内点 X 关掉
 * 成员压缩条后，store 记录已删而 ref 里的 turn 级条目仍在，枚举法会漏掉
 * 它，下一 turn 的计数/详情被上一 turn 抬高（CR-001 残余路径）。
 */
export function clearMemberCompressionEntriesForSession<T>(
  entries: Map<string, T>,
  sessionId: string,
): void {
  const prefix = `${sessionId}\u0000`;
  for (const key of entries.keys()) {
    if (key.startsWith(prefix)) entries.delete(key);
  }
}

/**
 * 以 turn 级基数为起点累计一次完成。
 *
 * count 与详情列表必须同步从基数起算：若详情从 store 里保留的上一 turn
 * 展示 summary 续接，徽标显示「完成 1 次」而详情带着上一 turn 的条目，
 * 编号与 N 次对不上（CR-002 残余路径）。基数在 ref、展示在 store，
 * 二者分离后 store 保留的 summary 只用于展示，不再是任何累加起点。
 */
export function nextMemberCompressionSummary(
  current: ContextCompressionSummary | undefined,
  summaryText: string,
): ContextCompressionSummary {
  return {
    count: (current?.count ?? 0) + 1,
    summaries: [...(current?.summaries ?? []), summaryText],
  };
}

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
 * 取指定会话这一轮手动 /compact 的起始时刻。
 *
 * 压缩状态按会话记录（多会话可并发），展示时必须按当前活跃会话取值，
 * 不能读某次发起时写入的全局单值，否则切换会话后会显示别的会话的起点。
 */
export function resolveCompactStartedAt(
  startedAtBySession: ReadonlyMap<string, number>,
  sessionId: string | null | undefined,
): number | undefined {
  return sessionId ? startedAtBySession.get(sessionId) : undefined;
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

/** cancelled/paused 终态的中性文案。SDK 把用户暂停/取消的压缩标记为
 * `cancelled`/`paused`（reason=user_cancel/user_pause），不再是 failed；
 * 界面按中断呈现而不是报错。非中断状态返回 null，调用方回退到后端 summary。
 */
export function contextCompressionInterruptedText(
  t: (key: string) => string,
  status: string | undefined,
): string | null {
  if (status === 'paused') {
    return t('chat.contextCompressionPaused');
  }
  if (status === 'cancelled') {
    return t('chat.contextCompressionCancelled');
  }
  return null;
}
