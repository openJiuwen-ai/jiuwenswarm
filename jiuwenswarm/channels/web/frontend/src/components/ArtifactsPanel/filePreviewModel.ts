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

type DecoderOptions = { fatal?: boolean };

const textDecoders = new Map<string, TextDecoder | null>();

/**
 * Build (and cache) a TextDecoder without ever throwing at module scope.
 *
 * `gbk` / `utf-16be` require full ICU support. Runtimes built with small-icu
 * reject the label with a RangeError, and a module-scope constructor would then
 * break evaluation of this whole module — taking docx/pdf/image previews down
 * with it. Returning `null` keeps the caller in control of the degradation.
 */
function createTextDecoder(label: string, options?: DecoderOptions): TextDecoder | null {
  const cacheKey = options?.fatal ? `${label}|fatal` : label;
  if (!textDecoders.has(cacheKey)) {
    try {
      textDecoders.set(cacheKey, new TextDecoder(label, options));
    } catch {
      textDecoders.set(cacheKey, null);
    }
  }
  return textDecoders.get(cacheKey) ?? null;
}

/**
 * Decode artifact text: UTF-16 BOM, else strict UTF-8, else GBK.
 *
 * The inline download URL declares no charset, so `response.text()` would turn
 * legacy GBK artifacts into U+FFFD; GBK is a never-throwing last resort.
 *
 * Decoders are resolved lazily per call so an unsupported label degrades to the
 * next candidate instead of failing the import. Final fallback is a lenient
 * UTF-8 decode, which mirrors the pre-existing `response.text()` behaviour.
 */
export function decodeTextBytes(bytes: ArrayBuffer | Uint8Array): string {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  if (view.length >= 2) {
    if (view[0] === 0xff && view[1] === 0xfe) {
      const utf16le = createTextDecoder('utf-16le');
      if (utf16le) return utf16le.decode(view);
    }
    if (view[0] === 0xfe && view[1] === 0xff) {
      const utf16be = createTextDecoder('utf-16be');
      if (utf16be) return utf16be.decode(view);
    }
  }
  const strictUtf8 = createTextDecoder('utf-8', { fatal: true });
  if (strictUtf8) {
    try {
      return strictUtf8.decode(view);
    } catch {
      // not valid UTF-8: fall through to the legacy decoders below
    }
  }
  const gbk = createTextDecoder('gbk');
  if (gbk) return gbk.decode(view);
  const lenientUtf8 = createTextDecoder('utf-8');
  return lenientUtf8 ? lenientUtf8.decode(view) : '';
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

/**
 * Pretty-printed JSON / JSONL as shown in the artifact preview pane.
 * Returns null when the source is not valid JSON (the preview shows an error).
 */
export function formatArtifactPreviewText(kind: PreviewKind, content: string): string | null {
  try {
    if (kind === 'json') {
      return JSON.stringify(JSON.parse(content), null, 2);
    }
    if (kind === 'jsonl') {
      const value = content
        .split(/\r?\n/)
        .filter(Boolean)
        .map((line) => JSON.parse(line));
      return JSON.stringify(value, null, 2);
    }
  } catch {
    return null;
  }
  if (kind === 'markdown' || kind === 'text' || kind === 'code') return content;
  return null;
}

/**
 * Clipboard text for the artifact-dialog Copy button.
 * JSON / JSONL copy the pretty-printed preview; invalid JSON keeps the source.
 * Non-text files copy the path (same as today).
 */
export function getArtifactCopyText(kind: PreviewKind, content: string, fallbackPath: string): string {
  if (kind === 'json' || kind === 'jsonl') {
    return formatArtifactPreviewText(kind, content) ?? content;
  }
  if (kind === 'markdown' || kind === 'text' || kind === 'code') return content;
  return fallbackPath;
}
