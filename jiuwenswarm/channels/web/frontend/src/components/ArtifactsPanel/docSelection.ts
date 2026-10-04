export type SelectionKind = 'doc' | 'slide' | 'sheet' | 'html' | 'markdown' | 'text';

export interface DocSelection {
  kind: SelectionKind;
  source: string;
  path: string;
  range: string;
  preview: string;
}

export const SELECTION_PREVIEW_CAP = 4000;

export function capSelectionPreview(text: string, max = SELECTION_PREVIEW_CAP): string {
  const t = (text || '').trim();
  if (t.length <= max) return t;
  return `${t.slice(0, max)}…`;
}

export function needSwitchConfirm(current: DocSelection | null, next: DocSelection): boolean {
  if (!current) return false;
  const id = (s: DocSelection) => `${s.kind}::${s.path || s.source}`;
  return id(current) !== id(next);
}
