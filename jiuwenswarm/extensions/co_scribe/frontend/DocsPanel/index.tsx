/**
 * The Docs panel -- the whole UI for cloud-document co-editing.
 *
 * Once the co-scribe plugin is enabled, every one of its surfaces lives here; there
 * is no cloud-doc page in Settings any more. Connection management came back into
 * this panel with it, which is also where it belongs: the connections and the
 * documents they carry are read together.
 *
 * The layout matches Channel Management in structure and proportion: connections in
 * the left column at minmax(340,430), detail filling the remaining width on the
 * right, and Refresh at the top right of the detail header, where Channel puts its
 * own.
 *
 * **One level, no navigation.** The left column is content, not a nav rail: the
 * add-connection control, the connection list and the deployment-default model
 * picker are all visible at once, with no tab strip, no accordion and no sub-page
 * to drill into. The only thing that opens over this surface is the per-document
 * record dialog, which is detail on demand for one row rather than a second level.
 *
 * **Adding comes first, and there is one list.** The control that makes a connection
 * sits at the top, because the list under it is empty exactly when a new deployment
 * needs that control. And the list is one list: a connection *is* a key, so the old
 * pair -- identities under "Connected", files under "Or use a saved key" -- printed
 * the same account twice and left the reader to pair them up. See `connKeyRows` for
 * the join and for the two ways the halves come apart.
 *
 * The wholly empty state, with no connections at all, keeps the same skeleton: the
 * left column's add-connection control is where it always is, with an empty list
 * under it, and the right column carries the guidance. A user sees the shape this
 * page will take before configuring anything, so where they learn things are does
 * not change once they have.
 */
import type { ReactNode } from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../../../channels/web/frontend/src/services/webClient';
import { requestOpenDoc } from '../features/clouddoc/openDocSignal';
import { connFullName, connShortNames, providerLabel } from '../features/clouddoc/provider';
import { type ReceiptRow } from '../features/clouddoc/receipts';
import { DetailTimeline, fmtTs } from './DetailTimeline';
import { AdoptByLink, type AddResult } from './AdoptByLink';
import { type LineageRow } from './docDetail';
import ModelPicker from '../../../../channels/web/frontend/src/components/ModelPicker';
import ConfirmDialog from '../../../../channels/web/frontend/src/components/CronPanel/ConfirmDialog';

interface DocRow {
  doc_id: string;
  url: string;
  title: string;
  checked_at?: number | null;
  status: 'ok' | 'comment_only' | 'frozen' | 'backoff';
  /** This document's own model pin. Empty means it follows the deployment default. */
  model_name?: string;
  // The document's format. A spreadsheet under management looked exactly like a
  // document in this list, which is how one went unnoticed while it was being polled.
  kind?: string;
  retry_at?: number | null;
  provider?: string;
  provider_name?: string;
  connection_id?: string;
}

interface Connection {
  id: string;
  provider: string;
  provider_name: string;
  agent_address: string;
  agent_display?: string;
  docs_count: number;
  health?: 'ok' | 'attention' | 'down' | 'idle';
  ok?: number;
  attention?: number;
  down?: number;
  /** The key file this identity runs on -- basename, full path, and whether it
      lives in the managed clouddoc-keys/ directory (the only place delete_key can
      reach). A path-mode connection's key is not in the stored-key listing at all,
      which is why the connection has to carry it. */
  key_filename?: string;
  key_path?: string;
  key_managed?: boolean;
}

/** A key file under clouddoc-keys/. `connection_id` is the join: null means no
    connection is running on it -- what remove_connection deliberately leaves behind. */
interface SavedKey {
  filename: string;
  path: string;
  client_email: string;
  address?: string;
  provider?: string;
  provider_name?: string;
  connection_id?: string | null;
  in_use: boolean;
}

interface ConfPayload {
  enabled: boolean;
  mode?: 'mandate' | 'recorded' | 'direct';
  /** The host offers a question channel; without one the always-ask floor refuses. */
  ask_channel?: boolean;
  agent_address?: string;
  /** The deployment default every document falls back to; empty means the
      agentserver's own default. */
  model_name?: string;
  connections?: Connection[];
}

// Provider to logo asset, following the same rule as ChannelsPanel's logo_src. An
// unregistered provider falls back to a generic document glyph, so a new provider
// without an asset leaves no hole in the layout.
const PROVIDER_LOGOS: Record<string, string> = { google: '/googledocs.svg' };

// One look per format, in the colours the platforms themselves use: a document is
// blue, a spreadsheet green, a deck amber. They shared one icon until now, so a
// spreadsheet in the managed list was indistinguishable from a document -- which is
// how one sat there being polled while the person looking at the list could not tell
// it was there.
//
// Drawn inline rather than from files: only the Docs logo exists as an asset, and a
// per-format set has to cover Feishu too, where the same three formats appear under a
// different brand. A shape the panel owns stays right for both.
const KIND_STYLE: Record<string, { bg: string; fg: string; glyph: 'doc' | 'sheet' | 'deck' | 'file' }> = {
  document: { bg: 'var(--color-feedback-info-subtle)', fg: 'var(--color-feedback-info)', glyph: 'doc' },
  spreadsheet: { bg: 'var(--color-feedback-success-subtle)', fg: 'var(--color-feedback-success)', glyph: 'sheet' },
  presentation: { bg: 'var(--color-feedback-warning-subtle)', fg: 'var(--color-feedback-warning)', glyph: 'deck' },
  markdown: { bg: 'var(--color-surface-muted)', fg: 'var(--color-text-secondary)', glyph: 'file' },
};

const GLYPHS: Record<string, ReactNode> = {
  doc: (
    <>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <path d="M14 2v6h6" />
    </>
  ),
  sheet: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M3 10h18M3 15h18M9 4v16M15 4v16" />
    </>
  ),
  deck: (
    // A slide, not a projector screen on a stand. The stand version drew its frame in
    // the top half and a thin pole and base below, which at the size this renders --
    // half of a 26px badge, so about 13 -- merged into a smudge, and left the deck the
    // only glyph whose body was not centred in its box. Sitting beside the document and
    // spreadsheet icons it read as a different kind of thing entirely.
    <>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <path d="M7 10h10M7 14h6" />
    </>
  ),
  file: (
    <>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <path d="M14 2v6h6M8 13h8M8 17h5" />
    </>
  ),
};

const DocIcon = ({ px = 32, provider, kind }: { px?: number; provider?: string; kind?: string }) => {
  const style = KIND_STYLE[kind || ''] ?? KIND_STYLE.document;
  // The platform logo is used only where it is still true: it is the Docs logo, and
  // putting it on a spreadsheet says the wrong thing.
  const src = provider && (!kind || kind === 'document') ? PROVIDER_LOGOS[provider] : undefined;
  if (src) {
    return <img src={src} alt="" aria-hidden style={{ height: px, width: px * 0.75 }} className="flex-none object-contain" />;
  }
  return (
    <span
      data-testid="docs-panel-kind-icon"
      data-kind={kind || 'document'}
      title={kind || 'document'}
      className="flex flex-none items-center justify-center rounded-full"
      style={{ height: px, width: px, background: style.bg, color: style.fg }}
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} style={{ height: px / 2, width: px / 2 }}>
        {GLYPHS[style.glyph]}
      </svg>
    </span>
  );
};

const ExtIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="h-3 w-3 flex-none">
    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
    <path d="M15 3h6v6" />
    <path d="M10 14 21 3" />
  </svg>
);

function docSubline(d: DocRow, t: (k: string, o?: Record<string, unknown>) => string): string {
  const hm = (ts?: number | null) =>
    ts ? new Date(ts * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }) : '';
  switch (d.status) {
    case 'comment_only':
      return t('docs.sub.commentOnly');
    case 'frozen':
      return t('docs.sub.frozen');
    case 'backoff':
      return d.retry_at ? t('docs.sub.backoffAt', { time: hm(d.retry_at) }) : t('docs.sub.backoff');
    default:
      // Nothing. A healthy row's check time is its own column and its platform is
      // the connection column; printing either here said the same thing twice on
      // every row. The subline now appears only when there is something to say.
      return '';
  }
}

function StatusPill({ status }: { status: DocRow['status'] }) {
  const { t } = useTranslation();
  const styles: Record<DocRow['status'], string> = {
    ok: 'text-ok bg-ok-subtle',
    comment_only: 'text-warn bg-warn-subtle',
    frozen: 'text-text-muted bg-bg-muted',
    backoff: 'text-text-muted bg-bg-muted',
  };
  return (
    <span
      className={`flex-none whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs ${styles[status]}`}
      title={
        status === 'comment_only'
          ? t('docs.status.commentOnlyHint')
          : status === 'frozen' || status === 'backoff'
            ? t('docs.status.frozenHint')
            : undefined
      }
    >
      {t(`docs.status.${status}`)}
    </span>
  );
}

function EmptyIllustration() {
  return (
    <svg viewBox="0 0 88 66" fill="none" stroke="currentColor" strokeWidth={1.6} className="mx-auto h-16 w-20 text-text-muted/50">
      <rect x="18" y="10" width="42" height="52" rx="4" />
      <path d="M26 22h26M26 30h26M26 38h16" />
      <circle cx="64" cy="46" r="13" fill="var(--color-surface-elevated)" />
      <path d="M64 40v12m-6-6h12" stroke="var(--color-action-primary)" strokeWidth={2} />
    </svg>
  );
}



