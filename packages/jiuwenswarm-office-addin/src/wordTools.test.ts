import { describe, expect, it } from 'vitest';

import type { ClientToolResult } from './clientAttachment';
import { ClientToolDispatcher } from './clientToolDispatcher';
import { DocEditReview } from './docEditReview';
import { FakeDocument, FakeSettings } from './fakeWord';
import type { JsonObject } from './gatewayClient';
import { HostWriteQueue } from './hostWriteQueue';
import { WORD } from './wordApp';
import { wordRuntime } from './wordTools';

const HOST = { isSetSupported: () => true, worker: true };
const LONG = `${'The Supplier shall deliver the Goods '.repeat(8)}by the Delivery Date.`;

function contract(): FakeDocument {
  return new FakeDocument([
    { text: 'Services Agreement', style: 'Title' },
    { text: 'Definitions', style: 'Heading1', list: { kind: 'number', id: 1 } },
    'The “Services” mean the work in Schedule 1.',
    { text: 'Payment', style: 'Heading1', list: { kind: 'number', id: 1 } },
    'Invoices are payable within thirty (30) days of receipt.',
    { text: 'See clause 4 for the cap.', inline: [{ at: 11, kind: 'fields' }] },
    LONG,
    '',
    '',
    '',
    { text: 'First step', list: { kind: 'number', id: 2 } },
    { text: 'Second step', list: { kind: 'number', id: 2 } },
    { text: 'Liability', style: 'Heading1', list: { kind: 'number', id: 1 } },
    { text: 'Cell', table: 1 },
  ], { tables: [[2, 2, 1]] });
}

/** Calls ``toolName`` on ``doc`` as the backend would, and reads the answer the model gets. */
async function call(doc: FakeDocument, toolName: string, input: JsonObject,
  reviews = new DocEditReview(new FakeSettings(), doc.run, new HostWriteQueue())): Promise<JsonObject> {
  const runtime = wordRuntime({ run: doc.run, host: HOST, enums: {}, reviews });
  const result: ClientToolResult = await new ClientToolDispatcher(WORD, runtime, new HostWriteQueue())
    .execute({ requestId: 'call_1', interruptId: 'int_1', toolName, input, downloads: {}, sessionId: 's1' as never });
  const [first] = result.content;
  return first.type === 'text' ? JSON.parse(first.text) : {};
}

const texts = (doc: FakeDocument) => doc.paras.map(para => para.text);

describe('edit_doc_text', () => {
  it.each([
    ['one changed word', 'within thirty (30) days', 'within forty-five (45) days',
      'Invoices are payable within forty-five (45) days of receipt.', ['45', 'forty-five']],
    ['straight quotes for the document’s curly ones', 'The "Services" mean', 'The "Services" include',
      'The “Services” include the work in Schedule 1.', ['include']],
    ['a passage past the search limit', LONG, LONG.replace('Delivery Date', 'agreed date'),
      LONG.replace('Delivery Date', 'agreed date'), ['agreed date']],
  ])('replaces only the words that differ: %s', async (_case, oldText, newText, after, revised) => {
    const doc = contract();
    doc.changeTrackingMode = 'TrackAll';
    const answer = await call(doc, 'edit_doc_text', { old_text: oldText, new_text: newText });
    expect(answer).toMatchObject({ success: true, result: { replaced: true } });
    expect(texts(doc)).toContain(after);
    expect(doc.revisions.map(revision => revision.text)).toEqual(revised);
  });

  it('finds text with a caret, which Word\'s search reads as the start of a special character', async () => {
    const doc = new FakeDocument(['The area is x^2 metres.']);
    await call(doc, 'edit_doc_text', { old_text: 'is x^2 metres', new_text: 'is x^3 metres' });
    expect(texts(doc)).toEqual(['The area is x^3 metres.']);
  });

  it('replaces across paragraphs when old_text names them whole, one per line', async () => {
    const doc = contract();
    await call(doc, 'edit_doc_text', { old_text: 'Payment\nInvoices are payable within thirty (30) days of receipt.',
      new_text: 'Fees\nInvoices are payable within thirty (30) days of receipt.' });
    expect(texts(doc).slice(3, 5)).toEqual(['Fees', 'Invoices are payable within thirty (30) days of receipt.']);
  });

  it.each([
    ['text that is not there', 'within sixty days', 'was not found'],
    ['text found twice', 'step', 'matched 2 places'],
    ['a field inside the words to replace', 'clause 4 for', 'spans 1 field(s)'],
    ['lines that are not whole paragraphs', 'Payment\nInvoices are payable', 'each line must be one whole paragraph'],
  ])('refuses %s and changes nothing', async (_case, oldText, message) => {
    const doc = contract();
    const before = texts(doc);
    const answer = await call(doc, 'edit_doc_text', { old_text: oldText, new_text: 'x' });
    expect(answer).toMatchObject({ success: false, error: expect.stringContaining(message) });
    expect(texts(doc)).toEqual(before);
  });
});

