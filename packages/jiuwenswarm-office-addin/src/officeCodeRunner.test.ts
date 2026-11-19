import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';

import type { ClientToolResult } from './clientAttachment';
import { ClientToolDispatcher } from './clientToolDispatcher';
import { EXCEL } from './excelApp';
import { excelRuntime } from './excelTools';
import { FakeWorkbook } from './fakeExcel';
import { HostWriteQueue } from './hostWriteQueue';
import { DocEditReview } from './docEditReview';
import { FakeDocument, FakeSettings } from './fakeWord';
import { SYNC_MS } from './officeCodeRunner';
import { WORD } from './wordApp';
import { wordRuntime } from './wordTools';

// MicrosoftAjax as Office.js loads it before any code runs, reduced to extensions its own APIs call, two that
// evaluate text in the page's scope, and, sloppy as MicrosoftAjax is, two that see the page's global object as
// ``this`` when called without a receiver and three whose ``caller`` would name the page function calling them.
Object.assign(Function, { _validateParams: (args: unknown[], params: { type: { isInstanceOfType(value: unknown):
  boolean } }[]) => (params.some((param, index) => !param.type.isInstanceOfType(args[index]))
  ? (Error as never as Record<string, () => Error>).argumentType() : null) });
Object.assign(Function.prototype, { isInstanceOfType: () => false, registerClass: () => undefined });
Object.assign(Error, { argumentType: () => new Error('Sys.ArgumentTypeException') });
Object.assign(String, { __typeName: 'String' });
Object.assign(Array, { parse: (text: string) => eval(text) });
(0, eval)(`Function.createCallback = function (method, context) { return function () { return method.call(this, context); }; };
  Object.getType = function (value) { return value.constructor.__typeName ? value.constructor : Object; };
  String.format = function (format) { var args = arguments;
    return format.replace(/\\{(\\d)\\}/g, function (match, index) { return String(args[Number(index) + 1]); }); };
  Error.prototype.popStackFrame = function () {};
  Date.prototype._toFormattedString = function (format, culture) {
    culture.dateTimeFormat.Calendar.convert.fromGregorian(this); return ''; };`);

const HOST = { isSetSupported: () => true, worker: true };
const SHEET = 'const sheet = context.workbook.worksheets.getActiveWorksheet();';

function run(book: FakeWorkbook, code: string, writes = new HostWriteQueue()): Promise<ClientToolResult> {
  const runtime = excelRuntime({ run: book.run, host: HOST, enums: { RangeCopyType: { all: 'All' } },
    surface: async () => '', writes });
  return new ClientToolDispatcher(EXCEL, runtime, writes).execute(
    { requestId: 'call_1', interruptId: 'int_1', toolName: 'execute_office_js', input: { code, explanation: 'x' },
      downloads: {} });
}

function answer(result: ClientToolResult): Record<string, unknown> {
  const [first] = result.content;
  return first.type === 'text' ? JSON.parse(first.text) : {};
}

