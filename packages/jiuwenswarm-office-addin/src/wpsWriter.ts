/**
 * The Word object model the Word tools use, over WPS Writer's VBA-style object model, so ``wordTools``,
 * ``wordState`` and ``docEditReview`` run unchanged in WPS. WPS calls are synchronous and land at once: ``load`` is
 * a no-op, and ``sync`` yields one macrotask so the pane can paint. A WPS range is live, as an Office.js range is:
 * it moves with the edits before it. Only ``wpsWordRun`` casts to the Word types.
 *
 * WPS gives paragraphs, comments and lists no ids. A paragraph's ``uniqueLocalId`` is where it starts, which holds
 * within one ``Word.run`` that reads; a comment's ``id`` hashes its author, date and text; a list's ``id`` names a
 * paragraph of it within one ``Word.run``. WPS raises no paragraph change events, so the host offers no
 * ``WordApi`` set and messages carry no ``<user_changes>``.
 */
import type { OfficeHost } from './appContract';
import type { DocumentSettings } from './documentIdentity';
import type { WordRun } from './wordPassage';
import type { WpsObject } from './wpsDocument';
import { WpsFiles } from './wpsFiles';

/** Shapes anchored to a paragraph and a section's text columns, which WPS has; not the paragraph events. */
export const WPS_WRITER_HOST: OfficeHost = { isSetSupported: name => name === 'WordApiDesktop', worker: true };

/** ``Word.run`` over a WPS document. */
export function wpsWordRun(document: WpsObject): WordRun {
  let styles: Map<string, string> | undefined;
  const builtIn = () => (styles ??= builtInStyles(document));
  return async batch => batch(new Context(document, builtIn) as unknown as Word.RequestContext);
}

/** ``wdUndefined``: what a property reads when only some of the range has it. */
const MIXED = 9999999;
const AUTOMATIC_COLOR = -16777216;
const WITHIN_TABLE = 12;
const EXPORT_PDF = 17;
/** ``WdBuiltinStyle``, by the name Office.js's ``styleBuiltIn`` gives it. */
const BUILT_IN_STYLES: Record<string, number> = {
  Normal: -1, Heading1: -2, Heading2: -3, Heading3: -4, Heading4: -5, Heading5: -6, Heading6: -7, Heading7: -8,
  Heading8: -9, Heading9: -10, Title: -63, Subtitle: -75, ListParagraph: -180, Quote: -181, IntenseQuote: -182,
};
const ALIGNMENTS = ['Left', 'Centered', 'Right', 'Justified'];
const REVISION_TYPES: Record<number, string> = { 1: 'Added', 2: 'Deleted' };
const HEADER_KINDS: Record<string, number> = { Primary: 1, FirstPage: 2, EvenPages: 3 };
/** ``WdStoryType`` as ``Word.BodyType``. */
const STORY_BODIES: Record<number, string> = {
  2: 'Footnote', 3: 'Endnote', 5: 'Shape', 6: 'Header', 7: 'Header', 8: 'Footer', 9: 'Footer', 10: 'Header',
  11: 'Footer',
};
const CONTROL_TYPES = ['RichText', 'PlainText', 'Picture', 'ComboBox', 'DropDownList', 'BuildingBlockGallery',
  'DatePicker', 'Group', 'CheckBox', 'RepeatingSection'];

/** Each built-in style's name in WPS's display language (``标题 1``), as the name Office.js gives it. */
function builtInStyles(document: WpsObject): Map<string, string> {
  const names = new Map<string, string>();
  for (const [name, index] of Object.entries(BUILT_IN_STYLES)) {
    try {
      names.set(styleName(document.Styles.Item(index)), name);
    } catch {
      // A build without this built-in style shows paragraphs in it by name.
    }
  }
  return names;
}

/** WPS gives a paragraph's style as a ``Style`` or as its name. */
function styleName(style: unknown): string {
  return typeof style === 'string' ? style : String((style as WpsObject).NameLocal);
}

/** One ``Word.run``: the WPS document, its built-in style names, and the lists named so far. */
class Context {
  readonly document: Document;
  private readonly lists: WpsObject[] = [];

