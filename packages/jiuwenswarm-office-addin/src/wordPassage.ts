/**
 * A passage of the document named by its exact text (``old_text``), and its replacement word by word. One rule
 * for every tool that edits text (``edit_doc_text``, ``propose_doc_edits`` and its cards): the text must match
 * exactly once, curly and straight quotes alike; text past Word's 255-character search limit is found by its first
 * and last 255 characters; a text of several lines names that many whole paragraphs in a row. Only the words that
 * change are replaced, and never across a field, footnote or picture, which a replacement would destroy.
 */
import {
  anchorProblem, matchProblem, occurrenceAt, paragraphLines, SEARCH_LIMIT, sameText, searchForms, wordHunks,
} from './wordText';

export type WordRun = <T>(batch: (context: Word.RequestContext) => Promise<T>) => Promise<T>;

/** A request a tool refuses before or instead of changing the document; ``blocked`` when the reason is safety. */
export class Refused extends Error {
  constructor(message: string, readonly blocked = false) {
    super(blocked ? `ToolExecutionBlocked: ${message}` : message);
  }
}

/** The ranges ``old_text`` names: one per paragraph it spans, each with its text as the document has it. */
export interface Passage {
  parts: Word.Range[];
  lines: string[];
}

/** Start and end candidates multiplied past this are too many to compare. */
const MAX_CANDIDATES = 256;
const UNPLACED = 'The change could not be placed word by word inside old_text; give old_text and new_text again '
  + 'with more of the unchanged words around the change.';

/** Search ``scope`` for ``text`` exactly: Word reads ``^`` as the start of a special character, ``^^`` as itself. */
function search(scope: Word.Body | Word.Range, text: string): Word.RangeCollection {
  return scope.search(text.replace(/\^/g, '^^'), { matchCase: true });
}

/** The ranges in ``scope`` whose text is ``line`` (one paragraph's text at most). */
export async function matchesOf(context: Word.RequestContext, scope: Word.Body | Word.Range, line: string):
Promise<Word.Range[]> {
  if (line.length <= SEARCH_LIMIT) {
    for (const form of searchForms(line)) {
      const hits = search(scope, form);
      hits.load('items/text');
      await context.sync();
      if (hits.items.length) return hits.items;
    }
    return [];
  }
  const head = line.slice(0, SEARCH_LIMIT);
  const tail = line.slice(-SEARCH_LIMIT);
  if (head === tail) {
    throw new Refused('old_text is one long repeated run (a signature line or rule) that cannot be found by its '
      + 'content; include the words next to it.');
  }
  const found = (part: string) => searchForms(part).map(form => {
    const hits = search(scope, form);
    hits.load('items');
    return hits;
  });
  const [heads, tails] = [found(head), found(tail)];
  await context.sync();
  const starts = heads.flatMap(hits => hits.items);
  const ends = tails.flatMap(hits => hits.items);
  if (starts.length * ends.length > MAX_CANDIDATES) {
    throw new Refused(`old_text starts or ends with wording found all over the document (${starts.length} × `
      + `${ends.length} places); shorten it to the distinctive part of the change.`);
  }
  const spans = starts.flatMap(start => ends.map(end => start.expandTo(end).load('text')));
  await context.sync();
  return spans.filter(span => sameText(span.text, line));
}

/** The one passage ``oldText`` names; refused when it is found nowhere or more than once. */
export async function findPassage(context: Word.RequestContext, oldText: string): Promise<Passage> {
  const problem = anchorProblem(oldText);
  if (problem) throw new Refused(problem);
  const lines = paragraphLines(oldText);
  const hits = await matchesOf(context, context.document.body, lines[0]);
  if (lines.length === 1) {
    const unmatched = matchProblem(hits.length);
    if (unmatched) throw new Refused(unmatched);
    return { parts: [hits[0]], lines: [hits[0].text] };
  }
  const runs = hits.map(hit => {
    const paragraphs = [hit.paragraphs.getFirst()];
    for (let index = 1; index < lines.length; index++) paragraphs.push(paragraphs[index - 1].getNextOrNullObject());
    paragraphs.forEach(paragraph => paragraph.load('text'));
    return paragraphs;
  });
  await context.sync();
  const whole = runs.filter(run => run.every((paragraph, index) => !paragraph.isNullObject
    && sameText(paragraph.text, lines[index])));
  const unmatched = matchProblem(whole.length);
  if (unmatched) {
    throw new Refused(whole.length ? unmatched : `${unmatched} With several lines, each line must be one whole `
      + 'paragraph, in order.');
  }
  return { parts: whole[0].map(paragraph => paragraph.getRange('Content')), lines: whole[0].map(item => item.text) };
}

