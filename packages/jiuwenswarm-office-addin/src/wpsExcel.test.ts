import { describe, expect, it } from 'vitest';

import excelTools from '@office/excel/tools.json';

import { type ClientToolRuntime, ClientToolDispatcher, joinRuntimes } from './clientToolDispatcher';
import { EXCEL, WPS_SPREADSHEET } from './excelApp';
import { type Block, blockName, cellsOf, contains, overlap, parseBlock } from './excelAddress';
import { screenWpsSheetWrite, WPS_SHEET_UNREACHABLE } from './excelGuard';
import { ExcelState } from './excelState';
import { excelRuntime, type ExcelRun } from './excelTools';
import { type FakeSheet, FakeRange, FakeWorkbook } from './fakeExcel';
import type { JsonObject } from './gatewayClient';
import { HostWriteQueue } from './hostWriteQueue';
import { wpsCodeRuntime } from './officeCodeRunner';
import type { WpsObject } from './wpsDocument';
import { WPS_EXCEL_HOST, wpsExcelRun } from './wpsExcel';

/** A WPS collection, read by 1-based index or by name. */
const collection = (items: () => WpsObject[]) => ({
  get Count() { return items().length; },
  Item: (key: number | string) => (typeof key === 'number' ? items()[key - 1]
    : items().find(item => item.Name === key)),
});
const bare = (rows: unknown[][]) => (rows.length === 1 && rows[0].length === 1 ? rows[0][0] : rows);
const rows = (value: unknown) => (Array.isArray(value) ? value : [[value]]);
const same = (values: unknown[][]) => values.flat().every(value => value === values.flat()[0])
  ? values.flat()[0] : null;
const bgr = (color: string) => {
  const value = parseInt(color.slice(1), 16);
  return ((value & 0xff) << 16) | (value & 0xff00) | (value >> 16);
};
const hex = (value: number) => `#${[value & 0xff, (value >> 8) & 0xff, value >> 16]
  .map(channel => channel.toString(16).padStart(2, '0')).join('').toUpperCase()}`;

/** WPS Spreadsheets' object model over a fake workbook's cells, as far as the adapter reads and writes it. */
function etRange(sheet: FakeSheet, block: Block): WpsObject {
  const office = new FakeRange(sheet, block);
  const at = (top: number, left: number, bottom = top, right = left) => etRange(sheet, { top, left, bottom, right });
  const looks = () => office.getCellProperties().value.map(row => row.map(cell => cell.format));
  const merges = () => sheet.merged.map(area => parseBlock(area, { rows: 0, columns: 0 })!)
    .filter(area => overlap(area, block));
  const note = () => sheet.cells.get(blockName(block))?.note;
  return {
    office, Address: () => blockName(block), Row: block.top, Column: block.left,
    Count: office.rowCount * office.columnCount,
    get Rows() {
      return { ...collection(() => Array.from({ length: office.rowCount },
        (_, r) => at(block.top + r, block.left, block.top + r, block.right))), AutoFit() {} };
    },
    Columns: { Count: office.columnCount, AutoFit() {} },
    Cells: { Item: (row: number, column: number) => at(block.top + row - 1, block.left + column - 1) },
    // An error reads as nothing, as an empty cell does; only its text shows it.
    get Value2() {
      return bare(office.values.map((row, r) => row.map((value, c) => (String(value).startsWith('#')
        && String(office.formulas[r][c]).startsWith('=') ? null : value))));
    }, set Value2(value) { office.values = rows(value); },
    get Text() { return String(office.values[0][0]); },
    get Formula() { return bare(office.formulas); }, set Formula(value) { office.formulas = rows(value); },
    get FormulaR1C1() { return bare(office.formulas); }, set FormulaR1C1(value) { office.formulas = rows(value); },
    get NumberFormat() { return same(office.numberFormat) ?? ''; }, set NumberFormat(value) {
      office.numberFormat = rows(value);
    },
    Font: { get Bold() { return same(looks().map(row => row.map(look => look.font.bold))); },
      set Bold(bold: unknown) { office.format.font.bold = Boolean(bold); },
      Italic: false, Underline: -4142, Strikethrough: false, Size: 11, Color: 0, Name: 'Calibri' },
    Interior: { get Color() {
      const fill = same(looks().map(row => row.map(look => look.fill.color)));
      return fill === null ? null : bgr(String(fill));
    }, set Color(color: number | null) { office.format.fill.color = hex(Number(color)); } },
    HorizontalAlignment: 1,
    Borders: { Item: () => ({ LineStyle: -4142, Weight: 2, Color: 0 }) },
    get MergeCells() {
      const areas = merges();
      return !areas.length ? false : areas.some(area => contains(area, block)) ? true : null;
    },
    get MergeArea() { const area = merges()[0]; return at(area.top, area.left, area.bottom, area.right); },
    get Comment() {
      return note() === undefined ? null : { Parent: at(block.top, block.left),
        Text: (text?: string) => { if (text === undefined) return note(); office.cell().note = text; return text; },
        Delete: () => { delete office.cell().note; } };
    },
    AddComment: (text: string) => { office.cell().note = text; },
    Copy: (destination: WpsObject) => destination.office.copyFrom(office, 'All'),
  };
}

