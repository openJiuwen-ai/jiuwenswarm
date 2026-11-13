/**
 * A deck that a fake ``PowerPoint.run`` writes to, for the tests of the pane's
 * writes. Changes land at once; a reference to a slide or shape that is not
 * there, or a table read off a shape that is not one, fails the next ``sync``
 * as the host does.
 *
 * A slide exports as a real single-slide package, as PowerPoint 16.114 on the
 * Mac was seen to export one: ``ppt/slides/slide1.xml`` with its master and
 * every layout of that master, the theme, the notes, and ``docProps/core.xml``
 * with the time of the export. A shape's id, name, position, size and paragraphs
 * are its XML; what else the fake keeps of a slide rides along as JSON in the
 * slide's ``extLst``. Inserting a package keeps the shape ids of its XML and
 * gives the slide a new id: the XML wins where an edit changed it, so a slide
 * inserted from an unedited export is the same slide under a new id. A shape
 * the fake knows only as XML (a picture, a chart, a group) exports as that XML.
 */
import { DOMParser, XMLSerializer, type Document, type Element } from '@xmldom/xmldom';
import { strFromU8, strToU8, unzipSync, zipSync } from 'fflate';

import type { PowerPointRun } from './powerPointReadTools';

type Bag = Record<string, unknown>;

export interface FakeShapeState {
  id: string;
  name: string;
  type: string;
  left: number;
  top: number;
  width: number;
  height: number;
  text?: string;
  [property: string]: unknown;
}

export interface FakeSlideState {
  id: string;
  layoutId?: string;
  background?: string;
  shapes: FakeShapeState[];
  /** Speaker notes; the slide exports a notes slide when it has them. */
  notes?: string;
  /** The parts the slide's XML names (pictures, charts) by package path. */
  parts?: Record<string, FakePart>;
}

/** A part of a slide's package and the relationship that names it from the slide. */
export interface FakePart { contentType: string; base64: string; relationshipId: string; relationshipType: string }

/** The theme and master a slide's package carries. */
export interface FakeTheme {
  name: string;
  palette: Record<string, string>;
  fonts: { major: string; minor: string };
  bodyFontPt: number;
}

export interface FakeLayout { id: string; name: string; placeholders?: number }

export interface FakeDeckOptions {
  slides?: { id?: string; layoutId?: string; shapes?: Partial<FakeShapeState>[]; notes?: string;
    parts?: Record<string, FakePart> }[];
  masters?: { id: string; name: string; layouts: FakeLayout[] }[];
  size?: { width: number; height: number };
  /** The highest PowerPointApi version the host has. */
  api?: string;
  theme?: FakeTheme;
}

/** The layouts of the stock master, as PowerPoint exports all of them with a slide. */
const OFFICE_LAYOUTS: FakeLayout[] = ['Title Slide', 'Title and Content', 'Section Header', 'Two Content',
  'Comparison', 'Title Only', 'Blank', 'Content with Caption', 'Picture with Caption', 'Title and Vertical Text',
  'Vertical Title and Text'].map(name => ({ id: name.toLowerCase().replace(/ /g, '-'), name }));

const OFFICE_THEME: FakeTheme = { name: 'Office Theme', palette: { dk1: '000000', lt1: 'FFFFFF', accent1: '4472C4' },
  fonts: { major: 'Calibri Light', minor: 'Calibri' }, bodyFontPt: 28 };

export class FakeDeck {
  slides: FakeSlideState[];
  readonly masters: { id: string; name: string; layouts: FakeLayout[] }[];
  readonly size: { width: number; height: number };
  readonly api: string;
  readonly theme: FakeTheme;
  syncs = 0;
  /** Slides it adds before the host refuses the next one, as a host error part way through a write. */
  addsBeforeFailure: number | undefined;
  /** Packages it inserts before the host refuses the next one. */
  insertsBeforeFailure: number | undefined;
  /** What each ``insertSlidesFromBase64`` was asked for. */
  readonly insertions: { formatting?: string; sourceSlideIds?: string[]; targetSlideId?: string }[] = [];
  /** The shapes whose frame the pane asked to fit its text again (``autoSizeSetting``, a table's row heights). */
  readonly refits: string[] = [];
  private nextId = 300;
  private failure: Error | undefined;

