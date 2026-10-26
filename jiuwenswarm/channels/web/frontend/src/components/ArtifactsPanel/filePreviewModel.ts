import { fileExtension, isCodeLanguageExtension } from './codeLanguageExtensions';

type PreviewFile = {
  name: string;
  mimeType?: string;
};

type PreviewResource = {
  downloadUrl?: string;
  path?: string;
};

type TextKind = 'markdown' | 'text' | 'code' | 'json' | 'jsonl';
export type PreviewKind = TextKind | 'html' | 'image' | 'pdf' | 'docx' | 'spreadsheet' | 'presentation' | 'unsupported';

const TEXT_EXTENSIONS = new Set(['conf', 'csv', 'ini', 'log', 'text', 'txt']);
const IMAGE_EXTENSIONS = new Set(['avif', 'bmp', 'gif', 'ico', 'jfif', 'jpeg', 'jpg', 'png', 'svg', 'webp']);

import { resolveApiUrl } from '../../utils/env';
function inlineDownloadUrl(downloadUrl: string, origin: string): string {
  // 文件下载/预览地址统一加接口前缀，再附加 inline=1 让浏览器内联预览而非下载。
  const resolved = resolveApiUrl(downloadUrl);
  const url = new URL(resolved, origin);
  url.searchParams.set('inline', '1');
  return url.pathname + url.search;
}

export function artifactDownloadUrl(resource: PreviewResource): string | null {
  if (resource.downloadUrl) return resolveApiUrl(resource.downloadUrl);
  return resource.path ? resolveApiUrl(`/file-api/raw-file?path=${encodeURIComponent(resource.path)}`) : null;
}

export function artifactBinaryPreviewUrl(resource: PreviewResource, origin: string): string | null {
  if (resource.downloadUrl) return inlineDownloadUrl(resource.downloadUrl, origin);
  return resource.path ? resolveApiUrl(`/file-api/raw-file?path=${encodeURIComponent(resource.path)}`) : null;
}

export function artifactTextPreviewUrl(resource: PreviewResource, origin: string): string | null {
  if (resource.downloadUrl) return inlineDownloadUrl(resource.downloadUrl, origin);
  return resource.path ? resolveApiUrl(`/file-api/file-content?path=${encodeURIComponent(resource.path)}&encoding=auto`) : null;
}

export function previewKind(file: PreviewFile): PreviewKind {
  const mime = (file.mimeType ?? '').toLowerCase();
  const ext = fileExtension(file.name);
  if (mime === 'text/markdown' || ['md', 'markdown'].includes(ext)) return 'markdown';
  if (mime === 'application/json' || mime === 'text/json' || ext === 'json') return 'json';
  if (mime === 'application/x-ndjson' || mime === 'application/jsonl' || ext === 'jsonl') return 'jsonl';
  if (mime === 'text/html' || mime === 'application/xhtml+xml' || ext === 'html' || ext === 'htm') return 'html';
  if (mime === 'application/javascript' || mime === 'text/javascript' || mime === 'application/typescript' || isCodeLanguageExtension(ext)) return 'code';
  if (mime.startsWith('image/') || IMAGE_EXTENSIONS.has(ext)) return 'image';
  if (mime === 'application/pdf' || ext === 'pdf') return 'pdf';
  if (mime === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' || ext === 'docx') return 'docx';
  if (
    mime === 'application/vnd.openxmlformats-officedocument.presentationml.presentation' ||
    mime === 'application/vnd.ms-powerpoint.presentation.macroenabled.12' ||
    ext === 'pptx' ||
    ext === 'pptm'
  ) {
    return 'presentation';
  }
  if (
    mime === 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' ||
    mime === 'application/vnd.ms-excel.sheet.macroenabled.12' ||
    ext === 'xlsx' ||
    ext === 'xlsm'
  ) {
    return 'spreadsheet';
  }
  if (mime.startsWith('text/') || TEXT_EXTENSIONS.has(ext)) return 'text';
  return 'unsupported';
}
