/**
 * A WPS Writer object model over a ``FakeDocument``, for the tests of the WPS Writer adapter: the VBA-style
 * members the adapter calls, writing into the fake document so a test reads the result as it does on Word. A
 * position counts the document's characters with one paragraph mark (``\r``) after each paragraph; ranges are the
 * fake's own, which move with edits. Style names come in WPS's Chinese display names. The PDF export and the
 * document variables live in ``files`` and ``variables``.
 */
import { FakeComment, type FakeDocument, FakeParagraph, FakeRange } from './fakeWord';
import type { WpsObject } from './wpsDocument';

/** ``WdBuiltinStyle`` index by the fake's style name, and the name WPS shows for it. */
const STYLES: Record<string, [number, string]> = {
  Normal: [-1, '正文'], Heading1: [-2, '标题 1'], Heading2: [-3, '标题 2'], Title: [-63, '标题'],
};
const local = (style: string) => STYLES[style]?.[1] ?? style;
const vba = (value: boolean) => (value ? -1 : 0);

export class FakeWpsWriter {
  readonly files = new Map<string, Uint8Array>();
  readonly variables = new Map<string, string>();
  readonly application: WpsObject;
  readonly document: WpsObject;

  constructor(readonly doc: FakeDocument, pdf = new Uint8Array([37, 80, 68, 70])) {
    const fake = this;
    this.application = { Env: { GetTempPath: () => '/wps/tmp' }, FileSystem: {
      ReadFileAsArrayBuffer: async (path: string) => {
        const bytes = this.files.get(path);
        if (!bytes) throw new Error(`no file ${path}`);
        return new Uint8Array(bytes).buffer;
      },
      unlinkSync: (path: string) => {
        if (!this.files.delete(path)) throw new Error(`no file ${path}`);
      },
    } };
    const variables = this.variables;
    this.document = {
      get Content() { return fake.range(new FakeRange(doc, 0, doc.text.length + 1)); },
      Range: (start: number, end: number) => this.range(new FakeRange(doc, start, end)),
      Paragraphs: { get First() { return fake.paragraph(doc.paras[0]?.id); } },
      get TrackRevisions(): number | boolean { return vba(doc.changeTrackingMode !== 'Off'); },
      set TrackRevisions(on: number | boolean) { doc.changeTrackingMode = on ? 'TrackAll' : 'Off'; },
      get Revisions() { return fake.revisions(doc.body.getTrackedChanges()); },
      get Comments() {
        return { ...collection(doc.comments.map(comment => fake.comment(comment))),
          Add: (range: WpsObject, text: string) => {
            const comment = new FakeComment(doc, `c${doc.comments.length + 1}`, text, fake.span(range), 'WorkSwarm');
            doc.comments.push(comment);
            return fake.comment(comment);
          } };
      },
      Bookmarks: {
        Exists: (name: string) => doc.bookmarks.has(name),
        Item: (name: string) => ({ Range: this.range(doc.bookmarks.get(name)!),
          Delete: () => doc.deleteBookmark(name) }),
        Add: (name: string, range: WpsObject) => { doc.bookmarks.set(name, this.span(range)); },
      },
      get Sections() {
        const section = doc.sections.items[0];
        const story = (text: string) => ({ Range: { Text: `${text}\r` } });
        return collection([{ PageSetup: { TextColumns: { Count: 1 } },
          Headers: { Item: (kind: number) => story(kind === 1 ? section.getHeader('Primary').text : '') },
          Footers: { Item: () => story('') } }]);
      },
      get Tables() {
        return collection(doc.body.tables.items.map(table => ({ Range: { Cells: collection(table.rows.items
          .flatMap((row, index) => Array.from({ length: row.cellCount }, () => ({ RowIndex: index + 1 })))) } })));
      },
      ContentControls: { Count: 0 },
      get ActiveWindow() { return { Selection: { Range: fake.range(doc.selection) } }; },
      Styles: { Item: (index: number) => {
        const found = Object.values(STYLES).find(([builtIn]) => builtIn === index);
        if (!found) throw new Error(`no style ${index}`);
        return { NameLocal: found[1] };
      } },
      Variables: {
        get Count() { return variables.size; },
        Item: (index: number) => {
          const name = [...variables.keys()][index - 1];
          return { Name: name, get Value() { return variables.get(name)!; },
            set Value(value: string) { variables.set(name, value); } };
        },
        Add: (name: string, value: string) => { variables.set(name, value); },
      },
      ExportAsFixedFormat: (path: string, format: number) => {
        if (format !== 17) throw new Error(`ExportAsFixedFormat format ${format}`);
        this.files.set(path, pdf);
      },
    };
  }

  /** The fake's own range for a WPS range the adapter hands back, clipped to the text. */
  private span(range: WpsObject): FakeRange {
    return new FakeRange(this.doc, range.Start, Math.min(range.End, this.doc.text.length));
  }

