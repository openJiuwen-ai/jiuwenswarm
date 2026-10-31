/**
 * cron 未读蓝点：chat.final 是否算「整趟结束」。
 * 企业版直播里中间段也会发 chat.final，不能见 final 就标未读。
 */

/** 推送认为这一趟已经收尾、可以亮蓝点的 status */
const CRON_TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'skipped']);

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
