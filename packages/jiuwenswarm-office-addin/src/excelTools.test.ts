import { describe, expect, it } from 'vitest';

import type { ClientToolResult } from './clientAttachment';
import { ClientToolDispatcher } from './clientToolDispatcher';
import { EXCEL } from './excelApp';
import { formulaThreat } from './excelGuard';
import { excelRuntime } from './excelTools';
import { FakeWorkbook } from './fakeExcel';
import type { JsonObject } from './gatewayClient';
import { HostWriteQueue } from './hostWriteQueue';

const NOTES = { isSetSupported: (name: string, version: string) => name === 'ExcelApi' && Number(version) <= 1.18,
  worker: true };

/** Calls ``toolName`` on ``book`` as the backend would, and reads the answer the model gets. */
async function call(book: FakeWorkbook, toolName: string, input: JsonObject): Promise<JsonObject> {
  const runtime = excelRuntime({ run: book.run, host: NOTES, enums: {}, surface: async () => '',
    writes: new HostWriteQueue() });
  const result: ClientToolResult = await new ClientToolDispatcher(EXCEL, runtime, new HostWriteQueue())
    .execute({ requestId: 'call_1', interruptId: 'int_1', toolName, input, downloads: {} });
  const [first] = result.content;
  return first.type === 'text' ? JSON.parse(first.text) : {};
}

function budget(): FakeWorkbook {
  const book = new FakeWorkbook(['Sheet1', 'Data'], formula => (formula === '=A2*2' ? 20 : '#NAME?'));
  const sheet = book.sheet('Sheet1');
  sheet.put('A1', [['Item', 'Cost'], ['Rent', 10], ['Food', '=A2*2']]);
  sheet.getRange('A1:B1').format.font.bold = true;
  sheet.notes.add('A1', 'Source: ledger');
  sheet.merged.push('D1:E1');
  return book;
}

