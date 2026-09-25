/**
 * The right rail: receipts follow the focused document, chat history is the
 * whole session across documents, and the foot carries that document's watch --
 * the same switch the panel's column is, not a second way out worded
 * differently. (A threads tab waits on a panel API for comment threads; see the
 * release notes' backlog.)
 */
import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { PanelRightClose } from 'lucide-react';
import { webRequest } from '../../../../channels/web/frontend/src/services/webClient';
import { useChatStore } from '../../../../channels/web/frontend/src/stores/chatStore';
import { useSessionStore } from '../../../../channels/web/frontend/src/stores/sessionStore';
import { ChatTimelineList } from '../../../../channels/web/frontend/src/components/ChatPanel/MessageList';
import { KindIcon } from './KindIcon';
import { type ReceiptRow } from '../features/clouddoc/receipts';
import { DetailTimeline } from '../DocsPanel/DetailTimeline';
import type { LineageRow } from '../DocsPanel/docDetail';
import type { RailTab, WorkbenchTab } from '../stores/docWorkbenchStore';

// The rail holds the ledger only: the registry's audit journal is the panel's
// fetch, and its rows are per-document authority, not per-tab. The shared
// timeline degrades to the write rows, which is what this tab has always shown.
const NO_LINEAGE: LineageRow[] = [];

export type WatchInfo = { mode: string; expires_at?: number | null; expired?: boolean } | undefined;

