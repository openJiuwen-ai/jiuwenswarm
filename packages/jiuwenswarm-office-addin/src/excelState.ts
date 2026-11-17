/**
 * The workbook's state as the model reads it, in the industry add-in's blocks: ``<user_context>`` (the active sheet,
 * the selected ranges or chart) each turn; ``<initial_state>`` (each sheet's id and size, its Excel tables and their
 * columns) with a conversation's first message from this pane; and ``<user_changes>``, what the user changed in the
 * workbook since the last message, from the host's change events less what the pane's own writes left.
 * ``read_surface_context`` gives the ``<user_context>`` block alone.
 */
import type { OfficeHost } from './appContract';
import { cellName, cellsOf, parseBlock } from './excelAddress';
import type { ExcelRun } from './excelTools';
import { type HostWriteQueue, type Look, type Looks, UserChanges } from './hostWriteQueue';
import type { ClientState } from './sessionFacade';
import { quoted } from './stateText';

/** How much of a changed value a change shows. */
const MAX_VALUE_CHARS = 200;
/** How long a sheet or chart name may run in ``<user_context>``, and a selected range. */
const MAX_NAME_CHARS = 60;
const MAX_RANGE_CHARS = 200;
const CHANGES_SET = ['ExcelApi', '1.9'] as const;
const RENAMES_SET = ['ExcelApi', '1.17'] as const;
/** A worksheet's size, which bounds a whole column or row a change event names. */
const SHEET = { rows: 1_048_576, columns: 16_384 };
/** Cells from one corner to another, as against a whole column or row. */
const BOUNDED = /^\$?[A-Z]+\$?\d+(?::\$?[A-Z]+\$?\d+)?$/i;

/** What the user changed: a cell edit, or a sheet added, deleted or renamed. */
interface Change {
  type: 'CellChanged' | 'SheetAdded' | 'SheetDeleted' | 'SheetRenamed';
  sheetName: string;
  address?: string;
  before?: unknown;
  after?: unknown;
}

