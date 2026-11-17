/**
 * The workbook's state as the model reads it, in the industry add-in's blocks: ``<user_context>`` (the active sheet,
 * the selected ranges or chart) each turn; ``<initial_state>`` (each sheet's id and size, its Excel tables and their
 * columns) with a conversation's first message from this pane; and ``<user_changes>``, what the user changed in the
 * workbook since the last message, from the host's change events. ``read_surface_context`` gives the
 * ``<user_context>`` block alone.
 */
import type { OfficeHost } from './appContract';
import { cellsOf } from './excelAddress';
import type { ExcelRun } from './excelTools';
import type { ClientState } from './sessionFacade';
import { quoted } from './stateText';

/** Changes the next message lists at most, the latest first kept, and how much of a value each shows. */
const MAX_CHANGES = 100;
const MAX_VALUE_CHARS = 200;
/** How long a sheet or chart name may run in ``<user_context>``, and a selected range. */
const MAX_NAME_CHARS = 60;
const MAX_RANGE_CHARS = 200;
/** Change events say whether this add-in made the change from ExcelApi 1.14; without that, none are listed. */
const CHANGES_SET = ['ExcelApi', '1.14'] as const;
const RENAMES_SET = ['ExcelApi', '1.17'] as const;

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

/** The ``<user_changes>`` block of ``changes``; empty when there are none. */
function changesBlock(changes: Change[], omitted: number): string {
  if (!changes.length) return '';
  const lines = changes.map(({ type, sheetName, address, before, after }) => [
    `[${type}]`, `sheetName:${shown(sheetName)}`, ...(address ? [`range:${cellsOf(address)}`] : []),
    ...(before !== undefined || after !== undefined ? [`${shown(before)}→${shown(after)}`] : []),
  ].join(' '));
  if (omitted) lines.push(`[${omitted} older change${omitted === 1 ? ' is' : 's are'} not listed.]`);
  return `<user_changes>\n${lines.join('\n')}\n</user_changes>`;
}

export class ExcelState {
  private changes: Change[] = [];
  private omitted = 0;
  /** Sheet names by id, for change events, which name the sheet by id. */
  private readonly names = new Map<string, string>();
  /** Conversations whose messages from this pane already carried ``<initial_state>``. */
  private readonly introduced = new Set<string>();

  constructor(private readonly run: ExcelRun, private readonly office: Pick<Office.Context, 'contentLanguage'
    | 'displayLanguage'>) {}

  /** Listens to the user's changes for as long as the pane runs, on ``context``, a context of its own. */
  async watch(context: Excel.RequestContext, host: OfficeHost): Promise<void> {
    if (!host.isSetSupported(...CHANGES_SET)) return;
    const sheets = context.workbook.worksheets;
    sheets.load('items/id,items/name');
    await context.sync();
    sheets.items.forEach(sheet => this.names.set(sheet.id, sheet.name));
    const nameOf = (id: string) => this.names.get(id) ?? id;
    sheets.onChanged.add(async event => {
      if (event.triggerSource === 'ThisLocalAddin') return;
      this.record({ type: 'CellChanged', sheetName: nameOf(event.worksheetId), address: event.address,
        before: event.details?.valueBefore, after: event.details?.valueAfter });
    });
    sheets.onAdded.add(async event => {
      const sheet = context.workbook.worksheets.getItem(event.worksheetId);
      sheet.load('name');
      await context.sync();
      this.names.set(event.worksheetId, sheet.name);
      this.record({ type: 'SheetAdded', sheetName: sheet.name });
    });
    sheets.onDeleted.add(async event => {
      this.record({ type: 'SheetDeleted', sheetName: nameOf(event.worksheetId) });
      this.names.delete(event.worksheetId);
    });
    if (host.isSetSupported(...RENAMES_SET)) {
      sheets.onNameChanged.add(async event => {
        this.names.set(event.worksheetId, event.nameAfter);
        this.record({ type: 'SheetRenamed', sheetName: event.nameAfter, before: event.nameBefore,
          after: event.nameAfter });
      });
    }
    await context.sync();
  }

  /**
   * The state the next message of conversation ``sessionId`` carries; once it is sent, the changes it lists (or,
   * with ``<initial_state>``, the ones before) are not listed again. A workbook that cannot be read just now (a
   * cell is being edited) still lets the message go, saying so.
   */
  async forMessage(sessionId: string): Promise<ClientState> {
    const changes = [...this.changes];
    const { omitted } = this;
    try {
      const now = await this.current();
      const introduce = !this.introduced.has(sessionId);
      const first = introduce ? await this.initialState() : changesBlock(changes, omitted);
      return { text: [first, now].filter(Boolean).join('\n'), sent: () => {
        if (introduce) this.introduced.add(sessionId);
        this.changes = this.changes.filter(change => !changes.includes(change));
        this.omitted = Math.max(0, this.omitted - omitted);
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

  private record(change: Change): void {
    this.changes.push(change);
    if (this.changes.length > MAX_CHANGES) {
      this.changes.shift();
      this.omitted += 1;
    }
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
