/**
 * 企业 HTTP：从 job.list 的 active_session_id 发现「到点/正在跑」的执行会话，
 * 记入 lastRunSessionId，交给已有的后台 live 订阅（与普通对话发送流类似：
 * 切走页面也能收到 processing=false → 亮蓝点）。
 *
 * 这里只负责「发现会话号并开始盯」，不在 list 里直接标未读。
 */

import { isEnterprise } from '../../edition';
import { getWebTransport } from '../../utils/env';
import { ensureSessionRuntimes } from '../../stores/ensureSessionRuntimes';
import { useChatStore } from '../../stores/chatStore';
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
  const chat = useChatStore.getState();
  const adopted: string[] = [];
  for (const { jobId, sessionId } of pending) {
    setLastRunSessionId(jobId, sessionId);
    ensureSessionRuntimes(sessionId);
    // 对齐普通对话：后台跑着时侧栏可以转圈，结束后 processing=false 再标未读。
    if (!chat.getRuntime(sessionId)?.isProcessing) {
      chat.setProcessing(sessionId, true);
    }
    adopted.push(jobId);
  }
  return adopted;
}