function etWorkbook(book: FakeWorkbook): WpsObject {
  const etSheet = (sheet: FakeSheet): WpsObject => ({
    get Name() { return sheet.name; },
    get UsedRange() { return etRange(sheet, sheet.getUsedRangeOrNullObject().block); },
    Range: (address: string) => etRange(sheet, parseBlock(address, sheet.extent())!),
    Comments: collection(() => [...sheet.cells].filter(([, cell]) => cell.note !== undefined)
      .map(([at]) => etSheet(sheet).Range(at).Comment)),
    ListObjects: collection(() => sheet.tables.items.map(table => ({ Name: table.name,
      Range: etSheet(sheet).Range(cellsOf(table.getRange().address)),
      ListColumns: collection(() => table.columns.items.map(column => ({ Name: column.name }))) }))),
  });
  return {
    get Name() { return book.name; },
    Worksheets: collection(() => book.sheets.map(etSheet)),
    get ActiveSheet() { return etSheet(book.active); },
    ActiveChart: null,
    Windows: collection(() => [{ get Selection() {
      const areas = book.selection.split(',').map(address => address.split('!'));
      const sheet = book.sheet(areas[0][0]);
      return { Worksheet: etSheet(sheet), Areas: collection(() => areas
        .map(([, cells]) => etSheet(sheet).Range(cells))) };
    } }]),
  };
}

function budget(): FakeWorkbook {
  const book = new FakeWorkbook(['Sheet1', 'Data'], formula => (formula === '=A2*2' ? 20 : '#NAME?'));
  const sheet = book.sheet('Sheet1');
  sheet.put('A1', [['Item', 'Cost'], ['Rent', 10], ['Food', '=A2*2']]);
  sheet.getRange('A1:B1').format.font.bold = true;
  sheet.notes.add('A1', 'Source: ledger');
  sheet.merged.push('D1:E1');
  book.addTable('Sheet1', 'Costs', 'A1:B3', ['Item', 'Cost']);
  book.selection = 'Sheet1!A1:B2,Sheet1!D4';
  return book;
}

async function call(app: typeof EXCEL, runtime: ClientToolRuntime, toolName: string, input: JsonObject) {
  const result = await new ClientToolDispatcher(app, runtime, new HostWriteQueue())
    .execute({ requestId: 'call_1', interruptId: 'int_1', toolName, input, downloads: {} });
  const [first] = result.content;
  return first.type === 'text' ? JSON.parse(first.text) : {};
}

const CALLS: [string, JsonObject][] = [
  ['get_cell_ranges', { sheetName: 'Sheet1', ranges: ['A1:E3'] }],
  ['get_range_as_csv', { sheetName: 'Sheet1', range: 'A:B', maxRows: 2 }],
  ['set_cell_range', { sheetName: 'Sheet1', cells: { A2: { value: 'Rent' } } }],
  ['set_cell_range', { sheetName: 'Sheet1', cells: { C1: { value: '=not a formula', note: 'Typed' },
    C2: { formula: '=A2*2', cellStyles: { fontWeight: 'bold', backgroundColor: '#336699', numberFormat: '0.0' } },
    D3: { formula: '=1/0' } } }],
  ['copy_paste_range', { sourceRange: 'A2:B2', destRange: 'A5', copyType: 'values' }],
  ['copy_paste_range', { sourceRange: 'A1:B1', destRange: 'A7:B8', copyType: 'formats' }],
  ['copy_paste_range', { sourceSheetName: 'Sheet1', sourceRange: 'A:B', destRange: 'C5', destSheetName: 'Data' }],
  ['copy_paste_range', { sourceRange: 'A1:B1', destRange: 'E1:F1' }],
  ['get_cell_ranges', { sheetName: 'Sheet1', ranges: ['A1:F8'] }],
];

