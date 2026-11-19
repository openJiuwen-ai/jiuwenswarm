import { describe, expect, it } from 'vitest';

import wordTools from '@office/word/tools.json';

import { ClientToolDispatcher, joinRuntimes } from './clientToolDispatcher';
import { DocEditReview } from './docEditReview';
import { FakeDocument } from './fakeWord';
import { FakeWpsWriter } from './fakeWpsWriter';
import type { JsonObject } from './gatewayClient';
import { HostWriteQueue } from './hostWriteQueue';
import { wpsCodeRuntime } from './officeCodeRunner';
import { WPS_WRITER } from './wordApp';
import { screenWpsWriterWrite, WPS_WRITER_UNREACHABLE } from './wordGuard';
import { WordState } from './wordState';
import { wordRuntime } from './wordTools';
import { WPS_WRITER_HOST, wpsDocumentPdf, wpsDocumentSettings, wpsWordRun } from './wpsWriter';

/** WPS's page gives its ``TextEncoder`` a non-configurable ``arguments``, which lockdown cannot delete. */
const PageTextEncoder = globalThis.TextEncoder;
Object.defineProperty(globalThis, 'TextEncoder', { configurable: true, writable: true,
  value: Object.defineProperty(function TextEncoder() { return new PageTextEncoder(); }, 'arguments',
    { value: null, configurable: false }) });

/** The Word runtime over the WPS adapter on a fake WPS Writer, as the WPS Writer pane wires it. */
function pane(doc: FakeDocument) {
  const wps = new FakeWpsWriter(doc);
  const run = wpsWordRun(wps.document);
  const writes = new HostWriteQueue();
  const reviews = new DocEditReview(wpsDocumentSettings(wps.document), run, writes);
  const pages = async () => ({ pageCount: 1, jpegs: [btoa(String.fromCharCode(
    ...await wpsDocumentPdf(wps.document, wps.application)))] });
  const runtime = joinRuntimes(wordRuntime({ run, host: WPS_WRITER_HOST, reviews, pages }),
    wpsCodeRuntime({ name: WPS_WRITER.host, parameter: 'document', document: wps.document,
      // Reading a WPS constant is a call into WPS: a run reads only those its code names.
      constants: { wdCollapseEnd: 0, get wdUnused(): number { throw new Error('read a constant the code names not'); } },
      screen: screenWpsWriterWrite, unreachable: WPS_WRITER_UNREACHABLE }));
  const dispatcher = new ClientToolDispatcher(WPS_WRITER, runtime, writes);
  const call = async (toolName: string, input: JsonObject = {}): Promise<JsonObject> => {
    const { content: [first, ...images] } = await dispatcher.execute({ requestId: 'call_1', interruptId: 'i',
      toolName, input, downloads: {}, sessionId: 's1' as never });
    return { ...(first.type === 'text' ? JSON.parse(first.text) : {}), images };
  };
  const state = new WordState(run, { contentLanguage: '', displayLanguage: 'zh-CN' }, WPS_WRITER_HOST, reviews,
    writes, 'wps');
  return { wps, run, reviews, call, state, runtime };
}

const contract = () => new FakeDocument([
  { text: '服务协议', style: 'Title' },
  { text: 'Payment', style: 'Heading1', list: { kind: 'number', id: 1 } },
  'Invoices are payable within thirty (30) days of receipt.',
  { text: 'See clause 4 for the cap.', inline: [{ at: 11, kind: 'fields' }] },
  { text: 'First step', list: { kind: 'number', id: 2 } },
  { text: 'Second step', list: { kind: 'number', id: 2 } },
  { text: 'Liability', style: 'Heading1', list: { kind: 'number', id: 1 } },
  { text: 'Cell', table: 1 },
], { tables: [[2, 1]], header: '保密' });
const texts = (doc: FakeDocument) => doc.paras.map(para => para.text);

