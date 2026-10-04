export type TextWrapStyle = 'bold' | 'italic' | 'underline' | 'strike' | 'code' | 'link';

export interface TextWrapResult {
  value: string;
  /** Visible inner text after the toggle (for keeping the float selection). */
  innerText: string;
  selectionStart: number;
  selectionEnd: number;
}

type MarkerPair = { style: TextWrapStyle; left: string; right: string };

/** Outer-check order: longer markers first so ** wins over *. */
const PEEK_MARKERS: MarkerPair[] = [
  { style: 'bold', left: '**', right: '**' },
  { style: 'bold', left: '__', right: '__' },
  { style: 'strike', left: '~~', right: '~~' },
  { style: 'underline', left: '<u>', right: '</u>' },
  { style: 'italic', left: '*', right: '*' },
  { style: 'italic', left: '_', right: '_' },
];

const APPLY_MARKERS: Record<Exclude<TextWrapStyle, 'code' | 'link'>, MarkerPair> = {
  bold: { style: 'bold', left: '**', right: '**' },
  italic: { style: 'italic', left: '*', right: '*' },
  underline: { style: 'underline', left: '<u>', right: '</u>' },
  strike: { style: 'strike', left: '~~', right: '~~' },
};

/** Find the start index of the n-th occurrence of needle in value (0-based). */
export function findNthOccurrence(value: string, needle: string, occurrenceIndex: number): number {
  if (!needle || occurrenceIndex < 0) return -1;
  let from = 0;
  for (let i = 0; i <= occurrenceIndex; i += 1) {
    const idx = value.indexOf(needle, from);
    if (idx < 0) return -1;
    if (i === occurrenceIndex) return idx;
    from = idx + needle.length;
  }
  return -1;
}

/**
 * Count how many times `needle` appears in text nodes of `root` before `range` starts.
 * Used to map a DOM selection to the matching source occurrence.
 */
export function countOccurrencesBeforeRange(root: HTMLElement, range: Range, needle: string): number {
  const trimmed = (needle || '').replace(/\u00a0/g, ' ');
  if (!trimmed) return 0;

  const preRange = document.createRange();
  preRange.selectNodeContents(root);
  try {
    preRange.setEnd(range.startContainer, range.startOffset);
  } catch {
    return 0;
  }
  const before = preRange.toString().replace(/\u00a0/g, ' ');
  if (!before) return 0;

  let count = 0;
  let from = 0;
  while (from <= before.length) {
    const idx = before.indexOf(trimmed, from);
    if (idx < 0) break;
    count += 1;
    from = idx + trimmed.length;
  }
  return count;
}

type WrapperLayer = { style: TextWrapStyle; left: string; right: string };

function sanitizeCodeLanguage(language: string): string {
  return (language || '').trim().replace(/[^a-zA-Z0-9_+#.-]/g, '').slice(0, 40);
}

/** Walk outward from [start,end] collecting matched markdown/html wrapper layers. */
export function collectWrapperLayers(
  value: string,
  start: number,
  end: number,
): { layers: WrapperLayer[]; coreStart: number; coreEnd: number } {
  let s = start;
  let e = end;
  const layers: WrapperLayer[] = [];

  let expanded = true;
  while (expanded) {
    expanded = false;

    // Fenced code block (optional language; opening fence at line start)
    const before = value.slice(0, s);
    const after = value.slice(e);
    const openFence = /(?:^|\n)```[^\n]*\n$/.exec(before);
    const closeFence = /^\n```(?:\n|$)/.exec(after);
    if (openFence && closeFence) {
      layers.push({ style: 'code', left: openFence[0], right: closeFence[0] });
      s -= openFence[0].length;
      e += closeFence[0].length;
      expanded = true;
      continue;
    }

    // Link [text](url)
    if (before.endsWith('[')) {
      const linkClose = /^\]\([^)]*\)/.exec(after);
      if (linkClose) {
        layers.push({ style: 'link', left: '[', right: linkClose[0] });
        s -= 1;
        e += linkClose[0].length;
        expanded = true;
        continue;
      }
    }

    // Inline code
    if (s >= 1 && e < value.length && value[s - 1] === '`' && value[e] === '`' && !value.slice(s, e).includes('\n')) {
      layers.push({ style: 'code', left: '`', right: '`' });
      s -= 1;
      e += 1;
      expanded = true;
      continue;
    }

    for (const marker of PEEK_MARKERS) {
      if (
        s >= marker.left.length &&
        e + marker.right.length <= value.length &&
        value.slice(s - marker.left.length, s) === marker.left &&
        value.slice(e, e + marker.right.length) === marker.right
      ) {
        // Avoid treating the inner * of ** as italic when bold is present.
        if (marker.style === 'italic' && marker.left === '*') {
          const boldLeft = value.slice(s - 2, s) === '**';
          const boldRight = value.slice(e, e + 2) === '**';
          if (boldLeft || boldRight) continue;
        }
        if (marker.style === 'italic' && marker.left === '_') {
          const boldLeft = value.slice(s - 2, s) === '__';
          const boldRight = value.slice(e, e + 2) === '__';
          if (boldLeft || boldRight) continue;
        }
        layers.push({ style: marker.style, left: marker.left, right: marker.right });
        s -= marker.left.length;
        e += marker.right.length;
        expanded = true;
        break;
      }
    }
  }

  return { layers, coreStart: start, coreEnd: end };
}

