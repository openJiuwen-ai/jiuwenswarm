import { useEffect, useRef, useState } from 'react';
import { CircleAlert, Loader2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import './SystemPluginRecoverySettings.css';

const RECOVERY_ROUTE = '/system-plugin-recovery';
const ENDPOINT_PATH = `${RECOVERY_ROUTE}/endpoint`;
const MAX_ENDPOINT_ATTEMPTS = 10;
const ENDPOINT_RETRY_DELAY_MS = 500;
const RECOVERY_HEIGHT_MESSAGE = 'jiuwenswarm.system-plugin-recovery.height';
const RECOVERY_SELECTED_RECORD_MESSAGE = 'jiuwenswarm.system-plugin-recovery.selected-record';
const RECOVERY_OPEN_CONFIRM_COMMAND_MESSAGE = 'jiuwenswarm.system-plugin-recovery.open-confirm';
const RECOVERY_DIALOG_OPENED_MESSAGE = 'jiuwenswarm.system-plugin-recovery.dialog-opened';
const RECOVERY_DIALOG_CLOSED_MESSAGE = 'jiuwenswarm.system-plugin-recovery.dialog-closed';
const RECOVERY_DIALOG_POSITION_MESSAGE = 'jiuwenswarm.system-plugin-recovery.dialog-position';
const INITIAL_FRAME_HEIGHT = 640;
const MAX_FRAME_HEIGHT = 100_000;

type RecoveryMessage = {
  type?: unknown;
  height?: unknown;
  recordId?: unknown;
  restoreLabel?: unknown;
  title?: unknown;
  description?: unknown;
  centerY?: unknown;
};

function recoveryPageUrl(value: unknown, locale: string): string | null {
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value);
    if (url.protocol !== 'http:') return null;
    if (url.hostname !== '127.0.0.1') return null;
    if (!url.port) return null;
    if (url.pathname !== RECOVERY_ROUTE) return null;
    url.searchParams.set('locale', locale);
    url.searchParams.set('parent_origin', window.location.origin);
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

function recoveryMessage(value: unknown): RecoveryMessage | null {
  if (!value || typeof value !== 'object') return null;
  return value as RecoveryMessage;
}

function recoveryFrameHeight(value: unknown): number | null {
  const message = recoveryMessage(value);
  if (message?.type !== RECOVERY_HEIGHT_MESSAGE || typeof message.height !== 'number') return null;
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
  const [selectedRecordId, setSelectedRecordId] = useState<string | null>(null);
  const [restoreLabel, setRestoreLabel] = useState('');
  const [pageTitle, setPageTitle] = useState('');
  const [pageDescription, setPageDescription] = useState('');
  const [dialogOpen, setDialogOpen] = useState(false);

  useEffect(() => {
    if (!url) return undefined;
    const expectedOrigin = new URL(url).origin;
    const receiveRecoveryMessage = (event: MessageEvent<unknown>) => {
      if (event.origin !== expectedOrigin || event.source !== frameRef.current?.contentWindow) return;
      const message = recoveryMessage(event.data);
      const nextHeight = recoveryFrameHeight(message);
      if (nextHeight !== null) {
        setFrameHeight(current => current === nextHeight ? current : nextHeight);
        return;
      }
      if (message?.type === RECOVERY_SELECTED_RECORD_MESSAGE) {
        setSelectedRecordId(typeof message.recordId === 'string' ? message.recordId : null);
        if (typeof message.restoreLabel === 'string') setRestoreLabel(message.restoreLabel);
        if (typeof message.title === 'string') setPageTitle(message.title);
        if (typeof message.description === 'string') setPageDescription(message.description);
        return;
      }
      if (message?.type === RECOVERY_DIALOG_OPENED_MESSAGE) {
        setDialogOpen(true);
        return;
      }
      if (message?.type === RECOVERY_DIALOG_CLOSED_MESSAGE) {
        setDialogOpen(false);
      }
    };
    window.addEventListener('message', receiveRecoveryMessage);
    return () => window.removeEventListener('message', receiveRecoveryMessage);
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
          setSelectedRecordId(null);
          setRestoreLabel('');
          setPageTitle('');
          setPageDescription('');
          setDialogOpen(false);
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

  useEffect(() => {
    if (!url || !dialogOpen) return undefined;
    const targetOrigin = new URL(url).origin;
    const updateDialogPosition = () => {
      const frame = frameRef.current;
      const frameWindow = frame?.contentWindow;
      if (!frame || !frameWindow) return;
      const bounds = frame.getBoundingClientRect();
      const visibleTop = Math.max(bounds.top, 0);
      const visibleBottom = Math.min(bounds.bottom, window.innerHeight);
      if (visibleBottom <= visibleTop) return;
      const centerY = Math.round((visibleTop + visibleBottom) / 2 - bounds.top);
      frameWindow.postMessage({ type: RECOVERY_DIALOG_POSITION_MESSAGE, centerY }, targetOrigin);
    };
    updateDialogPosition();
    window.addEventListener('resize', updateDialogPosition);
    window.addEventListener('scroll', updateDialogPosition, true);
    return () => {
      window.removeEventListener('resize', updateDialogPosition);
      window.removeEventListener('scroll', updateDialogPosition, true);
    };
  }, [dialogOpen, url]);

  const openRestoreConfirm = () => {
    if (!selectedRecordId || !url) return;
    const frameWindow = frameRef.current?.contentWindow;
    if (!frameWindow) return;
    frameWindow.postMessage(
      { type: RECOVERY_OPEN_CONFIRM_COMMAND_MESSAGE, recordId: selectedRecordId },
      new URL(url).origin,
    );
  };

  return (
    <section className="system-plugin-recovery" data-testid="settings-system-plugin-recovery">
      {url ? (
        <>
          <div className="system-plugin-recovery__actions" data-testid="settings-system-plugin-recovery-actions">
            <div className="system-plugin-recovery__actions-content">
              <div className="system-plugin-recovery__actions-copy">
                <h1 className="system-plugin-recovery__title" data-testid="settings-system-plugin-recovery-title">
                  {pageTitle || t('settingsPanel.categories.systemPluginRecovery')}
                </h1>
                <p className="system-plugin-recovery__description" data-testid="settings-system-plugin-recovery-description">
                  {pageDescription}
                </p>
              </div>
              <button
                type="button"
                className="system-plugin-recovery__restore"
                data-testid="settings-system-plugin-recovery-restore"
                disabled={!selectedRecordId || !restoreLabel}
                onClick={openRestoreConfirm}
              >
                {restoreLabel}
              </button>
            </div>
          </div>
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
        </>
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