export function SideRail({
  tab, tabs, railTab, receipts, watch, onTab, onRefresh, onLocate, onJump, onWatch, onHide,
}: {
  tab: WorkbenchTab | null;
  tabs: WorkbenchTab[];
  railTab: RailTab;
  receipts: ReceiptRow[];
  watch: WatchInfo;
  onTab: (t: RailTab) => void;
  onRefresh: () => void;
  onLocate: (r: ReceiptRow) => void;
  onJump: (docId: string) => void;
  onWatch: (mode: 'off' | 'apply_scoped') => void;
  onHide: () => void;
}) {
  const { t } = useTranslation();
  const sid = useChatStore((s) => s.activeSessionId) ?? '';
  const messages = useChatStore((s) => s.runtimes[sid]?.messages ?? []);
  const executions = useChatStore((s) => s.runtimes[sid]?.toolExecutions);
  const mode = useSessionStore((s) => s.runtimes[sid]?.mode ?? 'agent');
  const [acting, setActing] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  // Arms the on-direction, exactly as the panel's column does.
  const [armOn, setArmOn] = useState(false);

  const act = useCallback(async (method: 'clouddoc.unhighlight', receiptId: string) => {
    setActing(receiptId);
    setNote(null);
    try {
      const out = await webRequest<{ ok?: boolean; detail?: string }>(method, { receipt_id: receiptId }, { timeoutMs: 60_000 });
      if (out && out.ok === false) setNote(out.detail || t('docs.history.failed'));
    } catch {
      setNote(t('docs.history.actionUnconfirmed'));
    } finally {
      setActing(null);
      onRefresh();
    }
  }, [onRefresh, t]);

  useEffect(() => { setNote(null); setArmOn(false); }, [tab?.docId]);

  const elsewhere = tabs.filter((x) => x.docId !== tab?.docId && x.unread > 0);
  const watchOn = !!watch && !watch.expired && watch.mode === 'apply_scoped';
  const tierLabel = !watch ? t('docs.watch.watchOff')
    : watch.expired ? t('docs.watch.watchExpired')
      : watch.mode === 'apply_scoped' ? t('docs.watch.watchApply') : t('docs.watch.watchOff');

  return (
    <aside className="doc-workbench__rail" data-testid="doc-workbench-rail">
      <div className="doc-workbench__rail-tabs" role="tablist">
        {(['receipts', 'history'] as RailTab[]).map((k) => (
          <button key={k} type="button" role="tab" aria-selected={railTab === k} className={`doc-workbench__rail-tab${railTab === k ? ' doc-workbench__rail-tab--active' : ''}`} onClick={() => onTab(k)} data-testid="doc-workbench-rail-tab" data-variant={k}>
            {t(`docs.workbench.rail.${k}`)}
            {k === 'receipts' && receipts.length > 0 && <span className="rounded-full bg-bg-muted px-1.5 text-[10px] text-text-muted">{receipts.length}</span>}
          </button>
        ))}
        <span className="ml-auto" />
        <button type="button" className="doc-workbench__icon-btn" style={{ width: 26, height: 26 }} onClick={onHide} title={t('docs.workbench.toggleRail')} data-testid="doc-workbench-rail-hide">
          <PanelRightClose size={15} />
        </button>
      </div>

      {railTab === 'receipts' && (
        <div className="doc-workbench__rail-body" data-testid="doc-workbench-receipts">
          {/* No header. The tab strip above already names the document, and the
              document itself is on screen; the standing "this records, it does
              not undo" line said the same thing on every visit and cost two of
              the few lines this 258px rail has. The pointer to the platform's
              version history stays where someone goes looking for it: the
              panel's document record. This list is what the agent changed. */}
          {elsewhere.length > 0 && (
            <div className="doc-workbench__elsewhere" data-testid="doc-workbench-elsewhere">
              <span className="doc-workbench__tab-dot" style={{ background: 'var(--color-conversation-unread)' }} />
              <span className="flex-1">{t('docs.workbench.elsewhere', { n: elsewhere.reduce((a, x) => a + x.unread, 0) })}</span>
              {elsewhere.map((x) => (
                <a key={x.docId} className="inline-flex items-center gap-1 text-[11px] hover:underline" onClick={() => onJump(x.docId)}>
                  <KindIcon kind={x.kind} provider={x.provider} size={12} /> {x.title || x.docId} ·{x.unread}
                </a>
              ))}
            </div>
          )}
          <DetailTimeline
            dense
            docId={tab?.docId ?? ''}
            title={tab?.title}
            kind={tab?.kind}
            lineage={NO_LINEAGE}
            receipts={receipts}
            note={note ? <p className="text-xs text-danger" data-testid="doc-workbench-receipt-note">{note}</p> : null}
            onLocate={onLocate}
            onUnhighlight={(receiptId) => void act('clouddoc.unhighlight', receiptId)}
            actingOn={acting}
          />
        </div>
      )}

      {railTab === 'history' && (
        <div className="doc-workbench__rail-body" data-testid="doc-workbench-history">
          <div className="px-0.5 pb-1 text-[11px] text-text-muted">{t('docs.workbench.historyNote')}</div>
          <ChatTimelineList messages={messages} executions={executions ? Array.from(executions.values()) : []} mode={mode} disableA2UIInteraction />
        </div>
      )}

      {tab && (
        /* One row: what the watch is, until when, and the control that changes
           it. This used to be a red "stop and revoke" link, which named the same
           axis with a different word and gave the rail its own way out that the
           panel's switch did not have. It is the panel's switch now, with the
           panel's semantics: turning it on is the consequential act, so that
           direction asks twice; turning it off takes effect at once. */
        <div className="doc-workbench__rail-foot" data-testid="doc-workbench-rail-foot">
          <div className="flex items-center gap-2">
            <span className="doc-workbench__dot" style={{ background: watchOn ? 'var(--color-feedback-success)' : 'var(--color-context-track)' }} />
            <span>{t('docs.table.colTier')}</span>
            {watch?.expires_at && !watch.expired && (
              <span className="text-[11px] text-text-muted">
                {t('docs.usage.until')} {new Date(watch.expires_at * 1000).toLocaleDateString()}
              </span>
            )}
            <span className="ml-auto" />
            <button
              type="button"
              data-testid="doc-workbench-watch-toggle"
              data-on={watchOn ? '1' : '0'}
              title={armOn ? t('docs.watch.watchApplyConfirm') : t('docs.table.tierToggleHint')}
              onClick={() => {
                if (!watchOn && !armOn) {
                  setArmOn(true);
                  setTimeout(() => setArmOn(false), 5000);
                  return;
                }
                setArmOn(false);
                onWatch(watchOn ? 'off' : 'apply_scoped');
              }}
              className={`rounded-md px-1.5 py-0.5 text-[11px] font-medium hover:ring-1 hover:ring-border-strong ${
                armOn ? 'bg-accent-subtle text-accent'
                  : watchOn ? 'bg-danger-subtle text-danger' : 'bg-bg-muted text-text-muted'
              }`}
            >
              {armOn ? t('docs.watch.watchApplyConfirm') : tierLabel}
            </button>
          </div>
        </div>
      )}
    </aside>
  );
}
