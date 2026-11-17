/**
 * The document's state as each message carries it, in the industry add-in's blocks: ``<doc_state>`` (the full text
 * of a short document, else its opening and outline; fonts, language, custom styles, pictures, Track Changes,
 * columns, headers and footers, comment threads, content controls and pending tracked changes),
 * ``<user_selection>``, ``<user_changes>`` (the paragraphs the user added, changed or deleted since the last
 * message, from the host's paragraph events less what the pane's own writes left) and ``<user_reviewed_edits>``
 * (the user's decisions on suggestion cards). Comments, tracked changes and content controls come inside
 * ``<untrusted_content>``.
 */
import type { OfficeHost } from './appContract';
import type { DocEditReview } from './docEditReview';
import { type HostWriteQueue, type Looks, UserChanges } from './hostWriteQueue';
import type { ClientState } from './sessionFacade';
import { quoted } from './stateText';
import { headingLevel, markedLines, PARAGRAPH_FIELDS } from './wordParagraphs';
import type { WordRun } from './wordPassage';

/** Under both, ``<doc_state>`` carries the full text; otherwise the opening and the outline. */
const FULL_TEXT_WORDS = 5000;
const FULL_TEXT_CHARS = 30_000;
const OPENING_CHARS = 500;
/** How many of each list ``<doc_state>`` shows, and how long a quoted text may run. */
const MAX_HEADINGS = 200;
const MAX_LISTED = 50;
const MAX_QUOTE = 200;
/** How long a name (an author, a content control's title) may run. */
const MAX_NAME = 60;
const MAX_SELECTION = 10_000;
const COLUMNS_SET = ['WordApiDesktop', '1.3'] as const;
const EVENTS_SET = ['WordApi', '1.6'] as const;
/** Where a selection counts as in the body: the body itself, or a table cell in it. */
const BODY_TYPES = new Set(['MainDoc', 'TableCell']);

type ChangeType = 'ParagraphAdded' | 'ParagraphChanged' | 'ParagraphDeleted';

/** What the user did to one paragraph since the last message, its latest text and style. */
interface Change {
  added: boolean;
  deleted: boolean;
  coauthor: boolean;
  text?: string;
  style?: string;
}

const quote = (text: string, max = MAX_QUOTE) => quoted(text, max);

/** A paragraph's change as ``<user_changes>`` lists it. */
function changeLine(change: Change): string {
  return [
    `[${change.deleted ? 'ParagraphDeleted' : change.added ? 'ParagraphAdded' : 'ParagraphChanged'}]`,
    ...(change.coauthor ? ['(coauthor)'] : []),
    ...(change.style && change.style !== 'Normal' ? [`style:${change.style}`] : []),
    ...(change.text !== undefined ? [quote(change.text)] : []),
  ].join(' ');
}

/** A paragraph as the ledger of the pane's writes compares it: what ``<user_changes>`` shows of it. */
function look(paragraph: Pick<Word.Paragraph, 'style' | 'text'>): string {
  return `${paragraph.style}\n${paragraph.text}`;
}

/** Words as a reader counts them: runs of letters and digits, and each CJK character. */
function wordCount(text: string): number {
  return (text.match(/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]|[\p{L}\p{N}]+/gu)
    ?? []).length;
}

export class WordState {
  /** By paragraph id. */
  private readonly changes = new UserChanges<Change>(changeLine);

  constructor(private readonly run: WordRun, private readonly office: Pick<Office.Context, 'contentLanguage'
    | 'displayLanguage'>, private readonly host: OfficeHost, private readonly reviews: DocEditReview,
  private readonly writes: HostWriteQueue) {}

  /**
   * Listens to the user's paragraph edits for as long as the pane runs, on ``context``, a context of its own.
   * Word reports the pane's own writes as well, and late; a paragraph still as a write of the pane's left it is
   * taken as that write's.
   */
  async watch(context: Word.RequestContext): Promise<void> {
    if (!this.host.isSetSupported(...EVENTS_SET)) return;
    this.writes.observe(() => this.looks());
    const listen = (type: ChangeType) => async (event: Word.ParagraphAddedEventArgs) => {
      await this.writes.idle();
      for (const id of event.uniqueLocalIds) {
        let paragraph: Word.Paragraph | undefined;
        if (type !== 'ParagraphDeleted') {
          paragraph = context.document.getParagraphByUniqueLocalId(id).load('text,style,styleBuiltIn');
          await context.sync().catch(() => { paragraph = undefined; });
        }
        if (this.writes.leftAt(id) === (paragraph ? look(paragraph) : null)) continue;
        const change = this.changes.get(id) ?? { added: false, deleted: false, coauthor: false };
        if (change.added && type === 'ParagraphDeleted') {
          this.changes.set(id, undefined);
          continue;
        }
        change.added ||= type === 'ParagraphAdded';
        change.deleted = type === 'ParagraphDeleted';
        change.coauthor = event.source === 'Remote';
        if (paragraph) {
          change.text = paragraph.text;
          change.style = paragraph.styleBuiltIn;
        }
        this.changes.set(id, change);
      }
    };
    context.document.onParagraphAdded.add(listen('ParagraphAdded'));
    context.document.onParagraphChanged.add(listen('ParagraphChanged'));
    context.document.onParagraphDeleted.add(listen('ParagraphDeleted'));
    await context.sync();
  }

