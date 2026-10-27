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
export type PreviewKind = TextKind | 'html' | 'image' | 'video' | 'pdf' | 'docx' | 'spreadsheet' | 'presentation' | 'unsupported';

const TEXT_EXTENSIONS = new Set(['conf', 'csv', 'ini', 'log', 'text', 'txt']);
const IMAGE_MIME_TYPES = new Set([
  'image/apng',
  'image/avif',
  'image/bmp',
  'image/gif',
  'image/jpeg',
  'image/png',
  'image/svg+xml',
  'image/vnd.microsoft.icon',
  'image/webp',
  'image/x-icon',
]);
const IMAGE_EXTENSIONS = new Set([
  'apng',
  'avif',
  'bmp',
  'cur',
  'gif',
  'ico',
  'jpe',
  'jfif',
  'jpeg',
  'jpg',
  'pjp',
  'pjpeg',
  'png',
  'svg',
  'webp',
]);

/**
 * Sandbox policy for the HTML artifact preview iframe.
 *
 * Artifact HTML is routinely script-driven: self-contained reports (for example
 * generated briefings) ship an empty body placeholder and render every card
 * from inline JavaScript. An empty ``sandbox`` attribute blocks scripts, so such
 * artifacts load successfully yet paint a blank panel.
 *
 * ``allow-scripts`` lets that rendering run. ``allow-same-origin`` is
 * deliberately withheld: the frame keeps an opaque origin, so artifact scripts
 * cannot read the host app's cookies/localStorage, and cross-origin reads of
 * first-party ``/file-api`` responses stay blocked by CORS. ``allow-popups``
 * keeps "view original" style links usable; the opened tab inherits this
 * sandbox and also stays on an opaque origin.
 *
 * Do NOT add ``allow-popups-to-escape-sandbox``: artifact HTML is served from
 * the host origin (``/file-api`` returns ``text/html`` with no CSP), so that
 * flag would let artifact scripts open a top-level, unsandboxed, host-origin
 * document (cookies, storage, first-party API access) — equivalent to XSS.
 * Pairing ``allow-scripts`` with ``allow-same-origin`` is forbidden for the
 * same reason.
 */
export const HTML_PREVIEW_SANDBOX = 'allow-scripts allow-popups';

export function inlineDownloadUrl(downloadUrl: string, origin: string): string {
  const url = new URL(downloadUrl, origin);
  if (url.protocol === 'blob:' || url.protocol === 'data:') return url.href;
  url.searchParams.set('inline', '1');
  return url.pathname + url.search;
}

export function artifactDownloadUrl(resource: PreviewResource): string | null {
  if (resource.downloadUrl) return resource.downloadUrl;
  return resource.path ? `/file-api/raw-file?path=${encodeURIComponent(resource.path)}` : null;
}

export function artifactBinaryPreviewUrl(resource: PreviewResource, origin: string): string | null {
  if (resource.downloadUrl) return inlineDownloadUrl(resource.downloadUrl, origin);
  return resource.path ? `/file-api/raw-file?path=${encodeURIComponent(resource.path)}` : null;
}

export function artifactTextPreviewUrl(resource: PreviewResource, origin: string): string | null {
  if (resource.downloadUrl) return inlineDownloadUrl(resource.downloadUrl, origin);
  return resource.path ? `/file-api/file-content?path=${encodeURIComponent(resource.path)}&encoding=auto` : null;
}

export function previewKind(file: PreviewFile): PreviewKind {
  const mime = (file.mimeType ?? '').toLowerCase();
  const ext = fileExtension(file.name);
  if (mime === 'text/markdown' || ['md', 'markdown'].includes(ext)) return 'markdown';
  if (mime === 'application/json' || mime === 'text/json' || ext === 'json') return 'json';
  if (mime === 'application/x-ndjson' || mime === 'application/jsonl' || ext === 'jsonl') return 'jsonl';
  if (mime === 'text/html' || mime === 'application/xhtml+xml' || ext === 'html' || ext === 'htm') return 'html';
  if (mime === 'application/javascript' || mime === 'text/javascript' || mime === 'application/typescript' || isCodeLanguageExtension(ext)) return 'code';
  if (IMAGE_MIME_TYPES.has(mime) || IMAGE_EXTENSIONS.has(ext)) return 'image';
  if (mime.startsWith('video/')) return 'video';
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
