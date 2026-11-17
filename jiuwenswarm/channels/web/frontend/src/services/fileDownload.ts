/** Download tokens are checked by Gateway; login belongs to the upstream. */
export function fileDownloadHead(url: string): Promise<Response> {
  return fetch(url, { method: 'HEAD' });
}

/** A transient HEAD failure must not prevent an independently authorized GET. */
export async function prepareFileDownload(url: string): Promise<boolean> {
  try {
    const response = await fileDownloadHead(url);
    return response.ok || response.status >= 500 || response.status === 405;
  } catch {
    return true;
  }
}
