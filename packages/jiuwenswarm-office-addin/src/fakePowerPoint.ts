/**
 * A deck that a fake ``PowerPoint.run`` writes to, for the tests of the pane's
 * writes. Changes land at once; a reference to a slide or shape that is not
 * there, or a table read off a shape that is not one, fails the next ``sync``
 * as the host does. A slide exports as its own state, so a slide inserted from
 * an export is the same slide under a new id.
 */
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
}

export interface FakeLayout { id: string; name: string; placeholders?: number }

export interface FakeDeckOptions {
  slides?: { id?: string; layoutId?: string; shapes?: Partial<FakeShapeState>[] }[];
  masters?: { id: string; name: string; layouts: FakeLayout[] }[];
  size?: { width: number; height: number };
  /** The highest PowerPointApi version the host has. */
  api?: string;
}

export class FakeDeck {
  slides: FakeSlideState[];
  readonly masters: { id: string; name: string; layouts: FakeLayout[] }[];
  readonly size: { width: number; height: number };
  readonly api: string;
  syncs = 0;
  /** Slides it adds before the host refuses the next one, as a host error part way through a write. */
  addsBeforeFailure: number | undefined;
  private nextId = 300;
  private failure: Error | undefined;

  constructor(options: FakeDeckOptions = {}) {
    this.masters = options.masters ?? [{ id: 'm1', name: 'Office Theme', layouts: [{ id: 'blank', name: 'Blank' }] }];
    this.size = options.size ?? { width: 960, height: 540 };
    this.api = options.api ?? '1.10';
    this.slides = (options.slides ?? [{}]).map(slide => ({
      id: slide.id ?? this.slideId(), layoutId: slide.layoutId,
      shapes: (slide.shapes ?? []).map(shape => this.shapeState(shape)),
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
      exportAsBase64: () => ({ ...loadable, value: Buffer.from(JSON.stringify(state)).toString('base64') }),
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
          textRange: {
            get text() { return state.text ?? ''; },
            set text(value: string) { state.text = value; },
            font: part('font'),
            paragraphFormat: part('paragraph'),
          },
        }),
        getTable: () => {
          if (state.type !== 'Table') {
            deck.fail('InvalidArgument: not a table');
            return missing('table');
          }
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
            rows: { getItemAt: (row: number) => new Proxy({}, { set: (_t, _k, height) => {
              bag('rowHeights')[row] = height;
              return true;
            } }) },
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
        insertSlidesFromBase64: (base64: string, options: { targetSlideId?: string } = {}) => {
          const exported = JSON.parse(Buffer.from(base64, 'base64').toString()) as FakeSlideState;
          const after = options.targetSlideId ? deck.slides.findIndex(slide => slide.id === options.targetSlideId) : -1;
          deck.slides.splice(after + 1, 0, { ...exported, id: deck.slideId() });
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