  /** Each paragraph's look, by id. */
  private looks(): Promise<Looks> {
    return this.run(async context => {
      const paragraphs = context.document.body.paragraphs.load('items/uniqueLocalId,items/style,items/text');
      await context.sync();
      return new Map(paragraphs.items.map(paragraph => [paragraph.uniqueLocalId, look(paragraph)]));
    });
  }

  /**
   * The state the next message of conversation ``sessionId`` carries; once it is sent, the changes and decisions it
   * lists are not listed again. A document that cannot be read just now still lets the message go, saying so.
   */
  async forMessage(sessionId: string): Promise<ClientState> {
    let document: string;
    try {
      document = await this.run(async context => [await this.docState(context), await selection(context)].join('\n'));
    } catch (error) {
      document = `<doc_state>\nThe document could not be read when this message was sent (${
        error instanceof Error ? error.message : String(error)}).\n</doc_state>`;
    }
    const changes = this.changes.block();
    const reviewed = this.reviews.reviewed(sessionId);
    return { text: [document, changes.text, reviewed.text].filter(Boolean).join('\n'), sent: () => {
      changes.sent();
      reviewed.sent();
    } };
  }

  private async docState(context: Word.RequestContext): Promise<string> {
    const { document } = context;
    const { body } = document;
    document.load('changeTrackingMode');
    const paragraphs = body.paragraphs;
    paragraphs.load(`${PARAGRAPH_FIELDS},items/font/name,items/font/size,items/font/bold,items/font/color`);
    const comments = body.getComments();
    comments.load('items/id,items/content,items/authorName,items/resolved');
    const changes = body.getTrackedChanges();
    changes.load('items/type,items/author,items/date,items/text');
    const controls = body.contentControls;
    controls.load('items/title,items/tag,items/type,items/text,items/placeholderText');
    const pictures = body.inlinePictures;
    pictures.load('items');
    const sections = document.sections;
    sections.load('items');
    await context.sync();
    const items = paragraphs.items;
    const text = items.map(paragraph => paragraph.text).join('\n');
    const words = wordCount(text);
    const lines = [`Document: ${items.length} paragraph${items.length === 1 ? '' : 's'}, about ${words} words.`];
    lines.push(...this.fonts(items));
    if (pictures.items.length) {
      lines.push(`Embedded objects: ${pictures.items.length} inline picture(s) or chart(s), not in paragraph.text; `
        + 'do not delete or replace a paragraph that holds one.');
    }
    lines.push(document.changeTrackingMode === 'Off'
      ? 'Track Changes: Off; edits land directly. If this is a legal document and you are about to change legal '
        + 'wording, ask the user first whether to track the changes.'
      : `Track Changes: ${document.changeTrackingMode}; edits land as redlines.`);
    lines.push(...await this.columns(context, sections.items));
    lines.push(...await outline(context, items.filter(paragraph => headingLevel(paragraph) !== undefined)));
    lines.push(...await headersAndFooters(context, sections.items));
    if (words < FULL_TEXT_WORDS && text.length < FULL_TEXT_CHARS) {
      const reviewed = changes.items.length > 0;
      lines.push(reviewed ? 'Full text (pending tracked changes applied; the revisions listed below are not '
        + 'accepted yet):' : 'Full text:');
      lines.push(...await markedLines(context, items, { reviewed }));
    } else {
      lines.push(`Opening text: ${text.slice(0, OPENING_CHARS)}… [the document is long; read a section with `
        + 'read_doc_section by heading, or find a phrase with search_doc_text]');
    }
    lines.push(...await commentThreads(context, comments.items));
    if (controls.items.length) {
      lines.push('Content controls (template fields):', '<untrusted_content source_type="content_control">',
        ...controls.items.slice(0, MAX_LISTED).map((control, index) => `  ${quote(control.title || control.tag
          || `cc#${index}`, MAX_NAME)} (${control.type}): ${control.text ? quote(control.text, 120)
          : `(empty; placeholder ${quote(control.placeholderText ?? '', 120)})`}`),
        ...(controls.items.length > MAX_LISTED ? [`  (and ${controls.items.length - MAX_LISTED} more)`] : []),
        '</untrusted_content>');
    }
    if (changes.items.length) {
      lines.push(`Tracked changes: ${changes.items.length} pending revision(s). They are proposals, not accepted `
        + 'text:', '<untrusted_content source_type="tracked_change">',
      ...changes.items.slice(0, MAX_LISTED).map(change => `  [author: ${quote(change.author ?? '(unknown)',
        MAX_NAME)}, ${
        new Date(change.date).toISOString().slice(0, 10)}] ${change.type}: ${quote(change.text)}`),
      ...(changes.items.length > MAX_LISTED ? [`  (and ${changes.items.length - MAX_LISTED} more)`] : []),
      '</untrusted_content>');
    }
    return `<doc_state>\n${lines.join('\n')}\n</doc_state>`;
  }

  /** The body font, the language, and the custom styles in use. */
  private fonts(items: Word.Paragraph[]): string[] {
    const lines: string[] = [];
    const body = new Map<string, number>();
    const custom = new Map<string, { count: number; font: Word.Font }>();
    for (const paragraph of items) {
      if (paragraph.styleBuiltIn === 'Other') {
        const style = custom.get(paragraph.style) ?? { count: 0, font: paragraph.font };
        custom.set(paragraph.style, { ...style, count: style.count + 1 });
      } else if (paragraph.styleBuiltIn === 'Normal' && paragraph.text.trim() && paragraph.font.name) {
        const key = `${paragraph.font.name}\n${paragraph.font.size}`;
        body.set(key, (body.get(key) ?? 0) + 1);
      }
    }
    const [common] = [...body.entries()].sort((a, b) => b[1] - a[1]);
    if (common) {
      const [name, size] = common[0].split('\n');
      lines.push(`Body font: ${name} ${size}pt; give new content this font and size.`);
    }
    const { contentLanguage, displayLanguage } = this.office;
    const language = contentLanguage || displayLanguage;
    if (language) {
      const of = contentLanguage ? displayLanguage && displayLanguage !== contentLanguage
        ? ` (authoring language; Office is in ${displayLanguage})` : ' (authoring language)'
        : ' (Office display language; the document may be in another)';
      lines.push(`Language: ${language}${of}; write new text in this locale's spelling and conventions unless the `
        + 'document clearly follows another variety.');
    }
    if (custom.size) {
      lines.push(`Custom styles in use: ${[...custom.entries()].slice(0, MAX_LISTED).map(([name, { count, font }]) => (
        `"${name}" (${[font.name, font.size && `${font.size}pt`, font.bold && 'bold'].filter(Boolean).join(' ')}${
          font.name ? ', ' : ''}${count} paragraphs)`)).join(', ')}; give new content of that kind the same style.`);
    }
    return lines;
  }

