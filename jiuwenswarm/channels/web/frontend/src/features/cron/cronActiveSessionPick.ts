/** 从 list 里挑出尚未记入 lastRun 的 active cron 会话（纯函数，无 store 依赖）。 */

export function pickNewActiveCronSessions(
  jobs: Array<{ id?: string; active_session_id?: string | null }>,
  lastRun: Record<string, string>,
): Array<{ jobId: string; sessionId: string }> {
  const out: Array<{ jobId: string; sessionId: string }> = [];
  for (const job of jobs) {
    const jobId = String(job.id || '').trim();
    const sid = String(job.active_session_id || '').trim();
    if (!jobId || !sid) continue;
    if (!(sid.startsWith('cron_') || sid.startsWith('__cron__'))) continue;
    if (lastRun[jobId] === sid) continue;
    out.push({ jobId, sessionId: sid });
  }
  return out;
}
