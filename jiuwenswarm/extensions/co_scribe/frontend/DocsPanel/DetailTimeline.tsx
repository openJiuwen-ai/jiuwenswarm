/**
 * The document-detail timeline, rendered by both surfaces that show it.
 *
 * The Docs panel's 文档记录 dialog and the workbench's right rail were two
 * renderers of the same rows: one merged authority/dispatch/write list in the
 * dialog, a hand-written write-only list in the rail. Written twice, they drift
 * -- and a presentation rule that lived in three copies has already cost a day.
 * One component, two callers, one set of `data-testid`s.
 *
 * Ordering, filtering and the CSV live in ``docDetail.ts``, which stays free of
 * React; this file is the renderer and the only place the journal's vocabulary
 * is turned into words.
 */
import type { ReactNode } from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  RECEIPT_OP_KEY,
  RECEIPT_STATUS_KEY,
  canLocate,
  changedSpan,
  receiptRegions,
  type ReceiptRow,
} from '../features/clouddoc/receipts';
import {
  buildCsvRows,
  buildTimeline,
  csvFilename,
  downloadCsv,
  filterTimeline,
  toCsv,
  type DetailFilter,
  type LineageRow,
  type TimelineCategory,
} from './docDetail';

// One audit line in plain words. The journal's own vocabulary (grant / revoke /
// suspend_retired) is the mechanism's, and a person opening 文档记录 to find out
// why a watch is off should not have to learn it.
//
// The last four name operations this release no longer offers. They stay mapped,
// and their labels say "retired", because the journal is append-only and a line
// written when the operation existed still has to read as what happened. Dropping
// them would not delete the line -- it would print the mechanism's raw English
// into a Chinese timeline.
const LINEAGE_EVENT_KEY: Record<string, string> = {
  grant: 'docs.audit.evGrant',
  modify: 'docs.audit.evModify',
  revoke: 'docs.audit.evRevoke',
  expire: 'docs.audit.evExpire',
  dispatch: 'docs.audit.evDispatch',
  deny: 'docs.audit.evDeny',
  suspend: 'docs.audit.evSuspend',
  resume: 'docs.audit.evResume',
  unadopt: 'docs.audit.evUnadopt',
  readopt: 'docs.audit.evReadopt',
};

const LINEAGE_REASON_KEY: Record<string, string> = {
  document_removed: 'docs.audit.reasonDocumentRemoved',
  connection_removed: 'docs.audit.reasonConnectionRemoved',
  retired_tier: 'docs.audit.reasonRetiredTier',
  suspend_retired: 'docs.audit.reasonSuspendRetired',
  kill_switch: 'docs.audit.reasonKillSwitch',
};

type Translate = (k: string, o?: Record<string, unknown>) => string;

/** A timestamp as the timeline shows it. Exported because the panel's grant
 *  summary sits above the timeline and has to read the same clock. */
export function fmtTs(v: unknown): string {
  return typeof v === 'number' && v > 0 ? new Date(v * 1000).toLocaleString() : '—';
}

export function lineageEvent(row: LineageRow, t: Translate): string {
  return t(LINEAGE_EVENT_KEY[row.event ?? ''] ?? '', { defaultValue: row.event ?? '' });
}

export function lineageReason(row: LineageRow, t: Translate): string {
  // ``by: kill_switch`` is a reason wearing the other field's name -- the batch
  // revocation records who did it, and who is the answer to why.
  const raw = row.reason || (row.by === 'kill_switch' ? 'kill_switch' : '');
  if (!raw) {
    // A revocation with nothing else said is somebody pressing the button.
    return row.event === 'revoke' ? t('docs.audit.reasonManual') : '';
  }
  return t(LINEAGE_REASON_KEY[raw] ?? '', { defaultValue: raw });
}

