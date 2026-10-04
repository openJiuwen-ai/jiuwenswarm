import { capSelectionPreview, type DocSelection, type SelectionKind } from './docSelection';
import type { PreviewKind } from './filePreviewModel';

export function selectionKindFromPreview(kind: PreviewKind): SelectionKind | null {
  switch (kind) {
    case 'markdown':
      return 'markdown';
    case 'text':
    case 'code':
    case 'json':
    case 'jsonl':
      return 'text';
    case 'html':
      return 'html';
    case 'docx':
      return 'doc';
    case 'spreadsheet':
      return 'sheet';
    case 'presentation':
      return 'slide';
    default:
      return null;
  }
}

export function isPreviewStyleEditable(kind: PreviewKind): boolean {
  return kind === 'markdown' || kind === 'text';
}

export function supportsPreviewSelection(kind: PreviewKind): boolean {
  return selectionKindFromPreview(kind) != null;
}

export function isPreviewLocallyEditable(kind: PreviewKind): boolean {
  return kind === 'markdown' || kind === 'text' || kind === 'code' || kind === 'json' || kind === 'jsonl';
}

export function fileSourceName(path: string, title?: string): string {
  if (title?.trim()) return title.trim();
  const base = path.split(/[?#]/)[0].split('/').pop();
  return base || path || 'file';
}

export function defaultRangeLabel(kind: SelectionKind, extra?: string): string {
  if (extra?.trim()) return extra.trim();
  switch (kind) {
    case 'slide':
      return '当前页';
    case 'sheet':
      return '选区';
    case 'html':
      return 'HTML 选区';
    case 'markdown':
      return 'Markdown 选区';
    case 'text':
      return '文本选区';
    default:
      return '选区';
  }
}

export interface BuildDocSelectionInput {
  kind: PreviewKind;
  path: string;
  title?: string;
  selectedText: string;
  range?: string;
}

export function buildDocSelection(input: BuildDocSelectionInput): DocSelection | null {
  const sk = selectionKindFromPreview(input.kind);
  if (!sk) return null;
  const preview = capSelectionPreview(input.selectedText);
  if (!preview) return null;
  return {
    kind: sk,
    source: fileSourceName(input.path, input.title),
    path: input.path,
    range: defaultRangeLabel(sk, input.range),
    preview,
  };
}

export function composeAiEditPrompt(sel: DocSelection, instruction: string): string {
  const quote = sel.preview;
  const range = sel.range ? ` [${sel.range}]` : '';
  const header = sel.path.trim() ? `@file:${sel.path}${range}` : `文件「${sel.source}」${range}`;
  const body = (instruction || '').trim();
  return `${header}\n> ${quote}${body ? `\n\n${body}` : ''}`;
}

export function readDomSelectionText(sel: Selection | null | undefined): string | null {
  if (!sel || sel.isCollapsed) return null;
  const text = sel.toString();
  const trimmed = text.replace(/\u00a0/g, ' ').trim();
  return trimmed || null;
}

export interface FloatingBarPosition {
  top: number;
  left: number;
}

export interface FloatingBarOpts {
  barHeight?: number;
  barHalfWidth?: number;
  gap?: number;
}

export function floatingBarPosition(
  rangeRect: { top: number; left: number; width: number; bottom: number },
  panelRect: { top: number; left: number; width: number; height: number; bottom: number; right: number },
  opts: FloatingBarOpts = {},
): FloatingBarPosition {
  const barH = opts.barHeight ?? 40;
  const halfW = opts.barHalfWidth ?? 140;
  const gap = opts.gap ?? 8;
  const pad = 8;

  let top = rangeRect.top - barH - gap;
  if (top < panelRect.top + pad) {
    top = rangeRect.bottom + gap;
  }
  const maxTop = panelRect.bottom - barH - pad;
  top = Math.min(Math.max(top, panelRect.top + pad), Math.max(panelRect.top + pad, maxTop));

  let left = rangeRect.left + rangeRect.width / 2;
  const minLeft = panelRect.left + halfW + pad;
  const maxLeft = panelRect.right - halfW - pad;
  if (minLeft <= maxLeft) {
    left = Math.min(Math.max(left, minLeft), maxLeft);
  } else {
    left = panelRect.left + panelRect.width / 2;
  }
  return { top, left };
}
