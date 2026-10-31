/**
 * cron 未读蓝点：chat.final 是否算「整趟结束」。
 * 企业版直播里中间段也会发 chat.final，不能见 final 就标未读。
 */

/** 推送认为这一趟已经收尾、可以亮蓝点的 status */
const CRON_TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'skipped']);

/**
 * 本页已处理过的「job + 执行会话」收尾。
 * 立即执行时人在会话里看完会记一笔；切走后 live 回放再来终态时不要又亮蓝点。
 */
const acknowledgedCronSettles = new Set<string>();

function cronSettleAckKey(jobId: string, sessionId: string): string {
  return `${jobId.trim()}::${sessionId.trim()}`;
}

export function acknowledgeCronSettle(jobId: string, sessionId: string): void {
  const j = String(jobId || '').trim();
  const s = String(sessionId || '').trim();
  if (!j || !s) return;
  acknowledgedCronSettles.add(cronSettleAckKey(j, s));
}

export function wasCronSettleAcknowledged(jobId: string, sessionId: string): boolean {
  const j = String(jobId || '').trim();
  const s = String(sessionId || '').trim();
  if (!j || !s) return false;
  return acknowledgedCronSettles.has(cronSettleAckKey(j, s));
}

/**
 * 收尾时更新未读：正在看 → 清蓝点；不在看 → 亮蓝点。
 * 同一 job+session 只处理一次，避免看完切走后被 live 回放再次点亮。
 */
export function applyCronSettleUnread(options: {
  jobId: string;
  sessionId: string;
  viewing: boolean;
  mark: (jobId: string) => void;
  clear: (jobId: string) => void;
}): void {
  const jobId = String(options.jobId || '').trim();
  const sessionId = String(options.sessionId || '').trim();
  if (!jobId || !sessionId) return;
  if (wasCronSettleAcknowledged(jobId, sessionId)) {
    if (options.viewing) options.clear(jobId);
    return;
  }
  acknowledgeCronSettle(jobId, sessionId);
  if (options.viewing) options.clear(jobId);
  else options.mark(jobId);
}

/** 测试用：清空本页已确认收尾记录 */
export function resetCronSettleAcknowledgmentsForTests(): void {
  acknowledgedCronSettles.clear();
}

/**
 * chat.final 是否该亮蓝点。
 * 只有带终态 status 的推送终稿才标；无 status 的多半是流式中间段。
 */
export function shouldMarkCronUnreadFromFinal(cronMeta: {
  is_placeholder?: boolean;
  status?: string;
} | null | undefined): boolean {
  if (!cronMeta || typeof cronMeta !== 'object') return false;
  if (cronMeta.is_placeholder === true) return false;
  const status = String(cronMeta.status || '').trim().toLowerCase();
  if (!status) return false;
  if (status === 'running' || status === 'awaiting_approval') return false;
  return CRON_TERMINAL_STATUSES.has(status);
}