  constructor(options: FakeDeckOptions = {}) {
    this.masters = options.masters ?? [{ id: 'm1', name: 'Office Theme', layouts: OFFICE_LAYOUTS }];
    this.size = options.size ?? { width: 960, height: 540 };
    this.api = options.api ?? '1.10';
    this.theme = options.theme ?? OFFICE_THEME;
    this.slides = (options.slides ?? [{}]).map(slide => ({
      id: slide.id ?? this.slideId(), layoutId: slide.layoutId,
      shapes: (slide.shapes ?? []).map(shape => this.shapeState(shape)),
      ...(slide.notes === undefined ? {} : { notes: slide.notes }), ...(slide.parts ? { parts: slide.parts } : {}),
    }));
  }

  supports(version: string): boolean {
    const [maxMajor, maxMinor] = this.api.split('.').map(Number);
    const [major, minor] = version.split('.').map(Number);
    return major < maxMajor || (major === maxMajor && minor <= maxMinor);
  }

  readonly host = {
    isSetSupported: (_name: 'PowerPointApi', version: string) => this.supports(version),
    worker: true,
  };

  /** What the deck shows, without the ids PowerPoint hands out: each slide's layout, background and shapes. */
  content(): unknown[] {
    return this.slides.map(({ id: _id, ...slide }) => JSON.parse(JSON.stringify(slide)));
  }

  ids(): string[] {
    return this.slides.map(slide => slide.id);
  }

  slide(id: string): FakeSlideState {
    const slide = this.slides.find(item => item.id === id);
    if (!slide) throw new Error(`no slide ${id}`);
    return slide;
  }

  readonly run: PowerPointRun = async batch => batch(this.context() as unknown as PowerPoint.RequestContext);

  private slideId(): string {
    this.nextId += 1;
    return `${this.nextId}#`;
  }

  private shapeState(shape: Partial<FakeShapeState>): FakeShapeState {
    this.nextId += 1;
    return { id: String(this.nextId), name: `Shape ${this.nextId}`, type: 'TextBox', left: 0, top: 0, width: 100,
      height: 50, ...shape };
  }

  private fail(message: string): void {
    this.failure ??= new Error(message);
  }