describe('reading the document', () => {
  it('reads a section by heading, each paragraph with its index and markers', async () => {
    const answer = await call(contract(), 'read_doc_section', { heading: 'Payment' });
    expect(answer.result).toMatchObject({ total_paragraphs: 14, paragraph_start: 3, paragraph_end: 12, paragraphs: [
      '3: [Heading1] [2.] Payment',
      '4: Invoices are payable within thirty (30) days of receipt.',
      '5: [field] See clause 4 for the cap.',
      `6: ${LONG}`, '7: ', '8: ', '9: ', '10: [1.] First step', '11: [2.] Second step',
    ] });
  });

  it('finds a phrase with the paragraph it is in and a snippet around it', async () => {
    const answer = await call(contract(), 'search_doc_text', { query: 'step' });
    expect(answer.result).toEqual({ total_matches: 2, results: [
      { paragraph_index: 10, snippet: 'First step' }, { paragraph_index: 11, snippet: 'Second step' }] });
  });

  it('checks the structure: styles, list numbering and the cells of each table row', async () => {
    const answer = await call(contract(), 'verify_doc', {});
    expect(answer.result).toMatchObject({
      styles: { Title: 1, Heading1: 3, Normal: 10 },
      lists: [{ paragraph_index: 1, listString: '1.' }, { paragraph_index: 3, listString: '2.' },
        { paragraph_index: 10, listString: '1.' }, { paragraph_index: 11, listString: '2.' },
        { paragraph_index: 12, listString: '3.' }],
      tables: [{ table_index: 0, row_count: 3, cells_per_row: [2, 2, 1], uniform: false }],
    });
  });
});

describe('lists and blank paragraphs', () => {
  it('creates a numbered list after a paragraph and adds an item that continues one', async () => {
    const doc = contract();
    const created = await call(doc, 'edit_doc_list', { action: 'create_list', anchor_text: 'of receipt.',
      items: ['Net 30', 'Net 60'], list_style: 'number' });
    expect(created.result).toMatchObject({ items: [{ text: 'Net 30', listString: '1.' },
      { text: 'Net 60', listString: '2.' }] });
    const added = await call(doc, 'edit_doc_list', { action: 'insert_item', anchor_item_text: 'Second step',
      new_item_text: 'Third step', position: 'After' });
    expect(added.result).toMatchObject({ items: [{ text: 'Third step', isListItem: true, listString: '3.' }] });
  });

  it('shrinks runs of empty paragraphs and keeps page breaks and the ones that hold something', async () => {
    const doc = new FakeDocument(['A', '', '', '', '\f', '', { text: '', inline: [{ at: 0, kind: 'shapes' }] }, 'B',
      '', { text: '', inline: [{ at: 0, kind: 'pictures' }] }, { text: '', table: 1 }, 'C']);
    const answer = await call(doc, 'collapse_blank_paragraphs', { max_consecutive: 1 });
    expect(answer.result).toEqual({ deleted: 2, scanned: 12, skipped_with_content: 3 });
    expect(texts(doc)).toEqual(['A', '', '\f', '', '', 'B', '', '', '', 'C']);
  });
});

describe('suggestion cards', () => {
  it('shows the edits it finds as pending cards and returns the others to fix', async () => {
    const doc = contract();
    const settings = new FakeSettings();
    const reviews = new DocEditReview(settings, doc.run, new HostWriteQueue());
    const answer = await call(doc, 'propose_doc_edits', { edits: [
      { id: 'pay-1', section: 'Payment', change_summary: 'Pay in 45 days', old_text: 'thirty (30) days',
        new_text: 'forty-five (45) days' },
      { id: 'gone', section: 'Payment', change_summary: 'x', old_text: 'sixty days', new_text: 'ninety days' },
    ] }, reviews);
    expect(answer.result).toMatchObject({ pending_review: true, proposed: ['pay-1'], sections: ['Payment'],
      invalid_edits: [{ id: 'gone', error: expect.stringContaining('do not write it with another tool') }] });
    expect(reviews.card('call_1', 'pay-1')?.state).toEqual({ status: 'pending' });
    expect(reviews.card('call_1', 'gone')).toBeUndefined();
    expect(texts(doc)).toEqual(texts(contract()));
    // The cards outlive the pane: the document's settings hold them.
    expect(new DocEditReview(settings, doc.run, new HostWriteQueue()).card('call_1', 'pay-1')?.state)
      .toEqual({ status: 'pending' });

    const withdrawn = await call(doc, 'withdraw_doc_edits', { ids: ['pay-1', 'other'] }, reviews);
    expect(withdrawn.result).toEqual({ withdrawn: ['pay-1'], not_withdrawn: [
      { id: 'other', reason: 'no suggestion of this conversation has this id' }] });
  });
});
