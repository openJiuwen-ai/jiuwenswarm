import type { ReaSetupJob } from './types';

export const ACTIVE_INSTALL_JOB_STORAGE_KEY = 'reaSetup.activeJob.v2';
export const REMAINING_DISPLAY_INTERVAL_MS = 30000;

type JobStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

function isActiveStatus(status: ReaSetupJob['status'] | undefined): boolean {
  return status === 'queued' || status === 'running';
}

export function readPersistedActiveJob(storage: JobStorage | null): ReaSetupJob | null {
  if (!storage) return null;
  try {
    const raw = storage.getItem(ACTIVE_INSTALL_JOB_STORAGE_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<ReaSetupJob>;
    if (
      !value.job_id
      || (value.kind !== 'base' && value.kind !== 'enhanced')
      || !isActiveStatus(value.status)
      || typeof value.started_at !== 'number'
    ) {
      storage.removeItem(ACTIVE_INSTALL_JOB_STORAGE_KEY);
      return null;
    }
    return value as ReaSetupJob;
  } catch {
    storage.removeItem(ACTIVE_INSTALL_JOB_STORAGE_KEY);
    return null;
  }
}

export function persistActiveJob(storage: JobStorage | null, job: ReaSetupJob | null): void {
  if (!storage) return;
  try {
    if (!job || !isActiveStatus(job.status)) {
      storage.removeItem(ACTIVE_INSTALL_JOB_STORAGE_KEY);
      return;
    }
    storage.setItem(ACTIVE_INSTALL_JOB_STORAGE_KEY, JSON.stringify(job));
  } catch {
    // Persistence is best effort when local storage is unavailable.
  }
}

function positiveNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null;
}

export function estimateRemainingSeconds(
  job: ReaSetupJob | null,
  previousSeconds: number | null = null,
): number | null {
  if (!job || !isActiveStatus(job.status)) return null;
  const detail = job.detail;
  const rate = positiveNumber(detail?.bytes_per_second);
  const cumulativeDownloaded = positiveNumber(detail?.cumulative_downloaded_bytes);
  const cumulativeTotal = positiveNumber(detail?.cumulative_total_bytes);
  if (
    rate
    && cumulativeTotal
    && cumulativeDownloaded != null
    && cumulativeTotal > cumulativeDownloaded
  ) {
    return Math.max(1, Math.ceil((cumulativeTotal - cumulativeDownloaded) / rate));
  }
  const downloadEta = positiveNumber(detail?.eta_seconds);
  if (downloadEta) return Math.max(1, Math.ceil(downloadEta));
  return previousSeconds != null ? Math.max(1, Math.round(previousSeconds)) : null;
}

export function estimatePaceRemainingSeconds(
  job: ReaSetupJob | null,
  percent: number,
  elapsedSeconds: number,
): number | null {
  if (!job || !isActiveStatus(job.status)) return null;
  const boundedPercent = Math.max(0, Math.min(100, percent));
  const estimatedMinimum = positiveNumber(job.estimated_seconds_min);
  if (elapsedSeconds < 10 || boundedPercent < 3) {
    return estimatedMinimum == null ? null : Math.max(1, Math.round(estimatedMinimum));
  }

  const pace = elapsedSeconds * (100 - boundedPercent) / Math.max(3, boundedPercent);
  const estimatedMaximum = positiveNumber(job.estimated_seconds_max);
  const minimumRemaining = Math.min(15, Math.max(5, pace * 0.15));
  const maximumRemaining = Math.max(
    minimumRemaining,
    estimatedMaximum ?? (estimatedMinimum == null ? pace * 2 : estimatedMinimum * 1.5),
  );
  return Math.max(1, Math.round(Math.max(minimumRemaining, Math.min(pace, maximumRemaining))));
}

export function shouldDeferEnhancedDetection(
  job: ReaSetupJob | null,
  capabilityCardCount: number,
): boolean {
  return Boolean(
    job
    && isActiveStatus(job.status)
    && job.kind === 'base'
    && capabilityCardCount === 0,
  );
}

export function stabilizeRemainingSeconds(
  job: ReaSetupJob | null,
  candidateSeconds: number | null,
  previousSeconds: number | null,
): number | null {
  if (!job || !isActiveStatus(job.status)) return null;
  if (candidateSeconds == null) return previousSeconds;
  if (previousSeconds == null) return Math.max(1, Math.round(candidateSeconds));

  const previous = Math.max(1, previousSeconds);
  const maximumGrowth = Math.max(previous + 30, Math.ceil(previous * 1.5));
  const boundedCandidate = Math.max(1, Math.min(candidateSeconds, maximumGrowth));
  const smoothing = boundedCandidate < previous ? 0.4 : 0.3;
  return Math.max(1, Math.round(previous + (boundedCandidate - previous) * smoothing));
}

export interface DisplayedRemainingTime {
  jobId: string;
  seconds: number;
  updatedAt: number;
}

export function selectDisplayedRemainingSeconds(
  jobId: string,
  candidateSeconds: number | null,
  previous: DisplayedRemainingTime | null,
  nowMs: number,
  minimumIntervalMs = REMAINING_DISPLAY_INTERVAL_MS,
): DisplayedRemainingTime | null {
  if (candidateSeconds == null) return null;
  if (previous?.jobId === jobId) {
    const elapsed = Math.max(0, nowMs - previous.updatedAt);
    const relativeChange =
      Math.abs(candidateSeconds - previous.seconds) / Math.max(1, previous.seconds);
    if (elapsed < minimumIntervalMs && relativeChange < 0.25) {
      return previous;
    }
    if (
      previous.seconds >= 60
      && candidateSeconds >= 60
      && Math.ceil(previous.seconds / 60) === Math.ceil(candidateSeconds / 60)
      && relativeChange < 0.35
    ) {
      return previous;
    }
  }
  return {
    jobId,
    seconds: Math.max(1, Math.round(candidateSeconds)),
    updatedAt: nowMs,
  };
}

export function formatProgressDuration(
  value: number,
): { value: number; unit: 'minutes' | 'seconds' } {
  const seconds = Math.max(1, Math.ceil(value));
  if (seconds < 60) return { value: seconds, unit: 'seconds' };
  return { value: Math.max(1, Math.ceil(seconds / 60)), unit: 'minutes' };
}

export function messageWithoutEta(message: string): string {
  return message.replace(/[，,]\s*预计剩余\s*\d+\s*秒/g, '').trim();
}
