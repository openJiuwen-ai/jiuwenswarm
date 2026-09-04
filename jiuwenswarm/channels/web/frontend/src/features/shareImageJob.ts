export interface ShareImageExportJobStatus {
  job_id: string;
  session_id?: string;
  filename: string;
  state: 'queued' | 'running' | 'completed' | 'failed';
  phase?: string;
  error?: string | null;
  reused?: boolean;
}

type ShareImageJobStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
type ShareImageJobFetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

const PENDING_SHARE_IMAGE_JOB_KEY_PREFIX = 'jiuwenswarm:pending-share-image-job:';

function pendingShareImageJobKey(sessionId: string): string {
  return `${PENDING_SHARE_IMAGE_JOB_KEY_PREFIX}${encodeURIComponent(sessionId)}`;
}

export function readPendingShareImageJobId(
  storage: ShareImageJobStorage,
  sessionId: string,
): string | null {
  const jobId = storage.getItem(pendingShareImageJobKey(sessionId));
  if (jobId === null) return null;
  if (/^[a-f0-9]{32}$/.test(jobId)) return jobId;
  storage.removeItem(pendingShareImageJobKey(sessionId));
  return null;
}

export function rememberPendingShareImageJob(
  storage: ShareImageJobStorage,
  sessionId: string,
  jobId: string,
): void {
  if (!/^[a-f0-9]{32}$/.test(jobId)) {
    throw new Error('share_export_job_id_invalid');
  }
  storage.setItem(pendingShareImageJobKey(sessionId), jobId);
}

export function forgetPendingShareImageJob(
  storage: ShareImageJobStorage,
  sessionId: string,
  jobId: string,
): void {
  const key = pendingShareImageJobKey(sessionId);
  if (storage.getItem(key) === jobId) storage.removeItem(key);
}

export async function readShareImageJobResponse(response: Response): Promise<ShareImageExportJobStatus> {
  const payload = await response.json().catch(() => null) as Partial<ShareImageExportJobStatus> | null;
  if (!response.ok) {
    throw new Error(payload?.error || `HTTP ${response.status}`);
  }
  if (
    !payload
    || typeof payload.job_id !== 'string'
    || !/^[a-f0-9]{32}$/.test(payload.job_id)
    || typeof payload.filename !== 'string'
    || !payload.filename.trim()
    || !['queued', 'running', 'completed', 'failed'].includes(payload.state ?? '')
  ) {
    throw new Error('share_export_job_response_invalid');
  }
  return payload as ShareImageExportJobStatus;
}

function assertJobMatchesSession(status: ShareImageExportJobStatus, sessionId: string): void {
  if (status.session_id !== undefined && status.session_id !== sessionId) {
    throw new Error('share_export_job_session_mismatch');
  }
}

export async function findShareImageJobForSession(
  sessionId: string,
  storage: ShareImageJobStorage,
  request: ShareImageJobFetch = fetch,
): Promise<ShareImageExportJobStatus | null> {
  const pendingJobId = readPendingShareImageJobId(storage, sessionId);
  if (pendingJobId !== null) {
    const pendingResponse = await request(
      `/share-api/jobs/${encodeURIComponent(pendingJobId)}`,
      { cache: 'no-store' },
    );
    if (pendingResponse.status !== 404) {
      const status = await readShareImageJobResponse(pendingResponse);
      assertJobMatchesSession(status, sessionId);
      return status;
    }
    forgetPendingShareImageJob(storage, sessionId, pendingJobId);
  }

  const activeResponse = await request(
    `/share-api/jobs?${new URLSearchParams({ session_id: sessionId }).toString()}`,
    { cache: 'no-store' },
  );
  if (activeResponse.status === 404) return null;
  const status = await readShareImageJobResponse(activeResponse);
  assertJobMatchesSession(status, sessionId);
  rememberPendingShareImageJob(storage, sessionId, status.job_id);
  return status;
}
