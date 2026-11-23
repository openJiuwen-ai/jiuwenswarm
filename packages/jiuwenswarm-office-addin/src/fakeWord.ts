/**
 * A document that a fake ``Word.run`` reads and writes, for the tests of the Word tools, state, cards and code
 * runner. The body is its paragraphs' texts joined by line breaks; a range is a span of that text, kept in place as
 * edits land before it, as Word keeps its ranges. Changes land at once and loaded properties are readable without a
 * ``sync``. With Track Changes on, an edit is recorded as one revision that ``rejectAll`` undoes; the deleted words
 * leave the text at once. Only the Office.js surface the pane uses is here, as classes, as the host's objects are.
 */
import type { WordRun } from './wordPassage';

type Inline = 'pictures' | 'fields' | 'footnotes' | 'endnotes' | 'controls' | 'shapes';

export interface FakeParagraphSpec {
  text: string;
  style?: string;
  list?: { kind: 'bullet' | 'number'; id: number; level?: number };
  table?: number;
  /** Where in the text each picture, field or note reference sits. */
  inline?: { at: number; kind: Inline }[];
  bold?: boolean;
  font?: string;
}

interface Para extends FakeParagraphSpec {
  id: string;
}

type Handler = (event: Record<string, unknown>) => Promise<void>;

class FakeEvent {
  readonly handlers: Handler[] = [];
  add(handler: Handler): void {
    this.handlers.push(handler);
  }
}

class Loadable {
  isNullObject = false;
  load(): this {
    return this;
  }
}

class Collection<T> extends Loadable {
  constructor(readonly items: T[]) {
    super();
  }

  getFirst(): T {
    return this.items[0];
  }
}

class Revision extends Loadable {
  constructor(readonly document: FakeDocument, readonly range: FakeRange, readonly removed: string,
    readonly author = 'WorkSwarm', readonly date = new Date('2026-11-14T00:00:00Z')) {
    super();
  }

  get type(): string {
    return this.range.text ? 'Added' : 'Deleted';
  }

  get text(): string {
    return this.range.text || this.removed;
  }
}

class Revisions extends Collection<Revision> {
  rejectAll(): void {
    for (const revision of [...this.items].reverse()) {
      revision.document.edit(revision.range.start, revision.range.end, revision.removed, false);
      revision.document.revisions.splice(revision.document.revisions.indexOf(revision), 1);
    }
  }
}

export class FakeComment extends Loadable {
  resolved = false;
  readonly replies = new Collection<{ content: string; authorName: string }>([]);

  constructor(readonly document: FakeDocument, readonly id: string, readonly content: string,
    readonly range: FakeRange, readonly authorName = 'A. Chen') {
    super();
  }

  getRange(): FakeRange {
    return this.range;
  }

  delete(): void {
    this.document.comments.splice(this.document.comments.indexOf(this), 1);
  }
}

export class FakeRange extends Loadable {
  constructor(readonly document: FakeDocument, public start: number, public end: number) {
    super();
    document.ranges.add(this);
  }

  get text(): string {
    return this.document.text.slice(this.start, this.end);
  }

  get paragraphs(): Collection<FakeParagraph> {
    return new Collection(this.document.paragraphsIn(this.start, this.end));
  }

  get parentBody(): Loadable & { type: string } {
    return Object.assign(new Loadable(), { type: 'MainDoc' });
  }

  get fields() { return this.inline('fields'); }
  get footnotes() { return this.inline('footnotes'); }
  get endnotes() { return this.inline('endnotes'); }
  get inlinePictures() { return this.inline('pictures'); }

  private inline(kind: Inline): Collection<object> {
    return new Collection(this.document.inlineAt(this.start, this.end, kind));
  }

  /** As Word searches: ``^^`` is ``^``; a lone ``^`` starts a special character, which no text here has. */
  search(pattern: string): Collection<FakeRange> {
    const found: FakeRange[] = [];
    if (/\^(?!\^)/.test(pattern.replace(/\^\^/g, ''))) return new Collection(found);
    const text = pattern.replace(/\^\^/g, '^');
    const span = this.text;
    for (let at = span.indexOf(text); at !== -1 && text; at = span.indexOf(text, at + text.length)) {
      found.push(new FakeRange(this.document, this.start + at, this.start + at + text.length));
    }
    return new Collection(found);
  }

  expandTo(other: FakeRange): FakeRange {
    return new FakeRange(this.document, Math.min(this.start, other.start), Math.max(this.end, other.end));
  }