  private async columns(context: Word.RequestContext, sections: Word.Section[]): Promise<string[]> {
    if (!this.host.isSetSupported(...COLUMNS_SET)) return [];
    const columns = sections.map(section => section.pageSetup.textColumns.load('items'));
    await context.sync();
    const counts = columns.map(collection => Math.max(1, collection.items.length));
    if (counts.every(count => count === 1)) return [];
    return [`Page layout: ${counts.map((count, index) => `section ${index + 1} = ${count}-column`).join(', ')}. `
      + 'Keep the columns: edit the text inside a section, never its section break.'];
  }
}

/** The headings, indented by level, with Word's own numbering and their font. */
async function outline(context: Word.RequestContext, headings: Word.Paragraph[]): Promise<string[]> {
  if (!headings.length) return ['No headings.'];
  const shown = headings.slice(0, MAX_HEADINGS);
  const numbers = shown.map(heading => (heading.isListItem ? heading.listItemOrNullObject.load('listString')
    : undefined));
  await context.sync();
  const numbered = numbers.some(item => item && !item.isNullObject);
  return [
    `Heading outline${numbered ? '; leading numbers are drawn by Word and are not in the text, so leave them out '
      + 'of old_text' : ''}:`,
    ...shown.map((heading, index) => {
      const number = numbers[index] && !numbers[index]!.isNullObject ? `${numbers[index]!.listString} ` : '';
      const { font } = heading;
      const look = [font.size && `${font.size}pt`, font.bold && 'bold', font.color, font.name && `"${font.name}"`]
        .filter(Boolean).join(' ');
      return `${'  '.repeat(headingLevel(heading)! - 1)}${number}${heading.text}  [${heading.styleBuiltIn === 'Other'
        ? heading.style : heading.styleBuiltIn}${look ? `; ${look}` : ''}]`;
    }),
    ...(headings.length > MAX_HEADINGS ? [`  … ${headings.length - MAX_HEADINGS} more headings not shown`] : []),
  ];
}

