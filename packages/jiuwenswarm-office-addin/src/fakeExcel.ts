/**
 * A workbook that a fake ``Excel.run`` reads and writes, for the tests of the Excel tools, state and code runner.
 * Changes land at once and loaded properties are readable without a ``sync``; a formula's value is what
 * ``evaluate`` gives. Only the Office.js surface the pane uses is here, as classes, as the host's objects are.
 */
import { type Block, blockName, cellName, overlap, parseBlock } from './excelAddress';
import type { ExcelRun } from './excelTools';

interface FakeCell {
  value: unknown;
  formula?: string;
  numberFormat?: string;
  bold?: boolean;
  fill?: string;
  note?: string;
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

  set(properties: object): void {
    Object.assign(this, properties);
  }
}

export class FakeSheet extends Loadable {
  readonly cells = new Map<string, FakeCell>();
  readonly merged: string[] = [];
  readonly tables: { items: FakeTable[]; load(): void } = { items: [], load: () => undefined };
  readonly notes = new FakeNotes(this);

  constructor(readonly book: FakeWorkbook, public name: string, readonly id: string) {
    super();
  }

  showOutlineLevels(): void {}

  cell(row: number, column: number): FakeCell {
    const at = cellName(row, column);
    let cell = this.cells.get(at);
    if (!cell) this.cells.set(at, cell = { value: '' });
    return cell;
  }

  /** Puts values (and formulas, for text starting with ``=``) from ``A1`` of ``block`` on. */
  put(address: string, rows: unknown[][]): void {
    const { top, left } = parseBlock(address, { rows: 0, columns: 0 })!;
    rows.forEach((row, r) => row.forEach((value, c) => this.write(top + r, left + c, value)));
  }

  write(row: number, column: number, value: unknown): void {
    const cell = this.cell(row, column);
    if (typeof value === 'string' && value.startsWith('=')) {
      cell.formula = value;
      cell.value = this.book.evaluate(value);
    } else {
      delete cell.formula;
      cell.value = typeof value === 'string' && value.startsWith('\'') ? value.slice(1) : value;
    }
  }

  extent(): { rows: number; columns: number } {
    let rows = 0;
    let columns = 0;
    for (const [at, cell] of this.cells) {
      if (cell.value === '' && cell.formula === undefined) continue;
      const { bottom, right } = parseBlock(at, { rows: 0, columns: 0 })!;
      rows = Math.max(rows, bottom);
      columns = Math.max(columns, right);
    }
    return { rows, columns };
  }

  getUsedRangeOrNullObject(): FakeRange {
    const { rows, columns } = this.extent();
    const range = new FakeRange(this, { top: 1, left: 1, bottom: Math.max(rows, 1), right: Math.max(columns, 1) });
    range.isNullObject = rows === 0;
    return range;
  }

  getRange(address: string): FakeRange {
    return new FakeRange(this, parseBlock(address, this.extent())!);
  }

  getRangeByIndexes(row: number, column: number, rows: number, columns: number): FakeRange {
    return new FakeRange(this, { top: row + 1, left: column + 1, bottom: row + rows, right: column + columns });
  }
}

class FakeNotes {
  constructor(private readonly sheet: FakeSheet) {}

  get items() {
    return [...this.sheet.cells].filter(([, cell]) => cell.note !== undefined).map(([at]) => this.note(at));
  }

  load(): void {}

  getItemOrNullObject(at: string) {
    const note = this.note(at);
    note.isNullObject = this.sheet.cells.get(at)?.note === undefined;
    return note;
  }

  add(at: string, text: string): void {
    this.sheet.getRange(at).cell().note = text;
  }

  private note(at: string) {
    const { sheet } = this;
    return new (class extends Loadable {
      get content() {
        return sheet.cells.get(at)?.note ?? '';
      }
      set content(text: string) {
        sheet.cells.get(at)!.note = text;
      }
      getLocation = () => ({ address: `${sheet.name}!${at}`, load: () => undefined });
    })();
  }
}

