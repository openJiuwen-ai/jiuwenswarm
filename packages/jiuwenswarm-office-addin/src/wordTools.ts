/**
 * Word's tools on ``Word.run``: reading (``read_doc_section``, ``search_doc_text``, ``verify_doc``), editing text
 * and lists (``edit_doc_text``, ``edit_doc_list``, ``collapse_blank_paragraphs``), the model's own code
 * (``execute_office_js``), suggestion cards (``propose_doc_edits``, ``withdraw_doc_edits``) and the captures the
 * reviewers of ``explain_edits`` and ``verify_doc_visual`` judge. Results are shaped as the industry add-in shapes
 * them, which the Word persona reads. A request refused before it starts says why and changes nothing.
 */
import type { OfficeHost } from './appContract';
import type { ClientToolResponse, ClientToolRuntime } from './clientToolDispatcher';
import type { DocEditReview, ProposedEdit } from './docEditReview';
import type { JsonObject } from './gatewayClient';
import { type CodeHost, runOfficeCode } from './officeCodeRunner';
import type { RenderedPages } from './pdfPages';
import { screenWordWrite } from './wordGuard';
import { hasTrackedChanges, headingLevel, markedLines, PARAGRAPH_FIELDS } from './wordParagraphs';
import { findPassage, matchesOf, Refused, replacePassage, type WordRun } from './wordPassage';
import { straightQuotes, unescapeText } from './wordText';

export const WORD_TOOL_NAMES = ['read_doc_section', 'search_doc_text', 'edit_doc_text', 'edit_doc_list',
  'collapse_blank_paragraphs', 'verify_doc', 'execute_office_js', 'read_surface_context', 'propose_doc_edits',
  'withdraw_doc_edits', 'explain_edits', 'verify_doc_visual'] as const;
type WordToolName = (typeof WORD_TOOL_NAMES)[number];

/** Paragraphs one ``read_doc_section`` call returns at most; the result says where to go on. */
const MAX_SECTION = 400;
/** How much of a paragraph a search hit's snippet shows. */
const SNIPPET_CHARS = 160;
/** Empty paragraphs deleted per round trip. */
const DELETE_BATCH = 50;
/**
 * How a new list looks. The list is made with ``startNewList``: Word's List Bullet and List Number styles go by
 * names in Word's display language, and ``styleBuiltIn`` has neither.
 */
const LIST_STYLES = {
  bullet: (list: Word.List) => list.setLevelBullet(0, 'Solid'),
  number: (list: Word.List) => list.setLevelNumbering(0, 'Arabic', [0, '.']),
} as const;
/** Shapes anchored to a paragraph can be read from WordApiDesktop 1.2. */
const SHAPES_SET = ['WordApiDesktop', '1.2'] as const;
/** What a model's own ``read_surface_context`` call gets on Word: the state is the one added at the turn's start. */
const SURFACE_NOTE = 'The document state at the start of this turn is in the latest read_surface_context result '
  + 'above; it is not read again during the turn. Read more with read_doc_section or search_doc_text.';

interface Options {
  run: WordRun;
  host: OfficeHost;
  /** The enums ``execute_office_js`` code sees as ``Word``. */
  enums: Record<string, Readonly<Record<string, string>>>;
  reviews: DocEditReview;
  /** The document's first pages, drawn; absent where Word cannot export a PDF (Word on the web). */
  pages?: () => Promise<RenderedPages>;
}

type Call = { input: JsonObject; callId: string; sessionId?: string };