describe('execute_office_js', () => {
  // The clock is faked before the first run locks the realm down, which freezes what faking would replace.
  beforeAll(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  });

  afterAll(() => {
    vi.useRealTimers();
  });

  it('runs the code on the workbook, with only the enums and attachImage beside context', async () => {
    const book = new FakeWorkbook(['Sheet1']);
    const result = await run(book, `${SHEET} const range = sheet.getRange('A1'); range.values = [[42]];
      await context.sync(); range.load('values'); await context.sync(); attachImage('iVBORw0KGgo=');
      return { read: range.values, copyType: Excel.RangeCopyType.all, page: typeof window, lockedDown:
        Object.isFrozen(Array.prototype) };`);
    expect(answer(result)).toMatchObject({ success: true, result: { read: [[42]], copyType: 'All',
      page: 'undefined', lockedDown: true }, mutation: { state: 'applied' } });
    expect(result.content[1]).toMatchObject({ type: 'image' });
    expect(book.valueAt('Sheet1', 'A1')).toBe(42);
  });

  it.each([
    ['a range as an argument', `${SHEET} sheet.getRange('B1:B2').copyFrom(sheet.getRange('A1:A2'), 'All');`, 'applied'],
    ['a change of the outline view', `${SHEET} sheet.showOutlineLevels(1, 1);`, 'applied'],
  ])('screens and counts the writes of code that passes %s', async (_case, code, state) => {
    const book = new FakeWorkbook(['Sheet1']);
    book.sheet('Sheet1').put('A1', [[1], [2]]);
    const result = await run(book, `${code} await context.sync(); return 'ok';`);
    expect(answer(result)).toMatchObject({ success: true, result: 'ok', mutation: { state } });
  });

  it.each([
    ['writes left queued when it returns', `${SHEET} sheet.getRange('A1').values = [[1]];`,
      { code: 'OFFICE_JS_ERROR', error: expect.stringContaining('still queued'), mutation: { state: 'not_applied' } }],
    ['a formula that reaches the network, even when the code catches the refusal',
      `${SHEET} try { sheet.getRange('A1').formulas = [['=WEBSERVICE("http://x")']]; } catch (e) {} return 1;`,
      { code: 'TOOL_EXECUTION_BLOCKED', error: expect.stringContaining('Nothing was changed') }],
    ['Office.js internals', 'return Object.keys(context._pendingRequest);',
      { code: 'TOOL_EXECUTION_BLOCKED', error: expect.stringContaining('_pendingRequest') }],
    ['the network', 'await fetch("https://example.com");',
      { code: 'TOOL_EXECUTION_BLOCKED', error: expect.stringContaining('fetch reaches the network') }],
    ['a value that is not JSON', 'return 1n;', { code: 'OFFICE_JS_ERROR', error: expect.stringContaining('JSON') }],
    ['a formula in the user\'s language', `${SHEET} sheet.getRange('A1').formulasLocal = [['=WEBDIENST("x")']];`,
      { code: 'TOOL_EXECUTION_BLOCKED', error: expect.stringContaining('in English') }],
    ['a refresh of linked data types', 'context.workbook.linkedDataTypes.requestRefreshAll();',
      { code: 'TOOL_EXECUTION_BLOCKED', error: expect.stringContaining('linked data types') }],
    ['a link out of the workbook', `${SHEET} sheet.getRange('A1').hyperlink = { address: 'https://example.com' };`,
      { code: 'TOOL_EXECUTION_BLOCKED', error: expect.stringContaining('hyperlink') }],
    ['a link out of the workbook given to set()',
      `${SHEET} sheet.getRange('A1').set({ hyperlink: { address: 'file:///C:/x.xlsx' } });`,
      { code: 'TOOL_EXECUTION_BLOCKED', error: expect.stringContaining('hyperlink') }],
  ])('fails on %s', async (_case, code, expected) => {
    const result = await run(new FakeWorkbook(['Sheet1']), code);
    expect(result.is_error).toBe(true);
    expect(answer(result)).toMatchObject({ success: false, ...expected });
  });

  it.each([
    ['caught by the code', 'try { await context.sync(); } catch (e) { return { error: String(e) }; }'],
    ['the code did not wait for', 'context.sync(); return 1;'],
  ])('reports writes whose sync failed, %s, as partly applied', async (_case, sync) => {
    const book = new FakeWorkbook(['Sheet1']);
    let fail = (_error: Error) => undefined as void;
    book.context.sync = () => new Promise<void>((_resolve, reject) => { fail = reject; });
    const pending = run(book, `${SHEET} sheet.getRange('A1').values = [[1]]; ${sync}`);
    await vi.advanceTimersByTimeAsync(1);
    fail(new Error('The sheet is protected.'));
    expect(answer(await pending)).toMatchObject({ mutation: { state: 'partial' } });
  });

  it('leaves Office.js its MicrosoftAjax extensions, but not those that evaluate text to the code', async () => {
    const result = await run(new FakeWorkbook(['Sheet1']),
      'return [typeof Array.parse, typeof (() => 0).registerClass, typeof (() => 0).isInstanceOfType];');
    // Office.js reads the realm's Function, which lockdown replaced.
    const { _validateParams: validate } = Function as never as Record<string, (args: unknown[], params: object[])
      => Error | null>;
    expect(validate(['name', () => 0], [{ type: String }, { type: Function }])).toBeNull();
    expect(validate([1], [{ type: String }])).toEqual(new Error('Sys.ArgumentTypeException'));
    expect(answer(result)).toMatchObject({ success: true, result: ['undefined', 'undefined', 'function'] });
    expect((String as never as Record<string, (...args: unknown[]) => string>).format('{0} of {1}', 1, 2))
      .toBe('1 of 2');
  });

  it('gives the code no way to the page through what MicrosoftAjax added', async () => {
    const result = await run(new FakeWorkbook(['Sheet1']), `const reached = [];
      const attempts = [() => (() => 0).createCallback(function () { return this; })(),
        () => Date.prototype._toFormattedString.call(undefined, 'd', { dateTimeFormat: { Calendar: { convert: {
          fromGregorian: page => { reached.push(page); } } } } })];
      for (const attempt of attempts) { try { reached.push(attempt()); } catch {} }
      const callers = [Object.getType, String.format, Error.prototype.popStackFrame].filter(kept => {
        try { return kept.caller !== undefined; } catch { return false; } });
      return { page: reached.filter(value => typeof value?.fetch === 'function').length,
        functionStatics: ['createDelegate', '_validateParams'].map(name => typeof (() => 0)[name]),
        callers: callers.length };`);
    expect(answer(result)).toMatchObject({ success: true,
      result: { page: 0, functionStatics: ['undefined', 'undefined'], callers: 0 } });
  });

  it('stops waiting for a sync past 30 s, says the change may still land, and holds later writes until it does',
    async () => {
      const book = new FakeWorkbook(['Sheet1']);
      let land = () => undefined as void;
      book.context.sync = () => new Promise<void>(resolve => { land = resolve; });
      const writes = new HostWriteQueue();
      const pending = run(book, `${SHEET} sheet.getRange('A1').values = [[1]]; await context.sync();`, writes);
      await vi.advanceTimersByTimeAsync(SYNC_MS);
      expect(answer(await pending)).toMatchObject({ success: false, code: 'OFFICE_JS_TIMEOUT',
        error: expect.stringContaining('may still finish in the background'), mutation: { state: 'uncertain' } });
      let next = false;
      const later = writes.exclusive(async () => { next = true; });
      await vi.advanceTimersByTimeAsync(1000);
      expect(next).toBe(false);
      land();
      await vi.advanceTimersByTimeAsync(1);
      await later;
      expect(next).toBe(true);
    });
});

