import { afterEach, describe, expect, it, vi } from 'vitest';

import { waitingForCellEdit } from './excelCellEdit';
import { excelRuntime } from './excelTools';
import { FakeWorkbook } from './fakeExcel';
import { HostLine } from './hostLine';
import { HostWriteQueue } from './hostWriteQueue';

/** A workbook whose user is typing in a cell, and its tools behind the wait. */
function editing() {
  const book = new FakeWorkbook(['Sheet1']);
  book.sheet('Sheet1').put('A1', [['Rent', 10]]);
  let leave = (): void => undefined;
  book.cellLeft = new Promise(resolve => { leave = resolve; });
  const line = new HostLine();
  const runtime = waitingForCellEdit(excelRuntime({ run: book.run, host: { isSetSupported: () => true, worker: true },
    enums: {}, surface: async () => '', writes: new HostWriteQueue() }),
  { run: book.run, delayed: book.delayedRun, waitMs: 8_000, line });
  const call = (toolName: 'set_cell_range' | 'get_range_as_csv', input: object) => runtime.execute(
    { callId: 'c1', toolName, input: { sheetName: 'Sheet1', ...input }, downloads: {} });
  return { book, line, call, leave: () => { book.cellLeft = undefined; leave(); } };
}

afterEach(() => {
  vi.useRealTimers();
});

describe('a cell the user is editing', () => {
  it('holds a call, asking the user to leave the cell, and runs it once they do', async () => {
    const { book, line, call, leave } = editing();
    const answer = call('set_cell_range', { cells: { B1: { value: 20 } }, allow_overwrite: true });
    await vi.waitFor(() => expect(line.text()).toBe('你正在编辑单元格，按 Enter 或 Esc 结束编辑后，WorkSwarm 才能继续'));
    leave();
    expect(await answer).toMatchObject({ ok: true });
    expect(book.valueAt('Sheet1', 'B1')).toBe(20);
    expect(line.text()).toBeUndefined();
  });

  it('tells the model to have the user press Enter or Esc when they stay in the cell', async () => {
    vi.useFakeTimers();
    const { line, call } = editing();
    const answer = call('get_range_as_csv', { range: 'A1:B1' });
    await vi.advanceTimersByTimeAsync(8_000);
    expect(await answer).toMatchObject({ ok: false, error: { code: 'EXCEL_CELL_EDIT_MODE',
      message: expect.stringContaining('Ask the user to press Enter or Esc in the spreadsheet') } });
    expect(line.text()).toBeUndefined();
  });
});
