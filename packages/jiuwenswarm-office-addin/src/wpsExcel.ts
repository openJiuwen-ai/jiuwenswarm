/**
 * The Excel object model the Excel tools and ``ExcelState`` use, over WPS Spreadsheets' VBA-style one, so they run
 * unchanged in WPS. WPS calls are synchronous and land at once: ``load`` drops what a range read before, the next
 * read reads afresh, and ``sync`` only yields so the pane can paint. Only ``wpsExcelRun`` casts to the Excel types.
 *
 * A sheet's id is its name: WPS gives a sheet no id that outlives a rename. Colours cross as ``#RRGGBB`` here and
 * as WPS's ``RGB`` long (blue in the high byte) there; cell notes are WPS's comments. WPS raises no change events
 * the pane can read before and after values from, so ``<user_changes>`` is left out (``WPS_EXCEL_HOST``).
 */
import type { OfficeHost } from './appContract';
import { blockName, contains, parseBlock } from './excelAddress';
import type { ExcelRun } from './excelTools';
import type { WpsObject } from './wpsDocument';

/** The adapter has cell notes (ExcelApi 1.18) and none of the change events (1.9, 1.17). */
export const WPS_EXCEL_HOST: OfficeHost = {
  isSetSupported: (name, version) => name === 'ExcelApi' && version === '1.18',
  worker: typeof Worker === 'function',
};

/** ``Excel.run`` over a WPS workbook. */
export function wpsExcelRun(workbook: WpsObject): ExcelRun {
  const context = { workbook: new Workbook(workbook), sync: () => new Promise(resolve => setTimeout(resolve, 0)) };
  return async batch => batch(context as unknown as Excel.RequestContext);
}

/** The VBA enum values the adapter reads and writes, as Office.js names them. */
const UNDERLINES: Record<number, string> = { [-4142]: 'None', 2: 'Single', [-4119]: 'Double', 4: 'SingleAccountant',
  5: 'DoubleAccountant' };
const ALIGNMENTS: Record<number, string> = { 1: 'General', [-4131]: 'Left', [-4108]: 'Center', [-4152]: 'Right',
  5: 'Fill', [-4130]: 'Justify', 7: 'CenterAcrossSelection', [-4117]: 'Distributed' };
const LINE_STYLES: Record<number, string> = { [-4142]: 'None', 1: 'Continuous', [-4115]: 'Dash', [-4118]: 'Dot',
  [-4119]: 'Double', 4: 'DashDot', 5: 'DashDotDot', 13: 'SlantDashDot' };
const WEIGHTS: Record<number, string> = { 1: 'Hairline', 2: 'Thin', [-4138]: 'Medium', 4: 'Thick' };
/** ``XlBordersIndex`` by Office.js ``BorderIndex``, and the four edges a cell's properties read. */
const EDGES: Record<string, number> = { EdgeLeft: 7, EdgeTop: 8, EdgeBottom: 9, EdgeRight: 10, InsideVertical: 11,
  InsideHorizontal: 12 };
const SIDES = { left: 7, top: 8, bottom: 9, right: 10 } as const;

const valueOf = (table: Record<number, string>, name: string): number => Number(
  Object.entries(table).find(([, item]) => item === name)?.[0]);

function hex(rgb: number): string {
  const channels = [rgb & 0xff, (rgb >> 8) & 0xff, (rgb >> 16) & 0xff];
  return `#${channels.map(channel => channel.toString(16).padStart(2, '0')).join('').toUpperCase()}`;
}

