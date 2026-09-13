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
