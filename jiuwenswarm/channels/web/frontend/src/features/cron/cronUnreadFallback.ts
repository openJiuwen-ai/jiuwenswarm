/**
 * 企业 HTTP 下 cron 未读蓝点兜底：用 list 返回的 last_session_id 对比本地水位。
 * 个人版 / websocket 不走这里（仍靠 chat.final 推送标未读）。
 */

import { isEnterprise } from '../../edition';
import { getWebTransport } from '../../utils/env';

/**
 * 企业版默认 HTTP，没有个人版那种常驻 WS 来收 cron chat.final，
 * 蓝点靠 list 里的 last_session_id 变化兜底。个人版 / 显式 websocket 不走这条，
 * 避免 updated_at 抖动误亮未读。
 */
export function shouldUseCronUnreadListFallback(): boolean {
  return isEnterprise() && getWebTransport() === 'http';
}

/** 对比 list 水位，决定种子/标未读 */
export function reconcileCronUnreadFromJobs(
  jobs: Array<{ id: string; last_session_id?: string | null }>,
  seen: Record<string, string>,
): { nextSeen: Record<string, string>; unreadJobIds: string[] } {
  const nextSeen = { ...seen };
  const unreadJobIds: string[] = [];
  for (const job of jobs) {
    const jobId = String(job.id || '').trim();
    if (!jobId) continue;
    const sid = String(job.last_session_id || '').trim();
    if (!sid) continue;
    const prev = nextSeen[jobId];
    if (prev === undefined) {
      // 首次见到：只记水位，不把历史执行全部标成未读
      nextSeen[jobId] = sid;
      continue;
    }
    if (prev !== sid) {
      unreadJobIds.push(jobId);
    }
  }
  return { nextSeen, unreadJobIds };
}
