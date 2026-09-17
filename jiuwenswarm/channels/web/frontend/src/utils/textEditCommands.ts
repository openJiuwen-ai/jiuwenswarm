/** Text-edit helpers for desktop context-menu actions on inputs / contenteditable. */

import {
  DESKTOP_CLIPBOARD_IMAGES_EVENT,
  readClipboardImageFilesFromClipboardApi,
} from '../components/ChatPanel/clipboardImagePaste';
import {
  DESKTOP_LOCAL_FILES_EVENT,
  getClipboardFilePicks,
  type DesktopLocalFilesEventDetail,
} from '../features/workspace/localFilePicker';

const NON_TEXT_INPUT_TYPES = new Set([
  'button',
  'checkbox',
  'color',
  'file',
  'hidden',
  'image',
  'radio',
  'range',
  'reset',
  'submit',
]);

/** WHATWG: selectionStart/End apply only to these input types (+ textarea). */
const SELECTION_INPUT_TYPES = new Set(['text', 'search', 'tel', 'url', 'password']);

export type TextEditTarget = HTMLInputElement | HTMLTextAreaElement | HTMLElement;

export function isTextInputElement(el: Element): el is HTMLInputElement | HTMLTextAreaElement {
  if (el instanceof HTMLTextAreaElement) return true;
  if (!(el instanceof HTMLInputElement)) return false;
  const type = (el.type || 'text').toLowerCase();
  return !NON_TEXT_INPUT_TYPES.has(type);
}

export function isContentEditableElement(el: Element): el is HTMLElement {
  if (!(el instanceof HTMLElement)) return false;
  const value = el.getAttribute('contenteditable');
  return value === 'true' || value === '';
}

export function findTextEditTarget(node: EventTarget | null): TextEditTarget | null {
  if (!(node instanceof Node)) return null;
  const start = node instanceof Element ? node : node.parentElement;
  if (!start) return null;
  const candidate = start.closest('input, textarea, [contenteditable="true"], [contenteditable=""]');
  if (!(candidate instanceof Element)) return null;
  if (isTextInputElement(candidate)) {
    if (candidate.disabled) return null;
    return candidate;
  }
  if (isContentEditableElement(candidate)) {
    if (candidate.getAttribute('aria-disabled') === 'true') return null;
    return candidate;
  }
  return null;
}

export function isPasswordField(target: TextEditTarget): boolean {
  return target instanceof HTMLInputElement && target.type.toLowerCase() === 'password';
}

export function isEditableTarget(target: TextEditTarget): boolean {
  if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) {
    return !target.disabled && !target.readOnly;
  }
  if (target.getAttribute('aria-disabled') === 'true') return false;
  if (target.isContentEditable) return true;
  return isContentEditableElement(target);
}

export function isChatComposerTarget(target: TextEditTarget): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.getAttribute('data-testid') === 'chat-panel-input' ||
    target.classList.contains('chat-input-editor') ||
    Boolean(target.closest('.chat-input-editor, [data-testid="chat-panel-input"]'))
  );
}

export function supportsTextSelection(el: HTMLInputElement | HTMLTextAreaElement): boolean {
  if (el instanceof HTMLTextAreaElement) return true;
  const type = (el.type || 'text').toLowerCase();
  return SELECTION_INPUT_TYPES.has(type);
}

/**
 * Resolve the replace range for cut/paste.
 * Types without selection APIs (e.g. number) expose selectionStart/End as null in Chromium;
 * treat the full value as the range so paste replaces instead of prefixing at 0.
 */
export function getInputSelectionBounds(el: HTMLInputElement | HTMLTextAreaElement): {
  start: number;
  end: number;
  selectionApi: boolean;
} {
  if (!supportsTextSelection(el)) {
    return { start: 0, end: el.value.length, selectionApi: false };
  }
  const start = el.selectionStart;
  const end = el.selectionEnd;
  if (start == null || end == null) {
    return { start: 0, end: el.value.length, selectionApi: false };
  }
  return { start, end, selectionApi: true };
}

export function getSelectedText(target: TextEditTarget): string {
  if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) {
    const { start, end } = getInputSelectionBounds(target);
    if (end <= start) return '';
    return target.value.slice(start, end);
  }
  const selection = target.ownerDocument.getSelection();
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return '';
  const range = selection.getRangeAt(0);
  if (!target.contains(range.commonAncestorContainer)) return '';
  return selection.toString();
}

function getMaxLengthBudget(
  el: HTMLInputElement | HTMLTextAreaElement,
  start: number,
  end: number,
): number | null {
  const max = el.maxLength;
  // HTML default when unset is -1 (no limit).
  if (max == null || max < 0) return null;
  return max - (el.value.length - (end - start));
}

/** Truncate paste text so the result respects maxLength (native paste semantics). */
export function clampPasteText(
  el: HTMLInputElement | HTMLTextAreaElement,
  start: number,
  end: number,
  replacement: string,
): string {
  const budget = getMaxLengthBudget(el, start, end);
  if (budget == null) return replacement;
  if (budget <= 0) return '';
  return replacement.slice(0, budget);
}