  private context() {
    const deck = this;
    const loadable = { load: () => undefined };
    const slideApi = (state: FakeSlideState) => ({
      ...loadable,
      get id() { return state.id; },
      get index() { return deck.slides.indexOf(state); },
      shapes: shapesApi(state),
      background: { fill: { setSolidFill: ({ color }: { color: string }) => { state.background = color; } } },
      moveTo: (index: number) => {
        deck.slides.splice(deck.slides.indexOf(state), 1);
        deck.slides.splice(index, 0, state);
      },
      delete: () => {
        deck.slides = deck.slides.filter(slide => slide !== state);
      },
      applyLayout: (layout: { id: string }) => {
        state.layoutId = layout.id;
      },
      exportAsBase64: () => ({ ...loadable, value: exportSlide(state, deck) }),
    });
    const missing = (what: string): Bag => new Proxy({}, { get: (_target, key) => {
      deck.fail(`ItemNotFound: ${what}`);
      return key === 'load' || key === 'delete' ? () => undefined : missing(what);
    } });
    const shapesApi = (slide: FakeSlideState) => {
      const add = (shape: Partial<FakeShapeState>) => {
        const state = deck.shapeState(shape);
        slide.shapes.push(state);
        return shapeApi(slide, state);
      };
      return {
        ...loadable,
        get items() { return slide.shapes.map(shape => shapeApi(slide, shape)); },
        getItem: (id: string) => {
          const shape = slide.shapes.find(item => item.id === id);
          return shape ? shapeApi(slide, shape) : missing(`shape ${id}`);
        },
        addTextBox: (text: string, options: Bag) => add({ type: 'TextBox', text, ...options }),
        addGeometricShape: (geometry: string, options: Bag) => add({ type: 'GeometricShape', geometry, ...options }),
        addTable: (rows: number, columns: number, options: Bag) => {
          const problem = borderProblem(options.specificCellProperties as Bag[][] | undefined);
          if (!problem) return add({ type: 'Table', rows, columns, ...options });
          deck.fail(`InvalidArgument: ${problem}`);
          return missing('table');
        },
      };
    };
    const shapeApi = (slide: FakeSlideState, state: FakeShapeState): Bag => {
      const bag = (key: string): Bag => {
        state[key] ??= {};
        return state[key] as Bag;
      };
      /** Properties set on it land in the shape's ``key`` bag; ``methods`` are its calls. */
      const part = (key: string, methods: Bag = {}): Bag => new Proxy({}, {
        get: (_target, name: string) => methods[name] ?? (state[key] as Bag | undefined)?.[name],
        set: (_target, name: string, value) => {
          bag(key)[name] = value;
          if (key === 'frame' && name === 'autoSizeSetting') deck.refits.push(state.id);
          return true;
        },
      });
      const api: Bag = {
        ...loadable,
        delete: () => {
          slide.shapes = slide.shapes.filter(shape => shape !== state);
        },
        setZOrder: (order: string) => {
          state.zOrder = order;
        },
        fill: part('fill', {
          clear: () => Object.assign(bag('fill'), { color: 'none' }),
          setSolidColor: (color: string) => Object.assign(bag('fill'), { color }),
          setImage: (image: string) => Object.assign(bag('fill'), { image }),
        }),
        lineFormat: part('line'),
        textFrame: part('frame', {
          load: () => undefined,
          textRange: {
            get text() { return state.text ?? ''; },
            set text(value: string) {
              state.text = value;
              delete state.paragraphs;
            },
            font: part('font'),
            paragraphFormat: part('paragraph'),
          },
        }),
        getTable: () => {
          if (state.type !== 'Table') {
            deck.fail('InvalidArgument: not a table');
            return missing('table');
          }
          const row = (index: number) => ({
            get height() { return ((state.rowHeights as Bag | undefined)?.[index] as number | undefined) ?? 20; },
            set height(height: number) {
              bag('rowHeights')[index] = height;
              deck.refits.push(state.id);
            },
          });
          const cell = (row: number, column: number) => {
            const cells = bag('cells');
            cells[`${row},${column}`] ??= { fill: {}, font: {}, borders: {} };
            const value = cells[`${row},${column}`] as { fill: Bag; font: Bag; borders: Bag };
            // The host keeps an edge two cells share once: a row's top is the bottom of the row above.
            const shared: Record<string, () => Bag | undefined> = {
              top: () => (row > 0 ? cell(row - 1, column).borders.bottom : undefined),
              left: () => (column > 0 ? cell(row, column - 1).borders.right : undefined),
            };
            const side = (name: string): Bag => {
              value.borders[name] ??= shared[name]?.() ?? hostBorder(message => deck.fail(message));
              return value.borders[name] as Bag;
            };
            return Object.assign(value, {
              fill: Object.assign(value.fill, {
                clear: () => Object.assign(value.fill, { color: 'none' }),
                setSolidColor: (color: string) => Object.assign(value.fill, { color }),
              }),
              borders: Object.fromEntries(['left', 'right', 'top', 'bottom', 'diagonalDown', 'diagonalUp']
                .map(name => [name, side(name)])),
            });
          };
          return { ...loadable, rowCount: state.rows, columnCount: state.columns,
            getCellOrNullObject: cell,
            rows: { ...loadable, getItemAt: row,
              get items() { return Array.from({ length: state.rows as number }, (_, index) => row(index)); } },
            columns: { getItemAt: (column: number) => new Proxy({}, { set: (_t, _k, width) => {
              bag('columnWidths')[column] = width;
              return true;
            } }) } };
        },
      };
      for (const key of ['id', 'name', 'type', 'left', 'top', 'width', 'height', 'rotation', 'altTextDescription']) {
        Object.defineProperty(api, key, { enumerable: true, get: () => state[key],
          set: value => { state[key] = value; } });
      }
      return api;
    };
    const slidesApi = {
      ...loadable,
      get items() { return deck.slides.map(slideApi); },
      getItem: (id: string) => {
        const slide = deck.slides.find(item => item.id === id);
        return slide ? slideApi(slide) : missing(`slide ${id}`);
      },
      add: ({ layoutId }: { layoutId?: string } = {}) => {
        if (deck.addsBeforeFailure !== undefined && deck.addsBeforeFailure-- === 0) {
          deck.fail('GeneralException');
          return;
        }
        const layout = deck.masters.flatMap(master => master.layouts).find(item => item.id === layoutId);
        const slide: FakeSlideState = { id: deck.slideId(), layoutId, shapes: [] };
        for (let at = 0; at < (layout?.placeholders ?? 0); at += 1) {
          slide.shapes.push(deck.shapeState({ type: 'Placeholder', name: `Placeholder ${at + 1}` }));
        }
        deck.slides.push(slide);
      },
    };
    return {
      presentation: {
        slides: slidesApi,
        pageSetup: { ...loadable, slideWidth: deck.size.width, slideHeight: deck.size.height },
        slideMasters: { ...loadable, items: deck.masters.map(master => ({ id: master.id, name: master.name,
          layouts: { ...loadable, items: master.layouts } })) },
        insertSlidesFromBase64: (base64: string, options: PowerPoint.InsertSlideOptions = {}) => {
          deck.insertions.push(options);
          if (deck.insertsBeforeFailure !== undefined && deck.insertsBeforeFailure-- === 0) {
            deck.fail('GeneralException');
            return;
          }
          // Without a target the slides go first, as the API reference says.
          const after = options.targetSlideId ? deck.slides.findIndex(slide => slide.id === options.targetSlideId) : -1;
          if (options.targetSlideId && after < 0) {
            deck.fail(`ItemNotFound: slide ${options.targetSlideId}`);
            return;
          }
          const inserted = importSlides(base64, options.sourceSlideIds);
          deck.nextId = Math.max(deck.nextId, ...inserted.flatMap(slide => slide.shapes.map(shape => Number(shape.id)))
            .filter(Number.isInteger));
          deck.slides.splice(after + 1, 0, ...inserted.map(slide => ({ ...slide, id: deck.slideId() })));
        },
      },
      sync: async () => {
        deck.syncs += 1;
        const { failure } = deck;
        deck.failure = undefined;
        if (failure) throw failure;
      },
    };
  }
}

