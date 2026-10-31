/**
 * 企业 HTTP：从 job.list 的 active_session_id 发现「到点/正在跑」的执行会话，
 * 记入 lastRunSessionId，交给已有的后台 live 订阅（与普通对话发送流类似：
 * 切走页面也能收到 processing=false → 亮蓝点）。
 *
 * 这里只负责「发现会话号并开始盯」，不在 list 里直接标未读，
 * 也不盲设 isProcessing（等 live 首帧，避免陈旧 active_session_id 永久转圈）。
 */

import { isEnterprise } from '../../edition';
import { getWebTransport } from '../../utils/env';
import { ensureSessionRuntimes } from '../../stores/ensureSessionRuntimes';
import type { SidebarCronJob } from '../../stores/cronStore';
import { pickNewActiveCronSessions } from './cronActiveSessionPick';

export { pickNewActiveCronSessions } from './cronActiveSessionPick';

export function shouldDiscoverCronActiveSessions(): boolean {
  return isEnterprise() && getWebTransport() === 'http';
}

export function adoptActiveCronSessions(
  jobs: SidebarCronJob[],
  setLastRunSessionId: (jobId: string, sessionId: string) => void,
  getLastRunSessionId: () => Record<string, string>,
): string[] {
  if (!shouldDiscoverCronActiveSessions()) return [];
  const pending = pickNewActiveCronSessions(jobs, getLastRunSessionId());
  if (pending.length === 0) return [];
  const adopted: string[] = [];
  for (const { jobId, sessionId } of pending) {
    setLastRunSessionId(jobId, sessionId);
    ensureSessionRuntimes(sessionId);
    adopted.push(jobId);
  }
  return adopted;
}
