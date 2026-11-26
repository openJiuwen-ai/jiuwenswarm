/** Agent-facing hint block listing this turn's uploaded documents. */
export const UPLOAD_DOCUMENT_BLOCK_HEADER = '【上传文档】';

export interface UploadDocumentHint {
  filename: string;
  path?: string;
  /**
   * Preferred absolute path for `@path` refs. When set and different from
   * `path`, this wins (legacy sidecar cases); otherwise equals `path`.
   */
  originalPath?: string;
}

/** Quote `@path` when the path contains whitespace. */
export function formatAtPath(path: string): string {
  return /\s/.test(path) ? `@"${path}"` : `@${path}`;
}

/** Strip agent document-hint blocks from user-visible bubble text. */
export function stripUploadDocumentBlocks(content: string): string {
  if (!content || !content.includes('【上传文档')) {
    return content;
  }
  // Document hints are always appended after the user query — drop from the
  // marker through the end so titles / bubbles only keep the query text.
  return content
    // Generated blocks put the marker on its own line. Requiring that shape
    // keeps ordinary query text containing the marker intact.
    .replace(/(?:^|\r?\n)【上传文档】(?:\r?\n|$)[\s\S]*$/u, '')
    .replace(/(?:^|\r?\n)【上传文档[:：][^\r\n]*(?:\r?\n|$)[\s\S]*$/u, '')
    // `_auto_title` collapses newlines to spaces before persist; also cover
    // already-written bad titles: `【上传文档】 @...` or `query 【上传文档】 @...`.
    .replace(/(?:^|\s)【上传文档】\s+@[\s\S]*$/u, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

/** Session / header title: strip document hints and collapse whitespace. */
export function toDisplaySessionTitle(title: string): string {
  return stripUploadDocumentBlocks(title).replace(/\s+/g, ' ').trim();
}

const AT_PATH_RE = /@(?:"([^"]+)"|(\S+))/;

/** Basename of the first `@path` / `@"path"` in an upload-document block. */
function basenameFromUploadAtPath(content: string): string {
  const match = AT_PATH_RE.exec(content);
  if (!match) return '';
  const path = (match[1] || match[2] || '').trim();
  if (!path) return '';
  const parts = path.split(/[\\/]/);
  const name = parts[parts.length - 1] || '';
  return name && name !== '.' && name !== '..' ? name : '';
}

export interface QueueContentPreviewOptions {
  /** Queued task attachments; first document/image filename wins when text is empty. */
  mediaItems?: Array<{ filename?: string } | null | undefined>;
  /** Localized label when the turn is upload-only and no filename is available. */
  fallbackLabel?: string;
}

/**
 * Message-queue row preview: never show the agent-facing `【上传文档】` protocol
 * block. Prefer stripped user text, then a media filename, then `@path`
 * basename, then an optional localized fallback.
 */
export function toQueueContentPreview(
  content: string,
  options?: QueueContentPreviewOptions,
): string {
  const stripped = stripUploadDocumentBlocks(content || '').replace(/\s+/g, ' ').trim();
  if (stripped) return stripped;

  const fromMedia = (options?.mediaItems ?? [])
    .map((item) => (item && typeof item.filename === 'string' ? item.filename.trim() : ''))
    .find(Boolean);
  if (fromMedia) return fromMedia;

  const fromPath = basenameFromUploadAtPath(content || '');
  if (fromPath) return fromPath;

  return (options?.fallbackLabel || '').trim();
}

/**
 * Append the agent-facing document hint block to `text`.
 *
 * Format:
 *   【上传文档】
 *   @/abs/path
 *
 * Any block already present is stripped first, so calling this again with
 * freshly persisted records replaces incomplete lines with real `@path` refs.
 * Entries without a path are skipped.
 */
export function withUploadDocumentBlock(text: string, docs: UploadDocumentHint[]): string {
  const base = stripUploadDocumentBlocks(text);
  if (!docs.length) {
    return base;
  }
  const lines = docs
    .map((doc) => {
      const path = (doc.originalPath || doc.path || '').trim();
      return path ? formatAtPath(path) : '';
    })
    .filter(Boolean);
  if (!lines.length) {
    return base;
  }
  return [base, UPLOAD_DOCUMENT_BLOCK_HEADER, ...lines].filter(Boolean).join('\n');
}

function readString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined;
}

/** Extract document hints from persisted `media_items` returned by document.persist. */
export function toUploadDocumentHints(mediaItems: unknown): UploadDocumentHint[] {
  if (!Array.isArray(mediaItems)) {
    return [];
  }
  const hints: UploadDocumentHint[] = [];
  for (const item of mediaItems) {
    if (!item || typeof item !== 'object') continue;
    const record = item as Record<string, unknown>;
    if (record.type !== 'document') continue;
    const path = readString(record.path);
    const originalPath = readString(record.original_path) || path;
    const filename = readString(record.filename) || (path ? path.split(/[\\/]/).pop() : undefined);
    if (!filename) continue;
    hints.push({ filename, path, originalPath });
  }
  return hints;
}