export function wordRuntime(options: Options): ClientToolRuntime<WordToolName> {
  const { run, reviews, host } = options;
  const tools: Record<WordToolName, (call: Call) => Promise<ClientToolResponse>> = {
    read_doc_section: ({ input }) => run(context => readSection(context, input)),
    search_doc_text: ({ input }) => run(context => searchText(context, input)),
    edit_doc_text: ({ input }) => run(context => editText(context, input)),
    edit_doc_list: ({ input }) => run(context => editList(context, input)),
    collapse_blank_paragraphs: ({ input }) => run(context => collapseBlanks(context,
      Number(input.max_consecutive ?? 1), host.isSetSupported(...SHAPES_SET))),
    verify_doc: () => run(verifyDoc),
    execute_office_js: ({ input }) => runCode(options, String(input.code)),
    read_surface_context: async () => ({ ok: true, result: SURFACE_NOTE }),
    propose_doc_edits: ({ input, callId, sessionId }) => run(context => propose(context, input, reviews, callId,
      sessionId ?? '')),
    withdraw_doc_edits: async ({ input, sessionId }) => ({
      ok: true, result: reviews.withdraw(sessionId ?? '', input.ids as string[]) }),
    explain_edits: () => run(captureForReview),
    verify_doc_visual: async () => {
      const { pageCount, jpegs } = await options.pages!();
      return { ok: true, result: { page_count: pageCount },
        images: jpegs.map(data => ({ media_type: 'image/jpeg', data })) };
    },
  };
  return {
    tools: new Set(WORD_TOOL_NAMES.filter(name => name !== 'verify_doc_visual' || options.pages)),
    execute: async ({ toolName, input, callId, sessionId }) => {
      try {
        return await tools[toolName]({ input, callId, sessionId });
      } catch (error) {
        if (!(error instanceof Refused)) throw error;
        return { ok: false, error: {
          code: error.blocked ? 'TOOL_EXECUTION_BLOCKED' : 'WORD_REQUEST_REFUSED', message: error.message,
          retryable: false, category: 'validation', details: { outcome: 'not_applied', native_mutation_started: false },
        } };
      }
    },
  };
}

const applied = { mutationEvidence: { outcome: 'applied', native_mutation_started: true } };

async function bodyParagraphs(context: Word.RequestContext): Promise<Word.Paragraph[]> {
  const paragraphs = context.document.body.paragraphs;
  paragraphs.load(PARAGRAPH_FIELDS);
  await context.sync();
  return paragraphs.items;
}

/** The paragraph range a heading names: from it to the next heading at its level or above. */
function headingRange(paragraphs: Word.Paragraph[], heading: string): [number, number] {
  const wanted = heading.trim();
  const unnumbered = (text: string) => text.trim().replace(/^(\d+\.)*\d*\s+/, '').toLowerCase();
  const headings = paragraphs.flatMap((paragraph, index) => {
    const level = headingLevel(paragraph);
    return level === undefined ? [] : [{ index, level, text: paragraph.text }];
  });
  const found = headings.find(item => item.text.trim() === wanted)
    ?? headings.find(item => unnumbered(item.text) === unnumbered(wanted));
  if (!found) {
    throw new Refused(`There is no heading ${JSON.stringify(wanted)}; use a heading exactly as the outline shows it.`);
  }
  const next = headings.find(item => item.index > found.index && item.level <= found.level);
  return [found.index, next?.index ?? paragraphs.length];
}

async function readSection(context: Word.RequestContext, input: JsonObject): Promise<ClientToolResponse> {
  const paragraphs = await bodyParagraphs(context);
  const total = paragraphs.length;
  let [start, end]: [number, number] = [0, 0];
  if (typeof input.heading === 'string') {
    if (input.paragraph_start !== undefined) throw new Refused('Give heading or paragraph_start, not both.');
    [start, end] = headingRange(paragraphs, input.heading);
  } else if (typeof input.paragraph_start === 'number') {
    start = input.paragraph_start;
    end = typeof input.paragraph_end === 'number' ? input.paragraph_end : start + 1;
    if (start >= total || end > total || end <= start) {
      throw new Refused(`The document has ${total} paragraphs (0 to ${total - 1}); paragraph_start must be below `
        + 'paragraph_end, and paragraph_end at most the paragraph count.');
    }
  } else {
    throw new Refused('Give heading or paragraph_start.');
  }
  const stop = Math.min(end, start + MAX_SECTION);
  const lines = await markedLines(context, paragraphs.slice(start, stop),
    { formatting: input.include_formatting === true, reviewed: await hasTrackedChanges(context) });
  return { ok: true, result: {
    total_paragraphs: total, paragraph_start: start, paragraph_end: stop,
    paragraphs: lines.map((line, offset) => `${start + offset}: ${line}`),
    ...(stop < end ? { more: `The section goes on to paragraph ${end}; read from paragraph_start ${stop}.` } : {}),
  } };
}

