export interface ShareImageExportJobStatus {
  job_id: string;
  session_id?: string;
  filename: string;
  state: 'queued' | 'running' | 'completed' | 'failed';
  phase?: string;
  error?: string | null;
  reused?: boolean;
  renderer?: 'checking' | 'server' | 'browser';
  client_token?: string;
  locale?: string;
  progress?: number | null;
  download_claimed?: boolean;
  download_attempt_id?: string;
}

export async function updateBrowserShareImageJob(
  jobId: string,
  clientToken: string,
  update: {
    state: 'running' | 'failed'; phase: string; progress?: number; error?: string; claim?: boolean;
    claim_id?: string; expected_claim_id?: string; expected_state?: 'queued';
  },
): Promise<ShareImageExportJobStatus> {
  return fetchShareImageJobResponse(`/share-api/jobs/${encodeURIComponent(jobId)}/browser`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ ...update, client_token: clientToken }),
    cache: 'no-store',
  });
}

type ShareImageJobStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
type ShareImageJobFetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

const PENDING_SHARE_IMAGE_JOB_KEY_PREFIX = 'jiuwenswarm:pending-share-image-job:';

interface PendingShareImageJob {
  job_id: string;
  client_token?: string;
  locale?: string;
}

function readPendingShareImageJob(storage: ShareImageJobStorage, sessionId: string): PendingShareImageJob | null {
  const raw = storage.getItem(pendingShareImageJobKey(sessionId));
  if (raw === null) return null;
  // Migrate the previous job-id-only storage format without losing active jobs.
  if (/^[a-f0-9]{32}$/.test(raw)) return { job_id: raw };
  try {
    const value = JSON.parse(raw) as PendingShareImageJob;
    if (/^[a-f0-9]{32}$/.test(value.job_id)
      && (value.client_token === undefined || /^(?:[a-f0-9]{32}|[a-f0-9]{64})$/.test(value.client_token))
      && (value.locale === undefined || typeof value.locale === 'string')) return value;
  } catch { /* Invalid storage records cannot authorize a renderer. */ }
  storage.removeItem(pendingShareImageJobKey(sessionId));
  return null;
}

function pendingShareImageJobKey(sessionId: string): string {
  return `${PENDING_SHARE_IMAGE_JOB_KEY_PREFIX}${encodeURIComponent(sessionId)}`;
}

export function readPendingShareImageJobId(
  storage: ShareImageJobStorage,
  sessionId: string,
): string | null {
  return readPendingShareImageJob(storage, sessionId)?.job_id ?? null;
}

export function rememberPendingShareImageJob(
  storage: ShareImageJobStorage,
  sessionId: string,
  job: string | ShareImageExportJobStatus,
  locale?: string,
): void {
  const jobId = typeof job === 'string' ? job : job.job_id;
  if (!/^[a-f0-9]{32}$/.test(jobId)) {
    throw new Error('share_export_job_id_invalid');
  }
  const previous = readPendingShareImageJob(storage, sessionId);
  const jobLocale = locale || (typeof job !== 'string' ? job.locale : undefined);
  storage.setItem(pendingShareImageJobKey(sessionId), JSON.stringify({
    ...(previous?.job_id === jobId ? previous : {}),
    job_id: jobId,
    ...(typeof job !== 'string' && job.client_token ? { client_token: job.client_token } : {}),
    ...(jobLocale ? { locale: jobLocale } : {}),
  }));
}

export function forgetPendingShareImageJob(
  storage: ShareImageJobStorage,
  sessionId: string,
  jobId: string,
): void {
  const key = pendingShareImageJobKey(sessionId);
  if (readPendingShareImageJobId(storage, sessionId) === jobId) storage.removeItem(key);
}

/** Bound individual protocol requests, never the time spent generating an image. */
async function fetchShareImageProtocol<T>(
  input: RequestInfo | URL,
  init: RequestInit,
  request: ShareImageJobFetch,
  timeoutMs: number,
  read: (response: Response) => Promise<T>,
): Promise<T> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      request(input, { ...init, cache: 'no-store', signal: controller.signal })
        .then(read),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => {
          reject(new Error('share_export_status_timeout'));
          controller.abort();
        }, timeoutMs);
      }),
    ]);
  } finally { clearTimeout(timer); }
}