  constructor(readonly wps: WpsObject, readonly builtIn: () => Map<string, string>) {
    this.document = new Document(this);
  }

  range(start: number, end: number): Range {
    return new Range(this, this.wps.Range(start, end));
  }

  listId(paragraph: WpsObject): number {
    return this.lists.push(paragraph) - 1;
  }

  listParagraph(id: number): WpsObject {
    return this.lists[id];
  }

  async sync(): Promise<void> {
    await new Promise(resolve => setTimeout(resolve, 0));
  }
}

class Loadable {
  isNullObject = false;

  load(): this {
    return this;
  }
}

class Collection<T> extends Loadable {
  private loaded: T[] | undefined;

  constructor(private readonly read: () => T[]) {
    super();
  }

  get items(): T[] {
    this.loaded ??= this.read();
    return this.loaded;
  }

  getFirst(): T {
    return this.items[0];
  }
}

/** A collection only counted: whether a range holds pictures, fields, notes, shapes or comments. */
function counted(collection: WpsObject): Collection<object> {
  return new Collection(() => Array.from({ length: Number(collection.Count) }, () => ({})));
}

class Document extends Loadable {
  readonly body: Body;

  constructor(private readonly context: Context) {
    super();
    this.body = new Body(context);
  }

  get sections(): Collection<Section> {
    return new Collection(() => itemsOf(this.context.wps.Sections).map(section => new Section(section)));
  }

  get changeTrackingMode(): string {
    return truthy(this.context.wps.TrackRevisions) ? 'TrackAll' : 'Off';
  }

  set changeTrackingMode(mode: string) {
    this.context.wps.TrackRevisions = mode !== 'Off';
  }

  getSelection(): Range {
    return new Range(this.context, this.context.wps.ActiveWindow.Selection.Range);
  }

  getBookmarkRangeOrNullObject(name: string): Range {
    const { wps } = this.context;
    if (truthy(wps.Bookmarks.Exists(name))) return new Range(this.context, wps.Bookmarks.Item(name).Range);
    const nothing = this.context.range(0, 0);
    nothing.isNullObject = true;
    return nothing;
  }

  deleteBookmark(name: string): void {
    const bookmarks = this.context.wps.Bookmarks;
    if (truthy(bookmarks.Exists(name))) bookmarks.Item(name).Delete();
  }
}

class Body extends Loadable {
  constructor(private readonly context: Context) {
    super();
  }

  private get content(): Range {
    return new Range(this.context, this.context.wps.Content);
  }

  /** ``Paragraphs.Item(i)`` walks from the first paragraph each time, so the walk goes ``Next`` by ``Next``. */
  get paragraphs(): Collection<Paragraph> {
    return new Collection(() => {
      const paragraphs: Paragraph[] = [];
      for (let paragraph = this.context.wps.Paragraphs.First; paragraph; paragraph = paragraph.Next()) {
        paragraphs.push(new Paragraph(this.context, paragraph));
      }
      return paragraphs;
    });
  }

  get tables(): Collection<Table> {
    return new Collection(() => itemsOf(this.context.wps.Tables).map(table => new Table(table)));
  }

  get contentControls(): Collection<ContentControl> {
    return new Collection(() => itemsOf(this.context.wps.ContentControls).map(control => new ContentControl(control)));
  }

  get inlinePictures(): Collection<object> {
    return this.content.inlinePictures;
  }

  search(text: string, options?: { matchCase?: boolean }): Collection<Range> {
    return this.content.search(text, options);
  }

  getComments(): Collection<Comment> {
    return new Collection(() => itemsOf(this.context.wps.Comments).map(comment => new Comment(this.context, comment)));
  }

  getTrackedChanges(): TrackedChanges {
    return new TrackedChanges(this.context.wps.Revisions);
  }
}

class Paragraph extends Loadable {
  private fontOf: Font | undefined;

  /** ``wps`` is absent past the last paragraph, for ``getNextOrNullObject``. */
  constructor(private readonly context: Context, private readonly wps: WpsObject | null | undefined) {
    super();
    this.isNullObject = !wps;
  }