export function hasStyleAtOccurrence(
  value: string,
  selectedText: string,
  occurrenceIndex: number,
  style: TextWrapStyle,
): boolean {
  const needle = (selectedText ?? '').replace(/\u00a0/g, ' ').trim();
  if (!needle) return false;
  const idx = findNthOccurrence(value, needle, occurrenceIndex);
  if (idx < 0) return false;
  const { layers } = collectWrapperLayers(value, idx, idx + needle.length);
  return layers.some(layer => layer.style === style);
}

function removeLayerClean(
  value: string,
  coreStart: number,
  coreEnd: number,
  layers: WrapperLayer[],
  style: TextWrapStyle,
): TextWrapResult | null {
  const idx = layers.findIndex(layer => layer.style === style);
  if (idx < 0) return null;

  let outerStart = coreStart;
  let outerEnd = coreEnd;
  for (const layer of layers) {
    outerStart -= layer.left.length;
    outerEnd += layer.right.length;
  }

  const core = value.slice(coreStart, coreEnd);
  // Re-apply remaining layers from innermost to outermost (skip removed).
  // layers is innermost-first.
  let wrapped = core;
  for (let i = 0; i < layers.length; i += 1) {
    if (i === idx) continue;
    wrapped = layers[i].left + wrapped + layers[i].right;
  }

  const next = value.slice(0, outerStart) + wrapped + value.slice(outerEnd);
  // Find core inside the rebuilt segment for selection offsets.
  const coreAt = outerStart + wrapped.indexOf(core);
  return {
    value: next,
    innerText: core,
    selectionStart: coreAt,
    selectionEnd: coreAt + core.length,
  };
}

function addInnermost(
  value: string,
  coreStart: number,
  coreEnd: number,
  layers: WrapperLayer[],
  marker: MarkerPair,
): TextWrapResult {
  // Insert new markers directly around the core (inside existing wrappers).
  let outerStart = coreStart;
  let outerEnd = coreEnd;
  for (const layer of layers) {
    outerStart -= layer.left.length;
    outerEnd += layer.right.length;
  }
  const core = value.slice(coreStart, coreEnd);
  let wrapped = marker.left + core + marker.right;
  for (const layer of layers) {
    wrapped = layer.left + wrapped + layer.right;
  }
  const next = value.slice(0, outerStart) + wrapped + value.slice(outerEnd);
  const coreAt = outerStart + wrapped.indexOf(core);
  return {
    value: next,
    innerText: core,
    selectionStart: coreAt,
    selectionEnd: coreAt + core.length,
  };
}

/**
 * Ensure a fenced code block sits on its own lines so CommonMark recognizes it.
 * Mid-paragraph ``` breaks parsing (opening ignored; a later ``` can open an unclosed fence).
 */