  getRange(location: 'Start' | 'End' | 'Whole' | 'Content' = 'Whole'): FakeRange {
    if (location === 'Start') return new FakeRange(this.document, this.start, this.start);
    if (location === 'End') return new FakeRange(this.document, this.end, this.end);
    return new FakeRange(this.document, this.start, this.end);
  }

  insertText(text: string, where: 'Replace' | 'After' | 'Before' | 'Start' | 'End'): FakeRange {
    const [from, to] = where === 'Replace' ? [this.start, this.end]
      : where === 'After' || where === 'End' ? [this.end, this.end] : [this.start, this.start];
    this.document.edit(from, to, text);
    return new FakeRange(this.document, from, from + text.length);
  }

  insertField(): void {}

  insertBookmark(name: string): void {
    this.document.bookmarks.set(name, new FakeRange(this.document, this.start, this.end));
  }

  insertComment(text: string): FakeComment {
    const comment = new FakeComment(this.document, `c${this.document.comments.length + 1}`, text,
      new FakeRange(this.document, this.start, this.end), 'WorkSwarm');
    this.document.comments.push(comment);
    return comment;
  }

  getTrackedChanges(): Revisions {
    return new Revisions(this.document.revisions.filter(revision => revision.range.start <= this.end
      && revision.range.end >= this.start));
  }

  getReviewedText(): { value: string } {
    return { value: this.text };
  }
}

class FakeList extends Loadable {
  constructor(private readonly document: FakeDocument, readonly id: number) {
    super();
  }

  setLevelBullet(): void {
    this.document.paras.filter(para => para.list?.id === this.id).forEach(para => { para.list!.kind = 'bullet'; });
  }

  setLevelNumbering(): void {}
}

export class FakeParagraph extends Loadable {
  readonly outlineLevel = 10;
  readonly alignment = 'Left';
  readonly leftIndent = 0;
  readonly firstLineIndent = 0;
  readonly lineSpacing = 12;

  constructor(readonly document: FakeDocument, readonly uniqueLocalId: string) {
    super();
  }

  get spec(): Para {
    return this.document.paras.find(para => para.id === this.uniqueLocalId)!;
  }

  private get range(): FakeRange {
    const [start, end] = this.document.spanOf(this.uniqueLocalId);
    return new FakeRange(this.document, start, end);
  }

  get text(): string {
    return this.spec.text;
  }

  get styleBuiltIn(): string {
    const style = this.spec.style ?? 'Normal';
    return /^(?:Normal|Heading\d|Title|ListParagraph)$/.test(style) ? style : 'Other';
  }

  get style(): string {
    return this.spec.style ?? 'Normal';
  }

  /** A style by its name, which is in Word's display language: an English name is no list style here. */
  set style(style: string) {
    this.spec.style = style;
  }

  get isListItem(): boolean {
    return this.spec.list !== undefined;
  }

  get tableNestingLevel(): number {
    return this.spec.table ?? 0;
  }

  get font(): Loadable & Record<'name' | 'underline' | 'color', string> & { size: number; bold: boolean;
    italic: boolean; strikeThrough: boolean; } {
    return Object.assign(new Loadable(), { name: this.spec.font ?? 'Calibri', size: 11, bold: this.spec.bold ?? false,
      italic: false, underline: 'None', strikeThrough: false, color: '#000000' });
  }

  get listItemOrNullObject(): Loadable & { listString: string; level: number } {
    const { list } = this.spec;
    if (!list) return Object.assign(new Loadable(), { isNullObject: true, listString: '', level: 0 });
    const index = this.document.paras.filter(para => para.list?.id === list.id).indexOf(this.spec);
    return Object.assign(new Loadable(), { listString: list.kind === 'bullet' ? '•' : `${index + 1}.`,
      level: list.level ?? 0 });
  }

  get listOrNullObject(): Loadable & { id: number } {
    return Object.assign(new Loadable(), { isNullObject: !this.spec.list, id: this.spec.list?.id ?? 0 });
  }

  get inlinePictures() { return this.inline('pictures'); }
  get fields() { return this.inline('fields'); }
  get footnotes() { return this.inline('footnotes'); }
  get endnotes() { return this.inline('endnotes'); }
  get contentControls() { return this.inline('controls'); }
  get shapes() { return this.inline('shapes'); }

  private inline(kind: Inline): Collection<object> {
    return new Collection((this.spec.inline ?? []).filter(item => item.kind === kind));
  }

  getRange(location: 'Start' | 'End' | 'Whole' | 'Content' = 'Whole'): FakeRange {
    return this.range.getRange(location);
  }