/** Where a change goes: a range it replaces, or a range it goes next to. */
type Target = { range: Word.Range; where: 'Replace' | 'After' | 'Before' | 'Start' };

/**
 * Replace ``oldText``'s passage with ``newText``, changing only the words that differ. Returns the range from the
 * first change to the end of the last, which with Track Changes on covers the deleted words too; undefined when
 * nothing differs. Nothing changes when any change cannot be placed or would replace a field, note or picture.
 */
export async function replacePassage(context: Word.RequestContext, passage: Passage, oldText: string, newText: string):
Promise<Word.Range | undefined> {
  const hunks = wordHunks(paragraphLines(oldText).join('\n'), newText);
  if (!hunks.length) return undefined;
  const { parts, lines } = passage;
  const starts = lines.map((_line, index) => lines.slice(0, index).reduce((sum, line) => sum + line.length + 1, 0));
  const place = (position: number) => {
    let line = 0;
    while (line + 1 < starts.length && starts[line + 1] <= position) line += 1;
    return { line, column: position - starts[line] };
  };
  const fragment = (line: number, from: number, to: number) => {
    const index = occurrenceAt(lines[line], lines[line].slice(from, to), from);
    if (index === undefined) throw new Refused(UNPLACED);
    const hits = search(parts[line], lines[line].slice(from, to));
    hits.load('items');
    return () => {
      const hit = hits.items[index];
      if (!hit) throw new Refused(UNPLACED);
      return hit;
    };
  };
  const pending = hunks.map(({ start, end }): () => Target => {
    const from = place(start);
    const to = place(end);
    const line = lines[from.line];
    if (start === end) {
      if (from.column > 0) {
        const before = fragment(from.line, Math.max(0, from.column - SEARCH_LIMIT), from.column);
        return () => ({ range: before(), where: 'After' });
      }
      if (!line) return () => ({ range: parts[from.line], where: 'Start' });
      const after = fragment(from.line, 0, Math.min(line.length, SEARCH_LIMIT));
      return () => ({ range: after(), where: 'Before' });
    }
    if (from.line === to.line && end - start <= SEARCH_LIMIT) {
      const span = fragment(from.line, from.column, to.column);
      return () => ({ range: span(), where: 'Replace' });
    }
    const head = from.column < line.length
      ? fragment(from.line, from.column, Math.min(line.length, from.column + SEARCH_LIMIT)) : undefined;
    const tail = to.column > 0 ? fragment(to.line, Math.max(0, to.column - SEARCH_LIMIT), to.column) : undefined;
    return () => ({
      range: (head ? head().getRange('Start') : parts[from.line].getRange('End'))
        .expandTo(tail ? tail().getRange('End') : parts[to.line].getRange('Start')),
      where: 'Replace',
    });
  });
  await context.sync();
  const targets = pending.map(target => target());
  const replaced = targets.filter(target => target.where === 'Replace').map(({ range }) => {
    const inline = { fields: range.fields, footnotes: range.footnotes, endnotes: range.endnotes,
      pictures: range.inlinePictures };
    Object.values(inline).forEach(collection => collection.load('items'));
    return inline;
  });
  await context.sync();
  const kept = replaced.flatMap(inline => Object.entries(inline).filter(([, collection]) => collection.items.length)
    .map(([kind, collection]) => `${collection.items.length} ${kind === 'pictures' ? 'picture'
      : kind.slice(0, -1)}(s)`));
  if (kept.length) {
    throw new Refused(`old_text spans ${kept.join(', ')}, which a replacement would destroy; edit the words around `
      + 'them in separate edits instead.');
  }
  const first = targets[0].range.getRange(targets[0].where === 'After' ? 'End' : 'Start');
  let last: Word.Range | undefined;
  for (let index = targets.length - 1; index >= 0; index--) {
    const inserted = targets[index].range.insertText(hunks[index].text, targets[index].where);
    last ??= inserted;
  }
  await context.sync();
  return first.expandTo(last!.getRange('End'));
}
