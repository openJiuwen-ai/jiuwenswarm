import { resolveApiUrl } from '../utils/env';

/** Download tokens are checked by Gateway; login belongs to the upstream. */
export function fileDownloadHead(url: string): Promise<Response> {
  return fetch(resolveApiUrl(url), { method: 'HEAD' });
}

/**
 * Statuses that mean the download link is gone / forbidden / gone forever.
 * Transient probe failures (405 HEAD unsupported, 5xx, network) must NOT
 * paint the chat file card as「已过期」— GET download can still succeed.
 */
export const FILE_DOWNLOAD_EXPIRED_STATUSES = new Set([403, 404, 410]);

export function isFileDownloadExpiredStatus(status: number): boolean {
  return FILE_DOWNLOAD_EXPIRED_STATUSES.has(status);
}

/**
 * Return false only when the link is definitively expired/gone.
 * 401/429/other non-expired 4xx and probe failures must not block GET download.
 */
export async function prepareFileDownload(url: string): Promise<boolean> {
  try {
    const response = await fileDownloadHead(url);
    return !isFileDownloadExpiredStatus(response.status);
  } catch {
    return true;
  }
}
