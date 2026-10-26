/**
 * Cron job run-state sync (Pull transport).
 *
 * Compares successive `cron.job.list` snapshots and detects new executions via
 * `last_session_id`. Used by HTTP mode when server push is unavailable.
 */

export interface CronJobRunFingerprint {
  jobId: string;
  lastSessionId: string | null;
}

export interface CronJobRunListItem {
  id: string;
  last_session_id?: unknown;
}

export interface CronJobRunUpdateResult {
  updatedJobIds: string[];
  nextFingerprints: Record<string, CronJobRunFingerprint>;
}

export const CRON_JOB_SYNC_INTERVAL_MS = 30_000;

function normalizeSessionId(raw: unknown): string | null {
  if (typeof raw !== 'string') {
    return null;
  }
  const trimmed = raw.trim();
  return trimmed || null;
}

export function fingerprintFromCronJob(job: CronJobRunListItem): CronJobRunFingerprint {
  return {
    jobId: job.id,
    lastSessionId: normalizeSessionId(job.last_session_id),
  };
}

export function buildCronJobFingerprintMap(
  jobs: CronJobRunListItem[]
): Record<string, CronJobRunFingerprint> {
  const out: Record<string, CronJobRunFingerprint> = {};
  for (const job of jobs) {
    const id = String(job.id || '').trim();
    if (!id) {
      continue;
    }
    out[id] = fingerprintFromCronJob(job);
  }
  return out;
}

function fingerprintChanged(
  previous: CronJobRunFingerprint,
  next: CronJobRunFingerprint
): boolean {
  // 蓝点只应在新结果产生时提示。last_run_at 在 claim 认领那一刻就写库（此时 agent
  // 尚未执行、结果为空），不能作为"有新消息"的信号；last_session_id 在执行完成后
  // 由调度器回写，才是"这一趟跑完、产生了新会话"的权威标志。
  return previous.lastSessionId !== next.lastSessionId;
}

/**
 * Diff job list against the last sync baseline.
 * Jobs without a prior fingerprint only seed the baseline (no unread).
 */
export function detectCronJobRunUpdates(
  previous: Record<string, CronJobRunFingerprint>,
  jobs: CronJobRunListItem[]
): CronJobRunUpdateResult {
  const nextFingerprints = buildCronJobFingerprintMap(jobs);
  const updatedJobIds: string[] = [];

  for (const [jobId, nextFp] of Object.entries(nextFingerprints)) {
    const prevFp = previous[jobId];
    if (!prevFp) {
      continue;
    }
    if (fingerprintChanged(prevFp, nextFp)) {
      updatedJobIds.push(jobId);
    }
  }

  return { updatedJobIds, nextFingerprints };
}

/**
 * 企业版 HTTP 没有定时任务结果推送。当前窗口只要属于刚跑完的这个任务，
 * 就再读一次它的历史；不必等于任务上的最新会话。连续执行时最新会话会被
 * 下一次覆盖，正看着的上一个窗口否则要刷新页面才看得到回复。
 * 没有打开相关窗口时返回空串，不额外请求历史。
 */
export function resolveOpenCronHistoryRefresh(input: {
  jobId: string;
  lastSessionId: string;
  activeSessionId: string;
  activeCronId: string;
  rememberedRunSessionId: string;
}): string {
  const active = input.activeSessionId.trim();
  if (!active) {
    return '';
  }
  const last = input.lastSessionId.trim();
  if (last && last === active) {
    return active;
  }
  if (input.activeCronId.trim() === input.jobId) {
    return active;
  }
  if (input.rememberedRunSessionId.trim() === active) {
    return active;
  }
  return '';
}