class FakeTable {
  readonly columns: { items: { name: string }[]; load(): void };

  constructor(private readonly sheet: FakeSheet, readonly name: string, private readonly address: string,
    headers: string[]) {
    this.columns = { items: headers.map(header => ({ name: header })), load: () => undefined };
  }

  getRange() {
    return { address: `${this.sheet.name}!${this.address}`, load() { return this; } };
  }
}

export class FakeRange extends Loadable {
  constructor(readonly sheet: FakeSheet, readonly block: Block) {
    super();
  }

  get address(): string {
    return `${this.sheet.name}!${blockName(this.block)}`;
  }
  get rowIndex(): number { return this.block.top - 1; }
  get columnIndex(): number { return this.block.left - 1; }
  get rowCount(): number { return this.block.bottom - this.block.top + 1; }
  get columnCount(): number { return this.block.right - this.block.left + 1; }

  cell(): FakeCell {
    return this.sheet.cell(this.block.top, this.block.left);
  }

  private grid<T>(read: (cell: FakeCell) => T): T[][] {
    return Array.from({ length: this.rowCount }, (_r, r) => Array.from({ length: this.columnCount },
      (_c, c) => read(this.sheet.cell(this.block.top + r, this.block.left + c))));
  }

  private fill(rows: unknown[][], write: (row: number, column: number, value: unknown) => void): void {
    rows.forEach((row, r) => row.forEach((value, c) => write(this.block.top + r, this.block.left + c, value)));
  }

  get values(): unknown[][] { return this.grid(cell => cell.value); }
  set values(rows: unknown[][]) { this.fill(rows, (r, c, value) => this.sheet.write(r, c, value)); }
  get formulas(): unknown[][] { return this.grid(cell => cell.formula ?? cell.value); }
  set formulas(rows: unknown[][]) { this.fill(rows, (r, c, value) => this.sheet.write(r, c, value)); }
  get numberFormat(): unknown[][] { return this.grid(cell => cell.numberFormat ?? 'General'); }
  set numberFormat(rows: unknown[][]) {
    this.fill(rows, (r, c, value) => { this.sheet.cell(r, c).numberFormat = String(value); });
  }

  get format() {
    const cells = () => this.grid(cell => cell).flat();
    return {
      font: {
        set bold(bold: boolean) { cells().forEach(cell => { cell.bold = bold; }); },
        set color(_color: string) {}, set size(_size: number) {}, set name(_name: string) {},
        set italic(_italic: boolean) {}, set underline(_underline: string) {}, set strikethrough(_strike: boolean) {},
      },
      fill: { set color(color: string) { cells().forEach(cell => { cell.fill = color; }); } },
      borders: { getItem: () => ({}) },
      autofitColumns: () => undefined,
      autofitRows: () => undefined,
    };
  }

  getCellProperties() {
    return { value: this.grid(cell => ({ format: {
      font: { bold: cell.bold ?? false, italic: false, underline: 'None', strikethrough: false, size: 11,
        color: '#000000', name: 'Calibri' },
      fill: { color: cell.fill ?? '#FFFFFF' }, horizontalAlignment: 'General',
      borders: { top: { style: 'None' }, bottom: { style: 'None' }, left: { style: 'None' }, right: { style: 'None' } },
    } })) };
  }

  getMergedAreasOrNullObject() {
    const items = this.sheet.merged
      .filter(address => overlap(parseBlock(address, { rows: 0, columns: 0 })!, this.block))
      .map(address => ({ address: `${this.sheet.name}!${address}` }));
    return { isNullObject: items.length === 0, areas: { items }, load() { return this; } };
  }