export type DetailTimelineProps = {
  docId: string;
  /** Shown only in the CSV (its title column and the filename); the surfaces
   *  name the document in their own header. */
  title?: string;
  /** The document's format, for deciding whether a write can be located. */
  kind?: string;
  /** The registry's audit journal. Empty where a surface only holds the ledger:
   *  the merge then degrades to the write rows, which is the truth it has. */
  lineage: LineageRow[];
  receipts: ReceiptRow[];
  loading?: boolean;
  /**
   * The 258px rail. Density only: the same rows, the same testids, tighter
   * padding -- and the three things that need width or data the rail has not
   * got are left out (see the render).
   */
  dense?: boolean;
  /** Rendered above the filter row. The panel puts its grant summary here; it
   *  reads the registry, which is the panel's own fetch. */
  summary?: ReactNode;
  /** Rendered under the export hint. Each surface keeps its own note element,
   *  because each has its own testid and its own failure to report. */
  note?: ReactNode;
  /** Offered per receipt, and only where ``canLocate`` says it can act. Leave
   *  it out and no locate control is rendered at all. */
  onLocate?: (r: ReceiptRow) => void;
  onUnhighlight?: (receiptId: string) => void;
  /** The receipt an action is in flight for, so its button disables. */
  actingOn?: string | null;
};