describe('WPS Spreadsheets', () => {
  it('declares every Excel tool, with execute_wps_js in place of execute_office_js', () => {
    const workbook = etWorkbook(budget());
    const runtime = joinRuntimes(excelRuntime({ run: wpsExcelRun(workbook), host: WPS_EXCEL_HOST,
      surface: async () => '', writes: new HostWriteQueue() }), wpsCodeRuntime({ name: 'WPS Spreadsheets',
      parameter: 'workbook', document: workbook, constants: {}, screen: screenWpsSheetWrite,
      unreachable: WPS_SHEET_UNREACHABLE }));
    expect(WPS_SPREADSHEET.contract.supported('wps', WPS_EXCEL_HOST, runtime.tools))
      .toEqual(excelTools.map(tool => tool.name).filter(name => name !== 'execute_office_js'));
  });

  it('answers each Excel tool call and leaves the cells as Excel does', async () => {
    const [excel, wps] = [budget(), budget()];
    const runtime = (run: ExcelRun) => excelRuntime({ run, host: WPS_EXCEL_HOST, surface: async () => '',
      writes: new HostWriteQueue() });
    for (const [tool, input] of CALLS) {
      expect(await call(WPS_SPREADSHEET, runtime(wpsExcelRun(etWorkbook(wps))), tool, input))
        .toEqual(await call(EXCEL, runtime(excel.run), tool, input));
    }
    const filled = (book: FakeWorkbook) => book.sheets.map(sheet => Object.fromEntries([...sheet.cells]
      .filter(([, cell]) => JSON.stringify(cell) !== '{"value":""}')));
    expect(filled(wps)).toEqual(filled(excel));
  });

  it('opens a conversation with the workbook, its tables and the selection, as in Excel', async () => {
    const state = (run: ExcelRun) => new ExcelState(run, { contentLanguage: 'zh-CN', displayLanguage: 'zh-CN' },
      new HostWriteQueue()).forMessage('s1').then(({ text }) => text);
    expect(await state(wpsExcelRun(etWorkbook(budget()))))
      .toBe((await state(budget().run)).replace(/\{sheet-1\}/g, 'Sheet1').replace(/\{sheet-2\}/g, 'Data'));
  });

  it.each([
    ['runs code on the workbook, each write landing as it is made',
      'const sheet = workbook.Worksheets.Item("Sheet1"); sheet.Range("C1").Value2 = 5; '
      + 'return [sheet.Range("A2").Value2, xlCenter];', { result: ['Rent', -4108],
        mutation: expect.objectContaining({ state: 'applied' }) }, 5],
    ['lists an object\'s members, leaving out those out of reach',
      'const cell = workbook.Worksheets.Item("Sheet1").Range("A1"); '
      + 'return [Object.keys(cell).includes("MergeArea"), Object.keys(cell.Comment)];',
      { result: [true, ['Text', 'Delete']] }, ''],
    ['keeps the application out of reach', 'return workbook.Application.Name;',
      { success: false, code: 'TOOL_EXECUTION_BLOCKED' }, ''],
    ['refuses a formula that reaches the network before it lands',
      'workbook.Worksheets.Item(1).Range("C1").Formula = \'=WEBSERVICE("http://x")\';',
      { success: false, code: 'TOOL_EXECUTION_BLOCKED' }, ''],
  ])('execute_wps_js %s', async (_case, code, expected, c1) => {
    const book = budget();
    const workbook = etWorkbook(book);
    const runtime = joinRuntimes(wpsCodeRuntime({ name: 'WPS Spreadsheets', parameter: 'workbook', document: workbook,
      constants: { xlCenter: -4108 }, screen: screenWpsSheetWrite, unreachable: WPS_SHEET_UNREACHABLE }));
    expect(await call(WPS_SPREADSHEET, runtime, 'execute_wps_js', { code, explanation: 'x' }))
      .toMatchObject(expected);
    expect(book.valueAt('Sheet1', 'C1')).toBe(c1);
  });
});
