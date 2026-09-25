/**
 * The handoff between a chat reference chip (or a Docs panel row) and the
 * document workbench (release §14). A click can race a view's mount -- the nav
 * switch and the event fire in the same tick -- so the doc id travels through
 * this module-level latch rather than the event payload: the event only says
 * "look", and the one consumer (App, which owns the workbench) gets the id
 * exactly once.
 */

/**
 * Whether a string can be a cloud-document reference at all.
 *
 * The model supplies doc_id, and a required opaque parameter invites invention.
 * Measured 2026-09-10: a chat turn called clouddoc_read with the document's
 * TITLE -- "Co-scribe Google Sheet Test 01" -- in doc_id. The backend settles
 * that on its own, but the session strip and the workbench read the RAW
 * argument the model sent, so the title became a second chip beside the real
 * document and a tab whose only content was "platform link unknown, paste the
 * link again" -- a document that never existed, asking the person to repair it.
 *
 * Neither platform's id carries whitespace, so whitespace is proof of prose and
 * the cheapest honest filter. Deliberately narrow: an id merely absent from the
 * deployment's list still opens, because absence is not proof of invention.
 */
export function looksLikeDocRef(id: string): boolean {
  return !!id && !/\s/.test(id);
}

export const OPEN_DOC_EVENT = 'jiuwen:clouddoc-open-doc';

let pendingDocId: string | null = null;
// A receipt the opener wants landed on, travelling with the id for the same
// reason the id does: the workbench may not be mounted yet, and an event
// payload read by nobody is a locate that silently does nothing.
let pendingReceiptId = '';

export function requestOpenDoc(docId: string, receiptId = ''): void {
  // Refused here rather than at each caller: this latch is the one road into
  // the workbench, so a reference that cannot be a document never becomes a tab.
  if (!looksLikeDocRef(docId)) return;
  pendingDocId = docId;
  pendingReceiptId = receiptId;
  window.dispatchEvent(new Event(OPEN_DOC_EVENT));
}

export function consumePendingOpenDoc(): { docId: string; receiptId: string } | null {
  const v = pendingDocId;
  const r = pendingReceiptId;
  pendingDocId = null;
  pendingReceiptId = '';
  return v ? { docId: v, receiptId: r } : null;
}