async function searchText(context: Word.RequestContext, input: JsonObject): Promise<ClientToolResponse> {
  const query = String(input.query);
  if (/[\r\n]/.test(query)) throw new Refused('query must be a phrase within one paragraph, without line breaks.');
  const hits = await matchesOf(context, context.document.body, query);
  const shown = hits.slice(0, Number(input.max_results ?? 10)).map(hit => hit.paragraphs.getFirst()
    .load('uniqueLocalId,text'));
  const paragraphs = context.document.body.paragraphs;
  paragraphs.load('items/uniqueLocalId');
  await context.sync();
  const indexes = new Map(paragraphs.items.map((paragraph, index) => [paragraph.uniqueLocalId, index]));
  return { ok: true, result: {
    total_matches: hits.length,
    results: shown.map(paragraph => ({
      paragraph_index: indexes.get(paragraph.uniqueLocalId) ?? -1, snippet: snippet(paragraph.text, query) })),
  } };
}

/** The part of ``text`` around ``query``, at most ``SNIPPET_CHARS`` long, with ``…`` where it is cut. */
function snippet(text: string, query: string): string {
  if (text.length <= SNIPPET_CHARS) return text;
  const at = Math.max(0, straightQuotes(text).indexOf(straightQuotes(query)));
  const from = Math.max(0, Math.min(at - Math.floor((SNIPPET_CHARS - query.length) / 2), text.length - SNIPPET_CHARS));
  const to = from + SNIPPET_CHARS;
  return `${from > 0 ? '…' : ''}${text.slice(from, to)}${to < text.length ? '…' : ''}`;
}

async function editText(context: Word.RequestContext, input: JsonObject): Promise<ClientToolResponse> {
  const oldText = String(input.old_text);
  const passage = await findPassage(context, oldText);
  const span = await replacePassage(context, passage, oldText, unescapeText(String(input.new_text)));
  if (!span) {
    return { ok: true, result: { replaced: false, note: 'new_text is the same as old_text; nothing changed.' } };
  }
  const paragraph = span.paragraphs.getFirst().load('text,styleBuiltIn');
  await context.sync();
  return { ok: true, ...applied,
    result: { replaced: true, paragraph: { text: paragraph.text, styleBuiltIn: paragraph.styleBuiltIn } } };
}

/** The paragraph a unique anchor text sits in. */
async function anchorParagraph(context: Word.RequestContext, field: string, text: unknown): Promise<Word.Paragraph> {
  if (typeof text !== 'string') throw new Refused(`${field} is required for this action.`);
  const hits = await matchesOf(context, context.document.body, text);
  if (hits.length !== 1) {
    throw new Refused(`${field} matched ${hits.length} places; it must match exactly one. Use more of the paragraph.`);
  }
  return hits[0].paragraphs.getFirst();
}

