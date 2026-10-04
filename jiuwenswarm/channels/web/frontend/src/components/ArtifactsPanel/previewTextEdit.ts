export type TextWrapStyle = 'bold' | 'italic' | 'underline' | 'strike' | 'code' | 'link';

const WRAP: Record<TextWrapStyle, { left: string; right: string }> = {
  bold: { left: '**', right: '**' },
  italic: { left: '_', right: '_' },
  underline: { left: '<u>', right: '</u>' },
  strike: { left: '~~', right: '~~' },
  code: { left: '`', right: '`' },
  link: { left: '[', right: '](url)' },
};

export interface TextWrapResult {
  value: string;
  selectionStart: number;
  selectionEnd: number;
}

export function wrapTextSelection(
  value: string,
  start: number,
  end: number,
  style: TextWrapStyle,
  linkUrl = 'url',
): TextWrapResult {
  const s = Math.max(0, Math.min(start, value.length));
  const e = Math.max(s, Math.min(end, value.length));
  let left = WRAP[style].left;
  let right = WRAP[style].right;
  if (style === 'link') {
    const safe = (linkUrl || 'url').trim() || 'url';
    left = '[';
    right = `](${safe})`;
  }
  const selected = value.slice(s, e);
  const next = value.slice(0, s) + left + selected + right + value.slice(e);
  const selectionStart = s + left.length;
  const selectionEnd = selectionStart + selected.length;
  return { value: next, selectionStart, selectionEnd };
}

export function wrapFirstOccurrence(
  value: string,
  selected: string,
  style: TextWrapStyle,
  linkUrl = 'url',
): TextWrapResult | null {
  const needle = selected.trim();
  if (!needle) return null;
  const idx = value.indexOf(needle);
  if (idx < 0) return null;
  return wrapTextSelection(value, idx, idx + needle.length, style, linkUrl);
}

/** Strip script / on* handlers for safer HTML preview srcdoc. */
export function stripHtmlScripts(html: string): string {
  let out = html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '');
  out = out.replace(/\son[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, '');
  out = out.replace(/javascript:/gi, '');
  return out;
}