const SIDES = ['left', 'right', 'top', 'bottom', 'diagonalDown', 'diagonalUp'];

/** Why the host refuses a new table's borders: one not given in full, or two cells giving their shared edge apart. */
function borderProblem(cells: Bag[][] | undefined): string | undefined {
  if (!cells) return undefined;
  const border = (row: number, column: number, side: string): Bag | undefined =>
    (cells[row]?.[column]?.borders as Bag | undefined)?.[side] as Bag | undefined;
  for (const [row, cellsOfRow] of cells.entries()) {
    for (const column of cellsOfRow.keys()) {
      for (const side of SIDES) {
        const given = border(row, column, side);
        if (given && (given.color === undefined || given.weight === undefined)) return `${side} border not in full`;
      }
      for (const [mine, theirs] of [[border(row, column, 'bottom'), border(row + 1, column, 'top')],
        [border(row, column, 'right'), border(row, column + 1, 'left')]]) {
        if (mine && theirs && JSON.stringify(mine) !== JSON.stringify(theirs)) return 'a shared edge given twice';
      }
    }
  }
  return undefined;
}

/** A table cell's border: the host refuses a transparency set before the border's color and weight. */
function hostBorder(fail: (message: string) => void): Bag {
  return new Proxy({} as Bag, { set: (target, name: string, value) => {
    if (name === 'transparency' && (target.color === undefined || target.weight === undefined)) {
      fail('InvalidArgument: Border.transparency');
    }
    target[name] = value;
    return true;
  } });
}


const NS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
  + 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
  + 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"';
const A = 'http://schemas.openxmlformats.org/drawingml/2006/main';
const P = 'http://schemas.openxmlformats.org/presentationml/2006/main';
const R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
const RELS = 'http://schemas.openxmlformats.org/package/2006/relationships';
const TYPES = 'http://schemas.openxmlformats.org/package/2006/content-types';
const PML = 'application/vnd.openxmlformats-officedocument.presentationml';
const CORE_PROPERTIES = 'http://schemas.openxmlformats.org/package/2006/metadata/core-properties';
/** The ``p:ext`` that carries the slide's state the XML does not hold. */
const STATE_EXTENSION = 'urn:fake-deck:state';
const EMU = 12_700;
const SHAPE_ELEMENTS = ['sp', 'pic', 'graphicFrame', 'grpSp', 'cxnSp'];