  copyFrom(source: FakeRange, copyType: string): void {
    const from = source.grid(cell => cell);
    const size = {
      rows: Math.max(this.rowCount, source.rowCount), columns: Math.max(this.columnCount, source.columnCount),
    };
    for (let r = 0; r < size.rows; r += 1) {
      for (let c = 0; c < size.columns; c += 1) {
        const origin = from[r % from.length][c % from[0].length];
        const target = this.sheet.cell(this.block.top + r, this.block.left + c);
        if (copyType === 'Formats') Object.assign(target, { bold: origin.bold, fill: origin.fill });
        else Object.assign(target, copyType === 'Values' ? { value: origin.value } : { ...origin });
      }
    }
  }
}

class FakeWorksheets {
  readonly onChanged = new FakeEvent();
  readonly onAdded = new FakeEvent();
  readonly onDeleted = new FakeEvent();
  readonly onNameChanged = new FakeEvent();

  constructor(private readonly book: FakeWorkbook) {}

  get items(): FakeSheet[] { return this.book.sheets; }
  load(): void {}
  getItem(idOrName: string): FakeSheet {
    return this.book.sheets.find(sheet => sheet.id === idOrName || sheet.name === idOrName)!;
  }
  getItemOrNullObject(name: string): FakeSheet {
    const sheet = this.book.sheets.find(item => item.name === name);
    if (sheet) return sheet;
    const missing = new FakeSheet(this.book, name, '');
    missing.isNullObject = true;
    return missing;
  }
  getActiveWorksheet(): FakeSheet { return this.book.active; }
}

export class FakeWorkbook {
  readonly sheets: FakeSheet[] = [];
  active!: FakeSheet;
  name = 'Budget.xlsx';
  selection = '';
  chart?: { id: string; name: string; title: string };
  readonly worksheets = new FakeWorksheets(this);
  /** The context ``run`` hands out; a test can replace its ``sync``. */
  readonly context = new FakeContext(new FakeWorkbookApi(this));

  constructor(sheetNames: string[], readonly evaluate: (formula: string) => unknown = () => 0) {
    sheetNames.forEach((name, index) => this.addSheet(name, `{sheet-${index + 1}}`));
    this.active = this.sheets[0];
  }

  readonly run: ExcelRun = batch => batch(this.context as unknown as Excel.RequestContext);

  sheet(name: string): FakeSheet {
    return this.sheets.find(sheet => sheet.name === name)!;
  }

  addSheet(name: string, id: string): FakeSheet {
    const sheet = new FakeSheet(this, name, id);
    this.sheets.push(sheet);
    return sheet;
  }

  addTable(sheet: string, name: string, address: string, headers: string[]): void {
    this.sheet(sheet).tables.items.push(new FakeTable(this.sheet(sheet), name, address, headers));
  }

  /** The user's own change events, as Excel raises them. */
  async raise(event: 'onChanged' | 'onAdded' | 'onDeleted' | 'onNameChanged', args: Record<string, unknown>) {
    for (const handler of this.worksheets[event].handlers) await handler(args);
  }

  /** What ``A1``-addressed cells hold, for assertions. */
  valueAt(sheet: string, at: string): unknown {
    return this.sheet(sheet).cells.get(at)?.value ?? '';
  }

}

class FakeLinkedDataTypes {
  requestRefreshAll(): void {}
}

class FakeWorkbookApi extends Loadable {
  readonly linkedDataTypes = new FakeLinkedDataTypes();

  constructor(private readonly book: FakeWorkbook) {
    super();
  }

  get name(): string { return this.book.name; }
  get worksheets(): FakeWorksheets { return this.book.worksheets; }
  getSelectedRanges() {
    return { address: this.book.selection, load() {} };
  }
  getActiveChartOrNullObject() {
    const { chart } = this.book;
    return { isNullObject: !chart, id: chart?.id, name: chart?.name, title: { text: chart?.title ?? '', load() {} },
      load() {} };
  }
}

class FakeContext {
  constructor(readonly workbook: FakeWorkbookApi) {}
  async sync(): Promise<void> {}
}