describe('the Excel cell tools', () => {
  it('reads cells compactly, with shared formatting by block and the merged areas', async () => {
    const answer = await call(budget(), 'get_cell_ranges', { sheetName: 'Sheet1', ranges: ['A1:E3'] });
    expect(answer).toEqual({
      worksheet: {
        name: 'Sheet1', dimension: 'A1:B3',
        cells: { A1: ['Item', '', 'Source: ledger'], B1: 'Cost', A2: 'Rent', B2: 10, A3: 'Food', B3: [20, '=A2*2'] },
        styles: { 'A1:B1': { b: true, sz: 11, family: 'Calibri' }, 'A2:B3': { sz: 11, family: 'Calibri' } },
        borders: {},
      },
      hasMore: false,
      mergedRanges: ['D1:E1'],
    });
  });

  it('stops at the cell limit on a row boundary and says where to go on', async () => {
    const answer = await call(budget(), 'get_cell_ranges',
      { sheetName: 'Sheet1', ranges: ['A1:B3', 'A1'], includeStyles: false, cellLimit: 3 });
    expect(answer.worksheet).toMatchObject({ cells: { A1: ['Item', '', 'Source: ledger'], B1: 'Cost' } });
    expect(answer).toMatchObject({ hasMore: true, nextRanges: ['A2:B3', 'A1'] });
  });

  it('names every sheet when the one asked for is not there', async () => {
    const answer = await call(budget(), 'get_range_as_csv', { sheetName: 'Sheet 1', range: 'A1:B2' });
    expect(answer).toMatchObject({ success: false, error: expect.stringContaining('"Sheet1", "Data"') });
  });

  it('reads a range as CSV, quoting what needs it, and says when rows are left', async () => {
    const book = budget();
    book.sheet('Sheet1').put('A4', [['Gas, water', 'say "hi"']]);
    const answer = await call(book, 'get_range_as_csv', { sheetName: 'Sheet1', range: 'A:B', maxRows: 2 });
    expect(answer).toEqual({ csv: 'Item,Cost\nRent,10\nFood,20', rowCount: 2, columnCount: 2, hasMore: true,
      sheetName: 'Sheet1' });
  });

  it.each([
    ['refuses to overwrite cells', { A2: { value: 'Rent' }, C1: { value: 1 } }, false,
      { success: false, error: expect.stringContaining('Would overwrite 1 non-empty cell: A2') }],
    ['refuses a formula that reaches the network', { C1: { formula: '=WEBSERVICE("http://x")' } }, false,
      { success: false, code: 'TOOL_EXECUTION_BLOCKED', mutation: expect.objectContaining({ state: 'not_applied' }) }],
    ['writes, keeps text that looks like a formula as text, and returns the formulas\' results',
      { C1: { value: '=not a formula' }, C2: { formula: '=A2*2' }, A2: { value: 'Rent' } }, true,
      { formula_results: { C2: 20 }, mutation: expect.objectContaining({ state: 'applied' }) }],
  ])('set_cell_range %s', async (_case, cells, allow, expected) => {
    const book = budget();
    const answer = await call(book, 'set_cell_range', { sheetName: 'Sheet1', cells, allow_overwrite: allow });
    expect(answer).toMatchObject(expected);
    expect(book.valueAt('Sheet1', 'C1')).toBe(allow ? '=not a formula' : '');
  });

  it.each([
    ['refuses to cut across a merged area', { sourceRange: 'A1:B1', destRange: 'E1:F1' },
      { success: false, error: expect.stringContaining('Sheet1!D1:E1') }],
    ['refuses to overwrite cells', { sourceRange: 'A1:B1', destRange: 'A3' },
      { success: false, error: expect.stringContaining('Sheet1!A3:B3: A3, B3') }],
    ['copies whole columns as far as their sheet is used, to another sheet, and warns about relative references',
      { sourceRange: 'A:B', destRange: 'C5', destSheetName: 'Data' },
      { copied: 'Sheet1!A1:B3 → Data!C5:D7 (copyType: all)', messages: [expect.stringContaining('destination sheet')] }],
  ])('copy_paste_range %s', async (_case, input, expected) => {
    const answer = await call(budget(), 'copy_paste_range', { sourceSheetName: 'Sheet1', ...input });
    expect(answer).toMatchObject(expected);
  });

  it('refuses to copy formulas that reach the network', async () => {
    const book = budget();
    book.sheet('Sheet1').put('C3', [['=WEBSERVICE("http://x")']]);
    const answer = await call(book, 'copy_paste_range', { sourceRange: 'C3', destRange: 'C4' });
    expect(answer).toMatchObject({ success: false, code: 'TOOL_EXECUTION_BLOCKED' });
    expect(book.valueAt('Sheet1', 'C4')).toBe('');
  });
});

describe('formulaThreat', () => {
  it.each([
    ['=SUM(A1:A3)', false], ['=Data!B2*2', false], ['=\'My Sheet\'!A1', false], ['=SUM(Sales[Amount])', false],
    ['=IF(A1="http://x",1,0)', false], ['Plain text', false], ['-5', false],
    ['=WEBSERVICE("http://x")', true], ['=_xlfn.STOCKHISTORY("MSFT",1)', true], ['=INDIRECT(A1)', true],
    ['=[Book.xlsx]Sheet1!A1', true], ['=\'C:\\x\\[Book.xlsx]Sheet1\'!A1', true],
    ['=\'\\\\server\\share\\[B.xlsx]S\'!A1', true], ['=data.csv!A1', true], ['=cmd|\'/c calc\'!A0', true],
    ['=MAP(A1:A3, WEBSERVICE)', true], ['=Info!A1+Calls!B2', false], ['=SUM(Log[Info])', false],
  ])('%s is refused: %s', (formula, refused) => {
    expect(formulaThreat(formula) !== undefined).toBe(refused);
  });
});
