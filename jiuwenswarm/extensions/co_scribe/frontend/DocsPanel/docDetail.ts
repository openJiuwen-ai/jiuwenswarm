/**
 * The document-detail timeline: one ordered list built from the two halves the
 * mechanism keeps apart.
 *
 * The registry's audit journal answers "who authorised this and when"; the
 * receipt ledger answers "what was actually written". Read separately -- which
 * is what two dialogs forced -- neither answers the question a person opens
 * them for: *this* write, was it inside a grant, and which one. Merged on the
 * timestamp both sides already record, the order is the causality: issue →
 * dispatch → write → revoke → re-issue.
 *
 * Kept free of React and i18n so the ordering and the CSV escaping can be read
 * -- and reasoned about -- without a renderer around them. Labels come in from
 * the caller, because the export is a snapshot of what is on screen and the
 * words on screen are the ones the viewer chose.
 */
import type { ReceiptRow } from '../features/clouddoc/receipts';

/** One line of the registry's audit journal, as ``watch_usage`` returns it. */
export type LineageRow = {
  ts?: number;
  event?: string;
  reason?: string;
  by?: string;
  issued_by?: string;
  mode?: string;
};

/** The three things a timeline row can be. ``dispatch`` covers both the turn
 *  that ran and the one that was refused: they are the same act, with and
 *  without permission. */
export type TimelineCategory = 'authority' | 'dispatch' | 'write';

export type TimelineRow =
  | { id: string; ts: number; category: 'authority' | 'dispatch'; lineage: LineageRow }
  | { id: string; ts: number; category: 'write'; receipt: ReceiptRow };

/** The filter over the timeline. ``authority`` is the journal side whole --
 *  grants and the dispatches made under them -- because the split a person
 *  actually wants is "the authority record" against "the writes", and a
 *  dispatch belongs to the first. */
export type DetailFilter = 'all' | 'write' | 'authority';

const DISPATCH_EVENTS = new Set(['dispatch', 'deny']);

export function lineageCategory(event: string | undefined): 'authority' | 'dispatch' {
  return DISPATCH_EVENTS.has(event ?? '') ? 'dispatch' : 'authority';
}

// Descending display reverses causality, so within one timestamp the write --
// the last thing to happen -- goes on top, then the dispatch that carried it,
// then the grant that allowed it. Without this, a grant and the write it
// authorised, landing in the same second, could read in the order that says the
// write came first.
const TIE_RANK: Record<TimelineCategory, number> = { write: 0, dispatch: 1, authority: 2 };

/**
 * Merge the journal and the ledger into one list, newest first.
 *
 * Both sides carry ``ts`` as epoch seconds from the same clock (the registry
 * writes ``time.time()``, the ledger the same), so the merge is a sort, not a
 * conversion. A row with no timestamp is kept and sorted to the bottom rather
 * than dropped: a record that exists is a fact, and hiding it because a field
 * is missing is the one thing an audit view must not do.
 *
 * Receipts are de-duplicated by ``receipt_id``. Journal lines are **not**
 * de-duplicated: two dispatches in the same second are two dispatches, and
 * collapsing them would under-count the grant's use.
 */
export function buildTimeline(lineage: LineageRow[], receipts: ReceiptRow[]): TimelineRow[] {
  const rows: TimelineRow[] = [];
  lineage.forEach((row, i) => {
    rows.push({
      id: `a${i}`,
      ts: typeof row.ts === 'number' && isFinite(row.ts) ? row.ts : 0,
      category: lineageCategory(row.event),
      lineage: row,
    });
  });
  const seen = new Set<string>();
  for (const r of receipts) {
    if (!r || seen.has(r.receipt_id)) continue;
    seen.add(r.receipt_id);
    rows.push({
      id: `w${r.receipt_id}`,
      ts: typeof r.ts === 'number' && isFinite(r.ts) ? r.ts : 0,
      category: 'write',
      receipt: r,
    });
  }
  // A stable sort keeps each side's own order (both arrive newest first) where
  // timestamp and rank cannot separate two rows.
  return rows
    .map((row, i) => ({ row, i }))
    .sort((a, b) => {
      if (b.row.ts !== a.row.ts) return b.row.ts - a.row.ts;
      const rank = TIE_RANK[a.row.category] - TIE_RANK[b.row.category];
      if (rank !== 0) return rank;
      return a.i - b.i;
    })
    .map((x) => x.row);
}

