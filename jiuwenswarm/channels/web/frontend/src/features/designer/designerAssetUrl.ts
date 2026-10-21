/** Turn a Designer output_ref.uri into a browser-playable /file-api URL. */

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

export async function uploadDesignerAsset(file: File): Promise<{
  path: string;
  filename: string;
  mime_type?: string;
}> {
  if (file.size === 0) throw new Error('empty_file');
  if (file.size > 32 * 1024 * 1024) throw new Error('file_too_large');
  const { webRequest } = await import('../../services/webClient');
  const uploadId = crypto.randomUUID();
  const safeName = (file.name.split(/[\\/]/).pop() || 'upload.bin')
    .replace(/[\x00-\x1f\\/:*?"<>|]/g, '_')
    .slice(0, 120);
  const targetPath = `uploads/designer/${uploadId}_${safeName}`;
  const chunkSize = 512 * 1024;
  let resolvedPath = '';
  for (let offset = 0; offset < file.size; offset += chunkSize) {
    const bytes = new Uint8Array(await file.slice(offset, offset + chunkSize).arrayBuffer());
    let binary = '';
    for (let start = 0; start < bytes.length; start += 8192) {
      binary += String.fromCharCode(...bytes.subarray(start, start + 8192));
    }
    const uploaded = await webRequest<{ path: string }>('file.upload_chunk', {
      upload_id: uploadId,
      target_rel_path: targetPath,
      ...(resolvedPath ? { resolved_path: resolvedPath } : {}),
      data: btoa(binary),
      final: offset + chunkSize >= file.size,
    });
    if (!uploaded.path) throw new Error('upload_failed');
    resolvedPath = uploaded.path;
  }
  return {
    path: resolvedPath,
    filename: file.name,
    mime_type: file.type,
  };
}

export function designerAssetTextUrl(uri: string | null | undefined): string | null {
  const value = (uri || '').trim();
  if (!value || /^https?:\/\//i.test(value)) return null;
  const localPath = fileUriToLocalPath(value);
  if (!localPath) return null;
  return `/file-api/file-content?path=${encodeURIComponent(localPath)}&encoding=auto`;
}