function toggleCodeAt(
  value: string,
  coreStart: number,
  coreEnd: number,
  layers: WrapperLayer[],
  codeLanguage = '',
): TextWrapResult {
  const removed = removeLayerClean(value, coreStart, coreEnd, layers, 'code');
  if (removed) return removed;

  const lang = sanitizeCodeLanguage(codeLanguage);
  const open = `\`\`\`${lang}\n`;
  const close = '\n```';
  const core = value.slice(coreStart, coreEnd);

  // Drop other inline wrappers when promoting to a block fence.
  let outerStart = coreStart;
  let outerEnd = coreEnd;
  for (const layer of layers) {
    outerStart -= layer.left.length;
    outerEnd += layer.right.length;
  }

  let before = value.slice(0, outerStart);
  let after = value.slice(outerEnd);

  if (before.length > 0 && !before.endsWith('\n')) {
    before += '\n\n';
  } else if (before.endsWith('\n') && !before.endsWith('\n\n')) {
    before += '\n';
  }

  if (after.length > 0 && !after.startsWith('\n')) {
    after = `\n\n${after}`;
  } else if (after.startsWith('\n') && !after.startsWith('\n\n')) {
    after = `\n${after}`;
  }

  const block = open + core + close;
  const next = before + block + after;
  const coreAt = before.length + open.length;
  return {
    value: next,
    innerText: core,
    selectionStart: coreAt,
    selectionEnd: coreAt + core.length,
  };
}

function toggleLinkAt(
  value: string,
  coreStart: number,
  coreEnd: number,
  layers: WrapperLayer[],
  linkUrl: string,
): TextWrapResult {
  const removed = removeLayerClean(value, coreStart, coreEnd, layers, 'link');
  if (removed) return removed;
  const safe = sanitizeMarkdownLinkUrl(linkUrl);
  return addInnermost(value, coreStart, coreEnd, layers, { style: 'link', left: '[', right: `](${safe})` });
}

/** Allow http(s), mailto, anchors, and relative paths; drop other schemes. */
export function sanitizeMarkdownLinkUrl(linkUrl: string): string {
  const trimmed = (linkUrl || '').trim() || 'https://';
  if (/^(https?:\/\/|mailto:|#|\/|\.\/|\.\.\/)/i.test(trimmed)) return trimmed;
  if (/^[a-z][a-z0-9+.-]*:/i.test(trimmed)) return 'https://';
  return trimmed;
}

export function wrapTextSelection(
  value: string,
  start: number,
  end: number,
  style: TextWrapStyle,
  linkUrl = 'https://',
  codeLanguage = '',
): TextWrapResult {
  const s = Math.max(0, Math.min(start, end, value.length));
  const e = Math.max(s, Math.min(Math.max(start, end), value.length));
  const { layers, coreStart, coreEnd } = collectWrapperLayers(value, s, e);

  if (style === 'code') return toggleCodeAt(value, coreStart, coreEnd, layers, codeLanguage);
  if (style === 'link') return toggleLinkAt(value, coreStart, coreEnd, layers, linkUrl);

  const removed = removeLayerClean(value, coreStart, coreEnd, layers, style);
  if (removed) return removed;

  const marker = APPLY_MARKERS[style];
  return addInnermost(value, coreStart, coreEnd, layers, marker);
}

/**
 * Toggle a markdown style on the n-th occurrence of selectedText in value.
 * Returns null when the occurrence cannot be located.
 */
export function toggleStyleAtOccurrence(
  value: string,
  selectedText: string,
  occurrenceIndex: number,
  style: TextWrapStyle,
  linkUrl = 'https://',
  codeLanguage = '',
): TextWrapResult | null {
  const needle = (selectedText ?? '').replace(/\u00a0/g, ' ').trim();
  if (!needle) return null;
  const idx = findNthOccurrence(value, needle, occurrenceIndex);
  if (idx < 0) return null;
  return wrapTextSelection(value, idx, idx + needle.length, style, linkUrl, codeLanguage);
}

/** @deprecated use toggleStyleAtOccurrence */
export function wrapFirstOccurrence(
  value: string,
  selected: string,
  style: TextWrapStyle,
  linkUrl = 'https://',
  codeLanguage = '',
): TextWrapResult | null {
  return toggleStyleAtOccurrence(value, selected, 0, style, linkUrl, codeLanguage);
}

/** Strip script / on* handlers for safer HTML preview srcdoc. */
export function stripHtmlScripts(html: string): string {
  let out = html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '');
  out = out.replace(/\son[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, '');
  out = out.replace(/javascript:/gi, '');
  return out;
}