export function DetailTimeline({
  docId,
  title,
  kind,
  lineage,
  receipts,
  loading = false,
  dense = false,
  summary,
  note,
  onLocate,
  onUnhighlight,
  actingOn = null,
}: DetailTimelineProps) {
  const { t } = useTranslation();
  const [filter, setFilter] = useState<DetailFilter>('all');
  // Which write rows are open. A write is collapsed to one line by default --
  // the ledger's rows are long, and a timeline you have to scroll past one
  // receipt at a time is not a timeline.
  const [expandedWrites, setExpandedWrites] = useState<Set<string>>(new Set());
  // A different document is a different record: neither the filter nor the rows
  // a person opened on the last one say anything about this one.
  useEffect(() => {
    setFilter('all');
    setExpandedWrites(new Set());
  }, [docId]);

  // The journal's rows and the ledger's rows, ordered together. The merge is
  // client-side on purpose: both RPCs already return everything needed, and a
  // third endpoint would have to be kept honest about a join the caller can do
  // for free.
  const timeline = useMemo(() => buildTimeline(lineage, receipts), [lineage, receipts]);
  const visibleTimeline = useMemo(() => filterTimeline(timeline, filter), [timeline, filter]);

  const categoryLabel = useCallback(
    (c: TimelineCategory) =>
      t(
        c === 'write'
          ? 'docs.detail.catWrite'
          : c === 'dispatch'
            ? 'docs.detail.catDispatch'
            : 'docs.detail.catAuthority',
      ),
    [t],
  );

  const toggleWrite = useCallback((id: string) => {
    setExpandedWrites((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // The export is the **filtered** list, not everything fetched: what is on
  // screen is what a person believes they are exporting, and a file with rows
  // the filter hid would be a quiet lie. The hint beside the button says so, and
  // says where the whole record actually lives.
  const exportCsv = useCallback(() => {
    const name = title || docId;
    const cols = [
      'colDocId', 'colTitle', 'colTime', 'colCategory', 'colEvent', 'colReceiptId',
      'colStatus', 'colExecutor', 'colSource', 'colReason', 'colOldText', 'colNewText',
    ];
    const rows = buildCsvRows(visibleTimeline, {
      docId,
      title: name,
      labels: {
        header: cols.map((k) => t(`docs.detail.${k}`)),
        category: categoryLabel,
        event: (row) => lineageEvent(row, t),
        reason: (row) => lineageReason(row, t),
        status: (r) => t(RECEIPT_STATUS_KEY[r.status] ?? '', { defaultValue: r.status }),
      },
    });
    downloadCsv(csvFilename(name), toCsv(rows));
  }, [docId, title, visibleTimeline, categoryLabel, t]);

  return (
    <>
      {summary}

      {/* ── 筛选与导出 ── */}
      {/* Both are left out of the rail: the export needs a button's width beside
          a select, and the filter needs the authority rows the rail does not
          fetch -- offering it there would be two choices that empty the list. */}
      {!dense && (
        <>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <select
              value={filter}
              onChange={(e) => setFilter(e.target.value as DetailFilter)}
              data-testid="docs-panel-detail-filter"
              className="rounded-md border border-border bg-card px-2 py-1 text-xs"
            >
              <option value="all">{t('docs.detail.filterAll')}</option>
              <option value="write">{t('docs.detail.filterWrites')}</option>
              <option value="authority">{t('docs.detail.filterAuthority')}</option>
            </select>
            <span className="flex-1" />
            <button
              data-testid="docs-panel-detail-export"
              disabled={visibleTimeline.length === 0}
              onClick={exportCsv}
              className="rounded-md border border-border px-2.5 py-1 text-xs hover:bg-bg-hover disabled:opacity-40"
            >
              {t('docs.detail.export')}
            </button>
          </div>
          {/* The export is a view, not the record. Said on screen rather than
              only in the file, because the moment to know it is before the
              download, not after. */}
          <p className="mt-1 text-[11px] text-text-muted" data-testid="docs-panel-detail-export-hint">
            {t('docs.detail.exportHint')}
          </p>
        </>
      )}

      {note}

      {/* ── 时间线：签发 → 派发 → 写入 → 撤销 → 再签发，倒序 ── */}
      <div className={dense ? 'min-h-0' : 'mt-3 min-h-0 flex-1 overflow-auto'}>
        {loading ? (
          <p className="py-6 text-center text-xs text-text-muted">{t('docs.history.loading')}</p>
        ) : visibleTimeline.length === 0 ? (
          <p data-testid="docs-panel-detail-empty" className="py-6 text-center text-xs text-text-muted">
            {/* Without the journal there is only one thing that can be missing,
                and the receipts' own wording says which. */}
            {dense
              ? t('docs.history.empty')
              : timeline.length === 0
                ? t('docs.detail.empty')
                : t('docs.detail.emptyFiltered')}
          </p>
        ) : (
          <ul className={`flex flex-col ${dense ? 'gap-1' : 'gap-1.5'}`}>
            {visibleTimeline.map((row) => {
              if (row.category !== 'write') {
                const l = row.lineage;
                const why = lineageReason(l, t);
                const who = l.issued_by || l.by || '';
                return (
                  <li
                    key={row.id}
                    data-testid="docs-panel-lineage-row"
                    data-variant={row.category}
                    className={`rounded-md border border-border/60 text-xs text-text-muted ${
                      dense ? 'px-2 py-1' : 'px-3 py-1.5'
                    }`}
                  >
                    {!dense && (
                      <span
                        className={`mr-1.5 rounded px-1 text-[10px] ${
                          row.category === 'dispatch' ? 'bg-bg-muted' : 'bg-accent-subtle text-accent'
                        }`}
                      >
                        {categoryLabel(row.category)}
                      </span>
                    )}
                    <span className="font-mono">{fmtTs(row.ts)}</span>
                    {' · '}
                    <span className="text-text">{lineageEvent(l, t)}</span>
                    {why ? ` · ${t('docs.audit.reason', { reason: why })}` : ''}
                    {who ? ` · ${t('docs.audit.byLine', { by: who })}` : ''}
                  </li>
                );
              }
              const r = row.receipt;
              const edits = r.edits ?? [];
              const open = expandedWrites.has(r.receipt_id);
              // A person's save replaces the file, so its pair is the whole
              // document; trimmed to the lines that differ it reads as a change.
              const previewSpan = changedSpan(r, edits[0]?.old || '', edits[0]?.new || '');
              const preview = previewSpan.new || previewSpan.old || '';
              // A cell range or a slide address is where the write landed, which
              // is what a person reads first on a sheet or a deck; the text
              // follows it where there is room.
              const regions = receiptRegions(r).join(', ');
              return (
                <li
                  key={row.id}
                  data-testid="docs-panel-history-row"
                  data-variant={r.status}
                  className={`rounded-md border border-border ${dense ? 'px-2 py-1.5' : 'px-3 py-2'}`}
                >
                  <div className={dense ? 'flex flex-col gap-1' : 'flex items-start justify-between gap-3'}>
                    <button
                      type="button"
                      data-testid="docs-panel-history-toggle"
                      aria-expanded={open}
                      title={open ? t('docs.detail.collapse') : t('docs.detail.expand')}
                      onClick={() => toggleWrite(r.receipt_id)}
                      className="min-w-0 flex-1 text-left"
                    >
                      <span className="block text-xs text-text-muted">
                        {!dense && (
                          <span className="mr-1.5 rounded bg-bg-muted px-1 text-[10px]">
                            {categoryLabel('write')}
                          </span>
                        )}
                        {fmtTs(r.ts)}
                        {' · '}
                        <span className="font-mono">{(r.receipt_id || '').slice(0, 8)}</span>
                        {' · '}
                        <span data-testid="docs-panel-history-status">
                          {t(RECEIPT_STATUS_KEY[r.status] ?? '', { defaultValue: r.status })}
                        </span>
                        {r.highlight ? ` · ${t('docs.history.highlighted')}` : ''}
                      </span>
                      {!open && (
                        <span className="mt-0.5 block truncate text-xs text-text">
                          {regions ? `${regions}${preview ? ' · ' : ''}` : ''}
                          {preview}
                          {edits.length > 1 ? ` · ${t('docs.history.more', { n: edits.length - 1 })}` : ''}
                        </span>
                      )}
                    </button>
                    <span className="flex shrink-0 gap-2">
                      {/* Both actions are offered only where they mean something: a
                          write with nowhere to go has no locate, and one that
                          was not highlighted has nothing to clear. */}
                      {onLocate && canLocate(kind, r) && (
                        <button
                          data-testid="docs-panel-history-locate"
                          onClick={() => onLocate(r)}
                          className="rounded-md border border-border px-2 py-0.5 text-xs hover:bg-bg-hover"
                        >
                          {t('docs.detail.locate')}
                        </button>
                      )}
                      {onUnhighlight && r.status === 'applied' && r.highlight && (
                        <button
                          data-testid="docs-panel-history-unhighlight"
                          disabled={actingOn === r.receipt_id}
                          onClick={() => onUnhighlight(r.receipt_id)}
                          className="rounded-md border border-border px-2 py-0.5 text-xs hover:bg-bg-hover disabled:opacity-50"
                        >
                          {t('docs.history.unhighlight')}
                        </button>
                      )}
                    </span>
                  </div>
                  {r.status === 'unknown' && (
                    <p data-testid="docs-panel-history-unknown-note" className="mt-1 text-xs text-text-muted">
                      {r.abort_reason || t('docs.history.unknownNote')}
                    </p>
                  )}
                  {/* Applied, but the read-back did not match. The rail said so
                      and the dialog did not; the ledger records one fact, so
                      both say it. */}
                  {r.unverified_detail && (
                    <p className="mt-1 text-[11px] text-warn">{r.unverified_detail}</p>
                  )}
                  {r.op && r.op !== 'edit' && (
                    <p data-testid="docs-panel-history-op" className="mt-1 text-xs text-text">
                      <span className="font-medium">
                        {t(RECEIPT_OP_KEY[r.op] ?? '', { defaultValue: r.op })}
                      </span>
                      {r.subject?.email ? ` · ${r.subject.email}` : ''}
                      {r.subject?.title ? ` · ${r.subject.title}` : ''}
                    </p>
                  )}
                  {open &&
                    edits.map((e, i) => (
                      <p key={i} className="mt-1 whitespace-pre-wrap break-words text-xs text-text">
                        <span className="text-text-muted line-through">{e.old || ''}</span>
                        {' → '}
                        <span>{e.new || ''}</span>
                      </p>
                    ))}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </>
  );
}