  private get range(): WpsObject {
    return this.wps!.Range;
  }

  get uniqueLocalId(): string {
    return String(this.range.Start);
  }

  get text(): string {
    return paragraphText(this.range.Text);
  }

  get style(): string {
    return styleName(this.wps!.Style);
  }

  get styleBuiltIn(): string {
    return this.context.builtIn().get(this.style) ?? 'Other';
  }

  get outlineLevel(): number {
    return Number(this.wps!.OutlineLevel);
  }

  get isListItem(): boolean {
    return Number(this.range.ListFormat.ListType) !== 0;
  }

  get tableNestingLevel(): number {
    const tables = this.range.Tables;
    return Number(tables.Count) ? Number(tables.Item(1).NestingLevel) : 0;
  }

  get alignment(): string {
    return ALIGNMENTS[Number(this.wps!.Alignment)] ?? (Number(this.wps!.Alignment) === MIXED ? 'Mixed' : 'Justified');
  }

  get leftIndent(): number {
    return Number(this.wps!.LeftIndent);
  }

  get firstLineIndent(): number {
    return Number(this.wps!.FirstLineIndent);
  }

  get lineSpacing(): number {
    return Number(this.wps!.LineSpacing);
  }

  get font(): Font {
    this.fontOf ??= new Font(this.range.Font);
    return this.fontOf;
  }

  get inlinePictures() { return counted(this.range.InlineShapes); }
  get fields() { return counted(this.range.Fields); }
  get footnotes() { return counted(this.range.Footnotes); }
  get endnotes() { return counted(this.range.Endnotes); }
  get contentControls() { return counted(this.range.ContentControls); }
  get shapes() { return counted(this.range.ShapeRange); }

  get listItemOrNullObject(): Loadable & { listString: string; level: number } {
    const format = this.range.ListFormat;
    const listed = Number(format.ListType) !== 0;
    return Object.assign(new Loadable(), { isNullObject: !listed, listString: listed ? String(format.ListString) : '',
      level: listed ? Number(format.ListLevelNumber) - 1 : 0 });
  }

  get listOrNullObject(): Loadable & { id: number } {
    return Object.assign(new Loadable(), { isNullObject: !this.isListItem, id: this.context.listId(this.wps!) });
  }

  /** The paragraph without its mark, whatever ``location`` names: the tools ask only for its content. */
  getRange(location: 'Whole' | 'Start' | 'End' | 'Content' = 'Whole'): Range {
    const { Start, End } = this.range;
    return this.context.range(Number(Start), Number(End) - 1).getRange(location);
  }

  /** The text with pending tracked deletions taken out, as ``getReviewedText('Current')`` gives it. */
  getReviewedText(): { value: string } {
    const { range } = this;
    const [start, end] = [Number(range.Start), Number(range.End)];
    const deleted = itemsOf(range.Revisions).filter(revision => Number(revision.Type) === 2)
      .map(revision => [Math.max(Number(revision.Range.Start), start) - start,
        Math.min(Number(revision.Range.End), end) - start]).sort((a, b) => b[0] - a[0]);
    let text = String(range.Text);
    for (const [from, to] of deleted) text = text.slice(0, from) + text.slice(to);
    return { value: paragraphText(text) };
  }

  getComments(): Collection<object> {
    return counted(this.range.Comments);
  }

  getTrackedChanges(): TrackedChanges {
    return new TrackedChanges(this.range.Revisions);
  }

  getNextOrNullObject(): Paragraph {
    return new Paragraph(this.context, this.wps?.Next());
  }

  /** A new paragraph of ``text`` next to this one; WPS gives it this one's formatting, a list's included. */
  insertParagraph(text: string, where: 'Before' | 'After'): Paragraph {
    const { range } = this;
    const at = Number(where === 'After' ? range.End : range.Start);
    if (where === 'After') range.InsertParagraphAfter();
    else range.InsertParagraphBefore();
    const inserted = this.context.wps.Range(at, at);
    inserted.InsertAfter(wpsText(text));
    return new Paragraph(this.context, inserted.Paragraphs.Item(1));
  }