export function fetchShareImageJobResponse(input: RequestInfo | URL, init: RequestInit = {}, request: ShareImageJobFetch = fetch, timeoutMs = 10_000): Promise<ShareImageExportJobStatus> {
  return fetchShareImageProtocol(input, init, request, timeoutMs, readShareImageJobResponse);
}

function fetchExistingShareImageJob(input: RequestInfo | URL, request: ShareImageJobFetch): Promise<ShareImageExportJobStatus | null> {
  return fetchShareImageProtocol(input, {}, request, 10_000,
    async response => response.status === 404 ? null : readShareImageJobResponse(response));
}

export function fetchShareImageJobStatus(jobId: string, request: ShareImageJobFetch = fetch, timeoutMs = 10_000): Promise<ShareImageExportJobStatus> {
  return fetchShareImageJobResponse(`/share-api/jobs/${encodeURIComponent(jobId)}`, {}, request, timeoutMs);
}

/** One atomic claim covers both the renderer and a chat restored after reload. */
export async function startShareImageJobDownload(status: ShareImageExportJobStatus): Promise<ShareImageExportJobStatus> {
  if (!status.client_token) throw new Error('share_export_client_token_missing');
  const claimed = await fetchShareImageJobResponse(`/share-api/jobs/${encodeURIComponent(status.job_id)}/browser`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ client_token: status.client_token, claim_download: true }),
  });
  if (claimed.download_claimed) {
    if (!/^[a-f0-9]{32}$/.test(claimed.download_attempt_id ?? '')) throw new Error('share_export_download_attempt_invalid');
    downloadShareImageJob(status.job_id, status.filename, claimed.download_attempt_id);
  }
  return { ...claimed, client_token: status.client_token };
}

export async function readShareImageJobResponse(response: Response): Promise<ShareImageExportJobStatus> {
  const payload = await response.json().catch(() => null) as Partial<ShareImageExportJobStatus> | null;
  return validateShareImageJobResponse(response, payload);
}

