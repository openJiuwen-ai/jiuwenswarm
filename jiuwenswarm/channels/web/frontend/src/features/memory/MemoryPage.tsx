import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Loading } from '../../components/ui';
import type { SettingsRequest } from '../settings/services/settingsContract';
import './MemoryPage.css';

type PortalResult = {
  status: 'ready' | 'disabled' | 'unavailable';
  agent?: string;
  code?: string;
  url?: string;
};

export function MemoryPage({ isConnected, request }: { isConnected: boolean; request: SettingsRequest }) {
  const { t } = useTranslation();
  const [result, setResult] = useState<PortalResult | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let current = true;
    setResult(null);
    setLoading(false);
    if (!isConnected) return;
    setLoading(true);
    void request<PortalResult>('memory.portal.open', {}, { timeoutMs: 45_000 })
      .then((next) => { if (current) setResult(next); })
      .catch(() => {
        if (current) setResult({ status: 'unavailable', code: 'portal_unavailable' });
      })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [isConnected, attempt, request]);

  const code = !isConnected ? 'disconnected' : result?.code ?? 'not_enabled';
  const knownCodes = ['disconnected', 'not_enabled', 'dependency_unavailable', 'timeout', 'portal_unavailable'];
  const knownCode = knownCodes.includes(code) ? code : 'portal_unavailable';
  return (
    <section className="memory-page" data-testid="memory-page" data-variant={loading ? 'loading' : result?.status ?? 'idle'}>
      <header className="memory-page-header" data-testid="memory-page-header">
        <h1 data-testid="memory-page-title">{t('nav.memory')}</h1>
        <Button disabled={!isConnected || loading} onClick={() => setAttempt((value) => value + 1)}
          data-testid="memory-page-reconnect">
          {t('settingsPanel.memory.reconnect')}
        </Button>
      </header>
      {loading ? <Loading aria-label={t('common.loading')} /> : null}
      {!loading && (!isConnected || result?.status !== 'ready') ? (
        <p role="status" data-testid="memory-page-status">{t(`settingsPanel.memory.errors.${knownCode}`)}</p>
      ) : null}
      {isConnected && result?.status === 'ready' && result.url ? (
        <iframe className="memory-page-frame" src={result.url} title={t('nav.memory')}
          referrerPolicy="no-referrer" sandbox="allow-scripts allow-same-origin allow-forms"
          data-testid="memory-page-frame" />
      ) : null}
    </section>
  );
}
