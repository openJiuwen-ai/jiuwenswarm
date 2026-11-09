import { useEffect, useRef, useState } from 'react';
import { CircleAlert, Loader2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import './SystemPluginRecoverySettings.css';

const RECOVERY_ROUTE = '/system-plugin-recovery';
const ENDPOINT_PATH = `${RECOVERY_ROUTE}/endpoint`;
const MAX_ENDPOINT_ATTEMPTS = 10;
const ENDPOINT_RETRY_DELAY_MS = 500;
const RECOVERY_HEIGHT_MESSAGE = 'jiuwenswarm.system-plugin-recovery.height';
const INITIAL_FRAME_HEIGHT = 640;
const MAX_FRAME_HEIGHT = 100_000;

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

function recoveryFrameHeight(value: unknown): number | null {
  if (!value || typeof value !== 'object') return null;
  const message = value as { type?: unknown; height?: unknown };
  if (message.type !== RECOVERY_HEIGHT_MESSAGE || typeof message.height !== 'number') return null;
  if (!Number.isFinite(message.height) || message.height <= 0 || message.height > MAX_FRAME_HEIGHT) return null;
  return Math.ceil(message.height);
}

export function SystemPluginRecoverySettings() {
  const { t, i18n } = useTranslation();
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [frameHeight, setFrameHeight] = useState(INITIAL_FRAME_HEIGHT);
  const [loading, setLoading] = useState(true);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    if (!url) return undefined;
    const expectedOrigin = new URL(url).origin;
    const updateFrameHeight = (event: MessageEvent<unknown>) => {
      if (event.origin !== expectedOrigin || event.source !== frameRef.current?.contentWindow) return;
      const nextHeight = recoveryFrameHeight(event.data);
      if (nextHeight !== null) setFrameHeight(current => current === nextHeight ? current : nextHeight);
    };
    window.addEventListener('message', updateFrameHeight);
    return () => window.removeEventListener('message', updateFrameHeight);
  }, [url]);

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
          setFrameHeight(INITIAL_FRAME_HEIGHT);
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
          ref={frameRef}
          referrerPolicy="no-referrer"
          scrolling="no"
          src={url}
          style={{ height: `${frameHeight}px` }}
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
