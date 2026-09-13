/**
 * Receipt rows as the panel RPCs return them, and the two label maps the Docs
 * panel and the document workbench share. One place, so a status or act added
 * to the ledger is labelled the same everywhere it is shown.
 */
import type { ToolExecution } from '../../../../../channels/web/frontend/src/types/message';

export type ReceiptOp = 'edit' | 'create' | 'share' | 'trash' | 'restore' | 'unshare';

export type ReceiptRow = {
  receipt_id: string;
  ts: number;
  doc_id: string;
  status: string;
  abort_reason?: string;
  unverified_detail?: string;
  highlight?: boolean;
  executor?: string;
  source?: string;
  edits?: { old?: string; new?: string; region?: string; anchor?: string }[];
  op?: ReceiptOp;
  subject?: { title?: string; email?: string; role?: string };
};

export const RECEIPT_STATUS_KEY: Record<string, string> = {
  pending: 'docs.history.statusPending',
  applied: 'docs.history.statusApplied',
  applied_unverified: 'docs.history.statusAppliedUnverified',
  aborted: 'docs.history.statusAborted',
  reverted: 'docs.history.statusReverted',
  unknown: 'docs.history.statusUnknown',
};

export const RECEIPT_OP_KEY: Record<string, string> = {
  create: 'docs.history.opCreate',
  share: 'docs.history.opShare',
  trash: 'docs.history.opTrash',
  restore: 'docs.history.opRestore',
  unshare: 'docs.history.opUnshare',
};

/** The region a receipt wrote, for the workbench's locate: a cell range, a
 *  slide address or, for a text edit, nothing (text is located by content). */
export function receiptRegions(r: ReceiptRow): string[] {
  return (r.edits || []).map((e) => e.region || '').filter(Boolean);
}

/** The platform URL fragment that lands a browser on the receipt's first
 *  region (`#gid=…&range=…`, `#slide=id.…`), recorded by the writer; empty for
 *  a text edit or a platform without anchors. */
export function receiptAnchor(r: ReceiptRow): string {
  return (r.edits || []).map((e) => e.anchor || '').find(Boolean) ?? '';
}

/**
 * Whether a receipt has somewhere to go, so the surfaces only offer 定位 where
 * pressing it lands the reader on the write.
 *
 * Three things have to be true. The write has to have happened (a batch that was
 * refused or is still in flight has no place on the page yet), it has to be an
 * edit (a share or a trash changed no text), and the receipt has to carry the
 * platform's own anchor (``#gid=…&range=…``, ``#slide=id.…``), which the frame
 * reloads onto -- every format is the platform's own page in a frame now.
 *
 * A plain document with no anchor is the case this rules out. The frame merely
 * reloaded and nothing moved -- a control that answers "where?" with the same
 * page is worse than no control, because the reader concludes the record is
 * wrong rather than that the platform has no address for it.
 */
export function canLocate(_kind: string | undefined, r: ReceiptRow): boolean {
  if (r.status !== 'applied' && r.status !== 'applied_unverified') return false;
  if (r.op && r.op !== 'edit') return false;
  return !!receiptAnchor(r);
}

/**
 * The clouddoc tools that change a document's text. Sharing, trashing, replying
 * and reading all touch a document without editing it, and the strip's line
 * claims editing specifically.
 */
const WRITING_TOOLS = new Set([
  'clouddoc_batch_edit',
  'clouddoc_write_region',
  'clouddoc_add_page',
  'clouddoc_apply_for_comment',
  'clouddoc_create_document',
]);

/**
 * The document being edited **right now**, or empty when none is.
 *
 * Present tense on purpose. It backs a line that reads "editing: X", so what it
 * has to report is a writing tool still in flight -- not the document most
 * recently touched, which was the earlier reading and is a different claim.
 * It deliberately has no fallback to the focused tab -- naming the
 * document the reader happens to have open turns a status line into a decoration
 * that is wrong every time nothing is running, which in an audited product is
 * the worst kind of wrong.
 */
