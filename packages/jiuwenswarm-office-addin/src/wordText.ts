/**
 * The text rules of Word's edit tools, apart from Word: how a model's ``old_text`` is compared with the document,
 * how it splits into paragraphs, and which words of it a replacement changes. An edit replaces only those words,
 * so with Track Changes on the redline shows the words that changed, not a paragraph deleted and inserted.
 */

/** The longest text Word's search API takes. */
export const SEARCH_LIMIT = 255;

const CURLY = /[‘’“”]/g;

/** Text with curly quotes as straight ones, as the tools compare it; the length is unchanged. */
export function straightQuotes(text: string): string {
  return text.replace(CURLY, quote => (quote === '‘' || quote === '’' ? '\'' : '"'));
}

/** Text with straight quotes as the curly ones Word's AutoFormat types: opening after a space or bracket. */
export function curlyQuotes(text: string): string {
  const opens = (before: string | undefined) => !before || /[\s([{‘“-]/.test(before);
  return text.replace(/["']/g, (quote, at: number) => {
    const open = opens(text[at - 1]);
    if (quote === '"') return open ? '“' : '”';
    return open ? '‘' : '’';
  });
}

export function sameText(a: string, b: string): boolean {
  return straightQuotes(a) === straightQuotes(b);
}

/** The forms of ``text`` to search the document for: itself, then with the other kind of quotes. */
export function searchForms(text: string): string[] {
  const other = /["']/.test(text) ? curlyQuotes(text) : straightQuotes(text);
  return other === text ? [text] : [text, other];
}

/** A model's replacement text with the escapes the contract allows: ``\\n``, ``\\t`` and ``\\\\``. */
export function unescapeText(text: string): string {
  return text.replace(/\\(\\|n|t)/g, (_match, code: string) => (code === 'n' ? '\n' : code === 't' ? '\t' : '\\'));
}

/** The paragraphs ``old_text`` names, one per line; a trailing line break names none. */
export function paragraphLines(text: string): string[] {
  return text.replace(/[\r\n]+$/, '').split(/\r\n|\r|\n/);
}

/** Why ``old_text`` cannot anchor an edit, before the document is searched; undefined when it can. */
export function anchorProblem(oldText: string): string | undefined {
  const lines = paragraphLines(oldText);
  if (lines[0] === '') {
    return lines.length > 1 ? 'old_text starts with a blank line; start it at the first words of the passage.'
      : 'old_text is empty; give the exact words to replace.';
  }
  return undefined;
}

/** Where ``found`` matches of ``old_text`` leave the edit: undefined for exactly one. */
export function matchProblem(found: number): string | undefined {
  if (found === 1) return undefined;
  return found === 0
    ? 'old_text was not found in the document (curly and straight quotes match each other). Check spelling and '
      + 'whitespace, or read the passage again with read_doc_section to get the exact text.'
    : `old_text matched ${found} places; it must be unique. Add neighbouring words until it is.`;
}

/** One change of ``old``: the characters ``[start, end)`` become ``text``. */
export interface Hunk {
  start: number;
  end: number;
  text: string;
}

/** Above this many token pairs the comparison gives up on word level and replaces the one span that differs. */
const MAX_PAIRS = 250_000;

function tokens(text: string): { text: string; at: number }[] {
  return [...text.matchAll(/\s+|[\p{L}\p{N}_]+|[^\s\p{L}\p{N}_]/gu)]
    .map(match => ({ text: match[0], at: match.index }));
}

/**
 * The changes that turn ``old`` into ``next``, word by word, in the order of ``old``. Words both keep are left
 * alone; changes only whitespace apart join into one, so a reworded phrase reads as one revision.
 */
export function wordHunks(old: string, next: string): Hunk[] {
  if (old === next) return [];
  const a = tokens(old);
  const b = tokens(next);
  const end = (list: typeof a, text: string, index: number) => (index < list.length ? list[index].at : text.length);
  if (a.length * b.length > MAX_PAIRS) return [trimmed(old, next)];
  // Longest common subsequence of tokens, from the end, so the walk below goes forward.
  const width = b.length + 1;
  const common = new Uint32Array((a.length + 1) * width);
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      common[i * width + j] = a[i].text === b[j].text ? common[(i + 1) * width + j + 1] + 1
        : Math.max(common[(i + 1) * width + j], common[i * width + j + 1]);
    }
  }
  const hunks: Hunk[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i].text === b[j].text) {
      i += 1;
      j += 1;
      continue;
    }
    const [fromA, fromB] = [i, j];
    while ((i < a.length || j < b.length) && !(i < a.length && j < b.length && a[i].text === b[j].text)) {
      if (j >= b.length || (i < a.length && common[(i + 1) * width + j] >= common[i * width + j + 1])) i += 1;
      else j += 1;
    }
    const hunk = { start: end(a, old, fromA), end: end(a, old, i),
      text: next.slice(end(b, next, fromB), end(b, next, j)) };
    const last = hunks.at(-1);
    if (last && /^\s*$/.test(old.slice(last.end, hunk.start))) {
      last.text += old.slice(last.end, hunk.start) + hunk.text;
      last.end = hunk.end;
    } else {
      hunks.push(hunk);
    }
  }
  return hunks;
}

function trimmed(old: string, next: string): Hunk {
  let start = 0;
  while (start < old.length && start < next.length && old[start] === next[start]) start += 1;
  let tail = 0;
  while (tail < old.length - start && tail < next.length - start
    && old[old.length - 1 - tail] === next[next.length - 1 - tail]) tail += 1;
  return { start, end: old.length - tail, text: next.slice(start, next.length - tail) };
}

/** ``old`` and ``next`` as runs a card shows: kept text, removed text and added text. */
export function diffRuns(old: string, next: string): { kind: 'same' | 'removed' | 'added'; text: string }[] {
  const runs: { kind: 'same' | 'removed' | 'added'; text: string }[] = [];
  let at = 0;
  for (const hunk of wordHunks(old, next)) {
    if (hunk.start > at) runs.push({ kind: 'same', text: old.slice(at, hunk.start) });
    if (hunk.end > hunk.start) runs.push({ kind: 'removed', text: old.slice(hunk.start, hunk.end) });
    if (hunk.text) runs.push({ kind: 'added', text: hunk.text });
    at = hunk.end;
  }
  if (at < old.length) runs.push({ kind: 'same', text: old.slice(at) });
  return runs;
}

/**
 * Where in ``line`` the occurrence of ``fragment`` that starts at ``at`` is, counted as Word's search counts
 * matches (left to right, not overlapping); undefined when no match starts there.
 */
export function occurrenceAt(line: string, fragment: string, at: number): number | undefined {
  const key = straightQuotes(line);
  const wanted = straightQuotes(fragment);
  let index = 0;
  for (let from = key.indexOf(wanted); from !== -1; from = key.indexOf(wanted, from + wanted.length)) {
    if (from === at) return index;
    if (from > at) return undefined;
    index += 1;
  }
  return undefined;
}
