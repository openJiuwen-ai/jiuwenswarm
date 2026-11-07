/**
 * Export handling (Gateway S_10 section 7): a webui.download offer opens the desktop save dialog,
 * or a download link in a browser.
 */

import { isRecord } from '../protocol/utils.ts';

export const DOWNLOAD_EVENT = 'webui.download';
const DOWNLOAD_URL = /^\/extension-downloads\/[0-9a-f]{32}$/;

export interface WebUIDownloadOffer {
  packageId: string;
  url: string;
  filename: string;
  size: number;
}

/** Validate a download frame; anything else is ignored. */
export function readDownloadOffer(value: unknown): WebUIDownloadOffer | null {
  if (!isRecord(value)) return null;
  const { package_id: packageId, url, filename, size } = value;
  if (typeof packageId !== 'string' || !packageId || typeof url !== 'string' || !DOWNLOAD_URL.test(url)
    || typeof filename !== 'string' || !filename.trim() || /[/\\\u0000-\u001f\u007f]/.test(filename)
    || typeof size !== 'number' || !Number.isInteger(size) || size < 0) return null;
  return { packageId, url, filename, size };
}

export interface WebUIDownloadTarget {
  desktopDownload?: (url: string, filename: string) => Promise<unknown>;
  document?: Document;
}

/** Hand the offer to the desktop save dialog, or click a download link in a browser. */
export async function startDownload(offer: WebUIDownloadOffer, target: WebUIDownloadTarget): Promise<void> {
  if (target.desktopDownload) {
    await target.desktopDownload(offer.url, offer.filename);
    return;
  }
  const doc = target.document;
  if (!doc) return;
  const link = doc.createElement('a');
  link.href = offer.url;
  link.download = offer.filename;
  link.rel = 'noopener';
  link.style.display = 'none';
  doc.body.appendChild(link);
  link.click();
  link.remove();
}
