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

/**
 * Kinds the chat can actually render inline. Text and table outputs (e.g. the
 * storyboard) are legitimate "@" references but have no thumbnail of their own —
 * giving them an <img> only ever produced a broken image, so they are shown by
 * name alone.
 */
export function isPreviewableMediaKind(kind: string | null | undefined): boolean {
  const value = (kind || '').trim().toLowerCase();
  return value === 'image' || value === 'video';
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
  const form = new FormData();
  form.append('file', file);
  const response = await fetch('/file-api/upload', {
    method: 'POST',
    body: form,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail.slice(0, 160) || `HTTP ${response.status}`);
  }
  const payload = (await response.json()) as {
    files?: Array<{ path?: string; filename?: string; mime_type?: string }>;
    errors?: Array<{ error?: string }>;
  };
  const uploaded = payload.files?.find((item) => item.path);
  if (!uploaded?.path) {
    throw new Error(payload.errors?.[0]?.error || 'upload_failed');
  }
  return {
    path: uploaded.path,
    filename: uploaded.filename || file.name,
    mime_type: uploaded.mime_type,
  };
}

export function designerAssetTextUrl(uri: string | null | undefined): string | null {
  const value = (uri || '').trim();
  if (!value || /^https?:\/\//i.test(value)) return null;
  const localPath = fileUriToLocalPath(value);
  if (!localPath) return null;
  return `/file-api/file-content?path=${encodeURIComponent(localPath)}&encoding=auto`;
}
