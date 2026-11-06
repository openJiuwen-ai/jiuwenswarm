import { useEffect, useRef, useState } from 'react';
import { AppWindow } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import type { AppMarketPreview } from '../../stores/sessionStore';
import { Button } from '../ui/Button/Button';
import { toast } from '../ui/Toast/toastStore';
import { openAppMarketView, type InstanceInfo } from './appMarketViews';

type EditorApp = { app_id: string; name: string; editor_session_id: string };

export function AppMarketEditorAction({ sessionId, onOpen }: {
  sessionId: string;
  onOpen: (preview: AppMarketPreview) => void;
}) {
  const { t } = useTranslation();
  const [app, setApp] = useState<EditorApp | null>(null);
  const [loading, setLoading] = useState(false);
  const active = useRef(false);
  useEffect(() => {
    active.current = true;
    let current = true;
    void webRequest<{ apps: EditorApp[] }>('app_market.list').then(({ apps }) => {
      if (current) setApp(apps.find(item => item.editor_session_id === sessionId) ?? null);
    }).catch(() => undefined);
    return () => { current = false; active.current = false; };
  }, [sessionId]);

  if (!app) return null;
  return <Button variant="quiet" size="sm" icon={<AppWindow size={16} aria-hidden="true" />}
    data-testid="app-market-editor-open-preview" disabled={loading}
    title={t('appMarket.openPreview')} onClick={() => {
      if (loading) return;
      setLoading(true);
      void (async () => {
        const { instances } = await webRequest<{ instances: InstanceInfo[] }>('app_market.instances');
        let instance = instances.find(item => item.app_id === app.app_id && item.state === 'running');
        if (!instance) instance = await webRequest<InstanceInfo>('app_market.start', { app_id: app.app_id }, { timeoutMs: 120_000 });
        if (!active.current) return;
        if (instance.state !== 'running' || !await openAppMarketView(instance, app.name, true)) throw new Error('Preview unavailable');
        if (active.current) onOpen({ appId: app.app_id, name: app.name, instanceId: instance.instance_id });
      })().catch(() => {
        if (active.current) toast.open({ content: t('appMarket.openFailed'), variant: 'error' });
      }).finally(() => { if (active.current) setLoading(false); });
    }}>{t('appMarket.openPreview')}</Button>;
}
