/**
 * Files a tool call inserts into the presentation, such as an image.
 *
 * The model publishes a file of its conversation with the backend's asset tool
 * (``office/assets.py``) and passes the descriptor it gets back; the call's request carries a signed,
 * short-lived ``/file-api/download`` link per handle (``downloads``), which the
 * model never sees. The pane fetches the link from its own origin and takes the
 * bytes only if their size and SHA-256 are the descriptor's.
 */

/** The reference the model passes, as ``office/assets.py`` describes the published file. */
export interface AssetDescriptor {
  handle: string;
  mime_type: string;
  size: number;
  sha256: string;
}

const DOWNLOAD_URL = /^\/file-api\/download\?token=[A-Za-z0-9%._~-]+$/;

/** The asset's bytes as base64, checked against its descriptor; throws when they are not the published file. */
export async function downloadAsset(
  asset: AssetDescriptor, downloads: Readonly<Record<string, string>>,
  fetcher: (url: string, init: RequestInit) => Promise<Response> = (url, init) => fetch(url, init),
): Promise<string> {
  const url = downloads[asset.handle];
  if (!url) throw new Error(`no download was granted for asset ${asset.handle}`);
  // Only the backend's own download route on this origin; a link elsewhere is not one it signed.
  if (!DOWNLOAD_URL.test(url)) throw new Error(`the download link of asset ${asset.handle} is not a file-api link`);
  const response = await fetcher(url, { redirect: 'error' });
  if (!response.ok) throw new Error(`asset ${asset.handle} did not download (HTTP ${response.status})`);
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes.byteLength !== asset.size || await sha256Hex(bytes) !== asset.sha256) {
    throw new Error(`asset ${asset.handle} is not the published file: its size or SHA-256 differs`);
  }
  return bytesToBase64(bytes);
}

export async function sha256Hex(bytes: BufferSource): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  for (let at = 0; at < bytes.length; at += 32768) binary += String.fromCharCode(...bytes.subarray(at, at + 32768));
  return btoa(binary);
}