async function editList(context: Word.RequestContext, input: JsonObject): Promise<ClientToolResponse> {
  let added: Word.Paragraph[];
  if (input.action === 'create_list') {
    const items = input.items as string[] | undefined;
    const style = LIST_STYLES[input.list_style as keyof typeof LIST_STYLES];
    if (!items || !style) throw new Refused('create_list needs anchor_text, items and list_style.');
    let previous = await anchorParagraph(context, 'anchor_text', input.anchor_text);
    previous.load('isListItem');
    await context.sync();
    if (previous.isListItem) {
      throw new Refused('anchor_text is in a list; add to that list with insert_item, or anchor the new list on a '
        + 'paragraph outside it.');
    }
    added = items.map(item => (previous = previous.insertParagraph(item, 'After')));
    const list = added[0].startNewList().load('id');
    await context.sync();
    style(list);
    added.slice(1).forEach(paragraph => paragraph.attachToList(list.id, 0));
  } else {
    if (typeof input.new_item_text !== 'string' || !input.position) {
      throw new Refused('insert_item needs anchor_item_text, new_item_text and position.');
    }
    const anchor = await anchorParagraph(context, 'anchor_item_text', input.anchor_item_text);
    const list = anchor.listOrNullObject.load('id');
    const item = anchor.listItemOrNullObject.load('level');
    anchor.load('isListItem');
    await context.sync();
    if (!anchor.isListItem) throw new Refused('anchor_item_text is not in a list item; use create_list instead.');
    const paragraph = anchor.insertParagraph(input.new_item_text, input.position as 'Before' | 'After');
    paragraph.load('isListItem');
    await context.sync();
    if (!paragraph.isListItem) paragraph.attachToList(list.id, item.level);
    added = [paragraph];
  }
  const lists = added.map(paragraph => {
    paragraph.load('text,isListItem');
    return paragraph.listItemOrNullObject.load('level,listString');
  });
  await context.sync();
  const result = added.map((paragraph, index) => ({ text: paragraph.text, isListItem: paragraph.isListItem,
    listString: lists[index].isNullObject ? '' : lists[index].listString,
    level: lists[index].isNullObject ? 0 : lists[index].level }));
  const plain = result.filter(item => !item.isListItem).length;
  if (plain) {
    return { ok: false, error: {
      code: 'WORD_LIST_NOT_ATTACHED', retryable: false, category: 'host',
      details: { outcome: 'partial', items: result },
      message: `${plain} of ${result.length} item(s) came in as plain paragraphs without a list marker; delete them `
        + 'and build the list with execute_office_js (attachToList), as Word refused the list style here.',
    }, ...applied };
  }
  return { ok: true, result: { items: result }, ...applied };
}

/**
 * Delete empty paragraphs past ``max`` in a row. A paragraph is empty when it holds only spaces and tabs (a page or
 * column break, ``\f``, or a line break, ``\v``, is content) and nothing else: no picture, shape, comment, tracked
 * change, field or content control.
 */
async function collapseBlanks(context: Word.RequestContext, max: number, shapes: boolean):
Promise<ClientToolResponse> {
  const paragraphs = await bodyParagraphs(context);
  const blank = paragraphs.map((paragraph, index) => ({ paragraph, index }))
    .filter(({ paragraph, index }) => /^[ \t\u00a0\u3000]*$/.test(paragraph.text) && index < paragraphs.length - 1);
  const holds = blank.map(({ paragraph }) => [paragraph.inlinePictures, paragraph.getComments(),
    paragraph.getTrackedChanges(), paragraph.fields, paragraph.contentControls,
    ...(shapes ? [paragraph.shapes] : [])].map(collection => collection.load('items')));
  await context.sync();
  let run = 0;
  let previous = -2;
  let kept = 0;
  const doomed: Word.Paragraph[] = [];
  blank.forEach(({ paragraph, index }, at) => {
    run = index === previous + 1 ? run + 1 : 1;
    previous = index;
    const content = holds[at].some(collection => collection.items.length > 0);
    if (content || paragraph.isListItem || paragraph.tableNestingLevel > 0) {
      kept += 1;
    } else if (run > max) {
      doomed.push(paragraph);
    }
  });
  for (let from = doomed.length; from > 0; from -= DELETE_BATCH) {
    doomed.slice(Math.max(0, from - DELETE_BATCH), from).reverse().forEach(paragraph => paragraph.delete());
    await context.sync();
  }
  return { ok: true, result: { deleted: doomed.length, scanned: paragraphs.length, skipped_with_content: kept },
    ...(doomed.length ? applied : { mutationEvidence: { outcome: 'not_applied', native_mutation_started: false } }) };
}

async function verifyDoc(context: Word.RequestContext): Promise<ClientToolResponse> {
  const paragraphs = await bodyParagraphs(context);
  const lists = paragraphs.map(paragraph => (paragraph.isListItem
    ? paragraph.listItemOrNullObject.load('level,listString') : undefined));
  const tables = context.document.body.tables;
  tables.load('items');
  await context.sync();
  const rows = tables.items.map(table => table.rows.load('items/cellCount'));
  await context.sync();
  const styles: Record<string, number> = {};
  for (const paragraph of paragraphs) {
    const style = paragraph.styleBuiltIn === 'Other' ? paragraph.style : paragraph.styleBuiltIn;
    styles[style] = (styles[style] ?? 0) + 1;
  }
  return { ok: true, result: {
    styles,
    lists: lists.flatMap((item, index) => (item && !item.isNullObject
      ? [{ paragraph_index: index, level: item.level, listString: item.listString }] : [])),
    tables: rows.map((table, index) => {
      const cells = table.items.map(row => row.cellCount);
      return { table_index: index, row_count: cells.length, cells_per_row: cells,
        uniform: cells.every(count => count === cells[0]) };
    }),
  } };
}

