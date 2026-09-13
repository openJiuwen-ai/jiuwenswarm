/**
 * 文档工作台（release §14）：打开一份纳管文档时替换会话工作区。
 * 主列 = 标签栏 + 主界面（平台编辑器 iframe / md 编辑器）+ 底部聊天条；右栏 = 回执 / 聊天历史 / 状态。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useChatStore } from '../../../../channels/web/frontend/src/stores/chatStore';
import { useDocWorkbenchStore } from '../stores/docWorkbenchStore';
import { webClient, webRequest } from '../../../../channels/web/frontend/src/services/webClient';
import { canLocate, receiptAnchor, receiptsFromExecutions, type ReceiptRow } from '../features/clouddoc/receipts';
import { TabStrip } from './TabStrip';
import { DocFrame } from './DocFrame';
import type { ComponentProps } from 'react';
import { ChatStrip, type ComposerProps } from './ChatStrip';
import { SideRail, type WatchInfo } from './SideRail';
import './DocWorkbench.css';

// The gateway pushes clouddoc.receipts_changed when the ledger file moves; the
// poll survives only as a slow fallback for a dropped frame.
const POLL_MS = 60_000;

type WatchListPayload = { watches?: { doc_id: string; mode: string; expires_at?: number | null; expired?: boolean; revoked?: boolean }[] };

export function DocWorkbench({ composer, onUserAnswer }: {
  composer: ComposerProps;
  /** Passed straight to the docked strip, which needs its own authorization slot. */
  onUserAnswer: ComponentProps<typeof ChatStrip>['onUserAnswer'];
}) {
  const wb = useDocWorkbenchStore();
  const sid = useChatStore((s) => s.activeSessionId) ?? '';
  const executions = useChatStore((s) => s.runtimes[sid]?.toolExecutions);
  const tab = useMemo(() => wb.tabs.find((t) => t.docId === wb.activeDocId) ?? null, [wb.tabs, wb.activeDocId]);
  const [receipts, setReceipts] = useState<Record<string, ReceiptRow[]>>({});
  const [watches, setWatches] = useState<Record<string, WatchInfo>>({});
  // Titles for every registered document, not only the open tabs: the model can
  // work on a document that has no tab, and the strip must still name it.
  const [docTitles, setDocTitles] = useState<Record<string, string>>({});
  useEffect(() => {
    let alive = true;
    webRequest<{ docs?: { doc_id: string; title?: string }[] }>('clouddoc.list_docs')
      .then((out) => {
        if (!alive) return;
        const map: Record<string, string> = {};
        for (const d of out?.docs ?? []) if (d.doc_id && d.title) map[d.doc_id] = d.title;
        setDocTitles(map);
      })
      .catch(() => undefined);
    return () => { alive = false; };
  }, [wb.tabs.length]);
  const titleOf = useCallback(
    (docId: string) => wb.tabs.find((x) => x.docId === docId)?.title || docTitles[docId] || '',
    [wb.tabs, docTitles],
  );

  // Receipts for every open tab: the focused one to show, the others to count
  // as unread. Polling, since no push event exists for the ledger yet.
  const refresh = useCallback(async () => {
    const ids = useDocWorkbenchStore.getState().tabs.map((t) => t.docId);
    const next: Record<string, ReceiptRow[]> = {};
    await Promise.all(ids.map(async (id) => {
      try {
        const out = await webRequest<{ receipts?: ReceiptRow[] }>('clouddoc.receipts', { doc_id: id, limit: 50 });
        next[id] = out?.receipts ?? [];
      } catch {
        next[id] = receipts[id] ?? [];
      }
    }));
    setReceipts(next);
    for (const id of ids) {
      const applied = (next[id] ?? []).filter((r) => r.status !== 'pending').map((r) => r.receipt_id);
      useDocWorkbenchStore.getState().noteReceipts(id, applied);
    }
    try {
      const w = await webRequest<WatchListPayload>('clouddoc.watch_list');
      const map: Record<string, WatchInfo> = {};
      for (const it of w?.watches ?? []) {
        if (it.revoked) continue;
        const lapsed = !!it.expired || (it.expires_at != null && it.expires_at * 1000 <= Date.now());
        map[it.doc_id] = { mode: it.mode, expires_at: it.expires_at ?? null, expired: lapsed };
      }
      setWatches(map);
    } catch { /* keep what we had */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wb.tabs.length]);

  const executionCount = executions ? executions.size : 0;
  useEffect(() => { void refresh(); }, [refresh, wb.activeDocId, executionCount]);
  useEffect(() => {
    const id = window.setInterval(() => { void refresh(); }, POLL_MS);
    return () => window.clearInterval(id);
  }, [refresh]);
  useEffect(() => webClient.on('clouddoc.receipts_changed', () => { void refresh(); }), [refresh]);

  const tiers = useMemo(() => {
    const out: Record<string, string | undefined> = {};
    for (const t of wb.tabs) out[t.docId] = watches[t.docId] && !watches[t.docId]?.expired ? watches[t.docId]?.mode : undefined;
    return out;
  }, [wb.tabs, watches]);

  // Locate: every format is now the platform's own page in a frame, so locating is
  // reloading the frame on the receipt's anchor. A sheet or deck receipt carries the
  // platform's own URL fragment; a document receipt carries none and the frame just
  // reloads (anchors are best-effort and documented as such).
  const locateReceipt = useCallback((r: ReceiptRow) => {
    if (!wb.tabs.some((t) => t.docId === r.doc_id)) return;
    wb.activate(r.doc_id);
    wb.requestLocate(r.doc_id, r.receipt_id, receiptAnchor(r));
  }, [wb]);

  // The chat strip offers locate on the last receipt but holds neither the row
  // nor the document's kind; it asks here so the rule stays in one place.
  const canLocateById = useCallback((docId: string, receiptId: string) => {
    const r = (receipts[docId] ?? []).find((x) => x.receipt_id === receiptId);
    if (!r) return false;
    return canLocate(wb.tabs.find((x) => x.docId === docId)?.kind, r);
  }, [receipts, wb.tabs]);

  const locateById = useCallback((docId: string, receiptId: string) => {
    const r = (receipts[docId] ?? []).find((x) => x.receipt_id === receiptId);
    if (r) locateReceipt(r);
    else wb.activate(docId);
  }, [receipts, locateReceipt, wb]);

  // A receipt this session just produced lands in the rail without waiting for the poll.
  const ownReceipts = useMemo(() => receiptsFromExecutions(executions ? executions.values() : undefined), [executions]);
  useEffect(() => { if (ownReceipts.length) void refresh(); }, [ownReceipts.length, refresh]);

  // Off or on, and off is a revocation: the watch registry's tombstone is what
  // keeps the adoption policy from re-issuing behind the person's back.
  const setWatch = useCallback(async (mode: 'off' | 'apply_scoped') => {
    if (!tab) return;
    if (mode === 'off') await webRequest('clouddoc.watch_revoke', { doc_id: tab.docId });
    else await webRequest('clouddoc.watch_set', { doc_id: tab.docId, mode });
    void refresh();
  }, [tab, refresh]);

  const [frameNonce, setFrameNonce] = useState(0);
  const nonce = (tab ? wb.reloadNonce[tab.docId] ?? 0 : 0) + frameNonce + (wb.locate && wb.locate.docId === tab?.docId ? wb.locate.nonce : 0);

  if (!wb.open || !tab) return null;
  const unreadTotal = wb.tabs.reduce((a, t) => a + t.unread, 0);

  return (
    <div className="doc-workbench" data-testid="doc-workbench">
      <div className="doc-workbench__main">
        <TabStrip
          tabs={wb.tabs}
          activeDocId={wb.activeDocId}
          tiers={tiers}
          railVisible={wb.railVisible}
          chatVisible={wb.chatVisible}
          canOpenExternal={!!tab.url && tab.url.startsWith('http')}
          onActivate={wb.activate}
          onClose={wb.closeTab}
          onOpenExternal={() => { if (tab.url) window.open(tab.url, '_blank', 'noopener'); }}
          onReload={() => setFrameNonce((n) => n + 1)}
          onToggleRail={wb.toggleRail}
          onToggleChat={wb.toggleChat}
          onExit={wb.exit}
        />
        <DocFrame
          tab={tab}
          nonce={nonce}
          anchor={wb.locate && wb.locate.docId === tab.docId ? wb.locate.anchor : ''}
          alwaysNewTab={!!wb.alwaysNewTab[tab.provider]}
          onAlwaysNewTab={(v) => wb.setAlwaysNewTab(tab.provider, v)}
          onReload={() => setFrameNonce((n) => n + 1)}
        />
        <ChatStrip
          composer={composer}
          onUserAnswer={onUserAnswer}
          visible={wb.chatVisible}
          titleOf={titleOf}
          onHide={wb.toggleChat}
          onShow={wb.toggleChat}
          unreadReceipts={unreadTotal}
          onLocate={locateById}
          canLocate={canLocateById}
          onHistory={() => { if (!wb.railVisible) wb.toggleRail(); wb.setRailTab('history'); }}
        />
      </div>
      {wb.railVisible && (
        <SideRail
          tab={tab}
          tabs={wb.tabs}
          railTab={wb.railTab}
          receipts={receipts[tab.docId] ?? []}
          watch={watches[tab.docId]}
          onTab={wb.setRailTab}
          onRefresh={() => void refresh()}
          onLocate={locateReceipt}
          onJump={wb.activate}
          onWatch={(m) => void setWatch(m)}
          onHide={wb.toggleRail}
        />
      )}
    </div>
  );
}
