/**
 * The platform's own editor in a frame. The app cannot read a cross-origin
 * frame's address, so a login redirect (which refuses to render in a frame)
 * looks the same as a slow load: a blank frame. The fallback is therefore
 * always offered from the status bar, never detected.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ExternalLink, RefreshCw, Lock } from 'lucide-react';
import type { WorkbenchTab } from '../stores/docWorkbenchStore';
import { providerLabel } from '../features/clouddoc/provider';
import { isEmbeddableDocUrl } from '../features/clouddoc/docUrl';

export function DocFrame({ tab, nonce, anchor = '', alwaysNewTab, onAlwaysNewTab, onReload }: {
  tab: WorkbenchTab;
  nonce: number;
  /** URL fragment from a receipt's locate (`#gid=…&range=…`, `#slide=id.…`). */
  anchor?: string;
  alwaysNewTab: boolean;
  onAlwaysNewTab: (v: boolean) => void;
  onReload: () => void;
}) {
  const { t } = useTranslation();
  const [help, setHelp] = useState(false);
  const external = tab.url && tab.url.startsWith('http') ? tab.url : '';
  // Only a platform-host link is framed; anything else is launched externally.
  const embeddable = isEmbeddableDocUrl(external);
  // The frame's address: the document's link with any old fragment replaced by
  // the located region's, so "locate" lands where the receipt wrote.
  const frameSrc = external ? (anchor ? `${external.split('#')[0]}${anchor}` : external) : '';
  const platformName = providerLabel(tab.provider, tab.providerName, t);

  if (!external || alwaysNewTab || !embeddable) {
    return (
      <div className="doc-workbench__body">
        <div className="flex flex-1 items-center justify-center">
          <div className="flex w-[440px] flex-col gap-3 rounded-xl border border-border bg-card p-7 shadow-md">
            <p className="text-sm font-semibold text-text-strong">{external ? t('docs.workbench.launcherTitle') : t('docs.linkUnknown')}</p>
            {external && (
              <a href={external} target="_blank" rel="noreferrer" className="inline-flex h-9 w-max items-center gap-2 rounded-lg bg-accent px-3.5 text-[13px] font-medium text-accent-foreground" data-testid="doc-workbench-launcher-open">
                <ExternalLink size={14} /> {t('docs.workbench.openExternal')}
              </a>
            )}
            {external && alwaysNewTab && (
              <button type="button" className="w-max text-xs text-text-link hover:underline" onClick={() => onAlwaysNewTab(false)} data-testid="doc-workbench-embed-again">
                {t('docs.workbench.embedAgain')}
              </button>
            )}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="doc-workbench__body">
      <iframe
        key={`${tab.docId}:${nonce}:${anchor}`}
        title={tab.title || tab.docId}
        src={frameSrc}
        className="doc-workbench__frame"
        referrerPolicy="no-referrer"
        allow="clipboard-read; clipboard-write"
        data-testid="doc-workbench-frame"
      />
      {help && (
        <div className="doc-workbench__fallback" data-testid="doc-workbench-login-help" onClick={() => setHelp(false)}>
          <div className="flex w-[440px] flex-col gap-3.5 rounded-xl border border-border bg-card p-7 shadow-md" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-2.5">
              <span className="flex h-9 w-9 items-center justify-center rounded-full bg-warn-subtle text-warn"><Lock size={18} /></span>
              <span className="text-base font-semibold text-text-strong">{t('docs.workbench.loginTitle', { platform: platformName })}</span>
            </div>
            <p className="m-0 text-[13px] leading-relaxed text-text-muted">{t('docs.workbench.loginBody', { platform: platformName })}</p>
            <div className="flex items-center gap-2.5 pt-1">
              <a href={external} target="_blank" rel="noreferrer" className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-accent px-3.5 text-[13px] font-medium text-accent-foreground">
                <ExternalLink size={14} /> {t('docs.workbench.loginOpen')}
              </a>
              <button type="button" className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-border bg-card px-3.5 text-[13px]" onClick={() => { setHelp(false); onReload(); }}>
                <RefreshCw size={14} /> {t('docs.workbench.loginRetry')}
              </button>
              <span className="flex-1" />
              <button type="button" className="text-xs text-text-link hover:underline" onClick={() => onAlwaysNewTab(true)} data-testid="doc-workbench-always-external">
                {t('docs.workbench.alwaysExternal')}
              </button>
            </div>
          </div>
        </div>
      )}
      <div className="doc-workbench__statusbar" data-testid="doc-workbench-statusbar">
        <span className="doc-workbench__dot" style={{ background: 'var(--color-feedback-success)' }} />
        <a onClick={() => setHelp(true)} data-testid="doc-workbench-cant-see">{t('docs.workbench.cantSee')}</a>
        <span>·</span>
        <span>{t('docs.workbench.reloadsOnReceipt')}</span>
      </div>
    </div>
  );
}