/**
 * ``execute_office_js`` on Word: the shared runner, Word's screen of writes, and a warning when the code changed
 * the Track Changes mode, which the user's redline depends on.
 */
async function runCode({ run, enums }: Options, code: string): Promise<ClientToolResponse> {
  let warning: string | undefined;
  const watched: CodeHost['run'] = batch => run(async context => {
    const { document } = context;
    document.load('changeTrackingMode');
    await context.sync();
    const before = document.changeTrackingMode;
    const result = await batch(context);
    document.load('changeTrackingMode');
    await context.sync();
    if (document.changeTrackingMode !== before) {
      warning = `The code changed Track Changes from ${before} to ${document.changeTrackingMode}. ${
        document.changeTrackingMode === 'Off' ? 'Turn it back on unless the user asked for it off; edits from here '
          + 'on land untracked.' : 'Edits from here on are tracked.'}`;
    }
    return result;
  });
  const response = await runOfficeCode({ name: 'Word', run: watched, enums, screen: screenWordWrite }, code);
  return warning && response.ok ? { ...response, successFields: { tracked_change_warning: warning } } : response;
}

/**
 * Check every edit's ``old_text`` and show the ones found as cards; the call returns at once and the user's
 * decisions reach the next turn. An edit that cannot be found comes back in ``invalid_edits``.
 */
async function propose(context: Word.RequestContext, input: JsonObject, reviews: DocEditReview, callId: string,
  sessionId: string): Promise<ClientToolResponse> {
  const valid: ProposedEdit[] = [];
  const invalid: { id: string; error: string }[] = [];
  const seen = new Set<string>();
  for (const edit of input.edits as unknown as ProposedEdit[]) {
    if (seen.has(edit.id)) {
      invalid.push({ id: edit.id, error: 'Another edit of this call has the same id; give each edit its own.' });
      continue;
    }
    seen.add(edit.id);
    try {
      await findPassage(context, edit.old_text);
      valid.push(edit);
    } catch (error) {
      if (!(error instanceof Refused)) throw error;
      invalid.push({ id: edit.id, error: `${error.message} Propose it again with a corrected old_text; do not `
        + 'write it with another tool.' });
    }
  }
  if (!valid.length) {
    throw new Refused(`No proposed edit passed validation: ${invalid.map(item => `${item.id}: ${item.error}`)
      .join(' ')}`);
  }
  reviews.propose(callId, sessionId, valid.map(edit => edit.id));
  return { ok: true, result: {
    pending_review: true, proposed: valid.map(edit => edit.id), sections: [...new Set(valid.map(edit => edit.section))],
    ...(invalid.length ? { invalid_edits: invalid } : {}),
  } };
}

/** What an ``explain_edits`` reviewer reads: the document's final text and its pending tracked changes. */
async function captureForReview(context: Word.RequestContext): Promise<ClientToolResponse> {
  const paragraphs = await bodyParagraphs(context);
  const changes = context.document.body.getTrackedChanges();
  changes.load('items/type,items/author,items/date,items/text');
  await context.sync();
  const reviewed = changes.items.length > 0;
  const texts = reviewed ? paragraphs.map(paragraph => paragraph.getReviewedText('Current')) : [];
  if (reviewed) await context.sync();
  return { ok: true, result: {
    paragraphs: paragraphs.map((paragraph, index) => (reviewed ? texts[index].value.replace(/\r$/, '')
      : paragraph.text)),
    tracked_changes: changes.items.map(change => ({ type: change.type, author: change.author ?? '(unknown)',
      date: change.date, text: change.text })),
  } };
}
