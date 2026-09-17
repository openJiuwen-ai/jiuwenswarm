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
    .replace(/【上传文档】[\s\S]*$/u, '')
    .replace(/【上传文档[:：][\s\S]*$/u, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

/** Session / header title: strip document hints and collapse whitespace. */
export function toDisplaySessionTitle(title: string): string {
  return stripUploadDocumentBlocks(title).replace(/\s+/g, ' ').trim();
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