function rgb(color: string): number {
  const value = parseInt(color.replace(/^#/, ''), 16);
  return ((value & 0xff) << 16) | (value & 0xff00) | ((value >> 16) & 0xff);
}

/** A sheet-qualified address as Excel writes one: the name quoted when it is not a plain word. */
function onSheet(sheet: string, cells: string): string {
  return /^[\p{L}_][\p{L}\p{N}_.]*$/u.test(sheet) ? `${sheet}!${cells}` : `'${sheet.replace(/'/g, "''")}'!${cells}`;
}

const cellsOf = (range: WpsObject): string => String(range.Address(false, false));

function itemsOf(collection: WpsObject): WpsObject[] {
  return Array.from({ length: Number(collection.Count) }, (_, index) => collection.Item(index + 1));
}

/** A read of a whole range as rows: WPS gives one cell's value bare, an empty cell's as nothing. */
function rowsOf(value: unknown): unknown[][] {
  const rows = Array.isArray(value) ? value as unknown[][] : [[value]];
  return rows.map(row => row.map(cell => cell ?? ''));
}

/**
 * A range's values as Excel gives them. WPS reads an error as nothing, as it does an empty cell: a cell with a
 * formula and no value is an error, given as it shows (``#DIV/0!``).
 */
function valuesOf(range: WpsObject): unknown[][] {
  const raw: unknown = range.Value2;
  const values = Array.isArray(raw) ? raw as unknown[][] : [[raw]];
  const formulas = values.some(row => row.some(value => value === null || value === undefined))
    ? rowsOf(range.Formula) : [];
  return values.map((row, r) => row.map((value, c) => value ?? (formulas[r][c] === '' ? ''
    : String(range.Cells.Item(r + 1, c + 1).Text))));
}

/** Writes ``rows`` to a range's ``property``, one cell's bare. */
function put(range: WpsObject, property: string, rows: unknown[][]): void {
  range[property] = rows.length === 1 && rows[0].length === 1 ? rows[0][0] : rows;
}

/** What ``getItemOrNullObject`` and its kin give for nothing: every read or call on it is nothing again. */
const NULL_OBJECT: any = new Proxy(function nothing() {}, {
  get: (_target, key) => (key === 'isNullObject' ? true : NULL_OBJECT), set: () => true, apply: () => NULL_OBJECT,
});

class Loadable {
  readonly isNullObject: boolean = false;
  load(): this {
    return this;
  }
}

class Workbook extends Loadable {
  readonly worksheets: Worksheets;

  constructor(private readonly wps: WpsObject) {
    super();
    this.worksheets = new Worksheets(wps);
  }

  get name(): string {
    return String(this.wps.Name);
  }

  getSelectedRanges(): { load(): void; address: string } {
    const selection = this.wps.Windows.Item(1).Selection;
    // A shape or chart selected is no range.
    const address = selection?.Areas ? itemsOf(selection.Areas)
      .map(area => onSheet(String(selection.Worksheet.Name), cellsOf(area))).join(',') : '';
    return { load() {}, address };
  }

  getActiveChartOrNullObject(): unknown {
    const chart = this.wps.ActiveChart;
    if (!chart) return NULL_OBJECT;
    const name = String(chart.Parent?.Name ?? chart.Name);
    return { isNullObject: false, load() {}, id: name, name,
      title: { load() {}, text: chart.HasTitle ? String(chart.ChartTitle.Text) : '' } };
  }
}

class Worksheets extends Loadable {
  constructor(private readonly workbook: WpsObject) {
    super();
  }

  get items(): Worksheet[] {
    return itemsOf(this.workbook.Worksheets).map(sheet => new Worksheet(sheet));
  }

  getItemOrNullObject(name: string): Worksheet {
    return this.items.find(sheet => sheet.name === name) ?? NULL_OBJECT;
  }

  getActiveWorksheet(): Worksheet {
    return new Worksheet(this.workbook.ActiveSheet);
  }
}

class Worksheet extends Loadable {
  constructor(readonly wps: WpsObject) {
    super();
  }

  get name(): string {
    return String(this.wps.Name);
  }

  get id(): string {
    return this.name;
  }

  /** WPS's used range of a sheet with nothing in it is its first cell; Excel's is nothing. */
  getUsedRangeOrNullObject(): Range {
    const used = this.wps.UsedRange;
    const empty = Number(used.Count) === 1 && rowsOf(used.Formula)[0][0] === '';
    return empty ? NULL_OBJECT : new Range(this, used);
  }

  getRange(address: string): Range {
    return new Range(this, this.wps.Range(address));
  }

  getRangeByIndexes(row: number, column: number, rows: number, columns: number): Range {
    return this.getRange(blockName({ top: row + 1, left: column + 1, bottom: row + rows, right: column + columns }));
  }

  get notes(): Notes {
    return new Notes(this);
  }

  get tables(): { load(): void; items: unknown[] } {
    return { load() {}, items: itemsOf(this.wps.ListObjects).map(table => ({
      name: String(table.Name),
      getRange: () => ({ load() { return this; }, address: onSheet(this.name, cellsOf(table.Range)) }),
      columns: { load() {}, items: itemsOf(table.ListColumns).map(column => ({ name: String(column.Name) })) },
    })) };
  }
}

/** A sheet's notes, which WPS keeps as comments. */
class Notes extends Loadable {
  constructor(private readonly sheet: Worksheet) {
    super();
  }

  get items(): unknown[] {
    return itemsOf(this.sheet.wps.Comments).map(note);
  }

  getItemOrNullObject(at: string): unknown {
    const comment = this.sheet.wps.Range(at).Comment;
    return comment ? note(comment) : NULL_OBJECT;
  }

  add(at: string, content: string): void {
    this.sheet.wps.Range(at).AddComment(content);
  }
}

function note(comment: WpsObject): unknown {
  return {
    isNullObject: false, load() {},
    get content() { return String(comment.Text()); },
    set content(text: string) { comment.Text(text); },
    getLocation: () => ({ load() {}, address: cellsOf(comment.Parent) }),
  };
}

/** A cell's look as ``getCellProperties`` gives it; the borders are the cell's own. */
function cellFormat(cell: WpsObject, shared: Look): Excel.CellPropertiesFormat {
  const look = Object.values(shared).some(value => value === null || value === undefined) ? lookOf(cell) : shared;
  const borders = Object.fromEntries(Object.entries(SIDES).map(([side, index]) => {
    const border = cell.Borders.Item(index);
    return [side, { style: LINE_STYLES[border.LineStyle] ?? 'None', weight: WEIGHTS[border.Weight] ?? 'Thin',
      color: hex(Number(border.Color)) }];
  }));
  return {
    font: { bold: Boolean(look.bold), italic: Boolean(look.italic), underline: UNDERLINES[look.underline] ?? 'None',
      strikethrough: Boolean(look.strikethrough), size: Number(look.size), color: hex(Number(look.color)),
      name: String(look.name) } as Excel.CellPropertiesFont,
    fill: { color: hex(Number(look.fill)) },
    horizontalAlignment: (ALIGNMENTS[look.alignment] ?? 'General') as Excel.HorizontalAlignment,
    borders,
  };
}

type Look = Record<'bold' | 'italic' | 'underline' | 'strikethrough' | 'size' | 'color' | 'name' | 'fill'
  | 'alignment', any>;

/** A range's font, fill and alignment; WPS gives a property nothing where the range's cells differ in it. */
function lookOf(range: WpsObject): Look {
  const font = range.Font;
  return { bold: font.Bold, italic: font.Italic, underline: font.Underline, strikethrough: font.Strikethrough,
    size: font.Size, color: font.Color, name: font.Name, fill: range.Interior.Color,
    alignment: range.HorizontalAlignment };
}

class Range extends Loadable {
  private readonly read = new Map<string, unknown[][]>();

  constructor(private readonly sheet: Worksheet, readonly wps: WpsObject) {
    super();
  }

  override load(): this {
    this.read.clear();
    return this;
  }

  private rows(property: string, read: () => unknown[][]): unknown[][] {
    if (!this.read.has(property)) this.read.set(property, read());
    return this.read.get(property)!;
  }

  private cell(row: number, column: number): WpsObject {
    return this.wps.Cells.Item(row + 1, column + 1);
  }

  get address(): string {
    return onSheet(this.sheet.name, cellsOf(this.wps));
  }

  get rowIndex(): number { return Number(this.wps.Row) - 1; }
  get columnIndex(): number { return Number(this.wps.Column) - 1; }
  get rowCount(): number { return Number(this.wps.Rows.Count); }
  get columnCount(): number { return Number(this.wps.Columns.Count); }

  get values(): unknown[][] {
    return this.rows('values', () => valuesOf(this.wps));
  }

  set values(rows: unknown[][]) {
    this.read.clear();
    put(this.wps, 'Value2', rows);
  }

  get formulas(): unknown[][] {
    return this.rows('formulas', () => rowsOf(this.wps.Formula));
  }

  set formulas(rows: unknown[][]) {
    this.read.clear();
    put(this.wps, 'Formula', rows);
  }

  /** WPS gives a range's number format as empty where its cells differ: then each cell's. */
  get numberFormat(): unknown[][] {
    return this.rows('numberFormat', () => {
      const shared: unknown = this.wps.NumberFormat;
      return Array.from({ length: this.rowCount }, (_r, row) => Array.from({ length: this.columnCount },
        (_c, column) => shared || this.cell(row, column).NumberFormat));
    });
  }

  set numberFormat(rows: unknown[][]) {
    this.read.clear();
    put(this.wps, 'NumberFormat', rows);
  }

  /** Each cell's look, read when the tools ask for that cell. */
  getCellProperties(): { value: { readonly format: Excel.CellPropertiesFormat }[][] } {
    const shared = lookOf(this.wps);
    const cell = (row: number, column: number) => this.cell(row, column);
    return { value: Array.from({ length: this.rowCount }, (_r, row) => Array.from({ length: this.columnCount },
      (_c, column) => ({ get format() { return cellFormat(cell(row, column), shared); } }))) };
  }

  /** The merged areas the range touches, looked for only in its rows that hold one. */
  getMergedAreasOrNullObject(): unknown {
    // MergeCells is false (or 0) where nothing is merged, true where all is, nothing where some is.
    const none = (merged: unknown) => merged === false || merged === 0;
    if (none(this.wps.MergeCells)) return NULL_OBJECT;
    const areas = new Set<string>();
    itemsOf(this.wps.Rows).forEach((row, at) => {
      if (none(row.MergeCells)) return;
      for (let column = 0; column < this.columnCount; column += 1) {
        const cell = this.cell(at, column);
        if (cell.MergeCells) areas.add(cellsOf(cell.MergeArea));
      }
    });
    return { isNullObject: areas.size === 0, load() { return this; },
      areas: { items: [...areas].map(address => ({ address })) } };
  }

  get format(): unknown {
    const range = this.wps;
    const sheet = this.sheet.wps;
    const border = (edge: string) => range.Borders.Item(EDGES[edge]);
    return {
      font: {
        set color(color: string) { range.Font.Color = rgb(color); },
        set size(size: number) { range.Font.Size = size; },
        set name(name: string) { range.Font.Name = name; },
        set bold(bold: boolean) { range.Font.Bold = bold; },
        set italic(italic: boolean) { range.Font.Italic = italic; },
        set underline(style: string) { range.Font.Underline = valueOf(UNDERLINES, style); },
        set strikethrough(strike: boolean) { range.Font.Strikethrough = strike; },
      },
      fill: { set color(color: string) { range.Interior.Color = rgb(color); } },
      set horizontalAlignment(alignment: string) { range.HorizontalAlignment = valueOf(ALIGNMENTS, alignment); },
      borders: { getItem: (edge: string) => ({
        set style(style: string) { border(edge).LineStyle = valueOf(LINE_STYLES, style); },
        set weight(weight: string) { border(edge).Weight = valueOf(WEIGHTS, weight); },
        set color(color: string) { border(edge).Color = rgb(color); },
      }) },
      // Office sets a width in points, WPS in characters of the standard font: the first column gives the ratio.
      set columnWidth(points: number) {
        const first = range.Columns.Item(1);
        const ratio = Number(first.ColumnWidth) / Number(first.Width);
        if (!Number.isFinite(ratio) || ratio <= 0) {
          throw new Error(`Column ${first.Address(false, false)} is hidden, so a width in points cannot be set here: `
            + 'unhide it first, or set the width in characters with execute_wps_js.');
        }
        range.ColumnWidth = points * ratio;
      },
      set useStandardWidth(_standard: boolean) { range.ColumnWidth = sheet.StandardWidth; },
      autofitColumns: () => range.Columns.AutoFit(),
      set rowHeight(points: number) { range.RowHeight = points; },
      set useStandardHeight(_standard: boolean) { range.RowHeight = sheet.StandardHeight; },
      autofitRows: () => range.Rows.AutoFit(),
    };
  }

  /** This range's notes by cell, which a ``Copy`` replaces with the source's. */
  private notes(): Map<string, WpsObject> {
    const block = parseBlock(cellsOf(this.wps), { rows: 0, columns: 0 })!;
    return new Map(itemsOf(this.sheet.wps.Comments).map(comment => [cellsOf(comment.Parent), comment] as const)
      .filter(([cell]) => contains(block, parseBlock(cell, { rows: 0, columns: 0 })!)));
  }

  /**
   * ``source`` repeated over this range, as Excel's ``copyFrom``: ``Copy`` for all, a plain write of the values or
   * of the formulas in R1C1 (so relative references move), and for formats a ``Copy`` that then puts the range's
   * own contents and notes back. None goes through the clipboard, which stays the user's.
   */
  copyFrom(source: Range, copyType: string): void {
    this.read.clear();
    const tiled = (rows: unknown[][]) => Array.from({ length: this.rowCount }, (_r, row) => Array.from(
      { length: this.columnCount }, (_c, column) => rows[row % rows.length][column % rows[0].length]));
    if (copyType === 'Values') {
      put(this.wps, 'Value2', tiled(valuesOf(source.wps)));
    } else if (copyType === 'Formulas') {
      put(this.wps, 'FormulaR1C1', tiled(rowsOf(source.wps.FormulaR1C1)));
    } else if (copyType === 'Formats') {
      const kept = rowsOf(this.wps.Formula);
      const notes = new Map([...this.notes()].map(([cell, comment]) => [cell, String(comment.Text())]));
      source.wps.Copy(this.wps);
      put(this.wps, 'Formula', kept);
      const copied = this.notes();
      for (const [cell, comment] of copied) if (!notes.has(cell)) comment.Delete();
      for (const [cell, text] of notes) {
        if (copied.has(cell)) copied.get(cell)!.Text(text);
        else this.sheet.wps.Range(cell).AddComment(text);
      }
    } else {
      source.wps.Copy(this.wps);
    }
  }
}