function validateShareImageJobResponse(
  response: Response,
  payload: Partial<ShareImageExportJobStatus> | null | undefined,
): ShareImageExportJobStatus {
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

/** Download the published HTTP artifact, whose lifetime is independent of the renderer. */
export function downloadShareImageJob(jobId: string, filename: string, attemptId?: string): void {
  const anchor = document.createElement('a');
  const query = attemptId ? `?${new URLSearchParams({ download_attempt_id: attemptId })}` : '';
  anchor.href = `/share-api/jobs/${encodeURIComponent(jobId)}/download${query}`;
  anchor.download = filename;
  anchor.style.display = 'none';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

/** Only acknowledge a download once the server has received its GET request. */
export async function waitForShareImageJobDownload(
  initialStatus: ShareImageExportJobStatus,
  isCurrent: () => boolean,
  request: ShareImageJobFetch = fetch,
  pause: () => Promise<void> = () => new Promise(resolve => window.setTimeout(resolve, 500)),
  options: { timeoutMs?: number; now?: () => number } = {},
): Promise<ShareImageExportJobStatus | null> {
  const now = options.now ?? Date.now;
  const deadline = now() + (options.timeoutMs ?? 60_000);
  let status = initialStatus;
  while (isCurrent()) {
    if (status.job_id !== initialStatus.job_id) throw new Error('share_export_job_id_mismatch');
    if (status.state === 'failed') throw new Error(status.error || 'share_export_job_failed');
    if (status.state !== 'completed') throw new Error('share_export_download_not_ready');
    if (status.phase === 'download_failed') throw new Error('share_export_download_interrupted');
    if (status.phase === 'download_started' || status.phase === 'download_sent') return status;
    await pause();
    if (!isCurrent()) return null;
    // Read fresh status even after a background-tab timer was delayed: a GET
    // may already have started the download while this monitor was suspended.
    try {
      status = await fetchShareImageJobStatus(initialStatus.job_id, request);
    } catch (error) {
      if (!(error instanceof TypeError) && !(error instanceof Error && error.message === 'share_export_status_timeout')) throw error;
      continue;
    }
    if (status.job_id !== initialStatus.job_id) throw new Error('share_export_job_id_mismatch');
    if (status.phase !== 'download_started' && status.phase !== 'download_sent' && now() >= deadline) throw new Error('share_export_download_start_timeout');
  }
  return null;
}

/** Keep the owning tab alive until the HTTP file stream ends; transfer has no deadline. */
export async function waitForShareImageJobTransfer(
  initialStatus: ShareImageExportJobStatus,
  isCurrent: () => boolean,
  onStatus: (status: ShareImageExportJobStatus) => void = () => {},
  request: ShareImageJobFetch = fetch,
  pause: () => Promise<void> = () => new Promise(resolve => window.setTimeout(resolve, 500)),
): Promise<ShareImageExportJobStatus | null> {
  let status = await waitForShareImageJobDownload(initialStatus, isCurrent, request, pause);
  while (status && isCurrent()) {
    if (status.job_id !== initialStatus.job_id) throw new Error('share_export_job_id_mismatch');
    if (status.state === 'failed') throw new Error(status.error || 'share_export_job_failed');
    if (status.phase === 'download_failed') throw new Error('share_export_download_interrupted');
    onStatus(status);
    if (status.phase === 'download_sent') return status;
    await pause();
    if (!isCurrent()) return null;
    try {
      status = await fetchShareImageJobStatus(initialStatus.job_id, request);
    } catch (error) {
      // A status request is independent of the active file transfer. Temporary
      // transport failure leaves the transfer and its next observation intact.
      if (!(error instanceof TypeError) && !(error instanceof Error && error.message === 'share_export_status_timeout')) throw error;
    }
  }
  return null;
}

function assertJobMatchesSession(status: ShareImageExportJobStatus, sessionId: string): void {
  if (status.session_id !== undefined && status.session_id !== sessionId) {
    throw new Error('share_export_job_session_mismatch');
  }
}

/** Recover a missing browser capability for this exact job, including legacy records. */
export async function resumeBrowserShareImageJob(
  status: ShareImageExportJobStatus,
  sessionId: string,
  storage: ShareImageJobStorage,
  request: ShareImageJobFetch = fetch,
): Promise<ShareImageExportJobStatus> {
  if (status.renderer !== 'browser' || status.state === 'failed'
    || status.phase === 'download_started' || status.phase === 'download_sent' || status.client_token) return status;
  const resumed = await fetchShareImageJobResponse('/share-api/jobs', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, job_id: status.job_id }),
  }, request);
  if (resumed.job_id !== status.job_id) throw new Error('share_export_job_id_mismatch');
  assertJobMatchesSession(resumed, sessionId);
  if (resumed.renderer !== 'browser' || !resumed.client_token) throw new Error('share_export_client_token_missing');
  rememberPendingShareImageJob(storage, sessionId, resumed);
  return resumed;
}

export async function findShareImageJobForSession(
  sessionId: string,
  storage: ShareImageJobStorage,
  request: ShareImageJobFetch = fetch,
): Promise<ShareImageExportJobStatus | null> {
  const pending = readPendingShareImageJob(storage, sessionId);
  const pendingJobId = pending?.job_id ?? null;
  if (pendingJobId !== null) {
    const status = await fetchExistingShareImageJob(`/share-api/jobs/${encodeURIComponent(pendingJobId)}`, request);
    if (status !== null) {
      assertJobMatchesSession(status, sessionId);
      return { ...status, client_token: pending?.client_token, locale: pending?.locale ?? status.locale };
    }
    forgetPendingShareImageJob(storage, sessionId, pendingJobId);
  }

  const status = await fetchShareImageProtocol(
    `/share-api/jobs?${new URLSearchParams({ session_id: sessionId }).toString()}`, {}, request, 10_000,
    async response => {
      const payload = await response.json().catch(() => undefined) as Partial<ShareImageExportJobStatus> | null | undefined;
      if (response.ok && payload === null) return null;
      return validateShareImageJobResponse(response, payload);
    },
  );
  if (status === null) return null;
  assertJobMatchesSession(status, sessionId);
  rememberPendingShareImageJob(storage, sessionId, status);
  return status;
}
