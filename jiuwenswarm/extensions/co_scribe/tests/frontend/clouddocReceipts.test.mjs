import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canLocate,
  changedSpan,
  editingDocId,
  isWholeDocumentReceipt,
} from '../../../../channels/web/frontend/node_modules/.cache/clouddoc-receipts/extensions/co_scribe/frontend/features/clouddoc/receipts.js';

// The workbench's markdown editor saved by replacing the file, so the ledger
// records the whole old text against the whole new text. Markdown was withdrawn
// and the editor went with it, but the receipts it wrote are still in the ledger:
// a record is not rewritten because the feature that wrote it is gone, so the
// surfaces that render an edit as a fragment still have to trim these pairs.

const saved = { source: 'workbench_save' };
const chat = { source: 'clouddoc_apply_direct' };

test('a workbench save is a whole-document pair, a chat write is not', () => {
  assert.equal(isWholeDocumentReceipt(saved), true);
  assert.equal(isWholeDocumentReceipt(chat), false);
  assert.equal(isWholeDocumentReceipt({}), false);
});

test('a fragment receipt is handed back untouched', () => {
  const span = changedSpan(chat, 'a\nb\nc', 'a\nX\nc');
  assert.deepEqual(span, { old: 'a\nb\nc', new: 'a\nX\nc', startLine: 1 });
});

test('a changed line is trimmed to itself, and the line number is where it sits', () => {
  const span = changedSpan(saved, 'title\nold body\ntail', 'title\nnew body\ntail');
  assert.deepEqual(span, { old: 'old body', new: 'new body', startLine: 2 });
});

test('an appended line reads as an insertion, not as the whole file', () => {
  assert.deepEqual(changedSpan(saved, 'a', 'a\nb'), { old: '', new: 'b', startLine: 2 });
});

test('a prepended line starts at line one', () => {
  assert.deepEqual(changedSpan(saved, 'b', 'a\nb'), { old: '', new: 'a', startLine: 1 });
});

test('a deleted line leaves an empty new side', () => {
  assert.deepEqual(changedSpan(saved, 'a\nb\nc', 'a\nc'), { old: 'b', new: '', startLine: 2 });
});

test('a rewrite that shares no line is the whole pair, from line one', () => {
  assert.deepEqual(changedSpan(saved, 'a\nb', 'x\ny'), { old: 'a\nb', new: 'x\ny', startLine: 1 });
});

test('a save that changed nothing yields an empty span, so the highlight clears', () => {
  const span = changedSpan(saved, 'a\nb\nc', 'a\nb\nc');
  assert.equal(span.old, '');
  assert.equal(span.new, '');
});

test('repeated identical lines do not confuse the head and tail walk', () => {
  // Both ends match everywhere; the trim must not consume the same line twice
  // and report a span longer than either text.
  assert.deepEqual(changedSpan(saved, 'x\nx\nx', 'x\nx\nx\nx'), {
    old: '',
    new: 'x',
    startLine: 4,
  });
});

test('an empty document gaining its first line starts at line one', () => {
  assert.deepEqual(changedSpan(saved, '', 'hello'), { old: '', new: 'hello', startLine: 1 });
});

test('a multi-line block replaced in the middle keeps both sides whole', () => {
  const before = 'intro\nalpha\nbeta\noutro';
  const after = 'intro\ngamma\ndelta\nepsilon\noutro';
  assert.deepEqual(changedSpan(saved, before, after), {
    old: 'alpha\nbeta',
    new: 'gamma\ndelta\nepsilon',
    startLine: 2,
  });
});

// Locate is offered only where pressing it lands the reader on the write. A
// plain document with no platform anchor merely reloaded the frame, so the
// control said "here" and pointed at the same page.

const applied = { status: 'applied', edits: [{ old: 'a', new: 'b' }] };
const anchored = { status: 'applied', edits: [{ old: 'a', new: 'b', anchor: '#gid=0&range=A1' }] };

