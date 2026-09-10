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

interface BrowserWritableFileStream {
  write: (data: Blob) => Promise<void>;
  close: () => Promise<void>;
}

interface BrowserFileHandle {
  createWritable: () => Promise<BrowserWritableFileStream>;
}

type BrowserFilePicker = (options: {
  suggestedName: string;
  types: Array<{
    description: string;
    accept: Record<string, string[]>;
  }>;
}) => Promise<BrowserFileHandle>;

const DESKTOP_BLOB_CHUNK_SIZE = 1024 * 1024;

interface DesktopBlobSaveApi {
  begin_blob_save: (filename: string, mimeType: string, totalSize: number) => Promise<DesktopBlobSaveStartResult> | DesktopBlobSaveStartResult;
  append_blob_save: (transferId: string, encodedChunk: string) => Promise<boolean> | boolean;
  finish_blob_save: (transferId: string) => Promise<DesktopSaveResult> | DesktopSaveResult;
  abort_blob_save: (transferId: string) => Promise<boolean> | boolean;
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

async function saveBlobWithBrowserFilePicker(
  blob: Blob,
  filename: string,
  picker: BrowserFilePicker,
): Promise<DesktopSaveOutcome> {
  try {
    const mimeType = blob.type.split(';', 1)[0] || 'application/octet-stream';
    const suffixMatch = filename.match(/(\.[a-zA-Z0-9]+)$/);
    const handle = await picker({
      suggestedName: filename,
      types: [{
        description: 'Export file',
        accept: { [mimeType]: suffixMatch === null ? [] : [suffixMatch[1]] },
      }],
    });
    const writable = await handle.createWritable();
    await writable.write(blob);
    await writable.close();
    return 'saved';
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') return 'cancelled';
    console.error('Browser file save failed:', error);
    return 'failed';
  }
}

function blobChunkToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result !== 'string') {
        reject(new Error('blob_chunk_encoding_failed'));
        return;
      }
      const separatorIndex = reader.result.indexOf(',');
      if (separatorIndex < 0) {
        reject(new Error('blob_chunk_encoding_failed'));
        return;
      }
      resolve(reader.result.slice(separatorIndex + 1));
    };
    reader.onerror = () => reject(reader.error ?? new Error('blob_chunk_encoding_failed'));
    reader.onabort = () => reject(new Error('blob_chunk_encoding_aborted'));
    reader.readAsDataURL(blob);
  });
}

function getDesktopBlobSaveApi(): DesktopBlobSaveApi | null {
  const api = window.pywebview?.api;
  if (!api) return null;
  if (!api.begin_blob_save || !api.append_blob_save || !api.finish_blob_save || !api.abort_blob_save) {
    return null;
  }
  return {
    begin_blob_save: (filename, mimeType, totalSize) => api.begin_blob_save!(filename, mimeType, totalSize),
    append_blob_save: (transferId, encodedChunk) => api.append_blob_save!(transferId, encodedChunk),
    finish_blob_save: transferId => api.finish_blob_save!(transferId),
    abort_blob_save: transferId => api.abort_blob_save!(transferId),
  };
}

async function saveBlobToDesktop(blob: Blob, filename: string, api: DesktopBlobSaveApi): Promise<DesktopSaveOutcome> {
  let transferId: string | null = null;
  try {
    const startResult = await api.begin_blob_save(filename, blob.type, blob.size);
    if (isDesktopSaveCancelled(startResult)) return 'cancelled';
    if (!isDesktopSaveOk(startResult) || !startResult.transfer_id) return 'failed';
    transferId = startResult.transfer_id;

    for (let offset = 0; offset < blob.size; offset += DESKTOP_BLOB_CHUNK_SIZE) {
      const chunk = blob.slice(offset, offset + DESKTOP_BLOB_CHUNK_SIZE);
      const encodedChunk = await blobChunkToBase64(chunk);
      if (!(await api.append_blob_save(transferId, encodedChunk))) {
        return 'failed';
      }
    }

    const finishResult = await api.finish_blob_save(transferId);
    transferId = null;
    return isDesktopSaveOk(finishResult) ? 'saved' : 'failed';
  } catch (error) {
    console.error('Desktop blob save failed:', error);
    return 'failed';
  } finally {
    if (transferId) {
      try {
        await api.abort_blob_save(transferId);
      } catch (error) {
        console.error('Failed to abort desktop blob save:', error);
      }
    }
  }
}

export async function saveBlobWithResult(
  blob: Blob,
  filename: string,
  options: BlobSaveOptions = {},
): Promise<BlobSaveResult> {
  if (!window.pywebview) {
    const picker = (window as Window & { showSaveFilePicker?: BrowserFilePicker }).showSaveFilePicker;
    if (options.preferBrowserFilePicker && picker !== undefined) {
      return {
        outcome: await saveBlobWithBrowserFilePicker(blob, filename, picker.bind(window)),
        transport: 'browser-file-picker',
      };
    }
    downloadBlob(blob, filename);
    return { outcome: 'saved', transport: 'browser-download' };
  }

  const api = getDesktopBlobSaveApi();
  if (!api) {
    console.error('Desktop blob save API is unavailable');
    return { outcome: 'failed', transport: 'desktop' };
  }
  return {
    outcome: await saveBlobToDesktop(blob, filename, api),
    transport: 'desktop',
  };
}

export async function saveBlob(blob: Blob, filename: string): Promise<DesktopSaveOutcome> {
  return (await saveBlobWithResult(blob, filename)).outcome;
}
