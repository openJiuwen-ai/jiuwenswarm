import { afterEach, describe, expect, it, vi } from 'vitest';

import { selectedCells } from './excelState';
import { FakeWorkbook } from './fakeExcel';
import { FakeDocument, FakeRange } from './fakeWord';
import { type PowerPointRun, selectedSlides } from './powerPointReadTools';
import { type Selection, selectionLine } from './selectionLine';
import { selectedText } from './wordState';

/** A deck of three slides, the third selected with two of its shapes. */
const deck: PowerPointRun = async batch => batch({ sync: async () => undefined, presentation: {
  slides: { items: [{ id: '256' }, { id: '257' }, { id: '258' }], load() {} },
  getSelectedSlides: () => ({ items: [{ id: '258' }], load() {} }),
  getSelectedShapes: () => ({ items: [{ id: '4' }, { id: '5' }], load() {} }),
} } as unknown as PowerPoint.RequestContext);

function workbook(selection: string): FakeWorkbook {
  return Object.assign(new FakeWorkbook(['Sheet1']), { selection });
}

/** The line once its first read is in. */
async function shown(read: () => Promise<Selection>, listen = (_moved: () => void) => undefined) {
  const line = selectionLine(listen, read);
  await vi.waitFor(() => expect(line.text()).toBeDefined());
  return line;
}

afterEach(() => {
  vi.useRealTimers();
});

describe('the selection line', () => {
  it.each([
    ['the cells in Excel', () => selectedCells(workbook("'Q1 data'!A1:B3,'Q1 data'!D4").run), '已选中 A1:B3, D4'],
    ['the chart in Excel, whose cells Excel cannot read', () => selectedCells(Object.assign(workbook('Sheet1!A1'),
      { chart: { id: 'c1', name: '图表 1', title: '销售额' } }).run), '已选中图表「图表 1」'],
    ['the slide and shapes in PowerPoint', () => selectedSlides(deck), '已选中第 3 页 · 2 个形状'],
    ['how many characters in Word, spaces aside', () => {
      const doc = new FakeDocument(['The fee is due monthly.']);
      doc.selection = new FakeRange(doc, 4, 14);
      return selectedText(doc.run);
    }, '已选中 8 个字'],
  ])('names %s', async (_host, read, text) => {
    expect((await shown(read)).text()).toBe(text);
  });

  it('follows the selection once it settles', async () => {
    const book = workbook('Sheet1!A1');
    let moved = (): void => undefined;
    const line = await shown(() => selectedCells(book.run), listener => { moved = listener; });
    vi.useFakeTimers();
    book.selection = 'Sheet1!C2:C9';
    moved();
    expect(line.text()).toBe('已选中 A1');
    await vi.advanceTimersByTimeAsync(300);
    expect(line.text()).toBe('已选中 C2:C9');
  });
});
