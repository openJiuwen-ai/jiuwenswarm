/**
 * A WPS Presentation object model over a ``FakeDeck``, for the tests of the WPS adapter: the VBA-style members
 * the adapter calls, writing into the deck's slide and shape state so a test reads the result as it does on
 * PowerPoint (a colour lands as ``#RRGGBB``, a VBA boolean as -1 or 0). ``SaveCopyAs`` writes a real .pptx of
 * the deck and ``InsertFromFile`` reads one back through the deck's own package import, so a slide package round
 * trip runs for real. The files WPS and the adapter write live in ``files``.
 */
import { strFromU8, strToU8, unzipSync, zipSync } from 'fflate';

import { exportSlide, importSlides, type FakeDeck, type FakeShapeState, type FakeSlideState } from './fakePowerPoint';
import { base64Of, bytesOf } from './presentationPackage';
import type { WpsObject } from './wpsPowerPoint';

type Bag = Record<string, unknown>;

/** ``MsoShapeType`` of the fake's shape types. */
const SHAPE_TYPES: Record<string, number> = { GeometricShape: 1, Image: 13, Placeholder: 14, TextBox: 17, Table: 19 };
const SHAPE_NAMES = { Name: 'name', Left: 'left', Top: 'top', Width: 'width', Height: 'height', Rotation: 'rotation',
  AlternativeText: 'altTextDescription' };
const LINE_NAMES = { Visible: 'visible', Weight: 'weight', Transparency: 'transparency', DashStyle: 'dashStyle' };
const FONT_NAMES = { Name: 'name', Size: 'size', Bold: 'bold', Italic: 'italic' };
const FRAME_NAMES = { AutoSize: 'autoSize', VerticalAnchor: 'verticalAlignment', WordWrap: 'wordWrap',
  MarginLeft: 'leftMargin', MarginRight: 'rightMargin', MarginTop: 'topMargin', MarginBottom: 'bottomMargin' };
/** The design of a new deck is called "WPS" in WPS Presentation (12.1), where PowerPoint calls its theme "Office Theme". */
const designName = (name: string) => (name === 'Office Theme' ? 'WPS' : name);
/** ``PpBorderType`` as the side it names. */
const BORDERS: Record<number, string> = { 1: 'top', 2: 'left', 3: 'bottom', 4: 'right' };

export class FakeWps {
  readonly files = new Map<string, Uint8Array>();
  /** Each ``Slide.Export``'s pixel size. */
  readonly exports: { width: number; height: number }[] = [];
  /** Each ``Slides.InsertFromFile``'s position and source range. */
  readonly inserts: { index: number; first: number; last: number }[] = [];
  saves = 0;
  readonly application: WpsObject;
  readonly presentation: WpsObject;
  private nextId = 900;

  constructor(readonly deck: FakeDeck, private readonly png = strToU8('PNG')) {
    this.application = { Enum: {}, Env: { GetTempPath: () => '/wps/tmp' }, FileSystem: {
      writeAsBinaryString: (path: string, binary: string) => {
        this.files.set(path, Uint8Array.from(binary, char => char.charCodeAt(0)));
      },
      ReadFileAsArrayBuffer: async (path: string) => new Uint8Array(this.file(path)),
      unlinkSync: (path: string) => {
        if (!this.files.delete(path)) throw new Error(`no file ${path}`);
      },
    } };
    this.presentation = {
      PageSetup: { SlideWidth: deck.size.width, SlideHeight: deck.size.height },
      Slides: this.slides(),
      Designs: collection(deck.masters.map(master => ({ Name: designName(master.name),
        SlideMaster: { CustomLayouts: collection(master.layouts.map(layout => ({ Name: layout.name }))) } }))),
      SaveCopyAs: (path: string, format: number) => {
        if (format !== 24) throw new Error(`SaveCopyAs format ${format}`);
        this.saves += 1;
        this.files.set(path, wholeDeck(deck));
      },
    };
  }

  private file(path: string): Uint8Array {
    const bytes = this.files.get(path);
    if (!bytes) throw new Error(`no file ${path}`);
    return bytes;
  }