async function headersAndFooters(context: Word.RequestContext, sections: Word.Section[]): Promise<string[]> {
  const kinds = ['Primary', 'FirstPage', 'EvenPages'] as const;
  const parts = sections.flatMap((section, index) => kinds.flatMap(kind => [
    { index, kind, place: 'Header', body: section.getHeader(kind).load('text') },
    { index, kind, place: 'Footer', body: section.getFooter(kind).load('text') },
  ]));
  await context.sync();
  const lines = parts.filter(part => part.body.text.trim()).map(({ index, kind, place, body }) => (
    `  [sections.items[${index}]${kind === 'Primary' ? '' : ` ${kind}`} ${place}] ${quote(body.text)}`));
  return lines.length ? ['Headers and footers (not in the body; edit them through sections.items[i].getHeader or '
    + 'getFooter):', ...lines] : [];
}

async function commentThreads(context: Word.RequestContext, comments: Word.Comment[]): Promise<string[]> {
  if (!comments.length) return [];
  const shown = comments.slice(0, MAX_LISTED).map(comment => ({
    comment, anchor: comment.getRange().load('text'), replies: comment.replies.load('items/content,items/authorName'),
  }));
  await context.sync();
  const open = comments.filter(comment => !comment.resolved).length;
  return [
    `Comment threads: ${open} open, ${comments.length - open} resolved${comments.length > MAX_LISTED
      ? ` (${MAX_LISTED} shown)` : ''}`,
    '<untrusted_content source_type="comment">',
    ...shown.flatMap(({ comment, anchor, replies }) => [
      `  [${comment.id}]${comment.resolved ? ' [resolved]' : ''} (author: ${quote(comment.authorName || '(unknown)',
        MAX_NAME)}) on ${
        quote(anchor.text, 80)}: ${quote(comment.content)}${replies.items.length ? `, ${replies.items.length} `
        + `repl${replies.items.length === 1 ? 'y' : 'ies'}` : ''}`,
      ...replies.items.map(reply => `    ↳ ${quote(reply.authorName || '(unknown)', MAX_NAME)}: ${
        quote(reply.content)}`),
    ]),
    '</untrusted_content>',
  ];
}

/** What the user selected: a cursor, a place outside the body, part of one paragraph, or several paragraphs. */
async function selection(context: Word.RequestContext): Promise<string> {
  const range = context.document.getSelection().load('text');
  const paragraphs = range.paragraphs.load('items/text,items/styleBuiltIn,items/style,items/tableNestingLevel,'
    + 'items/isListItem');
  const where = range.parentBody.load('type');
  await context.sync();
  const [first] = paragraphs.items;
  const style = first ? `style "${first.styleBuiltIn === 'Other' ? first.style : first.styleBuiltIn}"` : '';
  let block: string;
  if (!BODY_TYPES.has(where.type)) {
    block = `The selection is in the document's ${where.type.toLowerCase()}, not the body; its text is not shown `
      + 'here. Read it through the matching Word API, or ask the user to paste it.';
  } else if (!range.text) {
    block = `Cursor${style ? ` (${style})` : ''}; no text selected.`;
  } else {
    const facts = [`${paragraphs.items.length} paragraph${paragraphs.items.length === 1 ? '' : 's'}`];
    if (style) facts.push(`starts in ${style}`);
    if (paragraphs.items.some(paragraph => paragraph.tableNestingLevel > 0)) facts.push('inside a table');
    if (paragraphs.items.some(paragraph => paragraph.isListItem)) facts.push('in a list');
    const structure = `Structure: ${facts.join(', ')}`;
    if (paragraphs.items.length > 1) {
      block = `${structure}\nContent not included; read it with context.document.getSelection() in execute_office_js.`;
    } else {
      const whole = first.text.length > MAX_SELECTION ? `${first.text.slice(0, MAX_SELECTION)}… (cut at `
        + `${MAX_SELECTION} of ${first.text.length} characters)` : first.text;
      block = range.text.length < first.text.length - 10
        ? `${structure}\nThe user highlighted:\n${range.text}\nIn the paragraph (search a phrase of it to reach the `
          + `text):\n${whole}`
        : `${structure}\nSelected text (the whole paragraph):\n${whole}`;
    }
  }
  return `<user_selection>\n${block}\n</user_selection>`;
}