  /** The list exists once its first level has a look: WPS has no list without bullets or numbers. */
  startNewList(): List {
    return new List(this.context, this.wps!);
  }

  attachToList(id: number, level: number): void {
    const format = this.range.ListFormat;
    format.ApplyListTemplate(this.context.listParagraph(id).Range.ListFormat.ListTemplate, true);
    if (level > 0) format.ListLevelNumber = level + 1;
  }

  delete(): void {
    this.range.Delete();
  }
}

class List extends Loadable {
  readonly id: number;

  constructor(context: Context, private readonly paragraph: WpsObject) {
    super();
    this.id = context.listId(paragraph);
  }

  setLevelBullet(): void {
    this.paragraph.Range.ListFormat.ApplyBulletDefault();
  }

  setLevelNumbering(): void {
    this.paragraph.Range.ListFormat.ApplyNumberDefault();
  }
}

class Range extends Loadable {
  constructor(private readonly context: Context, private readonly wps: WpsObject) {
    super();
  }

  private get start(): number {
    return Number(this.wps.Start);
  }

  private get end(): number {
    return Number(this.wps.End);
  }

  /** Paragraphs end in ``\r``, as Word's own text does. */
  get text(): string {
    return String(this.wps.Text);
  }

  get paragraphs(): Collection<Paragraph> {
    return new Collection(() => itemsOf(this.wps.Paragraphs).map(paragraph => new Paragraph(this.context, paragraph)));
  }

  get parentBody(): Loadable & { type: string } {
    const story = Number(this.wps.StoryType);
    const type = story === 1 ? (truthy(this.wps.Information(WITHIN_TABLE)) ? 'TableCell' : 'MainDoc')
      : STORY_BODIES[story] ?? 'Unknown';
    return Object.assign(new Loadable(), { type });
  }

  get fields() { return counted(this.wps.Fields); }
  get footnotes() { return counted(this.wps.Footnotes); }
  get endnotes() { return counted(this.wps.Endnotes); }
  get inlinePictures() { return counted(this.wps.InlineShapes); }

  /**
   * Each match of ``text`` in the range, left to right and not overlapping; ``^^`` finds ``^``, as in Word. Find
   * passes over a match that is the whole range it searches, so a range that is one is taken as it is.
   */
  search(text: string, { matchCase = false }: { matchCase?: boolean } = {}): Collection<Range> {
    const literal = text.replace(/\^\^/g, '^');
    const same = (found: string) => (matchCase ? found === literal : found.toLowerCase() === literal.toLowerCase());
    return new Collection(() => {
      const hits: Range[] = [];
      const { end } = this;
      for (let from = this.start; text && from < end;) {
        const scope = this.context.wps.Range(from, end);
        if (end - from === literal.length) {
          if (same(String(scope.Text))) hits.push(new Range(this.context, scope));
          break;
        }
        const find = scope.Find;
        // Find's options outlast the call, in the Find dialog too, so each is set.
        Object.assign(find, { Text: text, MatchCase: matchCase, MatchWholeWord: false, MatchWildcards: false,
          Forward: true, Wrap: 0, Format: false });
        if (!truthy(find.Execute()) || Number(scope.End) > end || Number(scope.End) <= from) break;
        hits.push(new Range(this.context, scope));
        from = Number(scope.End);
      }
      return hits;
    });
  }

  expandTo(other: Range): Range {
    return this.context.range(Math.min(this.start, other.start), Math.max(this.end, other.end));
  }

  getRange(location: 'Whole' | 'Start' | 'End' | 'Content' = 'Whole'): Range {
    const { start, end } = this;
    if (location === 'Start') return this.context.range(start, start);
    if (location === 'End') return this.context.range(end, end);
    return this.context.range(start, end);
  }

