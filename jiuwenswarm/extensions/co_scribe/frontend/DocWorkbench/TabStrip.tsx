import { useTranslation } from 'react-i18next';
import { X, ExternalLink, RefreshCw, PanelRight, MessageSquare, LogOut } from 'lucide-react';
import type { WorkbenchTab } from '../stores/docWorkbenchStore';
import { KindIcon } from './KindIcon';

// One tier, one colour. The watch is binary now (off / apply_scoped), so an
// unknown key means no dot rather than a second colour.
const TIER_DOT: Record<string, string> = {
  apply_scoped: 'var(--color-feedback-success)',
};

export function TabStrip({
  tabs, activeDocId, tiers, railVisible, chatVisible, canOpenExternal,
  onActivate, onClose, onOpenExternal, onReload, onToggleRail, onToggleChat, onExit,
}: {
  tabs: WorkbenchTab[];
  activeDocId: string | null;
  tiers: Record<string, string | undefined>;
  railVisible: boolean;
  chatVisible: boolean;
  canOpenExternal: boolean;
  onActivate: (docId: string) => void;
  onClose: (docId: string) => void;
  onOpenExternal: () => void;
  onReload: () => void;
  onToggleRail: () => void;
  onToggleChat: () => void;
  onExit: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="doc-workbench__tabbar" data-testid="doc-workbench-tabs" role="tablist">
      {/* The tabs scroll; the controls do not. Without this track the row was one
          flex line, so a seventh document pushed 退出编辑 and the panel toggles out
          of the bar and over the side rail's own header. Which controls a person can
          reach must not depend on how many documents they happen to have open. */}
      <div className="doc-workbench__tabs">
      {tabs.map((tab) => {
        const active = tab.docId === activeDocId;
        return (
          <button
            key={tab.docId}
            type="button"
            role="tab"
            aria-selected={active}
            className={`doc-workbench__tab${active ? ' doc-workbench__tab--active' : ''}`}
            data-testid="doc-workbench-tab"
            data-variant={tab.docId}
            onClick={() => onActivate(tab.docId)}
            title={tab.title}
          >
            <KindIcon kind={tab.kind} provider={tab.provider} size={14} />
            <span className="max-w-[180px] truncate">{tab.title || tab.docId}</span>
            <span
              className="doc-workbench__tab-dot"
              style={{ background: TIER_DOT[tiers[tab.docId] ?? ''] ?? 'var(--color-context-track)' }}
              title={tiers[tab.docId] ?? t('docs.watch.watchOff')}
            />
            {tab.unread > 0 && (
              <span
                className="doc-workbench__tab-dot"
                style={{ background: 'var(--color-conversation-unread)' }}
                data-testid="doc-workbench-tab-unread"
                title={t('docs.workbench.newReceipts', { n: tab.unread })}
              />
            )}
            <span
              className="doc-workbench__tab-close"
              role="button"
              aria-label={t('docs.workbench.closeTab')}
              data-testid="doc-workbench-tab-close"
              onClick={(e) => { e.stopPropagation(); onClose(tab.docId); }}
            >
              <X size={12} />
            </span>
          </button>
        );
      })}
      </div>
      <div className="doc-workbench__actions">
        <button type="button" className="doc-workbench__text-btn" onClick={onOpenExternal} disabled={!canOpenExternal} data-testid="doc-workbench-open-external">
          <ExternalLink size={13} /> {t('docs.workbench.openExternal')}
        </button>
        <button type="button" className="doc-workbench__icon-btn" onClick={onReload} title={t('docs.workbench.reload')} data-testid="doc-workbench-reload">
          <RefreshCw size={16} />
        </button>
        <button type="button" className={`doc-workbench__icon-btn${chatVisible ? ' doc-workbench__icon-btn--on' : ''}`} onClick={onToggleChat} title={t('docs.workbench.toggleChat')} data-testid="doc-workbench-toggle-chat">
          <MessageSquare size={16} />
        </button>
        <button type="button" className={`doc-workbench__icon-btn${railVisible ? ' doc-workbench__icon-btn--on' : ''}`} onClick={onToggleRail} title={t('docs.workbench.toggleRail')} data-testid="doc-workbench-toggle-rail">
          <PanelRight size={16} />
        </button>
        <div className="h-5 w-px bg-border" />
        <button type="button" className="doc-workbench__icon-btn" onClick={onExit} title={t('docs.workbench.exit')} data-testid="doc-workbench-exit" style={{ width: 'auto', padding: '0 10px', gap: 6, fontSize: 12 }}>
          <LogOut size={13} /> {t('docs.workbench.exit')}
        </button>
      </div>
    </div>
  );
}