test('a platform anchor is locatable whatever the format', () => {
  assert.equal(canLocate('spreadsheet', anchored), true);
  assert.equal(canLocate('presentation', anchored), true);
});

test('a plain document with no anchor is not locatable', () => {
  assert.equal(canLocate('document', applied), false);
  assert.equal(canLocate(undefined, applied), false);
});

// Every format is the platform's own page in a frame now, so the anchor is the
// whole answer and the kind no longer changes it. A row left over from markdown
// asks the same question as any other and gets the same "nowhere to point".
test('the format no longer decides: an unanchored write is unlocatable whatever it is', () => {
  for (const kind of ['document', 'spreadsheet', 'presentation', 'markdown']) {
    assert.equal(canLocate(kind, applied), false);
  }
});

test('a write that has not landed has nowhere to go', () => {
  for (const status of ['pending', 'aborted', 'unknown']) {
    assert.equal(canLocate('spreadsheet', { ...anchored, status }), false);
  }
  assert.equal(canLocate('spreadsheet', { ...anchored, status: 'applied_unverified' }), true);
});

test('a share or a trash changed no text, so it has no place to point at', () => {
  assert.equal(canLocate('spreadsheet', { ...anchored, op: 'share' }), false);
  assert.equal(canLocate('spreadsheet', { ...anchored, op: 'trash' }), false);
  assert.equal(canLocate('spreadsheet', { ...anchored, op: 'edit' }), true);
});

// The chat strip's "editing: X" line. It is a claim in the present tense, so the
// predicate behind it has to be false whenever nothing is being written -- the
// version before this fell back to the focused tab and was therefore wrong most
// of the time it was on screen.

const exec = (name, docId, status = 'pending', updatedAt = '2026-09-07T10:00:00Z') => ({
  toolCallId: `${name}-${docId}`,
  toolCall: { name, arguments: { doc_id: docId } },
  status,
  startedAt: updatedAt,
  updatedAt,
});

test('a write in flight names the document it is aimed at', () => {
  assert.equal(editingDocId([exec('clouddoc_batch_edit', 'D1')]), 'D1');
  assert.equal(editingDocId([exec('clouddoc_write_region', 'D2')]), 'D2');
  assert.equal(editingDocId([exec('clouddoc_apply_for_comment', 'D3')]), 'D3');
  assert.equal(editingDocId([exec('clouddoc_add_page', 'D4')]), 'D4');
});

test('nothing running means nothing is being edited', () => {
  assert.equal(editingDocId(undefined), '');
  assert.equal(editingDocId([]), '');
});

test('a finished write is no longer being edited', () => {
  for (const status of ['completed', 'error', 'timeout']) {
    assert.equal(editingDocId([exec('clouddoc_batch_edit', 'D1', status)]), '');
  }
});

test('reading, listing, replying and sharing are not editing', () => {
  for (const name of [
    'clouddoc_read', 'clouddoc_list_comments', 'clouddoc_list_documents',
    'clouddoc_reply_comment', 'clouddoc_share_document', 'clouddoc_trash_document',
    'clouddoc_workmode_get',
  ]) {
    assert.equal(editingDocId([exec(name, 'D1')]), '', name);
  }
});

test('a non-clouddoc tool never names a document', () => {
  assert.equal(editingDocId([exec('write_file', 'D1')]), '');
});

test('with two writes in flight the newest one wins', () => {
  const older = exec('clouddoc_batch_edit', 'OLD', 'pending', '2026-09-07T10:00:00Z');
  const newer = exec('clouddoc_write_region', 'NEW', 'pending', '2026-09-07T10:05:00Z');
  assert.equal(editingDocId([older, newer]), 'NEW');
  assert.equal(editingDocId([newer, older]), 'NEW');
});

test('a write with no doc_id argument names nothing', () => {
  assert.equal(editingDocId([{ toolCall: { name: 'clouddoc_batch_edit', arguments: {} }, status: 'pending', startedAt: '', updatedAt: '' }]), '');
});