/** Characters a name may not bring into a state line: quotes but the apostrophe, brackets, bars, line breaks. */
const BREAKING = /(?!')[\p{Quotation_Mark}<>|\u2039\u203A\uFF1C\uFF1E\uFF5C]|[\r\n\v\f\u0085\u2028\u2029]/gu;
const INVISIBLE = /[\p{Cf}\p{Co}]/gu;

/** A name as a state line shows it, and whether that differs from the name. */
function label(text: string, max: number): { shown: string; altered: boolean } {
  const flat = text.replace(INVISIBLE, '').replace(BREAKING, ' ').replace(/\s+/g, ' ').trim();
  const shown = [...flat].length > max ? `${[...flat].slice(0, max).join('').trimEnd()}…` : flat;
  return { shown, altered: shown !== text };
}

/** Selected ranges as Excel joins them, split at the commas outside a quoted sheet name. */
function selectedRanges(address: string): string[] {
  return address.match(/(?:'(?:[^']|'')*'|[^,])+/g) ?? [];
}

/** A changed value: text quoted and cut short, a number or flag as it is. */
function shown(value: unknown): string {
  if (value === undefined || value === null) return '""';
  return typeof value === 'string' ? quoted(value, MAX_VALUE_CHARS) : String(value);
}

/** A change as ``<user_changes>`` lists it. */
function changeLine({ type, sheetName, address, before, after }: Change): string {
  return [
    `[${type}]`, `sheetName:${shown(sheetName)}`, ...(address ? [`range:${cellsOf(address)}`] : []),
    ...(before !== undefined || after !== undefined ? [`${shown(before)}→${shown(after)}`] : []),
  ].join(' ');
}

/** The sheet and the cells of an address as Excel gives it, as ``'Q1 data'!A1:B2``. */
function sheetAndCells(address: string): { sheetName: string; cells: string } {
  const bang = address.lastIndexOf('!');
  const sheet = address.slice(0, bang).trim();
  return { sheetName: /^'.*'$/.test(sheet) ? sheet.slice(1, -1).replace(/''/g, "'") : sheet,
    cells: address.slice(bang + 1).trim() };
}

/** A cell's place in the ledger of the pane's writes: the sheet's id and the cell, as ``{id}!B2``. */
function cellPlace(sheetId: string, row: number, column: number): string {
  return `${sheetId}!${cellName(row, column)}`;
}

/** A cell as the ledger compares it: its formula, else its value; null when it is empty. */
function cellLook(formula: unknown): Look {
  return formula === '' || formula === null || formula === undefined ? null : String(formula);
}

export class ExcelState {
  private readonly changes = new UserChanges<Change>(changeLine);
  /** Sheet names by id, for change events, which name the sheet by id. */
  private readonly names = new Map<string, string>();
  /** Conversations whose messages from this pane already carried ``<initial_state>``. */
  private readonly introduced = new Set<string>();

  constructor(private readonly run: ExcelRun, private readonly office: Pick<Office.Context, 'contentLanguage'
    | 'displayLanguage'>, private readonly writes: HostWriteQueue) {}

  /**
   * Listens to the user's changes for as long as the pane runs, on ``context``, a context of its own. A change at a
   * cell or sheet that still shows what the pane's last write left there is taken as that write's.
   */
  async watch(context: Excel.RequestContext, host: OfficeHost): Promise<void> {
    if (!host.isSetSupported(...CHANGES_SET)) return;
    this.writes.observe(touched => this.looks(touched));
    const sheets = context.workbook.worksheets;
    sheets.load('items/id,items/name');
    await context.sync();
    sheets.items.forEach(sheet => this.names.set(sheet.id, sheet.name));
    const nameOf = (id: string) => this.names.get(id) ?? id;
    const written = (sheetId: string, name: Look) => this.writes.leftAt(sheetId) === name;
    sheets.onChanged.add(async event => {
      // Excel tags its add-in's writes where it can. Only the tag tells a column the pane deleted, which also moves
      // cells whose content stays as it was, from the user's.
      if (event.triggerSource === 'ThisLocalAddin' || await this.cellsWritten(context, event)) return;
      this.changes.add({ type: 'CellChanged', sheetName: nameOf(event.worksheetId), address: event.address,
        before: event.details?.valueBefore, after: event.details?.valueAfter });
    });
    sheets.onAdded.add(async event => {
      await this.writes.idle();
      const sheet = context.workbook.worksheets.getItem(event.worksheetId);
      sheet.load('name');
      await context.sync();
      this.names.set(event.worksheetId, sheet.name);
      if (!written(event.worksheetId, sheet.name)) this.changes.add({ type: 'SheetAdded', sheetName: sheet.name });
    });
    sheets.onDeleted.add(async event => {
      await this.writes.idle();
      if (!written(event.worksheetId, null)) {
        this.changes.add({ type: 'SheetDeleted', sheetName: nameOf(event.worksheetId) });
      }
      this.names.delete(event.worksheetId);
    });
    if (host.isSetSupported(...RENAMES_SET)) {
      sheets.onNameChanged.add(async event => {
        await this.writes.idle();
        this.names.set(event.worksheetId, event.nameAfter);
        if (written(event.worksheetId, event.nameAfter)) return;
        this.changes.add({ type: 'SheetRenamed', sheetName: event.nameAfter, before: event.nameBefore,
          after: event.nameAfter });
      });
    }
    await context.sync();
  }

  /** Whether every cell of a change still shows what the pane's last write left there. */
  private async cellsWritten(context: Excel.RequestContext, event: Excel.WorksheetChangedEventArgs): Promise<boolean> {
    await this.writes.idle();
    const address = cellsOf(event.address);
    const block = parseBlock(address, SHEET);
    if (!block) return false;
    const places: string[] = [];
    // Stops at the first cell no write of the pane changed, before a whole column is read.
    for (let row = block.top; row <= block.bottom; row += 1) {
      for (let column = block.left; column <= block.right; column += 1) {
        const place = cellPlace(event.worksheetId, row, column);
        if (this.writes.leftAt(place) === undefined) return false;
        places.push(place);
      }
    }
    const range = context.workbook.worksheets.getItem(event.worksheetId).getRange(address);
    range.load('formulas');
    await context.sync();
    return range.formulas.flat().every((formula, index) => this.writes.leftAt(places[index]) === cellLook(formula));
  }

  /**
   * Each sheet's name by its id, and the formula or value of each cell in ``touched`` (addresses as Excel gives
   * them) by its place: of a whole column or row, the part in use.
   */
  private looks(touched: readonly string[]): Promise<Looks> {
    return this.run(async context => {
      const sheets = context.workbook.worksheets;
      sheets.load('items/id,items/name');
      await context.sync();
      const ranges = touched.flatMap(selectedRanges).flatMap(address => {
        const { sheetName, cells } = sheetAndCells(address);
        const sheet = sheets.items.find(item => item.name === sheetName);
        if (!sheet) return [];
        const range = BOUNDED.test(cells) ? sheet.getRange(cells)
          : sheet.getRange(cells).getUsedRangeOrNullObject(true);
        range.load('rowIndex,columnIndex,formulas');
        return [{ id: sheet.id, range }];
      });
      // Ranges too large to read back leave their cells out of the ledger; the sheets still count.
      const read = await context.sync().then(() => ranges, () => []);
      const looks: Looks = new Map(sheets.items.map(sheet => [sheet.id, sheet.name]));
      for (const { id, range } of read.filter(({ range }) => !range.isNullObject)) {
        range.formulas.forEach((row, r) => row.forEach((formula, c) => looks.set(
          cellPlace(id, range.rowIndex + r + 1, range.columnIndex + c + 1), cellLook(formula))));
      }
      return looks;
    });
  }

  /**
   * The state the next message of conversation ``sessionId`` carries; once it is sent, the changes it lists (or,
   * with ``<initial_state>``, the ones before) are not listed again. A workbook that cannot be read just now (a
   * cell is being edited) still lets the message go, saying so.
   */
  async forMessage(sessionId: string): Promise<ClientState> {
    const changes = this.changes.block();
    try {
      const now = await this.current();
      const introduce = !this.introduced.has(sessionId);
      const first = introduce ? await this.initialState() : changes.text;
      return { text: [first, now].filter(Boolean).join('\n'), sent: () => {
        if (introduce) this.introduced.add(sessionId);
        changes.sent();
      } };
    } catch (error) {
      return { text: `<user_context>\nThe workbook could not be read when this message was sent (${
        error instanceof Error ? error.message : String(error)}).\n</user_context>`, sent: () => undefined };
    }
  }

  /** The active sheet, and the selected chart or ranges. */
  current(): Promise<string> {
    return this.run(async context => {
      const sheet = context.workbook.worksheets.getActiveWorksheet();
      sheet.load('id,name');
      const chart = context.workbook.getActiveChartOrNullObject();
      chart.load('id,name');
      const selection = context.workbook.getSelectedRanges();
      selection.load('address');
      await context.sync();
      const name = label(sheet.name, MAX_NAME_CHARS);
      const lines = [`Current active sheet: "${name.shown}" (sheet id ${sheet.id}${
        name.altered ? '; name shown with characters removed' : ''})`];
      if (!chart.isNullObject) {
        chart.title.load('text');
        await context.sync();
        lines.push(`Selected chart: "${label(chart.title.text || chart.name, MAX_NAME_CHARS).shown}" (id: "${
          label(chart.id, MAX_NAME_CHARS).shown}")`);
      } else if (selection.address) {
        lines.push(`Selected ranges: ${selectedRanges(selection.address)
          .map(range => label(range, MAX_RANGE_CHARS).shown).join(' | ')}`);
      }
      return `<user_context>\n${lines.join('\n')}\n</user_context>`;
    });
  }

  private initialState(): Promise<string> {
    return this.run(async context => {
      const { workbook } = context;
      workbook.load('name');
      workbook.worksheets.load('items/id,items/name');
      await context.sync();
      const sheets = workbook.worksheets.items.map(sheet => {
        const used = sheet.getUsedRangeOrNullObject(true);
        used.load('rowIndex,columnIndex,rowCount,columnCount');
        sheet.tables.load('items/name');
        return { sheet, used };
      });
      await context.sync();
      const tables = sheets.map(({ sheet }) => sheet.tables.items.map(table => {
        const range = table.getRange().load('address');
        table.columns.load('items/name');
        return { table, range };
      }));
      await context.sync();
      const state = {
        fileName: workbook.name,
        sheetsMetadata: sheets.map(({ sheet, used }, index) => ({
          id: sheet.id, name: sheet.name,
          maxRows: used.isNullObject ? 0 : used.rowIndex + used.rowCount,
          maxColumns: used.isNullObject ? 0 : used.columnIndex + used.columnCount,
          tables: tables[index].map(({ table, range }) => ({
            name: table.name, range: cellsOf(range.address), headers: table.columns.items.map(column => column.name),
          })),
        })),
        totalSheets: sheets.length,
        contentLanguage: this.office.contentLanguage ?? null,
        displayLanguage: this.office.displayLanguage ?? null,
      };
      // A ``<`` in a name from the workbook is written ``\u003c``, as JSON allows, so no name can close the block.
      return `<initial_state>\n${JSON.stringify(state, null, 2).replace(/</g, '\\u003c')}\n</initial_state>`;
    });
  }
}