  private slides(): WpsObject {
    const { deck } = this;
    return {
      get Count() { return deck.slides.length; },
      Item: (index: number) => this.slide(deck.slides[index - 1]),
      Add: (index: number) => { deck.slides.splice(index - 1, 0, { id: `${this.nextId++}#`, shapes: [] }); },
      InsertFromFile: (path: string, index: number, first: number, last: number) => {
        this.inserts.push({ index, first, last });
        const inserted = importSlides(base64Of(this.file(path))).slides.slice(first - 1, last)
          .map(({ slide }) => ({ ...slide, id: `${this.nextId++}#` }));
        deck.slides.splice(index, 0, ...inserted);
        return inserted.length;
      },
    };
  }

  private slide(state: FakeSlideState): WpsObject {
    const { deck } = this;
    return {
      get SlideID() { return parseInt(state.id, 10); },
      get SlideIndex() { return deck.slides.indexOf(state) + 1; },
      Shapes: this.shapes(state),
      Delete: () => { deck.slides.splice(deck.slides.indexOf(state), 1); },
      MoveTo: (index: number) => {
        deck.slides.splice(deck.slides.indexOf(state), 1);
        deck.slides.splice(index - 1, 0, state);
      },
      Export: (path: string, _format: string, width: number, height: number) => {
        this.exports.push({ width, height });
        this.files.set(path, this.png);
      },
    };
  }

  private shapes(slide: FakeSlideState): WpsObject {
    const add = (shape: Partial<FakeShapeState>, [left, top, width, height]: number[]) => {
      const id = String(this.nextId++);
      const state: FakeShapeState = { id, name: `Shape ${id}`, type: 'TextBox', left, top, width, height, ...shape };
      slide.shapes.push(state);
      return this.shape(slide, state);
    };
    return {
      get Count() { return slide.shapes.length; },
      Item: (index: number) => this.shape(slide, slide.shapes[index - 1]),
      AddTextbox: (_orientation: number, ...box: number[]) => add({ type: 'TextBox', text: '' }, box),
      AddShape: (geometry: number, ...box: number[]) => add({ type: 'GeometricShape', geometry }, box),
      AddTable: (rows: number, columns: number, ...box: number[]) => add({ type: 'Table', rows, columns }, box),
      AddPicture: (path: string, _link: number, _save: number, ...box: number[]) =>
        add({ type: 'Image', fill: { image: base64Of(this.file(path)) } }, box),
    };
  }

  private shape(slide: FakeSlideState, state: FakeShapeState): WpsObject {
    const bag = (key: string): Bag => (state[key] ??= {}) as Bag;
    const api = format(state, SHAPE_NAMES, {
      get Id() { return Number(state.id); },
      get Type() { return SHAPE_TYPES[state.type]; },
      get HasTextFrame() { return state.type === 'Table' || state.type === 'Image' ? 0 : -1; },
      get HasTable() { return state.type === 'Table' ? -1 : 0; },
      HasChart: 0,
      Delete: () => { slide.shapes = slide.shapes.filter(shape => shape !== state); },
      ZOrder: (command: number) => { state.zOrder = command; },
      Fill: fill(bag('fill')),
      Line: format(bag('line'), LINE_NAMES, { ForeColor: color(bag('line')) }),
      TextFrame: format(bag('frame'), FRAME_NAMES, {
        get HasText() { return state.text ? -1 : 0; },
        TextRange: textRange(state, bag('font'), bag('paragraph')),
      }),
      get Table() { return table(state, bag); },
    });
    return api;
  }
}

/** A WPS collection: ``Count`` and ``Item(1)`` to ``Item(Count)``. */
function collection(items: WpsObject[]): WpsObject {
  return { Count: items.length, Item: (index: number) => items[index - 1] };
}

/**
 * A WPS object whose properties land in ``bag`` under the names the fake deck uses (``Weight`` as ``weight``);
 * ``own`` members stand in front.
 */
function format(bag: Bag, names: Record<string, string>, own: WpsObject = {}): WpsObject {
  return new Proxy(own, {
    get: (target, name: string) => (name in target ? target[name] : bag[names[name]]),
    set: (_target, name: string, value) => {
      bag[names[name]] = value;
      return true;
    },
  });
}

