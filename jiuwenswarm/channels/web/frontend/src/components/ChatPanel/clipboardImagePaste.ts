const ACCEPTED_IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
const IMAGE_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', '.webp', '.gif']);

/** i18n key shown when paste/select is blocked while image input is disabled. */
export const IMAGE_INPUT_DISABLED_ALERT_KEY = 'chat.addFileDisabled';

export type ImageInputDisabledState = {
  isListening: boolean;
  isCompactRunning: boolean;
  isInterruptible: boolean;
  isTeamMode: boolean;
  isAgentMode: boolean;
};

/**
 * Keep in sync with handleSubmit media gating:
 * Agent mode may queue follow-ups with attachments while interruptible.
 */
export function isImageInputDisabled(state: ImageInputDisabledState): boolean {
  const { isListening, isCompactRunning, isInterruptible, isTeamMode, isAgentMode } = state;
  return isListening || isCompactRunning || (isInterruptible && !isTeamMode && !isAgentMode);
}

/**
 * Desktop/browser paste with image files while input is disabled should surface
 * the same localized alert (never silently swallow).
 */
export function shouldAlertImagePasteDisabled(
  imageInputDisabled: boolean,
  hasClipboardImages: boolean,
): boolean {
  return imageInputDisabled && hasClipboardImages;
}

function getFileExtension(filename: string): string {
  const idx = filename.lastIndexOf('.');
  if (idx < 0) return '';
  return filename.slice(idx).toLowerCase();
}

const IMAGE_MIME_BY_EXTENSION: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
};

/**
 * Keep a MIME that mimetypes (or the browser) already resolved.
 * Fill image/webp only when that result is missing or octet-stream.
 */
export function resolveImageMimeType(filename: string, mimeType?: string): string {
  const normalized = (mimeType || '').toLowerCase().split(';')[0].trim();
  if (normalized && normalized !== 'application/octet-stream') {
    return normalized;
  }
  return IMAGE_MIME_BY_EXTENSION[getFileExtension(filename)] || normalized || 'application/octet-stream';
}

/** Desktop picks have no browser File; retry still works from base64 or a local path. */
export function canRetryAttachmentDraft(draft: {
  file?: unknown;
  base64Data?: string;
  localPath?: string;
}): boolean {
  if (draft.file) return true;
  if (typeof draft.base64Data === 'string' && draft.base64Data.length > 0) return true;
  return typeof draft.localPath === 'string' && draft.localPath.trim().length > 0;
}

function isImageFile(file: File): boolean {
  const type = file.type.toLowerCase();
  if (ACCEPTED_IMAGE_TYPES.has(type)) return true;
  if (type && type !== 'application/octet-stream') return false;
  return IMAGE_EXTENSIONS.has(getFileExtension(file.name || ''));
}

export function ensureClipboardImageFilename(file: File): File {
  if (file.name && getFileExtension(file.name)) return file;
  const ext =
    file.type === 'image/jpeg' ? '.jpg'
    : file.type === 'image/webp' ? '.webp'
    : file.type === 'image/gif' ? '.gif'
    : '.png';
  const type = ACCEPTED_IMAGE_TYPES.has(file.type) ? file.type : 'image/png';
  return new File([file], `clipboard-image${ext}`, { type, lastModified: file.lastModified });
}

export type ClipboardFileItemLike = {
  kind: string;
  getAsFile: () => File | null;
};

export type ClipboardDataLike = {
  items?: ArrayLike<ClipboardFileItemLike> | null;
  files?: ArrayLike<File> | null;
};

/**
 * Prefer clipboardData.items — Chromium often mirrors the same screenshot in files.
 * Fall back to files only when there are no file items.
 * Do not dedupe by name/size/MIME: distinct images can share those metadata fields.
 */
export function inspectClipboardImageFiles(clipboardData: ClipboardDataLike | null | undefined): {
  files: File[];
  hasUnsupportedFiles: boolean;
} {
  const files: File[] = [];
  let hasUnsupportedFiles = false;
  const fileItems = Array.from(clipboardData?.items || []).filter((item) => item.kind === 'file');
  const clipboardFiles = fileItems.length
    ? fileItems.map((item) => item.getAsFile())
    : Array.from(clipboardData?.files || []);
  for (const file of clipboardFiles) {
    if (!file || !isImageFile(file)) {
      hasUnsupportedFiles = true;
      continue;
    }
    files.push(ensureClipboardImageFilename(file));
  }
  return { files, hasUnsupportedFiles };
}

export function getClipboardImageFiles(clipboardData: ClipboardDataLike | null | undefined): File[] {
  return inspectClipboardImageFiles(clipboardData).files;
}

/** Dispatched by desktop context-menu paste when Clipboard API yields image blobs. */
export const DESKTOP_CLIPBOARD_IMAGES_EVENT = 'jiuwen-desktop-clipboard-images';

export type DesktopClipboardImagesEventDetail = {
  files?: File[];
};

/**
 * Read image blobs via Clipboard API (screenshots / copied bitmaps).
 * Used when there is no paste ClipboardEvent (e.g. custom context-menu Paste).
 */
export async function readClipboardImageFilesFromClipboardApi(
  clipboard: Clipboard | null | undefined = typeof navigator !== 'undefined' ? navigator.clipboard : undefined,
): Promise<File[]> {
  if (!clipboard || typeof clipboard.read !== 'function') return [];
  try {
    const items = await clipboard.read();
    const files: File[] = [];
    for (const item of items) {
      const imageType = item.types.find((type) => ACCEPTED_IMAGE_TYPES.has(type.toLowerCase()));
      if (!imageType) continue;
      const blob = await item.getType(imageType);
      const type = ACCEPTED_IMAGE_TYPES.has(blob.type) ? blob.type : imageType.toLowerCase();
      files.push(ensureClipboardImageFilename(new File([blob], '', { type })));
    }
    return files;
  } catch {
    return [];
  }
}