describe('the Word tools on WPS Writer', () => {
  it('declares every Word tool, with execute_wps_js in place of execute_office_js', () => {
    expect(WPS_WRITER.contract.supported('wps', WPS_WRITER_HOST, pane(contract()).runtime.tools))
      .toEqual(wordTools.map(tool => tool.name).filter(name => name !== 'execute_office_js'));
  });

  it('read a section by heading, find a phrase and check the structure, with WPS\'s own style names', async () => {
    const { call } = pane(contract());
    expect((await call('read_doc_section', { heading: 'Payment' })).result).toMatchObject({ paragraph_start: 1,
      paragraph_end: 6, paragraphs: ['1: [Heading1] [1.] Payment',
        '2: Invoices are payable within thirty (30) days of receipt.', '3: [field] See clause 4 for the cap.',
        '4: [1.] First step', '5: [2.] Second step'] });
    expect((await call('search_doc_text', { query: 'step' })).result).toEqual({ total_matches: 2, results: [
      { paragraph_index: 4, snippet: 'First step' }, { paragraph_index: 5, snippet: 'Second step' }] });
    expect((await call('verify_doc')).result).toMatchObject({ styles: { Title: 1, Heading1: 2, Normal: 5 },
      tables: [{ row_count: 2, cells_per_row: [2, 1], uniform: false }] });
  });

  it('replaces only the words that change, as a tracked change when Track Changes is on', async () => {
    const doc = contract();
    doc.changeTrackingMode = 'TrackAll';
    const answer = await pane(doc).call('edit_doc_text', { old_text: 'within thirty (30) days',
      new_text: 'within forty-five (45) days' });
    expect(answer).toMatchObject({ success: true, result: { replaced: true } });
    expect(doc.paras[2].text).toBe('Invoices are payable within forty-five (45) days of receipt.');
    expect(doc.revisions.map(revision => revision.text)).toEqual(['45', '30', 'forty-five', 'thirty']);
  });

  it('replaces a run of Chinese, which changes as one word', async () => {
    const doc = new FakeDocument(['系统运維与故障响应', '续签期间的系统运維费用另行约定。']);
    expect(await pane(doc).call('edit_doc_text', { old_text: '系统运維与故障响应', new_text: '系统运维与故障响应' }))
      .toMatchObject({ success: true });
    expect(texts(doc)).toEqual(['系统运维与故障响应', '续签期间的系统运維费用另行约定。']);
  });

  it('creates a bulleted list after a paragraph and continues a numbered one', async () => {
    const doc = contract();
    const { call } = pane(doc);
    expect((await call('edit_doc_list', { action: 'create_list', anchor_text: 'of receipt.',
      items: ['Net 30', 'Net 60'], list_style: 'bullet' })).result).toMatchObject({ items: [
      { text: 'Net 30', listString: '•' }, { text: 'Net 60', listString: '•' }] });
    expect((await call('edit_doc_list', { action: 'insert_item', anchor_item_text: 'Second step',
      new_item_text: 'Third step', position: 'After' })).result).toMatchObject({ items: [
      { text: 'Third step', listString: '3.' }] });
    expect(texts(doc).slice(2, 9)).toEqual(['Invoices are payable within thirty (30) days of receipt.', 'Net 30',
      'Net 60', 'See clause 4 for the cap.', 'First step', 'Second step', 'Third step']);
  });

  it('shrinks runs of empty paragraphs', async () => {
    const doc = new FakeDocument(['A', '', '', '', 'B', '', { text: '', inline: [{ at: 0, kind: 'pictures' }] }, 'C']);
    expect((await pane(doc).call('collapse_blank_paragraphs', { max_consecutive: 1 })).result)
      .toEqual({ deleted: 2, scanned: 8, skipped_with_content: 1 });
    expect(texts(doc)).toEqual(['A', '', 'B', '', '', 'C']);
  });

  it('carries the document state with each message: outline, header, selection, and no user changes', async () => {
    const doc = contract();
    doc.selection = doc.body.search('thirty').items[0];
    const { text } = await pane(doc).state.forMessage('s1');
    expect(text).toContain('Track Changes: Off; edits land directly.');
    expect(text).toContain('1. Payment  [Heading1; 11pt #000000 "Calibri"]');
    expect(text).toContain('[section 1 Header] "保密"');
    expect(text).toContain('The user highlighted:\nthirty\n');
    expect(text).not.toContain('<user_changes>');
  });

  it('keeps suggestion cards in the document, applies one as a tracked change and undoes it', async () => {
    const doc = contract();
    const { call, reviews, run, wps } = pane(doc);
    const edit = { id: 'pay-1', section: 'Payment', change_summary: 'Pay in 45 days', old_text: 'thirty (30) days',
      new_text: 'forty-five (45) days' };
    expect((await call('propose_doc_edits', { edits: [edit] })).result).toMatchObject({ proposed: ['pay-1'] });
    await reviews.apply('call_1', edit, 'tracked');
    expect(doc.paras[2].text).toBe('Invoices are payable within forty-five (45) days of receipt.');
    expect(doc.changeTrackingMode).toBe('Off');
    const reopened = new DocEditReview(wpsDocumentSettings(wps.document), run, new HostWriteQueue());
    expect(reopened.card('call_1', 'pay-1')?.state).toMatchObject({ status: 'applied', as: 'tracked' });
    await reopened.undo('call_1', edit);
    expect(doc.paras[2].text).toBe('Invoices are payable within thirty (30) days of receipt.');
    expect(reopened.card('call_1', 'pay-1')?.state).toEqual({ status: 'undone' });
  });

  it('draws pages from the document exported as PDF, and leaves no file behind', async () => {
    const { call, wps } = pane(contract());
    const answer = await call('verify_doc_visual');
    expect(answer.images).toMatchObject([{ type: 'image', source: { data: btoa('%PDF') } }]);
    expect(wps.files.size).toBe(0);
  });

  it.each([
    ['runs code on the document, each write landing as it is made',
      'document.Range(0, 0).InsertBefore("草案 "); return [document.Paragraphs.First.Range.Text, wdCollapseEnd];',
      { result: ['草案 服务协议\r', 0], mutation: expect.objectContaining({ state: 'applied' }) }, '草案 服务协议'],
    ['keeps the application out of reach', 'return document.Application.Name;',
      { success: false, code: 'TOOL_EXECUTION_BLOCKED' }, '服务协议'],
    ['refuses a field that fetches another file before it lands',
      'const at = document.Range(0, 0); at.Fields.Add(at, 68, \'"C:\\\\x.docx"\');',
      { success: false, code: 'TOOL_EXECUTION_BLOCKED' }, '服务协议'],
    ['refuses updating fields, which may fetch for fields already there', 'document.Range(0, 0).Fields.Update();',
      { success: false, code: 'TOOL_EXECUTION_BLOCKED' }, '服务协议'],
  ])('execute_wps_js %s', async (_case, code, expected, title) => {
    const doc = contract();
    expect(await pane(doc).call('execute_wps_js', { code, explanation: 'x' })).toMatchObject(expected);
    expect(doc.paras[0].text).toBe(title);
  });
});
