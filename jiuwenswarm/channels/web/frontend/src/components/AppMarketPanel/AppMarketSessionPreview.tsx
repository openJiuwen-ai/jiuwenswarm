import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useSessionStore, type AppMarketPreview } from '../../stores/sessionStore';
import { toast } from '../ui/Toast/toastStore';
import { Button } from '../ui/Button/Button';
import { AppMarketBrowserPane } from './AppMarketBrowserPane';
import { hasAppMarketView, readAppMarketInstances, watchAppMarketInstances } from './appMarketViews';
import { webAppViews } from './webAppViews';
import './AppMarketPanel.css';

export function AppMarketSessionPreview({ sessionId, preview }: {
  sessionId: string;
  preview: AppMarketPreview;
}) {
  const { t } = useTranslation();
  const [ready, setReady] = useState(!window.jiuwenDesktop?.isElectron);
  const reportError = () => toast.open({ content: t('appMarket.openFailed'), variant: 'error' });

  useEffect(() => {
    if (window.jiuwenDesktop?.isElectron) setReady(false);
    let active = true;
    let prepared = false;
    const poll = async () => {
      const instances = await readAppMarketInstances();
      if (!active) return;
      if (!hasAppMarketView(preview.appId, preview.instanceId)) {
        useSessionStore.getState().setAppMarketPreview(sessionId, undefined);
        return;
      }
      const desktop = window.jiuwenDesktop;
      if (desktop?.isElectron && !prepared && instances) {
        prepared = true;
        try {
          const instance = instances.find(item => item.instance_id === preview.instanceId);
          if (!instance?.url || !desktop.openMarketApp
            || !await desktop.openMarketApp(preview.appId, instance.url, preview.name, true)) {
            throw new Error('Application preview unavailable');
          }
          if (active) setReady(true);
        } catch {
          if (active) {
            useSessionStore.getState().setAppMarketPreview(sessionId, undefined);
            toast.open({ content: t('appMarket.openFailed'), variant: 'error' });
          }
        }
      }
    };
    void poll().catch(() => undefined);
    const unwatch = watchAppMarketInstances(poll);
    return () => { active = false; unwatch(); };
  }, [sessionId, preview.appId, preview.instanceId, preview.name, t]);

  return (
    <div className="app-market-layout" data-testid="app-market-session-preview" data-variant={preview.appId}>
      {ready ? <AppMarketBrowserPane appId={preview.appId} name={preview.name} onError={reportError} />
        : <p role="status" data-testid="app-market-preview-loading">{t('appMarket.loading')}</p>}
    </div>
  );
}

export function AppMarketPreviewReload({ preview }: { preview: AppMarketPreview }) {
  const { t } = useTranslation();
  const [loading, setLoading] = useState(false);
  return (
    <Button variant="quiet" size="sm" icon={<RefreshCw size={16} aria-hidden="true" />}
      data-testid="app-market-preview-reload" aria-label={t('appMarket.reloadPreview')}
      title={t('appMarket.reloadPreview')} disabled={loading} onClick={() => {
        if (loading) return;
        setLoading(true);
        void Promise.resolve().then(() => {
          if (!hasAppMarketView(preview.appId, preview.instanceId)) throw new Error('Application instance changed');
          return window.jiuwenDesktop?.isElectron
            ? window.jiuwenDesktop.reloadMarketApp?.(preview.appId)
            : webAppViews.reload(preview.appId);
        }).then(reloaded => {
          if (!reloaded) throw new Error('Application preview unavailable');
        }).catch(() => toast.open({ content: t('appMarket.reloadFailed'), variant: 'error' }))
          .finally(() => setLoading(false));
      }} />
  );
}
