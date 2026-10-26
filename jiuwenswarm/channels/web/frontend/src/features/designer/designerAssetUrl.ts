/** Turn a Designer output_ref.uri into a browser-playable /file-api URL. */

import { webRequest } from '../../services/webClient';

/** Canvas uploads keep the pre-existing 32 MiB ceiling; chunks stay at 512 KiB. */
export const DESIGNER_UPLOAD_MAX_BYTES = 32 * 1024 * 1024;
export const DESIGNER_UPLOAD_CHUNK_BYTES = 512 * 1024;
export const DESIGNER_UPLOAD_TOO_LARGE = 'file_too_large';
// ``file.upload_chunk`` resolves a relative target under the user workspace
// (~/.jiuwenswarm). Keep uploads under ``agent/`` so the app_web preview root
// (/file-api/raw-file) accepts the persisted path in production builds.
const DESIGNER_UPLOAD_DIR = 'agent/uploads/designer';

export function localPathToFileUri(path: string): string {
  const normalized = path.replace(/\\/g, '/');
  if (/^[A-Za-z]:\//.test(normalized)) {
    return `file:///${normalized}`;
  }
  if (normalized.startsWith('/')) {
    return `file://${normalized}`;
  }
  return `file:///${normalized}`;
}

export function fileUriToLocalPath(uri: string): string | null {
  const value = (uri || '').trim();
  if (!value) return null;
  if (value.startsWith('designer://')) return null;
  if (/^https?:\/\//i.test(value)) return null;
  if (value.startsWith('file:')) {
    try {
      const parsed = new URL(value);
      let pathname = decodeURIComponent(parsed.pathname);
      if (/^\/[A-Za-z]:\//.test(pathname)) {
        pathname = pathname.slice(1);
      }
      return pathname;
    } catch {
      return null;
    }
  }
  return value;
}

export function isPlaceholderAsset(uri: string | null | undefined): boolean {
  return Boolean(uri?.startsWith('designer://'));
}

export function designerAssetPreviewUrl(uri: string | null | undefined): string | null {
  const value = (uri || '').trim();
  if (!value) return null;
  if (/^https?:\/\//i.test(value)) return value;
  // Session-local uploads use blob URLs.
  if (value.startsWith('blob:')) return value;
  const localPath = fileUriToLocalPath(value);
  if (!localPath) return null;
  return `/file-api/raw-file?path=${encodeURIComponent(localPath)}`;
}

export async function saveDesignerTextFile(uri: string, content: string): Promise<void> {
  const filePath = fileUriToLocalPath(uri);
  if (!filePath) throw new Error('missing_file_path');
  const response = await fetch('/file-api/file-content', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ path: filePath, content }),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail.slice(0, 160) || `HTTP ${response.status}`);
  }
}

function uploadId(): string {
  const cryptoApi = typeof crypto !== 'undefined' ? crypto : undefined;
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') {
    return cryptoApi.randomUUID().replace(/-/g, '');
  }
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}

function safeUploadFilename(name: string): string {
  const trimmed = (name || 'upload.bin').trim() || 'upload.bin';
  return trimmed.replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_').slice(0, 120);
}

function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error('read_failed'));
    reader.onload = () => {
      const result = String(reader.result ?? '');
      const comma = result.indexOf(',');
      resolve(comma >= 0 ? result.slice(comma + 1) : '');
    };
    reader.readAsDataURL(blob);
  });
}

/**
 * Persist one canvas asset through the authenticated ``file.upload_chunk`` WS
 * RPC (512 KiB sequential chunks, abort on the first failure).
 *
 * The old implementation posted multipart to ``/file-api/upload``, which only
 * the Vite dev middleware accepts; the production app_web entry requires an
 * upload token and answered ``missing_token`` (issue #5325 / GitHub #8513).
 */
export async function uploadDesignerAsset(file: File): Promise<{
  path: string;
  filename: string;
  mime_type?: string;
}> {
  if (!file || file.size === 0) {
    throw new Error('empty_file');
  }
  if (file.size > DESIGNER_UPLOAD_MAX_BYTES) {
    throw new Error(DESIGNER_UPLOAD_TOO_LARGE);
  }
  const uploadToken = uploadId();
  const filename = safeUploadFilename(file.name);
  const targetRelPath = `${DESIGNER_UPLOAD_DIR}/${uploadToken}__${filename}`;
  let resolvedPath = '';
  for (let offset = 0; offset < file.size; offset += DESIGNER_UPLOAD_CHUNK_BYTES) {
    const chunk = file.slice(offset, Math.min(offset + DESIGNER_UPLOAD_CHUNK_BYTES, file.size));
    const data = await blobToBase64(chunk);
    if (!data) throw new Error('upload_failed');
    const response = await webRequest<{ path?: string }>(
      'file.upload_chunk',
      {
        upload_id: uploadToken,
        target_rel_path: targetRelPath,
        resolved_path: resolvedPath,
        data,
        final: offset + chunk.size >= file.size,
      },
      { timeoutMs: 120_000 },
    );
    const nextPath = String(response?.path ?? '').trim();
    if (!nextPath) throw new Error('upload_failed');
    resolvedPath = nextPath;
  }
  return { path: resolvedPath, filename, mime_type: file.type || undefined };
}

export function designerAssetTextUrl(uri: string | null | undefined): string | null {
  const value = (uri || '').trim();
  if (!value || /^https?:\/\//i.test(value)) return null;
  const localPath = fileUriToLocalPath(value);
  if (!localPath) return null;
  return `/file-api/file-content?path=${encodeURIComponent(localPath)}&encoding=auto`;
}