export function DocsPanel({ isConnected }: { isConnected: boolean }) {
  const { t } = useTranslation();
  const [conf, setConf] = useState<ConfPayload | null>(null);
  const [docs, setDocs] = useState<DocRow[]>([]);
  const [selectedConn, setSelectedConn] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  // Shared with this account but **comment-only**: adoption skips them because
  // admission would refuse them anyway, so they are shown with the fix instead.
  const [needsEditor, setNeedsEditor] = useState<{ doc_id: string; title: string; url: string }[]>([]);
  // Shared with this account but a type co-editing cannot take (spreadsheets,
  // presentations, uploaded Office files). Listed so "unsupported" and "the share
  // failed" stop looking identical.
  const [unsupported, setUnsupported] = useState<{ title: string; kind: string }[]>([]);
  const [adopted, setAdopted] = useState(0);
  // Documents the platform no longer offers, retired by the last sweep, and connections
  // that cannot enumerate at all. Both are things the panel used to know and not say.
  const [retired, setRetired] = useState<{ doc_id: string; title: string }[]>([]);
  const [noDiscovery, setNoDiscovery] = useState<{ connection_id: string; address: string; discovery_reason?: string }[]>([]);
  const [copied, setCopied] = useState(false);
  // The document detail view: one dialog over both halves of the record. The
  // registry says who authorised, the ledger says what was written, and they
  // used to be two menu entries -- so answering "who authorised *this* write"
  // meant closing one dialog and opening the other with the timestamp in your
  // head. One timeline puts the answer on the line above.
  const [detailFor, setDetailFor] = useState<DocRow | null>(null);
  const [receipts, setReceipts] = useState<ReceiptRow[]>([]);
  const [usage, setUsage] = useState<Record<string, unknown> | null>(null);
  const [detailBusy, setDetailBusy] = useState(false);
  const [docModelBusy, setDocModelBusy] = useState(false);
  const [detailNote, setDetailNote] = useState('');

  // Escape closes the detail dialog. The backdrop click already did, but a modal
  // that can only be dismissed by aiming at the region outside it is one a
  // keyboard user cannot leave. Bound on the document rather than on the dialog
  // div so it works without the dialog holding focus.
  useEffect(() => {
    if (!detailFor) return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setDetailFor(null);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [detailFor]);
  const [actingOn, setActingOn] = useState<string | null>(null);
  const [refreshedAt, setRefreshedAt] = useState<string>('');
  const [removing, setRemoving] = useState(false);
  // Keys kept on disk by remove_connection. Listed so finding one back is a click,
  // not an ls in a dotfile directory.
  const [savedKeys, setSavedKeys] = useState<SavedKey[]>([]);
  const [keyDeleteArm, setKeyDeleteArm] = useState<string | null>(null);
  // One field for both ways of naming a key: a path on this machine, or the JSON
  // pasted straight in. A mode switch in front of them would have been a two-item
  // menu the reader has to open to find out there is nothing to choose -- and the
  // column is one level, so the choice is made by what was typed, not by a tab.
  const [connInput, setConnInput] = useState('');
  const [connJson, setConnJson] = useState<{ name: string; body: string } | null>(null);
  const [connBusy, setConnBusy] = useState(false);
  const [connError, setConnError] = useState<string | null>(null);
  const [connNote, setConnNote] = useState('');
  const [copiedConn, setCopiedConn] = useState<string | null>(null);
  const [removeConnTarget, setRemoveConnTarget] = useState<Connection | null>(null);
  // The deployment-wide default, shown in this column because it is a
  // deployment-level fact like the connections themselves. A document may pin its
  // own; this is what the rest of them run on.
  const [modelBusy, setModelBusy] = useState(false);
  // PR2b: the standing-mandate registry -- per-doc watch level and suspension.
  // `revoked` is the registry's tombstone (E1): the entry stays after 只读 or
  // 移出纳管 so the adoption policy cannot re-issue silently. Dropped here, a revoked
  // row kept its 操作权 label on the next load (measured 2026-09-03). This is also
  // where a ledger left at the retired 建议权 tier lands: the registry marks it
  // revoked on load, so the row reads 关 rather than a level that no longer exists.
  type WatchRow = { mode: string; expires_at?: number | null; expired?: boolean; revoked?: boolean };  // revoked rows are skipped, never stored
  const [watches, setWatches] = useState<Record<string, WatchRow>>({});
  // Two-click confirmations: turning a watch on is the heavy act (a policy
  // signature), and so is turning a selection off -- both follow the
  // keyDeleteArm pattern.
  const [armApply, setArmApply] = useState<string | null>(null);
  const [armBulk, setArmBulk] = useState<'on' | 'off' | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkNote, setBulkNote] = useState('');
  // The selection. Deliberately **not** persisted across a reload: the list
  // itself moves under a reload (adoption, a filter, a status change), and a
  // selection that survived it would act on rows the person never saw.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // D21: the deployment's Direct/Mandate switch. Downgrading is explicit
  // (two-click, same pattern as the kill switch) and the direct state keeps a
  // standing banner -- nobody runs bare without knowing.
  const [armDirect, setArmDirect] = useState(false);
  // The one-table view: client-side filters and the adoption target.
  const [filterConn, setFilterConn] = useState('');
  // Default: **everything managed**. Opening on the watched few reads well until you
  // remember that a document arrives with its watch off: adoption and the default
  // filter are then in direct contradiction, and the panel says "2 newly shared
  // documents adopted" over an empty table. Measured on a live tenant -- two
  // documents shared, both adopted, neither visible, and pasting the link answered
  // "already managed", which is the worst version of the confusion because it tells
  // the owner the panel is lying. Seeing what you have is the list's first job;
  // narrowing to the watched few is one dropdown away and carries its own count.
  const [filterTier, setFilterTier] = useState('');
  const [filterKind, setFilterKind] = useState('');
  const setMode = useCallback(async (mode: 'mandate' | 'direct') => {
    await webRequest('clouddoc.set_mode', { mode });
    setArmDirect(false);
    await reload();
  }, []);
  // A chat reference chip may have asked for a document before this panel
  // mounted; the latch holds the id either way, and the effect below waits for
  // the row to exist before opening its history.

  const reload = useCallback(async () => {
    try {
      const [c, l] = await Promise.all([
        webRequest<ConfPayload>('clouddoc.get_conf'),
        webRequest<{ enabled: boolean; docs: DocRow[] }>('clouddoc.list_docs'),
      ]);
      setConf(c);
      setDocs(l.docs ?? []);
      // 所见即所选: a reload is a new list, so it is a new selection.
      setSelected(new Set());
      setSelectedConn((prev) => prev ?? c.connections?.[0]?.id ?? null);
      setRefreshedAt(new Date().toLocaleTimeString('en-GB'));
      try {
        const w = await webRequest<{
          watches: { doc_id: string; mode: string; expires_at?: number | null; expired?: boolean; revoked?: boolean }[];
        }>('clouddoc.watch_list');
        const map: Record<string, WatchRow> = {};
        for (const it of w.watches ?? []) {
          // A revoked entry is no mandate at all: the row reads 关 like an
          // unregistered one. Expiry is judged here as well as by the server, so
          // the row does not read "剩 0 天" between the deadline and the next poll.
          if (it.revoked) continue;
          const lapsed = !!it.expired || (it.expires_at != null && it.expires_at * 1000 <= Date.now());
          map[it.doc_id] = { mode: it.mode, expires_at: it.expires_at ?? null, expired: lapsed };
        }
        setWatches(map);
      } catch {
        /* the watch registry answers only when the gateway ships PR2b */
      }
    } catch {
      setConf({ enabled: false });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isConnected) void reload();
  }, [isConnected, reload]);

  const conns = conf?.connections ?? [];
  // Unique within this set, so the connection column can tell two service
  // accounts in different projects apart without printing both addresses.
  const connShort = useMemo(() => connShortNames(conns), [conns]);
  const conn = conns.find((c) => c.id === selectedConn) ?? conns[0] ?? null;
  const connDocs = docs.filter((d) => !d.connection_id || !conn || d.connection_id === conn.id);

  // ── One list, not two ──
  // A connection *is* a key. The column used to enumerate the same account twice --
  // once as an identity under "Connected", once as a file under "Or use a saved key"
  // -- and left the reader to match the two by eye. These rows are the union, joined
  // on the connection id the key listing now reports.
  //
  // Both halves exist alone, so the union is the only shape that loses nothing:
  //   * a key with no connection -- remove_connection keeps the file on purpose
  //     (deleting credentials is an operations decision, not a click's side effect);
  //   * a connection with no stored key -- add_connection accepts a path anywhere on
  //     the machine, and list_keys only ever walks clouddoc-keys/.
  // The first reads as "Not connected" and offers Connect + Delete file; the second
  // reads as "In use" with its key marked "External path" and no delete, because
  // delete_key cannot reach it.
  // Where a credential is revoked is a property of the platform that issued it, not
  // of this panel: naming Google's console under a Feishu app_id told the reader to
  // go somewhere that cannot revoke it. The neutral wording is the fallback for a
  // key whose platform did not resolve, and for a list holding both.
  const keyDeleteHintFor = (provider?: string) =>
    provider === 'google'
      ? t('docs.keyDeleteHintGoogle')
      : provider === 'feishu'
        ? t('docs.keyDeleteHintFeishu')
        : t('docs.keyDeleteHint');

  const connKeyRows = useMemo(() => {
    const byConn = new Map<string, SavedKey>();
    for (const k of savedKeys) if (k.connection_id) byConn.set(k.connection_id, k);
    const rows: { id: string; c: Connection | null; k: SavedKey | null }[] = conns.map((c) => ({
      id: c.id,
      c,
      k: byConn.get(c.id) ?? null,
    }));
    for (const k of savedKeys) if (!k.connection_id) rows.push({ id: `key:${k.filename}`, c: null, k });
    return rows;
  }, [conns, savedKeys]);

  // The rows actually on screen. Computed once and used by both the table and the
  // header checkbox, because 所见即所选 only holds if the two read the same list:
  // a select-all that walked ``docs`` would silently pick up rows a filter hides.
  const visibleDocs = useMemo(
    () =>
      docs
        .filter((d) => !filterConn || d.connection_id === filterConn)
        .filter((d) => !filterKind || (d.kind || 'document') === filterKind)
        .filter((d) => {
          if (!filterTier) return true;
          const w = watches[d.doc_id];
          return filterTier === 'off' ? !w : w?.mode === filterTier;
        })
        .sort((a, b) => (b.checked_at ?? 0) - (a.checked_at ?? 0)),
    [docs, filterConn, filterKind, filterTier, watches],
  );
  // Off is exactly "no live watch" -- there is no third state and no flag of its
  // own; the dropdown filters on that one fact.
  //
  // All three options carry their count. One number on one option read as a
  // label rather than a quantity; three of them read as a distribution, and
  // they sum to the total under the current connection, so the reader can
  // check them against each other. Counts follow the connection filter,
  // because that is the set the dropdown is about to choose from.
  const tierCounts = useMemo(() => {
    const inScope = docs.filter((d) => !filterConn || d.connection_id === filterConn);
    const on = inScope.filter((d) => watches[d.doc_id]).length;
    return { on, off: inScope.length - on, all: inScope.length };
  }, [docs, filterConn, watches]);
  const allVisibleSelected = visibleDocs.length > 0 && visibleDocs.every((d) => selected.has(d.doc_id));
  const toggleAllVisible = () =>
    setSelected(allVisibleSelected ? new Set() : new Set(visibleDocs.map((d) => d.doc_id)));
  const toggleOne = (docId: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(docId)) next.delete(docId);
      else next.add(docId);
      return next;
    });

  // One axis, two values -- and now the mechanism has no third one either. The
  // ``reply_only`` tier is retired: replying needs no mandate (an unauthorised @
  // still gets an answer), so it delegated "run an unattended turn", not
  // "reply", and the strictest fallback is not dispatching at all.
  const setWatchLevel = useCallback(
    async (docId: string, mode: 'off' | 'apply_scoped') => {
      try {
        if (mode === 'off') await webRequest('clouddoc.watch_revoke', { doc_id: docId });
        else await webRequest('clouddoc.watch_set', { doc_id: docId, mode });
      } finally {
        setArmApply(null);
        await reload();
      }
    },
    [reload],
  );

  // The detail view (E1 audit + the receipt feed): granted, used, and every
  // event of either kind in one order. Both RPCs are asked at once and merged
  // on the client -- neither side needed changing, because both already record
  // the same epoch-seconds clock; the join was only ever missing from the UI.
  const openDetail = useCallback(async (doc: DocRow) => {
    setDetailFor(doc);
    setUsage(null);
    setReceipts([]);
    setDetailNote('');
    setDetailBusy(true);
    const [u, r] = await Promise.allSettled([
      webRequest<Record<string, unknown>>('clouddoc.watch_usage', { doc_id: doc.doc_id }),
      webRequest<{ receipts?: ReceiptRow[] }>('clouddoc.receipts', { doc_id: doc.doc_id, limit: 50 }),
    ]);
    // One half failing must not blank the other: a grant whose ledger is
    // unreachable is still a grant, and saying so beats an empty dialog.
    setUsage(u.status === 'fulfilled' ? (u.value ?? null) : null);
    setReceipts(r.status === 'fulfilled' ? (r.value?.receipts ?? []) : []);
    if (r.status === 'rejected') setDetailNote(String(r.reason));
    setDetailBusy(false);
  }, []);
  const renew = useCallback(async (doc: DocRow, permanent: boolean) => {
    const w = watches[doc.doc_id];
    if (!w) return;
    await webRequest('clouddoc.watch_set', {
      doc_id: doc.doc_id, mode: w.mode, ...(permanent ? { permanent: true } : {}),
    });
    setDetailFor(null);
    await reload();
  }, [watches, reload]);


  // The per-document model pin. It lives in the record dialog rather than in a table
  // column: the table was just slimmed, and a picker on every row would put it back.
  // Empty clears the pin and the document goes back to the deployment default.
  const setDocModel = useCallback(async (docId: string, name: string) => {
    // Its own busy flag, not the dialog's: ``detailBusy`` blanks the grant summary
    // while the timeline loads, and a model change must not make the summary the
    // picker sits in disappear under it.
    setDocModelBusy(true);
    setDetailNote('');
    try {
      const out = await webRequest<{ ok?: boolean; detail?: string; model_name?: string }>(
        'clouddoc.set_doc_model', { doc_id: docId, model_name: name },
      );
      if (out?.ok === false) {
        setDetailNote(out.detail || '');
        return;
      }
      const stored = out?.model_name ?? name;
      setDocs((prev) => prev.map((d) => (d.doc_id === docId ? { ...d, model_name: stored } : d)));
      setDetailFor((prev) => (prev && prev.doc_id === docId ? { ...prev, model_name: stored } : prev));
    } catch (e) {
      setDetailNote(String(e));
    } finally {
      setDocModelBusy(false);
    }
  }, []);

  // Revert and un-highlight both answer with ok plus a detail, and both change what
  // the feed should show, so the outcome is surfaced and the feed re-read rather than
  // patched in place -- the store is the truth about a receipt's status.
  const actOnReceipt = useCallback(
    async (method: 'clouddoc.unhighlight', receiptId: string) => {
      if (!detailFor) return;
      setActingOn(receiptId);
      setDetailNote('');
      try {
        // A revert on Feishu walks several CLI round-trips (read, write, read-back
        // verify, thread notify); the 15s default timed out while the backend
        // finished, and the user saw an error over a revert that had landed.
        const out = await webRequest<{ ok?: boolean; detail?: string }>(
          method,
          { receipt_id: receiptId },
          { timeoutMs: 60000 },
        );
        if (out && out.ok === false) setDetailNote(out.detail || t('docs.history.failed'));
      } catch (e) {
        // The request failing does not mean the action failed -- a timeout can race a
        // completed revert. The refresh below shows the receipt's true status either
        // way; the note says so instead of presenting a raw error as the outcome.
        setDetailNote(t('docs.history.actionUnconfirmed'));
        console.warn('[docs] receipt action error', e);
      } finally {
        try {
          const fresh = await webRequest<{ receipts?: ReceiptRow[] }>('clouddoc.receipts', {
            doc_id: detailFor.doc_id,
            limit: 50,
          });
          setReceipts(fresh?.receipts ?? []);
        } catch { /* the feed keeps its last state */ }
        setActingOn(null);
      }
    },
    [detailFor, t],
  );

  // The journal's rows; the shared timeline merges them with the ledger's.
  const lineage = useMemo(() => (usage?.lineage ?? []) as LineageRow[], [usage]);

  // Bulk on/off acts on the **selection**, so what is changing is on screen while
  // it is decided. The deployment-wide 全部暂停 / 撤销全部授权 buttons that used to
  // live here changed rows nobody was looking at, and the pause they offered was
  // a third state with no way back that anyone could name.
  //
  // Hiding **is** turning the watch off, sent as the same per-document revocations.
  // The word changed, not the mechanism: what a person means by "get this out of
  // my list" is "stop watching it", and the panel now says so in one place.
  const bulkWatch = useCallback(
    async (turnOn: boolean) => {
      const ids = Array.from(selected);
      if (!ids.length || bulkBusy) return;
      if (armBulk !== (turnOn ? 'on' : 'off')) {
        setArmBulk(turnOn ? 'on' : 'off');
        setTimeout(() => setArmBulk(null), 5000);
        return;
      }
      setArmBulk(null);
      setBulkBusy(true);
      setBulkNote('');
      try {
        if (turnOn) {
          const out = await webRequest<{ issued?: string[]; skipped?: { reason: string }[] }>(
            'clouddoc.watch_set_many',
            { doc_ids: ids, mode: 'apply_scoped' },
            { timeoutMs: 60000 },
          );
          const issued = out?.issued?.length ?? 0;
          const already = (out?.skipped ?? []).filter((x) => x.reason === 'already_on').length;
          const failed = (out?.skipped ?? []).length - already;
          setBulkNote(
            (issued === 0 && already > 0
              ? t('docs.watch.bulkEnabledAllSkipped', { skipped: already })
              : already > 0
                ? t('docs.watch.bulkEnabledWithSkips', { count: issued, skipped: already })
                : t('docs.watch.bulkEnabled', { count: issued })) +
              (failed > 0 ? ` ${t('docs.watch.bulkFailed', { count: failed })}` : ''),
          );
        } else {
          const out = await webRequest<{ revoked?: string[] }>(
            'clouddoc.watch_revoke_many',
            { doc_ids: ids },
            { timeoutMs: 60000 },
          );
          setBulkNote(t('docs.watch.bulkDisabled', { count: out?.revoked?.length ?? 0 }));
        }
      } catch (e) {
        setBulkNote(String(e));
      } finally {
        setBulkBusy(false);
        await reload();
      }
    },
    [selected, armBulk, bulkBusy, reload, t],
  );

  // Sharing a document with the account is what puts it under management -- no second
  // confirmation here, because sharing is already a deliberate act performed in Google's
  // own interface. Returns how many were adopted so the caller knows to reload the list.
  // Every connection, not the selected one. Scoping this to ``conn`` -- which falls back
  // to the first -- meant a deployment with a Google account and a Feishu app discovered
  // for one of them and silently never for the other: the second account's shares simply
  // never arrived, with nothing on screen to say why. Refresh answers "what do I have",
  // which is not a per-account question.
  const syncShared = useCallback(async (): Promise<number> => {
    try {
      const out = await webRequest<{
        adopted: { doc_id: string; title: string }[];
        needs_editor: { doc_id: string; title: string; url: string }[];
        unsupported: { title: string; kind: string }[];
        retired: { doc_id: string; title: string }[];
        per_connection: { connection_id: string; address: string; discovery_available: boolean; discovery_reason?: string }[];
      }>('clouddoc.sync_all_shared_docs', {});
      setNeedsEditor(out?.needs_editor ?? []);
      setUnsupported(out?.unsupported ?? []);
      // "N newly adopted" reports a moment, not a state. It used to sit on screen until
      // the next sweep -- which on a panel nobody refreshes is never -- so a count from
      // ten minutes ago read as something that had just happened. It clears itself.
      const n = out?.adopted?.length ?? 0;
      setAdopted(n);
      if (n > 0) window.setTimeout(() => setAdopted(0), 8000);
      setRetired(out?.retired ?? []);
      setNoDiscovery((out?.per_connection ?? []).filter((c) => !c.discovery_available));
      return (out?.adopted?.length ?? 0) + (out?.retired?.length ?? 0);
    } catch {
      setNeedsEditor([]);     // a convenience; failing it must not break the panel
      setUnsupported([]);
      return 0;
    }
  }, []);

  // Opening the panel adopts whatever has been shared since it was last open, so the
  // documents are simply there. list_docs stays API-free; this one call is the deliberate
  // exception, and it is what makes sharing the only step a user performs.
  useEffect(() => {
    if (!isConnected || conns.length === 0) return;
    void syncShared().then((n) => { if (n > 0) void reload(); });
  }, [isConnected, conns.length, syncShared, reload]);

  // The address to share a document with. A verdict from the adoption field names
  // the connection it is about, and that address wins over the selected one: with
  // Google selected and a Feishu link pasted, the probe ran through the Feishu
  // connection, and the Google service-account address would be the wrong one to
  // copy.
  const copyAddress = (address?: string) => {
    const value = address || conn?.agent_address;
    if (!value) return;
    void navigator.clipboard.writeText(value).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    });
  };

  const adoptByLink = useCallback(async (url: string): Promise<AddResult> => {
    // No connection_id: the link says which provider, and only the identity the
    // document was shared with can see it, so the gateway asks each in turn and
    // returns the most informative verdict. A document that was adopted is picked
    // up by the same refresh discovery uses, so the row appears at once.
    const out = await webRequest<AddResult>('clouddoc.add_doc', { url });
    if (out?.result === 'ok') {
      await syncShared();
      await reload();
    }
    return out;
  }, [syncShared, reload]);

  // Refresh is repair plus adoption: re-check every watched document, then pull in
  // whatever has been shared since. Repair runs first so the list it walks is the one
  // the user was looking at; newly adopted documents were just probed by the listing.
  const handleRefresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      for (const d of connDocs) {
        await webRequest('clouddoc.update_doc', { doc_id: d.doc_id }).catch(() => {});
      }
      await syncShared();
      await reload();
    } finally {
      setRefreshing(false);
    }
  };

  // (row titles link out directly in the table view)

  const resetConnForm = () => {
    setConnInput('');
    setConnJson(null);
    setConnError(null);
  };

  const loadSavedKeys = useCallback(async () => {
    try {
      const out = await webRequest<{ keys: SavedKey[] }>('clouddoc.list_keys');
      setSavedKeys(out?.keys ?? []);
    } catch {
      setSavedKeys([]);
    }
  }, []);

  // The saved-key list is part of the left column now, not of a dialog that opened
  // it, so it loads with the panel.
  useEffect(() => {
    if (isConnected) void loadSavedKeys();
  }, [isConnected, loadSavedKeys]);

  const deleteSavedKey = useCallback(async (filename: string) => {
    try {
      await webRequest('clouddoc.delete_key', { filename });
    } finally {
      setKeyDeleteArm(null);
      void loadSavedKeys();
    }
  }, [loadSavedKeys]);

  const submitConnection = async (params: Record<string, unknown>) => {
    if (connBusy) return;
    setConnBusy(true);
    setConnError(null);
    setConnNote('');
    try {
      const out = await webRequest<{ result: string; detail?: string; connection?: Connection }>(
        'clouddoc.add_connection',
        params,
      );
      if (out.result === 'ok') {
        resetConnForm();
        setConnNote(t('docs.conn.added'));
        await reload();
        void loadSavedKeys();
        if (out.connection) setSelectedConn(out.connection.id);
      } else {
        // A payload without a result at all -- the shape a disabled backend returns --
        // would otherwise render the missing key's own name into the form.
        setConnError(
          t(`docs.connErr.${out.result ?? 'unknown'}`, {
            detail: out.detail ?? '',
            defaultValue: t('docs.connErr.unknown', { detail: out.detail ?? '' }),
          }),
        );
      }
    } catch (e) {
      setConnError(String(e));
    } finally {
      setConnBusy(false);
    }
  };

  // What was typed decides how it is sent: a pasted key starts with a brace, a path
  // does not, and a chosen file carries its own name so the key lands on disk under it.
  const handleAddConnection = async () => {
    const raw = connInput.trim();
    if (connJson) {
      await submitConnection({
        credentials_json: connJson.body,
        filename: connJson.name.replace(/\.json$/i, ''),
      });
      return;
    }
    if (!raw) return;
    await submitConnection(raw.startsWith('{') ? { credentials_json: raw } : { credentials_path: raw });
  };

  const copyConnAddress = (id: string, address?: string) => {
    if (!address) return;
    void navigator.clipboard?.writeText(address).then(() => {
      setCopiedConn(id);
      setTimeout(() => setCopiedConn(null), 1200);
    });
  };

  // Deployment-wide, like the mode: the model every document without a pin of its
  // own runs on. Empty restores the agentserver's default. Validation is server-side.
  const setPollInterval = async (seconds: number) => {
    setModelBusy(true);
    setConnNote('');
    try {
      const out = await webRequest<{
        ok?: boolean; detail?: string; poll_interval_seconds?: number; clamped?: boolean;
        min?: number; max?: number;
      }>('clouddoc.set_poll_interval', { seconds });
      if (out?.ok === false) {
        setConnError(out.detail || '');
        return;
      }
      const settled = out?.poll_interval_seconds ?? seconds;
      // Say so when the deployment did not take the number as typed. Silently
      // storing something else and showing it back is how a control loses trust.
      if (out?.clamped) {
        setConnNote(t('docs.poll.clamped', { n: settled, min: out?.min, max: out?.max }));
      }
      setConf((prev) => (prev ? { ...prev, poll_interval_seconds: settled } : prev));
    } catch (e) {
      setConnError(String(e));
    } finally {
      setModelBusy(false);
    }
  };

  const setDeploymentModel = async (name: string) => {
    setModelBusy(true);
    setConnNote('');
    try {
      const out = await webRequest<{ ok?: boolean; detail?: string; model_name?: string }>(
        'clouddoc.set_model', { model_name: name },
      );
      if (out?.ok === false) {
        setConnError(out.detail || '');
        return;
      }
      setConf((prev) => (prev ? { ...prev, model_name: out?.model_name ?? name } : prev));
    } catch (e) {
      setConnError(String(e));
    } finally {
      setModelBusy(false);
    }
  };

  const handleRemoveConnection = async () => {
    if (!removeConnTarget) return;
    setRemoving(true);
    try {
      await webRequest('clouddoc.remove_connection', { connection_id: removeConnTarget.id }).catch(() => {});
      setSelectedConn(null);
      await reload();
      // The key stays on disk; the saved list is how it is found again.
      void loadSavedKeys();
    } finally {
      setRemoving(false);
      setRemoveConnTarget(null);
    }
  };

  // The grant summary above the timeline: what the registry granted, until
  // when, and what has been used under it. It stays with the panel rather than
  // moving into the shared component because it reads ``watch_usage``, which
  // only the panel fetches, and the rail says the same thing on its foot row.
  const detailGrant = (doc: DocRow): ReactNode => {
    // Nothing is asserted about the grant until the registry has answered: a
    // summary rendered over an empty payload reads "关", which is a claim, not
    // a placeholder.
    if (detailBusy) return null;
    if (!usage) return <p className="text-xs text-text-muted">{t('docs.usage.unavailable')}</p>;
    const g = (usage?.granted ?? null) as Record<string, unknown> | null;
    const u = (usage?.used ?? {}) as Record<string, unknown>;
    const hints = (usage?.hints ?? []) as string[];
    const denials = (u.denials ?? {}) as Record<string, number>;
    const deniedTotal = Object.values(denials).reduce((a, b) => a + b, 0);
    const regions = (u.regions_envelope ?? []) as string[];
    const executors = (u.executors ?? []) as string[];
    const revoked = !g || !!g.revoked;
    const expired = !!g && !!g.expired;
    const expiresAt = g && typeof g.expires_at === 'number' ? g.expires_at : null;
    // The registry's own live watch is what the renewal buttons act
    // on: a tombstone has no term to extend.
    const live = !!watches[doc.doc_id];
    const daysLeft =
      expiresAt !== null && !revoked && !expired
        ? Math.max(0, Math.ceil((expiresAt * 1000 - Date.now()) / 86400000))
        : null;
    const term = revoked
      ? t('docs.watch.watchOff')
      : `${expired ? t('docs.watch.watchExpired') : t('docs.watch.watchApply')}` +
        ` · ${t('docs.usage.since')} ${fmtTs(g?.issued_at)}` +
        ` · ${
          expiresAt === null
            ? t('docs.usage.permanent')
            : `${t('docs.usage.until')} ${fmtTs(expiresAt)}`
        }` +
        (daysLeft !== null ? ` · ${t('docs.table.daysLeft', { count: daysLeft })}` : '');
    return (
      <>
        <div className="rounded-md bg-bg-hover px-3 py-2">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="font-medium">{t('docs.usage.granted')}</p>
              <p className="mt-1 text-text-muted" data-testid="docs-panel-detail-watch">{term}</p>
            </div>
            <span className="flex flex-none gap-2">
              <button
                data-testid="docs-panel-usage-renew"
                disabled={!live}
                onClick={() => void renew(doc, false)}
                className="rounded-md border border-border px-3 py-1.5 hover:bg-bg-hover disabled:opacity-40"
              >
                {t('docs.usage.renew30')}
              </button>
              <button
                data-testid="docs-panel-usage-permanent"
                disabled={!live}
                onClick={() => void renew(doc, true)}
                className="rounded-md border border-border px-3 py-1.5 text-text-muted hover:bg-bg-hover disabled:opacity-40"
              >
                {t('docs.usage.makePermanent')}
              </button>
            </span>
          </div>
          <p className="mt-2 font-medium">{t('docs.usage.used')}</p>
          <p className="mt-0.5 text-text-muted">
            {t('docs.usage.summaryLine', {
              dispatches: Number(u.dispatches ?? 0),
              denied: deniedTotal,
              writes: Number(u.write_batches ?? 0),
            })}
          </p>
          <p className="mt-0.5 text-text-muted">
            {t('docs.usage.lastWrite')} {fmtTs(u.last_write_at)}
            {executors.length > 0 ? ` · ${t('docs.usage.executors')} ${executors.join(', ')}` : ''}
          </p>
          {regions.length > 0 && (
            <p className="mt-0.5 break-all text-text-muted">
              {t('docs.usage.regions')} {regions.slice(0, 8).join(', ')}{regions.length > 8 ? '…' : ''}
            </p>
          )}
        </div>
        {hints.length > 0 && (
          <div className="rounded-md bg-warn-subtle px-3 py-2 text-warn">
            {hints.includes('idle_wide_grant') && <p>{t('docs.usage.hintIdle')}</p>}
            {hints.includes('frequent_denials') && <p>{t('docs.usage.hintFriction')}</p>}
          </div>
        )}
      </>
    );
  };

  // The grant summary and the per-document model, in that order. The model block
  // sits **outside** ``detailGrant`` on purpose: the grant half is empty while the
  // registry is being read and gone entirely when it cannot answer, and a document
  // whose registry is unreachable still has a model to set.
  const detailSummary = (doc: DocRow): ReactNode => (
    <div className="space-y-2 text-xs" data-testid="docs-panel-detail-summary">
      {detailGrant(doc)}
      {/* The per-document model, beside the term and the renewal buttons -- the
          other two facts about how this one document is run. A document with no
          pin of its own says which model it will actually use and where that
          comes from, because "empty" on its own reads as "no model". */}
      <div className="rounded-md bg-bg-hover px-3 py-2" data-testid="docs-doc-model">
        <p className="font-medium">{t('docs.docModel.title')}</p>
        <div className="mt-1.5 flex flex-wrap items-center gap-3">
          <ModelPicker
            testIdPrefix="docs-doc-model-picker"
            value={doc.model_name || null}
            onChange={(name) => void setDocModel(doc.doc_id, name)}
            disabled={docModelBusy}
          />
          {doc.model_name ? (
            <button
              data-testid="docs-doc-model-reset"
              className="text-xs underline"
              disabled={docModelBusy}
              onClick={() => void setDocModel(doc.doc_id, '')}
            >
              {t('docs.docModel.useDefault')}
            </button>
          ) : (
            <span className="text-text-muted" data-testid="docs-doc-model-inherit">
              {conf?.model_name
                ? t('docs.docModel.inherit', { model: conf.model_name })
                : t('docs.docModel.inheritAgentDefault')}
            </span>
          )}
        </div>
      </div>
    </div>
  );

  if (loading) {
    return (
      <div className="flex h-full w-full flex-col px-6 py-4">
        <div className="h-7 w-24 animate-pulse rounded-md bg-bg-muted" />
        <div className="mt-2 h-4 w-64 animate-pulse rounded-md bg-bg-muted" />
        <div className="mt-4 grid flex-1 gap-4" style={{ gridTemplateColumns: 'minmax(340px, 430px) 1fr' }}>
          <div className="animate-pulse rounded-xl border border-border bg-bg-muted/60" />
          <div className="animate-pulse rounded-xl border border-border bg-bg-muted/60" />
        </div>
      </div>
    );
  }

  const hasConn = conns.length > 0;

  return (
    <div className="flex h-full w-full flex-col px-6 py-4">
      <h1 className="text-[22px] font-semibold">{t('docs.title')}</h1>
      <p className="mt-0.5 text-[13px] text-text-muted">{t('docs.subtitle')}</p>

      {conf?.ask_channel === false && (
        <div
          data-testid="docs-ask-channel-banner"
          className="mt-3 rounded-lg border border-warn bg-warn-subtle px-4 py-2.5 text-[13px] text-warn"
        >
          {t('docs.mode.noAskChannel')}
        </div>
      )}
      {conf?.mode === 'direct' ? (
        <div
          data-testid="docs-mode-banner"
          className="mt-3 flex items-center justify-between gap-3 rounded-lg border border-warn bg-warn-subtle px-4 py-2.5 text-[13px] text-warn"
        >
          <span>{t('docs.mode.directBanner')}</span>
          <button
            onClick={() => void setMode('mandate')}
            className="flex-none rounded-md border border-warn px-3 py-1 font-medium hover:bg-bg-hover"
          >
            {t('docs.mode.restoreMandate')}
          </button>
        </div>
      ) : conf?.mode === 'mandate' ? (
        <div className="mt-2 flex items-center gap-2 text-[12px] text-text-muted">
          <span>{t('docs.mode.mandateNote')}</span>
          <button
            data-testid="docs-mode-downgrade"
            onClick={() => {
              if (!armDirect) {
                setArmDirect(true);
                setTimeout(() => setArmDirect(false), 5000);
                return;
              }
              void setMode('direct');
            }}
            className={`rounded-md border px-2 py-0.5 ${armDirect ? 'border-warn text-warn' : 'border-border hover:bg-bg-hover'}`}
          >
            {armDirect ? t('docs.mode.confirmDirect') : t('docs.mode.goDirect')}
          </button>
        </div>
      ) : null}

      {/* Two columns, Channel Management's proportions: deployment-level facts on the
          left at minmax(340,430), the documents themselves filling the rest. Both are
          plain content -- the left column is not a nav rail and nothing here is behind
          a tab or a disclosure. */}
      <div className="mt-3.5 grid min-h-0 flex-1 gap-4" style={{ gridTemplateColumns: 'minmax(340px, 430px) 1fr' }}>
        <aside
          data-testid="docs-conn-panel"
          className="flex min-h-0 flex-col overflow-hidden rounded-xl border border-border bg-card"
        >
          <div className="flex-none border-b border-border px-4 py-2.5 text-[13px] font-medium">
            {t('docs.conn.title')}
          </div>
          <div className="min-h-0 flex-1 space-y-4 overflow-auto px-4 py-3 text-[13px]">
            {/* ── Adding one, first ──
                The list below is empty exactly when someone needs this control, so
                putting it under the list hid it from every new deployment. One field
                takes both ways of naming a key: a path on this machine, or the JSON
                pasted straight in. ── */}
            <div>
              <div className="font-medium">{t('docs.conn.addTitle')}</div>
              <p className="mt-0.5 text-[11.5px] leading-relaxed text-text-muted">{t('docs.conn.addHint')}</p>
              <textarea
                data-testid="docs-conn-key-input"
                className="mt-2 h-20 w-full rounded-md border border-border bg-transparent p-2 font-mono text-[11px] placeholder:font-sans placeholder:text-text-muted focus:border-accent focus:outline-none"
                placeholder={t('docs.conn.keyPlaceholder')}
                value={connInput}
                disabled={connBusy}
                onChange={(e) => {
                  setConnInput(e.target.value);
                  setConnError(null);
                }}
              />
              <div className="mt-2 flex items-center gap-2">
                <input
                  id="clouddoc-key-file"
                  type="file"
                  accept=".json,application/json"
                  className="hidden"
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (!f) return;
                    void f.text().then((body) => {
                      setConnJson({ name: f.name, body });
                      setConnError(null);
                    });
                  }}
                />
                <label
                  htmlFor="clouddoc-key-file"
                  className="max-w-[55%] cursor-pointer truncate rounded-md border border-border px-2.5 py-1 text-xs hover:border-accent"
                  title={connJson ? connJson.name : undefined}
                >
                  {connJson ? connJson.name : t('docs.connChooseFile')}
                </label>
                <span className="flex-1" />
                <button
                  data-testid="docs-conn-add"
                  className="flex-none rounded-md border border-border px-3 py-1 text-xs hover:bg-bg-hover disabled:opacity-40"
                  disabled={connBusy || (!connInput.trim() && !connJson)}
                  onClick={() => void handleAddConnection()}
                >
                  {connBusy ? t('docs.adding') : t('docs.conn.add')}
                </button>
              </div>
              <p className="mt-1 text-[11px] text-text-muted">{t('docs.connUploadHint')}</p>

              {connError && (
                <div
                  data-testid="docs-conn-note"
                  className="mt-2 rounded-r-md border-l-[3px] border-danger bg-danger-subtle px-3 py-2 text-[11.5px]"
                >
                  {connError}
                </div>
              )}
              {!connError && connNote && (
                <div data-testid="docs-conn-note" className="mt-2 text-[11.5px] text-text-muted">
                  {connNote}
                </div>
              )}
            </div>

            {/* ── The connections, growing downward under the control that makes them ──
                One row per identity, carrying what the connection list and the saved-key
                list used to carry between them: platform, account address, document
                count, key file, whether it is in use, and both actions. A row without a
                live connection is a key file kept after "Disconnect"; a row whose key
                is not in the managed directory is a path-mode connection. ── */}
            <div className="border-t border-border pt-3">
              <div className="mb-1.5 font-medium">{t('docs.conn.listTitle')}</div>
              {connKeyRows.length === 0 ? (
                <p className="text-xs text-text-muted">{t('docs.conn.none')}</p>
              ) : (
                connKeyRows.map(({ id, c, k }) => {
                  const provider = c?.provider || k?.provider || '';
                  const providerName = c?.provider_name || k?.provider_name || '';
                  // The account this row acts under. A connection knows its own
                  // address; a key that nothing is running names the same account
                  // from its own contents (SA email, or the Feishu app id).
                  const address = c?.agent_address || k?.address || k?.client_email || '';
                  const keyName = k?.filename || c?.key_filename || '';
                  const keyPath = k?.path || c?.key_path || keyName;
                  // delete_key only reaches clouddoc-keys/, and refuses any file a
                  // connection holds. Both refusals are shown here rather than
                  // discovered by pressing the button.
                  const external = Boolean(c) && !k;
                  const armed = Boolean(k) && keyDeleteArm === k?.filename;
                  return (
                    <div
                      key={id}
                      data-testid="docs-conn-row"
                      data-connected={c ? 'true' : 'false'}
                      className="mb-2 rounded-md border border-border px-3 py-2"
                    >
                      <div className="flex items-center gap-2">
                        {PROVIDER_LOGOS[provider] && (
                          <img src={PROVIDER_LOGOS[provider]} alt="" aria-hidden className="h-3.5 w-3.5 flex-none object-contain" />
                        )}
                        <span className="min-w-0 flex-1 truncate font-medium">
                          {providerLabel(provider, providerName, t)}
                        </span>
                        {c ? (
                          <span className="flex-none rounded-full bg-ok-subtle px-2 py-0.5 text-[11px] text-ok">
                            {t('docs.keyInUse')}
                          </span>
                        ) : (
                          <span className="flex-none rounded-full bg-bg-hover px-2 py-0.5 text-[11px] text-text-muted">
                            {t('docs.conn.notConnected')}
                          </span>
                        )}
                      </div>
                      {/* The address is up to forty-five characters in a column that
                          is at most 430 wide: truncated, with the whole of it in the
                          title. Same for the filename on the line below. */}
                      <div className="mt-0.5 truncate font-mono text-[11px] text-text-muted" title={address}>
                        {address}
                      </div>
                      <div className="mt-0.5 flex items-center gap-2 text-[11px] text-text-muted">
                        {keyName && (
                          <span
                            className="min-w-0 flex-1 truncate font-mono"
                            title={`${t('docs.conn.keyFile')}: ${keyPath}`}
                          >
                            {keyName}
                          </span>
                        )}
                        {external && (
                          <span className="flex-none rounded border border-border px-1" title={keyPath}>
                            {t('docs.conn.keyExternal')}
                          </span>
                        )}
                        {/* A row with no connection has no documents to count --
                            the "Not connected" chip above is the whole story. */}
                        {c && <span className="flex-none">{t('docs.connDocs', { count: c.docs_count })}</span>}
                      </div>
                      <div className="mt-1.5 flex flex-wrap items-center gap-3">
                        {c ? (
                          <>
                            <button
                              data-testid="docs-conn-copy"
                              className="text-xs underline"
                              onClick={() => copyConnAddress(c.id, c.agent_address)}
                            >
                              {copiedConn === c.id ? t('docs.copied') : t('docs.copyAddress')}
                            </button>
                            {/* Disconnecting is a grant-lifecycle act -- every watch
                                issued under this identity stops meaning anything -- so
                                it goes through the same confirmation the panel had. */}
                            <button
                              data-testid="docs-conn-remove"
                              className="text-xs text-danger underline"
                              onClick={() => setRemoveConnTarget(c)}
                            >
                              {t('docs.conn.remove')}
                            </button>
                          </>
                        ) : (
                          k && (
                            <button
                              data-testid="docs-conn-saved-key"
                              className="text-xs underline disabled:opacity-40"
                              disabled={connBusy}
                              title={k.path}
                              onClick={() => void submitConnection({ credentials_path: k.path })}
                            >
                              {connBusy ? t('docs.adding') : t('docs.conn.add')}
                            </button>
                          )
                        )}
                        {/* "Disconnect" and "Delete file" are not one act with two
                            names: the first ends the connection and keeps the
                            credential on disk, the second removes the local copy and
                            leaves the credential valid until the cloud console
                            revokes it. Both stay on the row, and the second is
                            disabled -- not hidden -- while a connection holds the
                            file, so the order is legible. */}
                        {k && (
                          <button
                            data-testid="docs-conn-key-delete"
                            disabled={Boolean(c)}
                            title={c ? t('docs.keyDeleteBlocked') : keyDeleteHintFor(k.provider)}
                            className={`ml-auto flex-none rounded-md border px-2 py-0.5 text-[11px] disabled:opacity-40 ${
                              armed
                                ? 'border-danger bg-danger-subtle text-danger'
                                : 'border-border text-text-muted hover:border-danger hover:text-danger'
                            }`}
                            onClick={() => {
                              if (armed) void deleteSavedKey(k.filename);
                              else setKeyDeleteArm(k.filename);
                            }}
                          >
                            {armed ? t('docs.keyDeleteConfirm') : t('docs.keyDelete')}
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })
              )}
              {connKeyRows.some((r) => r.k) && (
                <p className="mt-1 text-[11px] text-text-muted">
                  {keyDeleteHintFor(
                    // One platform in the list names its own console; a mixed list
                    // falls back to the wording that is true of both.
                    connKeyRows
                      .map((r) => r.k?.provider)
                      .filter(Boolean)
                      .reduce<string | undefined | null>(
                        (acc, p) => (acc === null ? p : acc === p ? acc : undefined),
                        null,
                      ) ?? undefined,
                  )}
                </p>
              )}
            </div>

            {/* ── The deployment default model: a deployment-level fact, like the
                connections above it. A document may pin its own in its record
                dialog; this is what every other one runs on. ── */}
            <div data-testid="docs-poll" className="border-t border-border pt-3">
              <div className="font-medium">{t('docs.poll.title')}</div>
              <p className="mt-0.5 text-[11.5px] leading-relaxed text-text-muted">{t('docs.poll.hint')}</p>
              <div className="mt-1.5 flex flex-wrap items-center gap-2">
                <select
                  data-testid="docs-poll-select"
                  className="rounded-md border border-border bg-bg px-2 py-1 text-xs"
                  value={String(conf?.poll_interval_seconds ?? 30)}
                  disabled={modelBusy}
                  onChange={(e) => void setPollInterval(Number(e.target.value))}
                >
                  {/* A value the config already holds is offered too, even when it is
                      not one of the four: a select whose value is not among its options
                      renders blank, which reads as broken rather than as "60s, set by
                      hand". */}
                  {Array.from(new Set([5, 10, 15, 30, conf?.poll_interval_seconds ?? 30]))
                    .sort((a, b) => a - b)
                    .map((n) => (
                      <option key={n} value={n}>{t('docs.poll.every', { n })}</option>
                    ))}
                </select>
                <span className="text-[11.5px] text-text-muted" data-testid="docs-poll-expectation">
                  {t('docs.poll.expectation', {
                    avg: Math.round((conf?.poll_interval_seconds ?? 30) / 2 + 6),
                  })}
                </span>
              </div>
            </div>

            <div data-testid="docs-model" className="border-t border-border pt-3">
              <div className="font-medium">{t('docs.model.title')}</div>
              <p className="mt-0.5 text-[11.5px] leading-relaxed text-text-muted">{t('docs.model.hint')}</p>
              <div className="mt-1.5 flex flex-wrap items-center gap-3">
                <ModelPicker
                  testIdPrefix="docs-model-picker"
                  value={conf?.model_name || null}
                  onChange={(name) => void setDeploymentModel(name)}
                  disabled={modelBusy}
                />
                {conf?.model_name ? (
                  <button
                    data-testid="docs-model-reset"
                    className="text-xs underline"
                    disabled={modelBusy}
                    onClick={() => void setDeploymentModel('')}
                  >
                    {t('docs.model.useDefault')}
                  </button>
                ) : (
                  <span className="text-[11.5px] text-text-muted">{t('docs.model.default')}</span>
                )}
              </div>
            </div>

          </div>
        </aside>

        <section className="flex min-h-0 flex-col overflow-hidden rounded-xl border border-border bg-card">
        {/* ── 工具条：过滤 + 全局动作（连接归属与部署级设置在左栏）── */}
        <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2.5">
          {conns.length > 1 && (
            <select
              value={filterConn}
              onChange={(e) => setFilterConn(e.target.value)}
              className="rounded-md border border-border bg-card px-2 py-1 text-xs"
              data-testid="docs-filter-conn"
            >
              <option value="">{t('docs.table.allConns')}</option>
              {conns.map((c) => (
                <option key={c.id} value={c.id}>
                  {/* The address, shortened the same way the table's column does
                      it. `agent_display` is the app's name for a Feishu
                      connection ("bot"), which names no particular identity. */}
                  {providerLabel(c.provider, c.provider_name, t)} · {connShort[c.id] || c.id}
                </option>
              ))}
            </select>
          )}
          <select
            value={filterKind}
            onChange={(e) => setFilterKind(e.target.value)}
            className="rounded-md border border-border bg-card px-2 py-1 text-xs"
            data-testid="docs-filter-kind"
          >
            <option value="">{t('docs.table.allKinds')}</option>
            <option value="document">{t('docs.kind.document')}</option>
            <option value="spreadsheet">{t('docs.kind.spreadsheet')}</option>
            <option value="presentation">{t('docs.kind.presentation')}</option>
            <option value="file">{t('docs.kind.file')}</option>
          </select>
          <select
            value={filterTier}
            onChange={(e) => setFilterTier(e.target.value)}
            className="rounded-md border border-border bg-card px-2 py-1 text-xs"
            data-testid="docs-filter-tier"
          >
            {/* The filter says what it is showing, in full. Its own strings,
                not the switch's: "On" is the right word on a two-value toggle
                and the wrong one on a line that has to say what a list
                contains. */}
            <option value="apply_scoped">{t('docs.table.filterOn', { count: tierCounts.on })}</option>
            <option value="off">{t('docs.table.filterOff', { count: tierCounts.off })}</option>
            <option value="">{t('docs.table.filterAll', { count: tierCounts.all })}</option>
          </select>
          <span className="flex-1" />
          {hasConn && (
            <>
              <button
                onClick={() => void bulkWatch(true)}
                disabled={selected.size === 0 || bulkBusy}
                title={armBulk === 'on' ? t('docs.watch.enableSelectedConfirm', { count: selected.size }) : undefined}
                className={`rounded-md border px-2.5 py-1 text-xs disabled:opacity-40 ${
                  armBulk === 'on' ? 'border-accent text-accent' : 'border-border hover:bg-bg-hover'
                }`}
                data-testid="docs-bulk-enable"
              >
                {armBulk === 'on'
                  ? t('docs.watch.enableSelectedConfirm', { count: selected.size })
                  : t('docs.watch.enableSelected', { count: selected.size })}
              </button>
              {/* Danger colour and a gap of its own: hiding is the destructive
                  half of the pair -- the term restarts on the way back -- and the
                  two used to sit shoulder to shoulder. */}
              <button
                onClick={() => void bulkWatch(false)}
                disabled={selected.size === 0 || bulkBusy}
                title={armBulk === 'off' ? t('docs.watch.disableSelectedConfirm', { count: selected.size }) : undefined}
                className={`ml-2 rounded-md border px-2.5 py-1 text-xs disabled:opacity-40 ${
                  armBulk === 'off'
                    ? 'border-danger bg-danger-subtle text-danger'
                    : 'border-border text-danger hover:bg-danger-subtle'
                }`}
                data-testid="docs-bulk-off"
              >
                {armBulk === 'off'
                  ? t('docs.watch.disableSelectedConfirm', { count: selected.size })
                  : t('docs.watch.disableSelected', { count: selected.size })}
              </button>
              <button
                onClick={() => void handleRefresh()}
                disabled={refreshing}
                className="ml-2 rounded-md border border-border px-2.5 py-1 text-xs hover:bg-bg-hover"
                data-testid="docs-refresh"
              >
                {refreshing ? '…' : t('docs.refresh')}
              </button>
            </>
          )}
        </div>

        {hasConn ? (
          <>
            {bulkNote && (
              <div
                data-testid="docs-bulk-note"
                className="border-b border-border bg-bg-muted/40 px-4 py-2 text-[12px] text-text-muted"
              >
                {bulkNote}
              </div>
            )}
            <div className="min-h-0 flex-1 overflow-auto">
              <table className="w-full border-collapse text-[13px]">
                <thead className="sticky top-0 z-[1] bg-card">
                  <tr className="border-b border-border text-left text-[11.5px] uppercase tracking-wide text-text-muted">
                    <th className="w-9 px-3 py-2">
                      <input
                        type="checkbox"
                        checked={allVisibleSelected}
                        onChange={toggleAllVisible}
                        aria-label={t('docs.table.selectAll')}
                        title={t('docs.table.selectAll')}
                        data-testid="docs-select-all"
                        className="h-3.5 w-3.5 align-middle accent-accent"
                      />
                    </th>
                    <th className="px-4 py-2 font-medium">{t('docs.table.colDoc')}</th>
                    <th className="px-3 py-2 font-medium">{t('docs.table.colKind')}</th>
                    {conns.length > 1 && <th className="px-3 py-2 font-medium">{t('docs.table.colConn')}</th>}
                    <th className="px-3 py-2 font-medium">{t('docs.table.colStatus')}</th>
                    <th className="px-3 py-2 font-medium">{t('docs.table.colTier')}</th>
                    <th className="px-3 py-2 font-medium">{t('docs.table.colActivity')}</th>
                    <th className="w-24 whitespace-nowrap px-2 py-2" />
                  </tr>
                </thead>
                <tbody>
                  {visibleDocs
                    .map((d) => {
                      const w = watches[d.doc_id];
                      const cconn = conns.find((c) => c.id === d.connection_id);
                      // Three states, and only one of them is anybody's doing: off,
                      // on, and the one the calendar reaches on its own.
                      const tierLabel = !w
                        ? t('docs.watch.watchOff')
                        : w.expired
                          ? t('docs.watch.watchExpired')
                          : t('docs.watch.watchApply');
                      const tierCls = !w
                        ? 'bg-bg-hover text-text-muted'
                        : w.expired
                          ? 'bg-warn-subtle text-warn'
                          : 'bg-danger-subtle text-danger';
                      const isOn = !!w && !w.expired;
                      const daysLeft =
                        w?.expires_at && !w.expired
                          ? Math.max(0, Math.ceil((w.expires_at * 1000 - Date.now()) / 86400000))
                          : null;
                      return (
                        <tr key={d.doc_id} className="border-b border-border/60 hover:bg-bg-hover/40" data-testid="docs-table-row">
                          <td className="px-3 py-2">
                            <input
                              type="checkbox"
                              checked={selected.has(d.doc_id)}
                              onChange={() => toggleOne(d.doc_id)}
                              aria-label={t('docs.table.colSelect')}
                              data-testid="docs-select-row"
                              className="h-3.5 w-3.5 align-middle accent-accent"
                            />
                          </td>
                          <td className="max-w-[340px] px-4 py-2">
                            <span className="flex items-center gap-2.5">
                              <DocIcon px={26} provider={d.provider} kind={d.kind} />
                              <span className="min-w-0">
                                {d.url && d.url.startsWith('http') ? (
                                  <span className="flex items-center gap-1.5 truncate font-medium">
                                    <button
                                      type="button"
                                      className="truncate text-left hover:underline"
                                      title={t('docs.workbench.openHere')}
                                      onClick={() => requestOpenDoc(d.doc_id)}
                                      data-testid="docs-table-open-workbench"
                                    >
                                      {d.title || d.doc_id}
                                    </button>
                                    <a href={d.url} target="_blank" rel="noreferrer" title={t('docs.workbench.openExternal')} className="flex items-center">
                                      <ExtIcon />
                                    </a>
                                  </span>
                                ) : (
                                  <span
                                    className="block truncate font-medium"
                                    title={t('docs.linkUnknown')}
                                  >
                                    {d.title || d.doc_id}
                                  </span>
                                )}
                                {docSubline(d, t) && (
                                  <span className="block truncate text-[11px] text-text-muted">{docSubline(d, t)}</span>
                                )}
                              </span>
                            </span>
                          </td>
                          <td className="whitespace-nowrap px-3 py-2 text-xs text-text-muted">
                            {t(`docs.kind.${d.kind || 'document'}`, d.kind || 'document')}
                          </td>
                          {conns.length > 1 && (
                            <td className="whitespace-nowrap px-3 py-2 text-xs text-text-muted">
                              {/* Short enough to read down the column, long enough to
                                  tell two service accounts apart; the full address is
                                  on hover and in the document detail. */}
                              <span
                                className="inline-flex items-center gap-1.5"
                                title={cconn ? connFullName(cconn) : d.connection_id}
                              >
                                {PROVIDER_LOGOS[d.provider ?? ''] ? (
                                  <img src={PROVIDER_LOGOS[d.provider ?? '']} alt={providerLabel(d.provider, d.provider_name, t)} className="h-3.5 w-3.5 object-contain" />
                                ) : (
                                  <span className="rounded bg-bg-muted px-1 text-[10px]">{providerLabel(d.provider, d.provider_name, t)}</span>
                                )}
                                {(cconn && connShort[cconn.id]) || d.connection_id}
                              </span>
                            </td>
                          )}
                          <td className="whitespace-nowrap px-3 py-2"><StatusPill status={d.status} /></td>
                          {/* The column **is** the switch. A binary hidden behind a
                              menu of two items made the reader open a menu to find
                              out there was nothing to choose. */}
                          <td className="whitespace-nowrap px-3 py-2" onClick={(e) => e.stopPropagation()}>
                            <button
                              type="button"
                              data-testid="docs-tier-toggle"
                              data-on={isOn ? '1' : '0'}
                              title={armApply === d.doc_id ? t('docs.watch.watchApplyConfirm') : t('docs.table.tierToggleHint')}
                              onClick={() => {
                                if (!isOn && armApply !== d.doc_id) {
                                  setArmApply(d.doc_id);
                                  setTimeout(() => setArmApply(null), 5000);
                                  return;
                                }
                                void setWatchLevel(d.doc_id, isOn ? 'off' : 'apply_scoped');
                              }}
                              className={`rounded-md px-1.5 py-0.5 text-[11px] font-medium hover:ring-1 hover:ring-border-strong ${
                                armApply === d.doc_id ? 'bg-accent-subtle text-accent' : tierCls
                              }`}
                            >
                              {armApply === d.doc_id ? t('docs.watch.watchApplyConfirm') : tierLabel}
                            </button>
                            {daysLeft !== null && (
                              <span className="ml-1.5 text-[11px] text-text-muted">{t('docs.table.daysLeft', { count: daysLeft })}</span>
                            )}
                          </td>
                          <td className="whitespace-nowrap px-3 py-2 font-mono text-[11px] text-text-muted">
                            {d.checked_at ? new Date(d.checked_at * 1000).toLocaleString(undefined, { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'}
                          </td>
                          {/* Not a menu. Merging the two dialogs left one entry
                              behind it, and a menu of one asks the reader to open
                              it to discover there was nothing to choose. */}
                          <td className="whitespace-nowrap px-2 py-2 text-right" onClick={(e) => e.stopPropagation()}>
                            <button
                              type="button"
                              data-testid="docs-panel-detail-open"
                              onClick={() => void openDetail(d)}
                              title={t('docs.detail.open')}
                              className="whitespace-nowrap rounded-md px-2 py-0.5 text-xs text-text-muted hover:bg-bg-hover hover:text-text"
                            >
                              {t('docs.detail.open')}
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                </tbody>
              </table>
            </div>

            {/* ── The foot of the table: adopt by link ──
                Sharing is how documents normally arrive: discovery adopts everything
                the connection can edit, on panel load, on Refresh and on a timer.
                This box is the way in for a document the platform does not list
                (a Feishu file outside every managed wiki space, a document the app
                created): the link is verified, a document that passes is adopted
                under the same policy discovery applies -- polled, persisted, watch
                off unless the deployment policy says otherwise -- and the result
                says which. See AdoptByLink. */}
            <div className="border-t border-border px-4 py-3">
              <p className="mb-2 text-[11.5px] text-text-muted" data-testid="docs-discovery-hint">
                {t('docs.discoveryHint')}
              </p>
              {/* ── Where a platform will not list what it was given ──
                  The provider learns this by asking once and the panel used to keep it
                  to itself, so the screen promised documents that arrive on their own
                  over a list that never filled. Saying it here turns a silent failure
                  into a different, workable instruction: search by name instead. */}
                {noDiscovery.length > 0 && (
                  <div className="mt-2.5 rounded-lg border border-border bg-bg-muted/40 px-3 py-2"
                       data-testid="docs-no-discovery">
                    {/* The reason comes from the provider when it knows one -- on Feishu it is
                        usually a single space membership the owner can grant -- and the generic
                        line is the fallback for a platform that offers nothing to enumerate. */}
                    {noDiscovery.map((c) => (
                      <div key={c.connection_id} className="text-[11.5px] text-text-muted">
                        {c.discovery_reason
                          ? t('docs.noDiscoveryReason', { address: c.address, reason: c.discovery_reason })
                          : t('docs.noDiscovery', { address: c.address })}
                      </div>
                    ))}
                  </div>
                )}
            <AdoptByLink adopt={adoptByLink} copyAddress={copyAddress} copied={copied} />
            {adopted > 0 && (
              <div className="mt-2.5 rounded-r-lg border-l-[3px] border-ok bg-ok-subtle px-4 py-2 text-xs text-text-muted">
                {t('docs.adopted', { count: adopted })}
                </div>
              )}
              {retired.length > 0 && (
                <div className="mt-2.5 rounded-r-lg border-l-[3px] border-border bg-bg-muted/40 px-4 py-2 text-xs text-text-muted"
                     data-testid="docs-retired">
                  {t('docs.retired', { count: retired.length })}
                </div>
              )}
              {unsupported.length > 0 && (
                <div className="mt-2.5 rounded-lg border border-border bg-bg-muted/40 px-3 py-2">
                  <div className="text-[11.5px] text-text-muted">
                    {t('docs.unsupported', { count: unsupported.length })}
                  </div>
                  {unsupported.map((f, i) => (
                    <div key={i} className="flex items-center gap-2 py-1">
                      <span className="min-w-0 flex-1 truncate text-[13px]">{f.title}</span>
                      <span className="flex-none rounded-full bg-bg-muted px-2 py-0.5 text-[11px] text-text-muted">
                        {t(`docs.kind.${f.kind}`, f.kind)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
              {needsEditor.length > 0 && (
                <div className="mt-2.5 rounded-lg border border-border bg-bg-muted/40 px-3 py-2">
                  <div className="mb-1.5 text-[11.5px] text-text-muted">
                    {t('docs.needsEditor', { count: needsEditor.length })}
                  </div>
                  {needsEditor.map((d) => (
                    <div key={d.doc_id} className="flex items-center gap-2 py-1">
                      <a href={d.url} target="_blank" rel="noreferrer" className="min-w-0 flex-1 truncate text-[13px] hover:underline">
                        {d.title || d.doc_id}
                      </a>
                      <span className="flex-none rounded-full bg-warn-subtle px-2 py-0.5 text-[11px] text-warn">
                        {t('docs.status.comment_only')}
                      </span>
                    </div>
                  ))}
                </div>
              )}
              <div className="mt-2 text-right font-mono text-[11px] text-text-muted">
                {t('docs.footCount', { count: docs.length })}
                {refreshedAt ? ` · ${t('docs.refreshedAt', { time: refreshedAt })}` : ''}
              </div>
            </div>
          </>
        ) : (
          // The guidance, with the way in beside it rather than behind a button:
          // the left column already holds the add-connection card.
          <div className="flex flex-1 items-center justify-center">
            <div className="max-w-md px-8 text-center">
              <EmptyIllustration />
              <p className="mt-4 text-sm font-medium">{t('docs.noConn')}</p>
              <p className="mt-1.5 text-xs leading-relaxed text-text-muted">{t('docs.notEnabled')}</p>
            </div>
          </div>
        )}
        </section>
      </div>

      {detailFor && (
        <div
          data-testid="docs-panel-detail"
          className="fixed inset-0 z-30 flex items-center justify-center bg-black/30"
          onClick={() => setDetailFor(null)}
        >
          <div
            className="flex max-h-[84vh] w-[min(760px,94vw)] flex-col rounded-lg border border-border bg-card p-5 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-3 flex items-start justify-between gap-4">
              <div className="min-w-0">
                <h3 className="text-sm font-semibold text-text-strong">{t('docs.detail.title')}</h3>
                <p className="mt-0.5 truncate text-xs text-text-muted">{detailFor.title || detailFor.doc_id}</p>
                <p className="mt-1 text-[11px] text-text-muted" data-testid="docs-panel-history-undo-hint">
                  {t('docs.history.undoHint')}
                </p>
              </div>
              <button
                onClick={() => setDetailFor(null)}
                className="flex-none rounded-md px-2 py-1 text-text-muted hover:bg-bg-hover"
                data-testid="docs-panel-history-close"
              >
                ✕
              </button>
            </div>

            <DetailTimeline
              docId={detailFor.doc_id}
              title={detailFor.title}
              lineage={lineage}
              receipts={receipts}
              loading={detailBusy}
              summary={detailSummary(detailFor)}
              note={
                detailNote ? (
                  <p data-testid="docs-panel-history-note" className="mt-3 rounded-md bg-bg-hover px-3 py-2 text-xs text-text">
                    {detailNote}
                  </p>
                ) : null
              }
              onLocate={(r) => {
                // Locating means going to the document, so the dialog gets out
                // of the way rather than sitting over the thing it just pointed
                // at.
                setDetailFor(null);
                requestOpenDoc(detailFor.doc_id, r.receipt_id);
              }}
              onUnhighlight={(receiptId) => void actOnReceipt('clouddoc.unhighlight', receiptId)}
              actingOn={actingOn}
            />
          </div>
        </div>
      )}

      {removeConnTarget && (
        <ConfirmDialog
          title={t('docs.removeConn')}
          message={t('docs.removeConnConfirm', {
            address: removeConnTarget.agent_address,
            count: removeConnTarget.docs_count,
          })}
          loading={removing}
          onConfirm={() => void handleRemoveConnection()}
          onCancel={() => setRemoveConnTarget(null)}
        />
      )}

    </div>
  );
}
