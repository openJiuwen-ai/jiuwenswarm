export type DesktopSaveResult = {
  ok: boolean;
  cancelled?: boolean;
};

export type DesktopSaveApiResult = Promise<boolean | DesktopSaveResult> | boolean | DesktopSaveResult;

export type DesktopSaveOutcome = 'saved' | 'cancelled' | 'failed';

export type BlobSaveTransport = 'browser-download' | 'browser-file-picker' | 'desktop';

export interface BlobSaveResult {
  outcome: DesktopSaveOutcome;
  transport: BlobSaveTransport;
}

export interface BlobSaveOptions {
  preferBrowserFilePicker?: boolean;
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.style.display = 'none';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function saveBlobWithResult(
  blob: Blob,
  filename: string,
  _options: BlobSaveOptions = {},
): Promise<BlobSaveResult> {
  downloadBlob(blob, filename);
  return { outcome: 'saved', transport: 'browser-download' };
}

export function isDesktopSaveCancelled(result: boolean | DesktopSaveResult): boolean {
  return typeof result === 'object' && result.cancelled === true;
}

export function isDesktopSaveOk(result: boolean | DesktopSaveResult): boolean {
  return typeof result === 'boolean' ? result : result.ok;
}

export async function executeDesktopSave(save: () => DesktopSaveApiResult): Promise<DesktopSaveOutcome> {
  try {
    const result = await save();
    if (isDesktopSaveCancelled(result)) return 'cancelled';
    return isDesktopSaveOk(result) ? 'saved' : 'failed';
  } catch (error) {
    console.error('Desktop save API failed:', error);
    return 'failed';
  }
}