  /**
   * The inserted text's range. A replacement goes in before the old text, which is then cleared: setting a range's
   * text leaves where a tracked insertion lands to WPS, and ``Delete`` may take a neighbouring space with it, as
   * Word's smart cut does.
   */
  insertText(text: string, where: 'Replace' | 'After' | 'Before' | 'Start' | 'End'): Range {
    const value = wpsText(text);
    const { start, end } = this;
    const at = where === 'After' || where === 'End' ? end : start;
    const inserted = this.context.wps.Range(at, at);
    inserted.InsertAfter(value);
    if (where === 'Replace' && end > start) this.context.wps.Range(start + value.length, end + value.length).Text = '';
    return new Range(this.context, inserted);
  }

  insertBookmark(name: string): void {
    this.context.wps.Bookmarks.Add(name, this.context.wps.Range(this.start, this.end));
  }

  insertComment(text: string): Comment {
    return new Comment(this.context, this.context.wps.Comments.Add(this.context.wps.Range(this.start, this.end), text));
  }

  getTrackedChanges(): TrackedChanges {
    return new TrackedChanges(this.wps.Revisions);
  }
}

class Font extends Loadable {
  constructor(private readonly wps: WpsObject) {
    super();
  }

  get name(): string { return String(this.wps.Name ?? ''); }
  get size(): number | null { return mixed(this.wps.Size) ? null : Number(this.wps.Size); }
  get bold(): boolean | null { return flag(this.wps.Bold); }
  get italic(): boolean | null { return flag(this.wps.Italic); }
  get strikeThrough(): boolean | null { return flag(this.wps.StrikeThrough); }

  get underline(): string | null {
    const value = Number(this.wps.Underline);
    return value === MIXED ? null : value === 0 ? 'None' : 'Single';
  }

  get color(): string | null {
    const value = Number(this.wps.Color);
    if (value === MIXED || value === AUTOMATIC_COLOR || value < 0 || value > 0xffffff) return null;
    const rgb = ((value & 0xff) << 16) | (value & 0xff00) | (value >> 16);
    return `#${rgb.toString(16).padStart(6, '0').toUpperCase()}`;
  }
}

class TrackedChanges extends Collection<TrackedChange> {
  constructor(private readonly wps: WpsObject) {
    super(() => itemsOf(wps).map(revision => new TrackedChange(revision)));
  }

  rejectAll(): void {
    this.wps.RejectAll();
  }
}

class TrackedChange extends Loadable {
  constructor(private readonly wps: WpsObject) {
    super();
  }

  get type(): string { return REVISION_TYPES[Number(this.wps.Type)] ?? 'Formatted'; }
  get author(): string { return String(this.wps.Author ?? ''); }
  get date(): Date { return dateOf(this.wps.Date); }
  get text(): string { return String(this.wps.Range.Text); }
}

/** A comment. WPS has no replies or resolved state on some builds: there are none, and it is open. */
class Comment extends Loadable {
  constructor(private readonly context: Context, private readonly wps: WpsObject) {
    super();
  }

  get id(): string {
    let hash = 2166136261;
    for (const char of `${this.authorName}\n${String(this.wps.Date)}\n${this.content}`) {
      hash = Math.imul(hash ^ char.charCodeAt(0), 16777619) >>> 0;
    }
    return `c${hash.toString(36)}`;
  }

  get content(): string { return String(this.wps.Range.Text); }
  get authorName(): string { return String(this.wps.Author ?? ''); }
  get resolved(): boolean { return truthy(this.wps.Done); }

  get replies(): Collection<{ content: string; authorName: string }> {
    const replies = this.wps.Replies;
    return new Collection(() => (replies ? itemsOf(replies).map(reply => ({ content: String(reply.Range.Text),
      authorName: String(reply.Author ?? '') })) : []));
  }

  getRange(): Range {
    return new Range(this.context, this.wps.Scope);
  }

  delete(): void {
    this.wps.Delete();
  }
}

class Section extends Loadable {
  constructor(private readonly wps: WpsObject) {
    super();
  }

  get pageSetup(): { textColumns: Collection<object> } {
    return { textColumns: counted(this.wps.PageSetup.TextColumns) };
  }

