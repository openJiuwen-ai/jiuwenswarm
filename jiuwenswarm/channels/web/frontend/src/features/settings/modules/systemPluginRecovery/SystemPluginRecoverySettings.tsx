import { useEffect, useState } from 'react';
import { CircleAlert, Loader2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import './SystemPluginRecoverySettings.css';

const RECOVERY_ROUTE = '/system-plugin-recovery';
const ENDPOINT_PATH = `${RECOVERY_ROUTE}/endpoint`;
const MAX_ENDPOINT_ATTEMPTS = 10;
const ENDPOINT_RETRY_DELAY_MS = 500;

function recoveryPageUrl(value: unknown, locale: string): string | null {
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value);
    if (url.protocol !== 'http:') return null;
    if (url.hostname !== '127.0.0.1') return null;
    if (!url.port) return null;
    if (url.pathname !== RECOVERY_ROUTE) return null;
    url.searchParams.set('locale', locale);
    return url.toString();
  } catch {
    return null;
  }
}

function navigateToRecoveryRoute(): void {
  if (window.location.pathname === RECOVERY_ROUTE) return;
  window.history.pushState(null, '', RECOVERY_ROUTE);
  window.dispatchEvent(new PopStateEvent('popstate'));
}

export function SystemPluginRecoverySettings() {
  const { t, i18n } = useTranslation();
  const [url, setUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    navigateToRecoveryRoute();
    if (window.location.pathname !== RECOVERY_ROUTE) return undefined;

    let cancelled = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    const loadEndpoint = async (attempt: number) => {
      try {
        const response = await fetch(ENDPOINT_PATH, {
          cache: 'no-store',
          credentials: 'same-origin',
        });
        const payload = response.ok ? await response.json() as { url?: unknown } : null;
        const pageUrl = recoveryPageUrl(payload?.url, i18n.language);
        if (!pageUrl) throw new Error('recovery sidecar endpoint is unavailable');
        if (!cancelled) {
          setUrl(pageUrl);
          setLoading(false);
        }
      } catch {
        if (cancelled) return;
        if (attempt < MAX_ENDPOINT_ATTEMPTS) {
          retryTimer = setTimeout(() => void loadEndpoint(attempt + 1), ENDPOINT_RETRY_DELAY_MS);
          return;
        }
        setLoading(false);
        setUnavailable(true);
      }
    };

    void loadEndpoint(1);
    return () => {
      cancelled = true;
      if (retryTimer !== undefined) clearTimeout(retryTimer);
    };
  }, [i18n.language]);

  return (
    <section className="system-plugin-recovery" data-testid="settings-system-plugin-recovery">
      {url ? (
        <iframe
          className="system-plugin-recovery__frame"
          data-testid="system-plugin-recovery-frame"
          referrerPolicy="no-referrer"
          src={url}
          title={t('settingsPanel.categories.systemPluginRecovery')}
        />
      ) : loading ? (
        <div className="system-plugin-recovery__loading" role="status" data-testid="system-plugin-recovery-loading">
          <Loader2 aria-hidden="true" size={16} />
          {t('common.loading')}
        </div>
      ) : unavailable ? (
        <div className="system-plugin-recovery__notice" role="alert" data-testid="system-plugin-recovery-unavailable">
          <CircleAlert aria-hidden="true" size={16} />
          {t('settingsPanel.systemPluginRecovery.unavailable')}
        </div>
      ) : null}
    </section>
  );
}
