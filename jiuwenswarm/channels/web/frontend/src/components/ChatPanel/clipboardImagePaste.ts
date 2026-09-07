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

function isImageFile(file: File): boolean {
  if (ACCEPTED_IMAGE_TYPES.has(file.type)) return true;
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
};

/**
 * Prefer clipboardData.items only — Chromium often mirrors the same screenshot in files.
 * Do not dedupe by name/size/MIME: distinct images can share those metadata fields.
 */
export function getClipboardImageFiles(clipboardData: ClipboardDataLike | null | undefined): File[] {
  if (!clipboardData) return [];
  const files: File[] = [];
  for (const item of Array.from(clipboardData.items || [])) {
    if (item.kind !== 'file') continue;
    const file = item.getAsFile();
    if (!file || !isImageFile(file)) continue;
    files.push(ensureClipboardImageFilename(file));
  }
  return files;
}