export function filterTimeline(rows: TimelineRow[], filter: DetailFilter): TimelineRow[] {
  if (filter === 'all') return rows;
  if (filter === 'write') return rows.filter((r) => r.category === 'write');
  return rows.filter((r) => r.category !== 'write');
}

/**
 * ISO 8601 in the viewer's own zone, offset included.
 *
 * Not ``toISOString``: that is UTC, and a person reconciling the export against
 * the timeline they just looked at would be comparing two different clocks with
 * nothing on the page saying so. The offset makes it unambiguous either way.
 */
export function localIso(ts: number): string {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const p = (n: number, w = 2) => String(Math.abs(n)).padStart(w, '0');
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? '+' : '-';
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
    `T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}` +
    `${sign}${p(Math.floor(Math.abs(off) / 60))}:${p(Math.abs(off) % 60)}`
  );
}

/** RFC 4180: a field is quoted when it holds a quote, a comma or a newline,
 *  and an embedded quote is doubled. Nothing is truncated -- being complete is
 *  the whole reason to export rather than read the screen. */
export function csvField(value: unknown): string {
  const s = value == null ? '' : String(value);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** CRLF between records, as RFC 4180 says, and a BOM in front so Excel reads
 *  the UTF-8 rather than guessing a local codepage at the first Chinese
 *  character. */
export function toCsv(rows: readonly (readonly unknown[])[]): string {
  return '\uFEFF' + rows.map((r) => r.map(csvField).join(',')).join('\r\n') + '\r\n';
}

/** ``<title>-<YYYYMMDD-HHmm>.csv``, with anything a filesystem or a browser
 *  would argue about replaced by a hyphen. */
export function csvFilename(title: string, at: Date = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0');
  const stamp =
    `${at.getFullYear()}${p(at.getMonth() + 1)}${p(at.getDate())}` +
    `-${p(at.getHours())}${p(at.getMinutes())}`;
  const clean = (title || 'document')
    .replace(/[\\/:*?"<>|\u0000-\u001F\s-]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^[.\-]+|[.\-]+$/g, '')
    .slice(0, 60);
  return `${clean || 'document'}-${stamp}.csv`;
}

/** The words the export borrows from the screen. */
export type CsvLabels = {
  header: string[];
  category: (c: TimelineCategory) => string;
  event: (row: LineageRow) => string;
  reason: (row: LineageRow) => string;
  status: (receipt: ReceiptRow) => string;
};

export type CsvContext = { docId: string; title: string; labels: CsvLabels };

/**
 * One CSV record per **edit**, not per receipt: a batch that changed four
 * places is four before/after pairs, and joining them into one cell would pair
 * them by line number -- which stops being true the moment an edit contains a
 * newline of its own. A receipt with no edits (a share, a trash) still gets its
 * one record.
 *
 * Authority records leave ``old_text``/``new_text`` empty and write records
 * leave ``event`` empty. Neither side is padded with a borrowed value: a column
 * that reads empty is telling the truth about which ledger the row came from.
 */
export function buildCsvRows(rows: TimelineRow[], ctx: CsvContext): string[][] {
  const { docId, title, labels } = ctx;
  const out: string[][] = [labels.header.slice()];
  for (const row of rows) {
    const head = [docId, title, localIso(row.ts), labels.category(row.category)];
    if (row.category === 'write') {
      const r = row.receipt;
      const edits = r.edits && r.edits.length ? r.edits : [{ old: '', new: '' }];
      for (const e of edits) {
        out.push([
          ...head,
          '', // event: the ledger records a status, not a journal event
          r.receipt_id ?? '',
          labels.status(r),
          r.executor ?? '',
          r.source ?? '',
          r.abort_reason ?? '',
          e.old ?? '',
          e.new ?? '',
        ]);
      }
    } else {
      const l = row.lineage;
      out.push([
        ...head,
        labels.event(l),
        '',
        '',
        // The journal's actor: who issued, who revoked. It is the same question
        // ``executor`` asks of a write, so it answers in the same column.
        l.issued_by || l.by || '',
        '',
        labels.reason(l),
        '',
        '',
      ]);
    }
  }
  return out;
}

/** Hand the browser the file. Purely a side effect, kept here so the caller is
 *  one line and the object URL is always released. */
export function downloadCsv(filename: string, body: string): void {
  const blob = new Blob([body], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
