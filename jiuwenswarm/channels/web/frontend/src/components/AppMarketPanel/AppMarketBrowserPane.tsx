import { useEffect, useRef, useState } from 'react';
import { Maximize2, Minimize2, Minus, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '../ui/Button/Button';
import { webAppViews } from './webAppViews';

export function AppMarketBrowserPane({ appId, name, expanded, onExpand, onHide, onClose, onError }: {
  appId: string;
  name: string;
  expanded: boolean;
  onExpand: () => void;
  onHide: () => void;
  onClose: () => void;
  onError: (error: unknown) => void;
}) {
  const { t } = useTranslation();
  const viewport = useRef<HTMLDivElement>(null);
  const reportError = useRef(onError);
  reportError.current = onError;
  const [hostError, setHostError] = useState(false);

  useEffect(() => {
    const setViewport = webAppViews.setViewport;
    let active = true;
    let frame = 0;
    let firstLayout = true;
    const sync = () => {
      frame = 0;
      const node = viewport.current;
      if (!active || !node) return;
      // Hide the floating app iframe while a modal covers the page.
      const modal = document.querySelector('[role="dialog"][aria-modal="true"], dialog[open]');
      const rect = node.getBoundingClientRect();
      const bounds = modal ? null : { x: rect.left, y: rect.top, width: rect.width, height: rect.height };
      const focus = firstLayout && !!bounds && rect.width > 0 && rect.height > 0;
      if (focus) firstLayout = false;
      void Promise.resolve().then(() => active && setViewport(appId, bounds, focus)).catch(error => {
        if (active) { setHostError(true); reportError.current(error); }
      });
    };
    const schedule = () => { if (!frame) frame = window.requestAnimationFrame(sync); };
    const resize = new ResizeObserver(schedule);
    if (viewport.current) resize.observe(viewport.current);
    const modals = new MutationObserver(schedule);
    modals.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['aria-modal', 'open'] });
    window.addEventListener('resize', schedule);
    window.addEventListener('scroll', schedule, true);
    schedule();
    return () => {
      active = false;
      if (frame) window.cancelAnimationFrame(frame);
      resize.disconnect();
      modals.disconnect();
      window.removeEventListener('resize', schedule);
      window.removeEventListener('scroll', schedule, true);
      void Promise.resolve(setViewport(appId, null)).catch(() => undefined);
    };
  }, [appId]);

  return (
    <section className="app-market-browser-pane" aria-label={name} data-testid="app-market-browser-pane" data-variant={appId}>
      <div className="app-market-browser-toolbar" data-testid="app-market-browser-toolbar">
        <span className="min-w-0 flex-1 truncate" data-testid="app-market-browser-title">{name} · WorkSwarm</span>
        <Button size="sm" variant="quiet" icon={expanded ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
          aria-label={t(expanded ? 'appMarket.restorePane' : 'appMarket.expandPane')}
          title={t(expanded ? 'appMarket.restorePane' : 'appMarket.expandPane')}
          data-testid="app-market-browser-expand" onClick={onExpand} />
        <Button size="sm" variant="quiet" icon={<Minus size={16} />} aria-label={t('appMarket.hidePane')}
          title={t('appMarket.hidePane')} data-testid="app-market-browser-hide" onClick={onHide} />
        <Button size="sm" variant="quiet" icon={<X size={16} />} aria-label={t('appMarket.closePane')}
          title={t('appMarket.closePane')} data-testid="app-market-browser-close" onClick={onClose} />
      </div>
      <div ref={viewport} className="app-market-browser-viewport" data-testid="app-market-browser-viewport">
        {hostError && <p role="alert" data-testid="app-market-browser-error">{t('appMarket.openFailed')}</p>}
      </div>
    </section>
  );
}