  search(text: string): Collection<FakeRange> {
    return this.range.search(text);
  }

  getReviewedText(): { value: string } {
    return { value: this.text };
  }

  getComments(): Collection<FakeComment> {
    const [start, end] = this.document.spanOf(this.uniqueLocalId);
    return new Collection(this.document.comments.filter(comment => comment.range.start >= start
      && comment.range.end <= end && comment.range.end > comment.range.start));
  }

  getTrackedChanges(): Revisions {
    return this.range.getTrackedChanges();
  }

  getNextOrNullObject(): FakeParagraph {
    const next = this.document.paras[this.document.paras.indexOf(this.spec) + 1];
    const paragraph = new FakeParagraph(this.document, next?.id ?? 'none');
    paragraph.isNullObject = !next;
    return paragraph;
  }

  insertParagraph(text: string, where: 'Before' | 'After'): FakeParagraph {
    const index = this.document.paras.indexOf(this.spec);
    const para = { ...this.spec, text, id: this.document.newId(), inline: [] };
    this.document.paras.splice(where === 'After' ? index + 1 : index, 0, para);
    this.document.shift(this.document.spanOf(para.id)[0], text.length + 1);
    return new FakeParagraph(this.document, para.id);
  }

  startNewList(): FakeList {
    const id = this.document.paras.length + 100;
    this.spec.list = { kind: 'number', id };
    return new FakeList(this.document, id);
  }

  attachToList(id: number, level: number): void {
    const kind = this.document.paras.find(para => para.list?.id === id)?.list?.kind ?? 'number';
    this.spec.list = { kind, id, level };
  }

  delete(): void {
    const [start, end] = this.document.spanOf(this.uniqueLocalId);
    this.document.paras.splice(this.document.paras.indexOf(this.spec), 1);
    this.document.shift(start, -(end - start + 1));
  }
}

class Body extends Loadable {
  readonly tables: Collection<{ rows: Collection<{ cellCount: number }> }>;
  readonly contentControls = new Collection<object>([]);

  constructor(private readonly document: FakeDocument, tables: number[][]) {
    super();
    this.tables = new Collection(tables.map(rows => ({
      rows: new Collection(rows.map(cellCount => ({ cellCount }))) })));
  }

  get paragraphs(): Collection<FakeParagraph> {
    return new Collection(this.document.paras.map(para => new FakeParagraph(this.document, para.id)));
  }

  get inlinePictures(): Collection<object> {
    return this.whole().inlinePictures;
  }

  private whole(): FakeRange {
    return new FakeRange(this.document, 0, this.document.text.length);
  }

  search(text: string): Collection<FakeRange> {
    return this.whole().search(text);
  }

  getComments(): Collection<FakeComment> {
    return new Collection([...this.document.comments]);
  }

  insertOoxml(): void {}

  insertFileFromBase64(): void {}

  getTrackedChanges(): Revisions {
    return new Revisions([...this.document.revisions]);
  }
}

/** For the code runner's screen: the shared templates, another document, outside content. */
class Template extends Loadable {
  save(): void {}
}

class Application extends Loadable {
  readonly templates = new Collection([new Template()]);

  createDocument(): Loadable {
    return new Loadable();
  }
}

class Context {
  readonly application = new Application();

  constructor(readonly document: FakeDocument) {}

  async sync(): Promise<void> {}
}

class Section extends Loadable {
  readonly pageSetup = { textColumns: new Collection<object>([]) };

  constructor(private readonly header: string) {
    super();
  }

  getHeader(kind: string): Loadable & { text: string } {
    return Object.assign(new Loadable(), { text: kind === 'Primary' ? this.header : '' });
  }

  getFooter(): Loadable & { text: string } {
    return Object.assign(new Loadable(), { text: '' });
  }
}

export class FakeDocument extends Loadable {
  paras: Para[];
  readonly ranges = new Set<FakeRange>();
  readonly revisions: Revision[] = [];
  readonly comments: FakeComment[] = [];
  readonly bookmarks = new Map<string, FakeRange>();
  readonly body: Body;
  readonly sections: Collection<Section>;
  readonly onParagraphAdded = new FakeEvent();
  readonly onParagraphChanged = new FakeEvent();
  readonly onParagraphDeleted = new FakeEvent();
  changeTrackingMode = 'Off';
  selection: FakeRange;
  private ids = 0;

