import { resolveApiUrl } from '../../utils/env';
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

/** Plain-text previews stay on the main thread, so stop once this many bytes are buffered. */
export const MAX_TEXT_PREVIEW_BYTES = 1024 * 1024;

export type LimitedTextPreview = {
  text: string;
  truncated: boolean;
};

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
  const url = new URL(resolveApiUrl(downloadUrl), origin);
  if (url.protocol === 'blob:' || url.protocol === 'data:') return url.href;
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

function utf8PrefixEnd(bytes: Uint8Array): number {
  const length = bytes.length;
  if (length === 0) return 0;
  let index = length - 1;
  let continuationCount = 0;
  while (index >= 0 && continuationCount < 3 && (bytes[index] & 0xc0) === 0x80) {
    index -= 1;
    continuationCount += 1;
  }
  if (index < 0) return length;
  const lead = bytes[index];
  let expected = 1;
  if (lead <= 0x7f) expected = 1;
  else if ((lead & 0xe0) === 0xc0) expected = 2;
  else if ((lead & 0xf0) === 0xe0) expected = 3;
  else if ((lead & 0xf8) === 0xf0) expected = 4;
  else return length;
  return length - index < expected ? index : length;
}

function boundedPreviewBytes(bytes: Uint8Array): Uint8Array {
  if (bytes.length >= 2) {
    const utf16 = (bytes[0] === 0xff && bytes[1] === 0xfe) || (bytes[0] === 0xfe && bytes[1] === 0xff);
    if (utf16) return bytes.subarray(0, bytes.length - (bytes.length % 2));
  }
  const utf8End = utf8PrefixEnd(bytes);
  const strictUtf8 = createTextDecoder('utf-8', { fatal: true });
  if (strictUtf8) {
    try {
      strictUtf8.decode(bytes.subarray(0, utf8End));
      return bytes.subarray(0, utf8End);
    } catch {
      // Not UTF-8. Keep a complete GBK pair when the cap splits a double-byte character.
    }
  }
  const last = bytes[bytes.length - 1];
  if (last >= 0x81 && last <= 0xfe) return bytes.subarray(0, bytes.length - 1);
  return bytes;
}

function previewTextFromBytes(bytes: Uint8Array, truncated: boolean): LimitedTextPreview {
  const slice = truncated ? boundedPreviewBytes(bytes) : bytes;
  return { text: decodeTextBytes(slice), truncated };
}

function clipTextBytes(bytes: Uint8Array, maxBytes: number): LimitedTextPreview {
  const truncated = bytes.byteLength > maxBytes;
  const slice = truncated ? bytes.subarray(0, maxBytes) : bytes;
  return previewTextFromBytes(slice, truncated);
}

export async function readLimitedTextPreview(
  response: Response,
  maxBytes: number = MAX_TEXT_PREVIEW_BYTES,
): Promise<LimitedTextPreview> {
  if (!Number.isInteger(maxBytes) || maxBytes < 1) throw new Error('invalid_text_preview_limit');
  const body = response.body;
  if (!body) return clipTextBytes(new Uint8Array(await response.arrayBuffer()), maxBytes);

  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  let truncated = false;
  try {
    while (size < maxBytes) {
      const { done, value } = await reader.read();
      if (done) break;
      const room = maxBytes - size;
      if (value.byteLength > room) {
        if (room > 0) chunks.push(value.subarray(0, room));
        size = maxBytes;
        truncated = true;
        await reader.cancel();
        break;
      }
      chunks.push(value);
      size += value.byteLength;
      if (size === maxBytes) {
        const next = await reader.read();
        if (!next.done) {
          truncated = true;
          await reader.cancel();
        }
        break;
      }
    }
  } catch (error) {
    if (size === 0) throw error;
    truncated = true;
  }

  const bytes = new Uint8Array(size);
  let offset = 0;
  chunks.forEach(chunk => {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  });
  return previewTextFromBytes(bytes, truncated);
}