  getHeader(kind: string): Loadable & { text: string } {
    return Object.assign(new Loadable(), { text: paragraphText(this.wps.Headers.Item(HEADER_KINDS[kind]).Range.Text) });
  }

  getFooter(kind: string): Loadable & { text: string } {
    return Object.assign(new Loadable(), { text: paragraphText(this.wps.Footers.Item(HEADER_KINDS[kind]).Range.Text) });
  }
}

/** A table's rows by their cells: ``Rows`` cannot be read in a table with vertically merged cells. */
class Table extends Loadable {
  constructor(private readonly wps: WpsObject) {
    super();
  }

  get rows(): Collection<{ cellCount: number }> {
    return new Collection(() => {
      const counts: number[] = [];
      for (const cell of itemsOf(this.wps.Range.Cells)) {
        const row = Number(cell.RowIndex) - 1;
        counts[row] = (counts[row] ?? 0) + 1;
      }
      return Array.from(counts, cellCount => ({ cellCount: cellCount ?? 0 }));
    });
  }
}

class ContentControl extends Loadable {
  constructor(private readonly wps: WpsObject) {
    super();
  }

  get title(): string { return String(this.wps.Title ?? ''); }
  get tag(): string { return String(this.wps.Tag ?? ''); }
  get type(): string { return CONTROL_TYPES[Number(this.wps.Type)] ?? 'Unknown'; }
  get text(): string { return paragraphText(this.wps.Range.Text); }

  get placeholderText(): string {
    const placeholder = this.wps.PlaceholderText;
    return typeof placeholder === 'string' ? placeholder : String(placeholder?.Value ?? '');
  }
}

/**
 * The document's own settings, kept in its document variables: a custom property holds 255 characters, too few
 * for the suggestion cards' state. A variable WPS changes is saved with the document.
 */
export function wpsDocumentSettings(document: WpsObject): DocumentSettings {
  const variables = document.Variables;
  const find = (name: string) => itemsOf(variables).find(variable => variable.Name === name);
  return {
    get: name => {
      const variable = find(name);
      if (!variable) return undefined;
      try {
        return JSON.parse(String(variable.Value));
      } catch {
        return undefined;
      }
    },
    set: (name, value) => {
      const text = JSON.stringify(value);
      const variable = find(name);
      if (variable) variable.Value = text;
      else variables.Add(name, text);
    },
    saveAsync: callback => callback({ status: 'succeeded' }),
  };
}

/** The document as PDF bytes, for ``verify_doc_visual``: exported to a temporary file, removed after. */
export function wpsDocumentPdf(document: WpsObject, application: WpsObject): Promise<Uint8Array> {
  return new WpsFiles(application, 'WPS Writer').written('jiuwenswarm-pages', '.pdf',
    path => document.ExportAsFixedFormat(path, EXPORT_PDF));
}

/** The members of a WPS collection, ``Item(1)`` to ``Item(Count)``. */
function itemsOf(collection: WpsObject): WpsObject[] {
  return Array.from({ length: Number(collection.Count) }, (_item, at) => collection.Item(at + 1));
}

/** A paragraph's text without its mark, or a table cell's end mark. */
function paragraphText(text: unknown): string {
  return String(text).replace(/[\r\x07]+$/, '');
}

/** Office.js's line breaks as WPS's paragraph marks. */
function wpsText(text: string): string {
  return text.replace(/\r\n|\n/g, '\r');
}

/** A VBA boolean: ``msoTrue`` is -1. */
function truthy(value: unknown): boolean {
  return value === true || value === -1 || value === 1;
}

function mixed(value: unknown): boolean {
  return Number(value) === MIXED;
}

function flag(value: unknown): boolean | null {
  return mixed(value) ? null : truthy(value);
}

/** A revision's date: WPS gives a ``Date``, an OLE date (days since 1899-12-30) or a string. */
function dateOf(value: unknown): Date {
  const date = value instanceof Date ? value
    : typeof value === 'number' ? new Date(Math.round((value - 25569) * 86_400_000)) : new Date(String(value));
  return Number.isNaN(date.getTime()) ? new Date(0) : date;
}