describe('execute_office_js on Word', () => {
  const runWord = (doc: FakeDocument, code: string) => new ClientToolDispatcher(WORD, wordRuntime({ run: doc.run,
    host: HOST, enums: {}, reviews: new DocEditReview(new FakeSettings(), doc.run, new HostWriteQueue()) }),
    new HostWriteQueue())
    .execute({ requestId: 'call_1', interruptId: 'int_1', toolName: 'execute_office_js',
      input: { code, explanation: 'x' }, downloads: {} });

  it('warns when the code changed the Track Changes mode', async () => {
    const doc = new FakeDocument(['Hello']);
    const result = await runWord(doc, `context.document.changeTrackingMode = 'Off'; await context.sync();
      context.document.changeTrackingMode = 'TrackAll'; await context.sync(); return 'ok';`);
    expect(answer(result)).toMatchObject({ success: true, result: 'ok',
      tracked_change_warning: expect.stringContaining('from Off to TrackAll') });
  });

  it.each([
    ['a change to the templates every document shares',
      'context.application.templates.getFirst().save(); await context.sync();', 'every document on this computer shares'],
    ['another document', 'context.application.createDocument(); await context.sync();', 'cannot open or create'],
    ['a field that fetches another file', `context.document.body.paragraphs.getFirst().getRange()
      .insertField('End', 'IncludeText', '"C:/secret.docx"'); await context.sync();`, 'fetch content from another'],
    ['a linked picture in OOXML', `context.document.body.insertOoxml('<a:blip r:link="rId1"/>', 'End');
      await context.sync();`, 'fetch from another file'],
    ['an outside part in OOXML', `context.document.body.insertOoxml('<Relationship Target="http://x" '
      + 'TargetMode="External"/>', 'End'); await context.sync();`, 'fetch from another file'],
    ['a document inserted whole', 'context.document.body.insertFileFromBase64("UEs=", "End"); await context.sync();',
      'Inserting another document'],
  ])('refuses %s', async (_case, code, reason) => {
    const result = await runWord(new FakeDocument(['Hello']), code);
    expect(answer(result)).toMatchObject({ success: false, code: 'TOOL_EXECUTION_BLOCKED',
      error: expect.stringContaining(reason) });
  });
});