function setNativeInputValue(el: HTMLInputElement | HTMLTextAreaElement, value: string): void {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
  descriptor?.set?.call(el, value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
}

function tryExecCommand(command: string, value?: string): boolean {
  try {
    if (value === undefined) return Boolean(document.execCommand(command));
    return Boolean(document.execCommand(command, false, value));
  } catch {
    return false;
  }
}

/**
 * Prefer insertText so Chromium records an undo entry and enforces maxLength.
 * Fall back to a maxLength-aware manual replace when execCommand is unavailable.
 */
function replaceInputSelection(el: HTMLInputElement | HTMLTextAreaElement, replacement: string): void {
  el.focus();

  // Keep the browser's real selection when possible (incl. type=number visual selection).
  if (tryExecCommand('insertText', replacement)) return;

  const { start, end, selectionApi } = getInputSelectionBounds(el);
  // Types without a selection API: replace the whole value (select-all semantics).
  const from = selectionApi ? start : 0;
  const to = selectionApi ? end : el.value.length;
  const text = clampPasteText(el, from, to, replacement);
  const next = el.value.slice(0, from) + text + el.value.slice(to);
  setNativeInputValue(el, next);
  try {
    el.setSelectionRange(from + text.length, from + text.length);
  } catch {
    // Some input types reject setSelectionRange.
  }
}

function deleteInputSelection(el: HTMLInputElement | HTMLTextAreaElement): void {
  el.focus();
  if (tryExecCommand('delete')) return;
  const { start, end, selectionApi } = getInputSelectionBounds(el);
  const from = selectionApi ? start : 0;
  const to = selectionApi ? end : el.value.length;
  setNativeInputValue(el, el.value.slice(0, from) + el.value.slice(to));
  try {
    el.setSelectionRange(from, from);
  } catch {
    // Some input types reject setSelectionRange.
  }
}

async function writeClipboardText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fall through to execCommand.
  }
  try {
    return document.execCommand('copy');
  } catch {
    return false;
  }
}

async function readClipboardText(): Promise<string> {
  try {
    if (navigator.clipboard?.readText) {
      return await navigator.clipboard.readText();
    }
  } catch {
    // Ignore and try empty / legacy path.
  }
  return '';
}

function insertIntoContentEditable(target: HTMLElement, text: string): void {
  target.focus();
  try {
    if (document.execCommand('insertText', false, text)) return;
  } catch {
    // Fall through.
  }
  const selection = target.ownerDocument.getSelection();
  if (!selection || selection.rangeCount === 0) {
    target.append(document.createTextNode(text));
    target.dispatchEvent(new Event('input', { bubbles: true }));
    return;
  }
  const range = selection.getRangeAt(0);
  range.deleteContents();
  const node = document.createTextNode(text);
  range.insertNode(node);
  range.setStartAfter(node);
  range.collapse(true);
  selection.removeAllRanges();
  selection.addRange(range);
  target.dispatchEvent(new Event('input', { bubbles: true }));
}

function dispatchDesktopClipboardFiles(picks: Awaited<ReturnType<typeof getClipboardFilePicks>>): void {
  if (!picks.length || typeof window === 'undefined') return;
  const detail: DesktopLocalFilesEventDetail = {
    source: 'paste',
    files: picks,
    trusted: true,
  };
  window.dispatchEvent(new CustomEvent(DESKTOP_LOCAL_FILES_EVENT, { detail }));
}

function dispatchDesktopClipboardImages(files: File[]): void {
  if (!files.length || typeof window === 'undefined') return;
  window.dispatchEvent(
    new CustomEvent(DESKTOP_CLIPBOARD_IMAGES_EVENT, {
      detail: { files },
    }),
  );
}

export type TextEditAction = 'cut' | 'copy' | 'paste' | 'selectAll';

export type TextEditCapabilities = {
  canCut: boolean;
  canCopy: boolean;
  canPaste: boolean;
  canSelectAll: boolean;
};

export function getTextEditCapabilities(target: TextEditTarget): TextEditCapabilities {
  const editable = isEditableTarget(target);
  const password = isPasswordField(target);
  const hasSelection = getSelectedText(target).length > 0;
  return {
    canCut: editable && hasSelection && !password,
    canCopy: hasSelection && !password,
    canPaste: editable,
    canSelectAll: true,
  };
}

export async function runTextEditAction(target: TextEditTarget, action: TextEditAction): Promise<void> {
  target.focus();
  const caps = getTextEditCapabilities(target);

  if (action === 'selectAll') {
    if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) {
      target.select();
      return;
    }
    const selection = target.ownerDocument.getSelection();
    if (!selection) return;
    const range = target.ownerDocument.createRange();
    range.selectNodeContents(target);
    selection.removeAllRanges();
    selection.addRange(range);
    return;
  }

  if (action === 'copy') {
    if (!caps.canCopy) return;
    const text = getSelectedText(target);
    if (!text) return;
    await writeClipboardText(text);
    return;
  }

  if (action === 'cut') {
    if (!caps.canCut) return;
    const text = getSelectedText(target);
    if (!text) return;
    const ok = await writeClipboardText(text);
    if (!ok) return;
    if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) {
      deleteInputSelection(target);
      return;
    }
    if (!tryExecCommand('delete')) {
      const selection = target.ownerDocument.getSelection();
      if (selection && selection.rangeCount > 0) {
        selection.getRangeAt(0).deleteContents();
        target.dispatchEvent(new Event('input', { bubbles: true }));
      }
    }
    return;
  }

  if (action === 'paste') {
    if (!caps.canPaste) return;

    const composer = isChatComposerTarget(target);
    if (composer) {
      // Native bridge first: Explorer/Finder files + screenshot bitmaps.
      // Avoid racing navigator.clipboard.read() against Win32 OpenClipboard.
      const clipboardPicks = await getClipboardFilePicks();
      if (clipboardPicks.length) {
        dispatchDesktopClipboardFiles(clipboardPicks);
        return;
      }

      const imageFiles = await readClipboardImageFilesFromClipboardApi();
      if (imageFiles.length) {
        dispatchDesktopClipboardImages(imageFiles);
        return;
      }
    }

    const text = await readClipboardText();
    if (!text) return;
    if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) {
      replaceInputSelection(target, text);
      return;
    }
    insertIntoContentEditable(target, text);
  }
}