  constructor(paragraphs: (string | FakeParagraphSpec)[], { tables = [], header = '' }: {
    tables?: number[][]; header?: string;
  } = {}) {
    super();
    this.paras = paragraphs.map(item => ({ ...(typeof item === 'string' ? { text: item } : item), id: this.newId() }));
    this.body = new Body(this, tables);
    this.sections = new Collection([new Section(header)]);
    this.selection = new FakeRange(this, 0, 0);
  }

  /** ``Word.run`` on this document. */
  readonly run: WordRun = batch => batch(this.context as unknown as Word.RequestContext);

  readonly context = new Context(this);

  get text(): string {
    return this.paras.map(para => para.text).join('\n');
  }

  newId(): string {
    this.ids += 1;
    return `p${this.ids}`;
  }

  /** Where paragraph ``id``'s text starts and ends in the body's text. */
  spanOf(id: string): [number, number] {
    let start = 0;
    for (const para of this.paras) {
      if (para.id === id) return [start, start + para.text.length];
      start += para.text.length + 1;
    }
    throw new Error(`no paragraph ${id}`);
  }

  paragraphsIn(start: number, end: number): FakeParagraph[] {
    return this.paras.filter(para => {
      const [from, to] = this.spanOf(para.id);
      return to >= start && from <= end;
    }).map(para => new FakeParagraph(this, para.id));
  }

  inlineAt(start: number, end: number, kind: Inline): object[] {
    return this.paras.flatMap(para => (para.inline ?? []).filter(item => item.kind === kind).map(item => (
      this.spanOf(para.id)[0] + item.at))).filter(at => at >= start && at < end).map(() => ({}));
  }

  /** Replace ``[start, end)`` of the body's text, as Word does: line breaks make paragraphs, ranges move along. */
  edit(start: number, end: number, text: string, record = this.changeTrackingMode !== 'Off'): void {
    const removed = this.text.slice(start, end);
    const first = this.paragraphsIn(start, start)[0].spec;
    const last = this.paragraphsIn(end, end).at(-1)!.spec;
    const [firstStart] = this.spanOf(first.id);
    const [, lastEnd] = this.spanOf(last.id);
    const joined = this.text.slice(firstStart, start) + text + this.text.slice(end, lastEnd);
    const made = joined.split('\n').map((line, index) => ({ ...first, text: line,
      id: index === 0 ? first.id : this.newId(), inline: index === 0 ? first.inline : [] }));
    this.paras.splice(this.paras.indexOf(first), this.paras.indexOf(last) - this.paras.indexOf(first) + 1, ...made);
    const delta = text.length - (end - start);
    for (const range of this.ranges) {
      const move = (at: number, side: 'start' | 'end') => {
        if (at < end || (start === end && at === start)) {
          return at > start ? (side === 'start' ? start : start + text.length) : at;
        }
        return at + delta;
      };
      [range.start, range.end] = [move(range.start, 'start'), move(range.end, 'end')];
    }
    if (record) this.revisions.push(new Revision(this, new FakeRange(this, start, start + text.length), removed));
  }

  /** Move every range at or after ``from`` by ``delta`` characters, after a paragraph was added or removed. */
  shift(from: number, delta: number): void {
    for (const range of this.ranges) {
      if (range.start >= from) range.start += delta;
      if (range.end >= from) range.end += delta;
    }
  }

  getSelection(): FakeRange {
    return this.selection;
  }

  getParagraphByUniqueLocalId(id: string): FakeParagraph {
    return new FakeParagraph(this, id);
  }

  getBookmarkRangeOrNullObject(name: string): FakeRange {
    const range = this.bookmarks.get(name) ?? new FakeRange(this, 0, 0);
    range.isNullObject = !this.bookmarks.has(name);
    return range;
  }

  deleteBookmark(name: string): void {
    this.bookmarks.delete(name);
  }

  /** The user edits paragraph ``index``: its text changes and Word says so. */
  async userEdits(index: number, text: string, source = 'Local'): Promise<void> {
    this.edit(...this.spanOf(this.paras[index].id), text, false);
    await Promise.all(this.onParagraphChanged.handlers.map(handler => handler(
      { type: 'ParagraphChanged', source, uniqueLocalIds: [this.paras[index].id] })));
  }
}

/** ``Office.context.document.settings``, in memory. */
export class FakeSettings {
  readonly values = new Map<string, unknown>();

  get(name: string): unknown {
    return structuredClone(this.values.get(name));
  }

  set(name: string, value: unknown): void {
    this.values.set(name, structuredClone(value));
  }

  saveAsync(callback: (result: { status: unknown }) => void): void {
    callback({ status: 'succeeded' });
  }
}