const escapeXml = (text: string) => text.replace(/[&<>"]/g, char => `&#${char.charCodeAt(0)};`);
const relationships = (entries: [id: string, type: string, target: string][]) => `<Relationships xmlns="${RELS}">`
  + entries.map(([id, type, target]) => `<Relationship Id="${id}" Type="${type.includes(':') ? type : `${R}/${type}`}" `
    + `Target="${target}"/>`).join('') + '</Relationships>';

/** Paragraphs as PowerPoint gives a text range's text: ``\r`` between paragraphs, ``\v`` for a line break. */
function paragraphsOfText(text: string): string {
  return text.split('\r').map(paragraph => `<a:p>${paragraph.split('\v').map(line => (line
    ? `<a:r><a:t>${escapeXml(line)}</a:t></a:r>` : '')).join('<a:br/>')}</a:p>`).join('');
}

function textOfParagraphs(paragraphs: Element[]): string {
  return paragraphs.map(paragraph => Array.from(paragraph.getElementsByTagNameNS(A, '*'))
    .map(item => (item.localName === 't' ? item.textContent ?? '' : item.localName === 'br' ? '\v' : '')).join(''))
    .join('\r');
}

function shapeXml(shape: FakeShapeState): string {
  if (typeof shape.xml === 'string') return shape.xml;
  const emu = (points: number) => Math.round(points * EMU);
  const paragraphs = (shape.paragraphs as string | undefined)
    ?? (shape.text === undefined ? undefined : paragraphsOfText(shape.text));
  return `<p:sp><p:nvSpPr><p:cNvPr id="${shape.id}" name="${escapeXml(shape.name)}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>`
    + `<p:spPr><a:xfrm><a:off x="${emu(shape.left)}" y="${emu(shape.top)}"/><a:ext cx="${emu(shape.width)}" `
    + `cy="${emu(shape.height)}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>`
    + (paragraphs === undefined ? '' : `<p:txBody><a:bodyPr/><a:lstStyle/>${paragraphs}</p:txBody>`) + '</p:sp>';
}

/**
 * ``slide`` as ``exportAsBase64`` gives it: a package of one slide with its master and every layout of that master
 * (the slide's own among them), the theme and the notes.
 */
function exportSlide(slide: FakeSlideState, deck: FakeDeck): string {
  const { theme, size } = deck;
  const { layouts } = deck.masters.find(master => master.layouts.some(layout => layout.id === slide.layoutId))
    ?? deck.masters[0];
  const own = Math.max(0, layouts.findIndex(layout => layout.id === slide.layoutId)) + 1;
  const { id: _id, shapes, notes, parts = {}, ...rest } = slide;
  const state = { ...rest, shapes: shapes.filter(shape => shape.xml === undefined) };
  const files: Record<string, string> = {
    '_rels/.rels': relationships([['rId1', 'officeDocument', 'ppt/presentation.xml'], ['rId2',
      `${RELS}/metadata/core-properties`, 'docProps/core.xml']]),
    // PowerPoint writes the time of each export here, so two exports of an unchanged slide differ.
    'docProps/core.xml': `<cp:coreProperties xmlns:cp="${CORE_PROPERTIES}" xmlns:dcterms="http://purl.org/dc/terms/" `
      + 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dcterms:modified xsi:type="dcterms:W3CDTF">'
      + `${new Date().toISOString().replace(/\.\d+Z$/, 'Z')}</dcterms:modified></cp:coreProperties>`,
    'ppt/presentation.xml': `<p:presentation ${NS}><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/>`
      + '</p:sldMasterIdLst><p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>'
      + `<p:sldSz cx="${Math.round(size.width * EMU)}" cy="${Math.round(size.height * EMU)}"/></p:presentation>`,
    'ppt/_rels/presentation.xml.rels': relationships([['rId1', 'slideMaster', 'slideMasters/slideMaster1.xml'],
      ['rId2', 'slide', 'slides/slide1.xml'], ['rId3', 'theme', 'theme/theme1.xml']]),
    'ppt/slides/slide1.xml': `<p:sld ${NS}><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/>`
      + `<p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>${shapes.map(shapeXml).join('')}</p:spTree><p:extLst>`
      + `<p:ext uri="${STATE_EXTENSION}">${escapeXml(JSON.stringify(state))}</p:ext></p:extLst></p:cSld></p:sld>`,
    'ppt/slides/_rels/slide1.xml.rels': relationships([
      ['rLayout', 'slideLayout', `../slideLayouts/slideLayout${own}.xml`],
      ...(notes === undefined ? [] : [['rNotes', 'notesSlide', '../notesSlides/notesSlide1.xml'] as [string, string, string]]),
      ...Object.entries(parts).map(([path, part]): [string, string, string] => [part.relationshipId,
        part.relationshipType, `../${path.slice('ppt/'.length)}`])]),
    'ppt/slideMasters/slideMaster1.xml': `<p:sldMaster ${NS}><p:cSld><p:spTree><p:sp><p:nvSpPr><p:cNvPr id="2" `
      + 'name="Title Placeholder 1"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/>'
      + `${paragraphsOfText('Click to edit Master title style')}</p:txBody></p:sp></p:spTree></p:cSld><p:sldLayoutIdLst>`
      + `${layouts.map((_layout, at) => `<p:sldLayoutId id="${2147483649 + at}" r:id="rLayout${at + 1}"/>`).join('')}`
      + '</p:sldLayoutIdLst><p:txStyles><p:bodyStyle><a:lvl1pPr>'
      + `<a:defRPr sz="${theme.bodyFontPt * 100}"/></a:lvl1pPr></p:bodyStyle></p:txStyles></p:sldMaster>`,
    'ppt/slideMasters/_rels/slideMaster1.xml.rels': relationships([...layouts.map((_layout, at):
      [string, string, string] => [`rLayout${at + 1}`, 'slideLayout', `../slideLayouts/slideLayout${at + 1}.xml`]),
    ['rTheme', 'theme', '../theme/theme1.xml']]),
    'ppt/theme/theme1.xml': `<a:theme ${NS} name="${escapeXml(theme.name)}"><a:themeElements>`
      + `<a:clrScheme name="${escapeXml(theme.name)}">${Object.entries(theme.palette)
        .map(([slot, hex]) => `<a:${slot}><a:srgbClr val="${hex}"/></a:${slot}>`).join('')}</a:clrScheme>`
      + `<a:fontScheme name="${escapeXml(theme.name)}"><a:majorFont><a:latin typeface="${theme.fonts.major}"/>`
      + `</a:majorFont><a:minorFont><a:latin typeface="${theme.fonts.minor}"/></a:minorFont></a:fontScheme>`
      + '</a:themeElements></a:theme>',
  };
  layouts.forEach(({ name }, at) => {
    // The stock layouts but Blank carry the title placeholder's prompt, as the master does.
    files[`ppt/slideLayouts/slideLayout${at + 1}.xml`] = `<p:sldLayout ${NS}><p:cSld name="${escapeXml(name)}">`
      + `<p:spTree>${name === 'Blank' ? '' : '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr/><p:nvPr>'
        + `<p:ph type="title"/></p:nvPr></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/>${paragraphsOfText('Click to edit '
        + 'Master title style')}</p:txBody></p:sp>`}</p:spTree></p:cSld></p:sldLayout>`;
    files[`ppt/slideLayouts/_rels/slideLayout${at + 1}.xml.rels`] = relationships([['rId1', 'slideMaster',
      '../slideMasters/slideMaster1.xml']]);
  });
  if (notes !== undefined) {
    files['ppt/notesSlides/notesSlide1.xml'] = `<p:notes ${NS}><p:cSld><p:spTree><p:sp><p:nvSpPr>`
      + '<p:cNvPr id="3" name="Notes Placeholder 2"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/>'
      + `${paragraphsOfText(notes)}</p:txBody></p:sp></p:spTree></p:cSld></p:notes>`;
    files['ppt/notesSlides/_rels/notesSlide1.xml.rels'] = relationships([['rId1', 'slide', '../slides/slide1.xml']]);
  }
  const overrides: [string, string][] = [['ppt/presentation.xml', `${PML}.presentation.main+xml`],
    ['ppt/slides/slide1.xml', `${PML}.slide+xml`], ...layouts.map((_layout, at): [string, string] => [
      `ppt/slideLayouts/slideLayout${at + 1}.xml`, `${PML}.slideLayout+xml`]),
    ['ppt/slideMasters/slideMaster1.xml', `${PML}.slideMaster+xml`],
    ['ppt/theme/theme1.xml', 'application/vnd.openxmlformats-officedocument.theme+xml'],
    ['docProps/core.xml', 'application/vnd.openxmlformats-package.core-properties+xml'],
    ...(notes === undefined ? [] : [['ppt/notesSlides/notesSlide1.xml', `${PML}.notesSlide+xml`] as [string, string]]),
    ...Object.entries(parts).map(([path, part]): [string, string] => [path, part.contentType])];
  files['[Content_Types].xml'] = `<Types xmlns="${TYPES}"><Default Extension="rels" `
    + 'ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" '
    + `ContentType="application/xml"/>${overrides.map(([path, type]) => `<Override PartName="/${path}" `
      + `ContentType="${type}"/>`).join('')}</Types>`;
  const zipped = zipSync({ ...Object.fromEntries(Object.entries(files).map(([path, xml]) => [path, strToU8(xml)])),
    ...Object.fromEntries(Object.entries(parts).map(([path, part]) => [path, Buffer.from(part.base64, 'base64')])) });
  return Buffer.from(zipped).toString('base64');
}

/** The slides of a package, those of ``sourceSlideIds`` (``nnn#``) when given, as the fake keeps them. */
function importSlides(base64: string, sourceSlideIds?: string[]): Omit<FakeSlideState, 'id'>[] {
  const files = unzipSync(Buffer.from(base64, 'base64'));
  const xml = (path: string): Document => new DOMParser().parseFromString(strFromU8(files[path]), 'application/xml');
  const elements = (root: Document | Element, namespace: string, name: string) =>
    Array.from(root.getElementsByTagNameNS(namespace, name));
  const resolve = (from: string, target: string) => `${from.slice(0, from.lastIndexOf('/'))}/${target}`
    .split('/').reduce<string[]>((path, segment) => (segment === '..' ? path.slice(0, -1) : [...path, segment]), [])
    .join('/');
  const targets = (part: string) => {
    const path = `${part.slice(0, part.lastIndexOf('/'))}/_rels/${part.slice(part.lastIndexOf('/') + 1)}.rels`;
    return files[path] ? elements(xml(path), RELS, 'Relationship') : [];
  };
  const presentationTargets = new Map(targets('ppt/presentation.xml')
    .map(item => [item.getAttribute('Id'), resolve('ppt/presentation.xml', item.getAttribute('Target') ?? '')]));
  const slidePaths = elements(xml('ppt/presentation.xml'), P, 'sldId')
    .filter(item => !sourceSlideIds || sourceSlideIds.includes(`${item.getAttribute('id')}#`))
    .map(item => presentationTargets.get(item.getAttributeNS(R, 'id'))!);
  const types = xml('[Content_Types].xml');
  const contentType = (path: string) => (elements(types, TYPES, 'Override')
    .find(item => item.getAttribute('PartName') === `/${path}`) ?? elements(types, TYPES, 'Default')
    .find(item => item.getAttribute('Extension') === path.slice(path.lastIndexOf('.') + 1)))?.getAttribute('ContentType');
  return slidePaths.map(path => {
    const slide = xml(path);
    const kept = elements(slide, P, 'ext').find(item => item.getAttribute('uri') === STATE_EXTENSION)?.textContent;
    const state = (kept ? JSON.parse(kept) : { shapes: [] }) as Omit<FakeSlideState, 'id'>;
    const tree = elements(slide, P, 'spTree')[0];
    const shapes = Array.from(tree.childNodes).filter((node): node is Element => node.nodeType === 1
      && SHAPE_ELEMENTS.includes(node.localName ?? '')).map(element => {
      const id = elements(element, P, 'cNvPr')[0].getAttribute('id')!;
      const exported = state.shapes.find(shape => shape.id === id);
      return exported ? shapeFromXml(exported, element) : xmlShape(element);
    });
    const parts: Record<string, FakePart> = {};
    let notes: string | undefined;
    for (const relationship of targets(path)) {
      const type = relationship.getAttribute('Type') ?? '';
      const target = resolve(path, relationship.getAttribute('Target') ?? '');
      if (type.endsWith('/notesSlide')) {
        notes = textOfParagraphs(elements(xml(target), A, 'p'));
      } else if (!type.endsWith('/slideLayout')) {
        parts[target] = { contentType: contentType(target) ?? 'application/octet-stream',
          base64: Buffer.from(files[target]).toString('base64'), relationshipId: relationship.getAttribute('Id')!,
          relationshipType: type };
      }
    }
    const { notes: _notes, parts: _parts, ...rest } = state;
    return { ...rest, shapes, ...(notes === undefined ? {} : { notes }),
      ...(Object.keys(parts).length ? { parts } : {}) };
  });
}

/** The offset and extent of the first ``xfrm`` of a shape element, in points. */
function geometry(element: Element): Pick<FakeShapeState, 'left' | 'top' | 'width' | 'height'> {
  const of = (name: string) => element.getElementsByTagNameNS(A, name)[0];
  const at = (name: string, attribute: string) => Number(of(name)?.getAttribute(attribute) ?? 0) / EMU;
  return { left: at('off', 'x'), top: at('off', 'y'), width: at('ext', 'cx'), height: at('ext', 'cy') };
}

function paragraphsOf(element: Element): { xml: string; text: string } | undefined {
  const body = element.getElementsByTagNameNS(P, 'txBody')[0];
  if (!body) return undefined;
  const paragraphs = Array.from(body.childNodes)
    .filter((node): node is Element => node.nodeType === 1 && node.localName === 'p');
  const serializer = new XMLSerializer();
  return { xml: paragraphs.map(paragraph => serializer.serializeToString(paragraph)).join(''),
    text: textOfParagraphs(paragraphs) };
}

/** A shape the fake exported, as the XML an edit may have changed shows it now. */
function shapeFromXml(exported: FakeShapeState, element: Element): FakeShapeState {
  const shape = { ...exported };
  const name = element.getElementsByTagNameNS(P, 'cNvPr')[0].getAttribute('name') ?? '';
  if (name !== shape.name) shape.name = name;
  const original = new DOMParser().parseFromString(shapeXml(exported).replace('<p:sp>', `<p:sp ${NS}>`),
    'application/xml').documentElement!;
  const [before, after] = [geometry(original), geometry(element)];
  for (const key of ['left', 'top', 'width', 'height'] as const) if (before[key] !== after[key]) shape[key] = after[key];
  const text = paragraphsOf(element);
  if (text && text.xml !== paragraphsOf(original)?.xml) {
    shape.text = text.text;
    shape.paragraphs = text.xml;
  }
  return shape;
}

/** A shape the fake knows only as its XML. */
function xmlShape(element: Element): FakeShapeState {
  const properties = element.getElementsByTagNameNS(P, 'cNvPr')[0];
  const table = element.getElementsByTagNameNS(A, 'tbl')[0];
  const type = table ? 'Table' : ({ pic: 'Image', graphicFrame: 'Chart', grpSp: 'Group', cxnSp: 'Line' } as
    Record<string, string>)[element.localName ?? ''] ?? (element.getElementsByTagNameNS(P, 'cNvSpPr')[0]
    ?.getAttribute('txBox') === '1' ? 'TextBox' : 'GeometricShape');
  const text = element.localName === 'sp' ? paragraphsOf(element)?.text : undefined;
  return { id: properties.getAttribute('id')!, name: properties.getAttribute('name') ?? '', type, ...geometry(element),
    ...(text === undefined ? {} : { text }),
    ...(table ? { rows: table.getElementsByTagNameNS(A, 'tr').length,
      columns: table.getElementsByTagNameNS(A, 'gridCol').length } : {}),
    xml: new XMLSerializer().serializeToString(element) };
}

/** A .pptx with one slide per title (a text shape with id 2), as a user would upload one, and ``extra`` parts. */
export function uploadedDeck(titles: string[], extra: Record<string, string> = {}): Uint8Array<ArrayBuffer> {
  return new Uint8Array(zipSync(Object.fromEntries(Object.entries({
    '[Content_Types].xml': `<Types xmlns="${TYPES}"/>`,
    'ppt/presentation.xml': `<p:presentation ${NS}><p:sldIdLst>${titles.map((_title, at) =>
      `<p:sldId id="${260 + at}" r:id="rId${at + 1}"/>`).join('')}</p:sldIdLst></p:presentation>`,
    'ppt/_rels/presentation.xml.rels': relationships(titles.map((_title, at) => [`rId${at + 1}`, 'slide',
      `slides/slide${at + 1}.xml`])),
    ...Object.fromEntries(titles.map((title, at) => [`ppt/slides/slide${at + 1}.xml`, `<p:sld ${NS}><p:cSld><p:spTree>`
      + '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/><p:sp><p:nvSpPr>'
      + '<p:cNvPr id="2" name="Title"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/>'
      + `${paragraphsOfText(title)}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>`])),
    ...extra,
  }).map(([path, xml]) => [path, strToU8(xml)]))));
}