export function editingDocId(
  executions: Iterable<ToolExecution> | undefined,
): string {
  if (!executions) return '';
  let newest = '';
  let newestAt = '';
  for (const exec of executions) {
    if (exec.status !== 'pending') continue;
    if (!WRITING_TOOLS.has(exec.toolCall?.name ?? '')) continue;
    const args = exec.toolCall?.arguments as { doc_id?: unknown } | undefined;
    const docId = typeof args?.doc_id === 'string' ? args.doc_id : '';
    if (!docId) continue;
    const at = exec.updatedAt || exec.startedAt || '';
    if (!newest || at > newestAt) { newest = docId; newestAt = at; }
  }
  return newest;
}


/**
 * Receipt ids the session's own tool calls produced, newest last. The tool
 * result is the ledger's word (`receipt_id` in the JSON the tool returned); a
 * model that merely mentions a number is not a source.
 */
export function receiptsFromExecutions(
  executions: Iterable<ToolExecution> | undefined,
): { receiptId: string; docId: string; toolName: string; at: string }[] {
  const out: { receiptId: string; docId: string; toolName: string; at: string }[] = [];
  if (!executions) return out;
  for (const exec of executions) {
    const name = exec.toolCall?.name ?? '';
    if (!name.startsWith('clouddoc_') || !exec.result?.result) continue;
    let parsed: unknown;
    try {
      parsed = JSON.parse(exec.result.result);
    } catch {
      continue;
    }
    if (!parsed || typeof parsed !== 'object') continue;
    const rid = (parsed as { receipt_id?: unknown }).receipt_id;
    const args = exec.toolCall?.arguments as { doc_id?: unknown } | undefined;
    const docId = (parsed as { doc_id?: unknown }).doc_id ?? args?.doc_id;
    if (typeof rid === 'string' && rid && typeof docId === 'string') {
      out.push({ receiptId: rid, docId, toolName: name, at: exec.updatedAt || exec.startedAt });
    }
  }
  return out;
}

/**
 * A receipt whose edit pair is the **whole document**, not the span that changed.
 *
 * The workbench's markdown editor saved by replacing the file, so the ledger
 * honestly records (whole old text, whole new text) -- that is what happened.
 * No new receipt has this shape: markdown was withdrawn and the editor went with
 * it. The ones already in the ledger stay, and a record is not rewritten because
 * the feature that wrote it is gone, so every surface that renders an edit as a
 * fragment still has to know the difference -- or it prints an entire document as
 * a one-line summary. One predicate so the two surfaces cannot drift apart.
 */
export function isWholeDocumentReceipt(r: Pick<ReceiptRow, 'source'>): boolean {
  return (r.source || '') === 'workbench_save';
}

/**
 * The part of a whole-document pair that actually changed, trimmed to whole
 * lines from both ends.
 *
 * Line granularity rather than character: the consumer is a one-line summary, and
 * a fragment that starts mid-line reads as noise. A pair that is not
 * whole-document is returned untouched --
 * it is already the fragment.
 */
export function changedSpan(
  r: Pick<ReceiptRow, 'source'>,
  oldText: string,
  newText: string,
): { old: string; new: string; startLine: number } {
  if (!isWholeDocumentReceipt(r)) return { old: oldText, new: newText, startLine: 1 };
  const before = oldText.split('\n');
  const after = newText.split('\n');
  let head = 0;
  while (head < before.length && head < after.length && before[head] === after[head]) head += 1;
  let tail = 0;
  while (
    tail < before.length - head
    && tail < after.length - head
    && before[before.length - 1 - tail] === after[after.length - 1 - tail]
  ) tail += 1;
  return {
    old: before.slice(head, before.length - tail).join('\n'),
    new: after.slice(head, after.length - tail).join('\n'),
    startLine: head + 1,
  };
}