/** ``ForeColor`` or ``Color``: ``RGB`` is WPS's long, kept as ``#RRGGBB``. */
function color(bag: Bag): WpsObject {
  const swap = (value: number) => ((value & 0xff) << 16) | (value & 0xff00) | (value >> 16);
  return {
    get RGB() { return typeof bag.color === 'string' ? swap(parseInt(bag.color.slice(1), 16)) : -1; },
    set RGB(value: number) { bag.color = `#${swap(value).toString(16).padStart(6, '0').toUpperCase()}`; },
  };
}

function fill(bag: Bag): WpsObject {
  return format(bag, { Visible: 'visible', Transparency: 'transparency' }, {
    get Visible() { return bag.visible ?? (bag.color ? -1 : 0); },
    Type: 1,
    Solid: () => { bag.visible = -1; },
    ForeColor: color(bag),
  });
}

function textRange(state: Bag, font: Bag, paragraph: Bag): WpsObject {
  return {
    get Text() { return (state.text as string | undefined) ?? ''; },
    set Text(value: string) {
      state.text = value;
      delete state.paragraphs;
    },
    Font: format(font, FONT_NAMES, { Color: color(font) }),
    ParagraphFormat: format(paragraph, { Alignment: 'horizontalAlignment' }),
  };
}

/** A table's rows, columns and cells; a cell is ``cells['r,c']`` of the shape, 0-based as the fake deck keeps it. */
function table(state: FakeShapeState, bag: (key: string) => Bag): WpsObject {
  const cell = (row: number, column: number): WpsObject => {
    const cells = bag('cells');
    const value = (cells[`${row - 1},${column - 1}`] ??= { font: {}, fill: {}, borders: {} }) as Record<string, Bag>;
    const borders = value.borders as Record<string, Bag>;
    return {
      Shape: { Fill: fill(value.fill), TextFrame: format(value, { VerticalAnchor: 'verticalAlignment' }, {
        TextRange: textRange(value, value.font, value) }) },
      Borders: { Item: (side: number) => {
        const border = (borders[BORDERS[side]] ??= {});
        return format(border, LINE_NAMES, { ForeColor: color(border) });
      } },
    };
  };
  const sized = (count: number, key: string, name: string) => ({ Count: count, Item: (index: number) =>
    format(bag(key), { [name]: String(index - 1) }) });
  return { Cell: cell, Rows: sized(state.rows as number, 'rowHeights', 'Height'),
    Columns: sized(state.columns as number, 'columnWidths', 'Width') };
}

/** The deck as one .pptx, as ``SaveCopyAs`` writes it: each slide's export merged into the first's package. */
function wholeDeck(deck: FakeDeck): Uint8Array {
  const exports = deck.slides.map(slide => unzipSync(bytesOf(exportSlide(slide, deck))));
  const parts = { ...exports[0] };
  const text = (path: string) => strFromU8(parts[path]);
  const each = (make: (at: number) => string) => deck.slides.map((_slide, at) => make(at + 1)).join('');
  parts['ppt/presentation.xml'] = strToU8(text('ppt/presentation.xml').replace(/<p:sldIdLst>.*<\/p:sldIdLst>/,
    `<p:sldIdLst>${each(at => `<p:sldId id="${parseInt(deck.slides[at - 1].id, 10)}" r:id="rSlide${at}"/>`)}</p:sldIdLst>`));
  parts['ppt/_rels/presentation.xml.rels'] = strToU8(text('ppt/_rels/presentation.xml.rels')
    .replace(/<Relationship Id="rId2"[^>]*\/>/, '').replace('</Relationships>', `${each(at => `<Relationship Id="rSlide${at}" `
      + 'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
      + `Target="slides/slide${at}.xml"/>`)}</Relationships>`));
  parts['[Content_Types].xml'] = strToU8(text('[Content_Types].xml').replace(/<Override PartName="\/ppt\/slides\/slide1.xml"[^>]*\/>/,
    each(at => `<Override PartName="/ppt/slides/slide${at}.xml" `
      + 'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>')));
  exports.forEach((exported, at) => {
    parts[`ppt/slides/slide${at + 1}.xml`] = exported['ppt/slides/slide1.xml'];
    parts[`ppt/slides/_rels/slide${at + 1}.xml.rels`] = exported['ppt/slides/_rels/slide1.xml.rels'];
  });
  return zipSync(parts);
}