  private paragraph(id: string | undefined): WpsObject | null {
    if (!id) return null;
    const { doc } = this;
    const paragraph = new FakeParagraph(doc, id);
    const range = () => {
      const [start, end] = doc.spanOf(id);
      return this.range(new FakeRange(doc, start, end + 1));
    };
    return {
      get Range() { return range(); },
      get Style() { return { NameLocal: local(paragraph.style) }; },
      OutlineLevel: 10, Alignment: 0, LeftIndent: 0, FirstLineIndent: 0, LineSpacing: 12,
      Next: () => this.paragraph(doc.paras[doc.paras.indexOf(paragraph.spec) + 1]?.id),
    };
  }

  private range(range: FakeRange): WpsObject {
    const { doc } = this;
    const fake = this;
    const first = () => doc.paragraphsIn(range.start, range.start)[0] ?? doc.paragraphsIn(range.start, range.end)[0];
    const text = () => doc.text.slice(range.start, range.end).replace(/\n/g, '\r')
      + (range.end > doc.text.length ? '\r' : '');
    const insert = (value: string, at: number) => {
      const [start, end] = [range.start, range.end];
      doc.edit(at, at, value.replace(/\r/g, '\n'));
      [range.start, range.end] = [start, end + value.length];
    };
    const count = (kind: 'fields' | 'footnotes' | 'endnotes' | 'inlinePictures') => ({
      get Count() { return range[kind].items.length; } });
    return {
      get Start() { return range.start; },
      get End() { return range.end; },
      get Text() { return text(); },
      set Text(value: string) {
        doc.edit(range.start, Math.min(range.end, doc.text.length), value.replace(/\r/g, '\n'));
      },
      StoryType: 1,
      Information: () => vba((first()?.tableNestingLevel ?? 0) > 0),
      get Paragraphs() {
        const ids = doc.paragraphsIn(range.start, Math.max(range.start, range.end - 1)).map(item => item.uniqueLocalId);
        return collection(ids.map(id => fake.paragraph(id)!));
      },
      get Font() {
        const font = first().font;
        return { Name: font.name, Size: font.size, Bold: vba(font.bold), Italic: 0, Underline: 0, StrikeThrough: 0,
          Color: 0 };
      },
      get ListFormat() { return fake.listFormat(first()); },
      get Tables() {
        const level = first().tableNestingLevel;
        return { Count: level ? 1 : 0, Item: () => ({ NestingLevel: level }) };
      },
      Fields: Object.assign(count('fields'), { Add: () => { throw new Error('the fake adds no fields'); }, Update: () => 0 }),
      Footnotes: count('footnotes'), Endnotes: count('endnotes'),
      InlineShapes: count('inlinePictures'),
      get ContentControls() { return { Count: first().contentControls.items.length }; },
      get ShapeRange() { return { Count: first().shapes.items.length }; },
      get Comments() { return { Count: first().getComments().items.length }; },
      get Revisions() { return fake.revisions(range.getTrackedChanges()); },
      get Find() {
        const find: WpsObject = { Execute: () => {
          // As WPS, Find passes over a match that is the whole range it searches.
          const [hit] = range.search(String(find.Text)).items.filter(item => item.start !== range.start
            || item.end !== range.end);
          if (hit) [range.start, range.end] = [hit.start, hit.end];
          return Boolean(hit);
        } };
        return find;
      },
      InsertAfter: (value: string) => insert(value, range.end),
      InsertBefore: (value: string) => insert(value, range.start),
      InsertParagraphAfter: () => { first().insertParagraph('', 'After'); },
      InsertParagraphBefore: () => { first().insertParagraph('', 'Before'); },
      Delete: () => { first().delete(); },
    };
  }

  private listFormat(paragraph: FakeParagraph): WpsObject {
    const { list } = paragraph.spec;
    return {
      ListType: list ? (list.kind === 'bullet' ? 2 : 1) : 0,
      ListString: paragraph.listItemOrNullObject.listString,
      ListLevelNumber: (list?.level ?? 0) + 1,
      ListTemplate: { id: list?.id },
      ApplyBulletDefault: () => { paragraph.startNewList().setLevelBullet(); },
      ApplyNumberDefault: () => { paragraph.startNewList(); },
      ApplyListTemplate: (template: { id: number }) => paragraph.attachToList(template.id, 0),
    };
  }

  private revisions(revisions: ReturnType<FakeDocument['body']['getTrackedChanges']>): WpsObject {
    return { ...collection(revisions.items.map(revision => ({ Type: revision.type === 'Added' ? 1 : 2,
      Author: revision.author, Date: revision.date,
      Range: { Text: revision.text, Start: revision.range.start, End: revision.range.end } }))),
    RejectAll: () => revisions.rejectAll() };
  }

  private comment(comment: FakeComment): WpsObject {
    return { Author: comment.authorName, Date: '2026/11/14 09:30', Range: { Text: comment.content },
      Scope: this.range(comment.range), Delete: () => comment.delete() };
  }
}

/** A WPS collection: ``Count`` and ``Item(1)`` to ``Item(Count)``. */
function collection(items: WpsObject[]): WpsObject {
  return { Count: items.length, Item: (index: number) => items[index - 1] };
}
