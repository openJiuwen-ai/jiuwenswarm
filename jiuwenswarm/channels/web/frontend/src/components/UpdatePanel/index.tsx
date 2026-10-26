import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

interface UpdatePanelProps {
  isConnected: boolean;
  request: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>;
}

interface UpdateStatusPayload {
  current_version?: unknown;
  latest_version?: unknown;
  state?: unknown;
  has_update?: unknown;
  install_mode?: unknown;
  matched_asset?: unknown;
  release_notes?: unknown;
  published_at?: unknown;
  downloaded_path?: unknown;
  downloaded_bytes?: unknown;
  total_bytes?: unknown;
  current_activity?: unknown;
  restart_command?: unknown;
  error?: unknown;
  platform_supported?: unknown;
}

interface UpdaterConfigPayload {
  release_api_type?: unknown;
  release_api_url?: unknown;
}

function normalizeString(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function normalizeBoolean(value: unknown): boolean {
  return value === true;
}

function normalizeNumber(value: unknown): number {
  const num = Number(value);
  return Number.isFinite(num) ? num : 0;
}

function formatBytes(bytes: number): string {
  if (bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 10 || unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function formatPublishedAt(value: string, locale: string): string {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(locale.startsWith('zh') ? 'zh-CN' : 'en-US', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

const UPDATER_STATUS_EVENT = 'jiuwenswarm:updater-status';

export function UpdatePanel({ isConnected, request }: UpdatePanelProps) {
  const { t, i18n } = useTranslation();
  const [status, setStatus] = useState<UpdateStatusPayload | null>(null);
  const [config, setConfig] = useState<UpdaterConfigPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [checking, setChecking] = useState(false);
  const [resettingSource, setResettingSource] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refreshStatus = useCallback(async () => {
    try {
      const payload = await request<UpdateStatusPayload>('updater.get_status');
      setStatus(payload);
      setError(normalizeString(payload?.error) || null);
      return payload;
    } catch (fetchError) {
      setError(fetchError instanceof Error ? fetchError.message : t('updatePanel.errors.loadFailed'));
      return null;
    }
  }, [request, t]);

  const refreshConfig = useCallback(async () => {
    try {
      const payload = await request<UpdaterConfigPayload>('updater.get_conf');
      setConfig(payload);
      return payload;
    } catch (fetchError) {
      setError(fetchError instanceof Error ? fetchError.message : t('updatePanel.errors.loadConfigFailed'));
      return null;
    }
  }, [request, t]);

  useEffect(() => {
    setLoading(true);
    void Promise.all([refreshStatus(), refreshConfig()]).finally(() => setLoading(false));
  }, [refreshConfig, refreshStatus]);

  useEffect(() => {
    const handleUpdaterStatus = (event: Event) => {
      const payload = (event as CustomEvent<UpdateStatusPayload>).detail;
      if (!payload || typeof payload !== 'object') {
        return;
      }
      setStatus(payload);
      setError(normalizeString(payload.error) || null);
    };
    window.addEventListener(UPDATER_STATUS_EVENT, handleUpdaterStatus);
    return () => {
      window.removeEventListener(UPDATER_STATUS_EVENT, handleUpdaterStatus);
    };
  }, []);

  useEffect(() => {
    if (
      normalizeString(status?.state) !== 'checking' &&
      normalizeString(status?.state) !== 'downloading' &&
      normalizeString(status?.state) !== 'upgrading' &&
      normalizeString(status?.state) !== 'installing'
    ) {
      return;
    }
    const timer = window.setInterval(() => {
      void refreshStatus();
    }, 1000);
    return () => {
      window.clearInterval(timer);
    };
  }, [refreshStatus, status?.state]);

  const handleCheck = useCallback(async () => {
    if (!isConnected || checking || normalizeString(status?.state) === 'downloading') return;
    setChecking(true);
    setError(null);
    try {
      const payload = await request<UpdateStatusPayload>('updater.check', { manual: true });
      setStatus(payload);
      setError(normalizeString(payload?.error) || null);
    } catch (checkError) {
      setError(checkError instanceof Error ? checkError.message : t('updatePanel.errors.checkFailed'));
    } finally {
      setChecking(false);
    }
  }, [checking, isConnected, status, request, t]);

  const handleDownload = useCallback(async () => {
    if (!isConnected) return;
    setError(null);
    try {
      const payload = await request<UpdateStatusPayload>('updater.download');
      setStatus(payload);
      setError(normalizeString(payload?.error) || null);
    } catch (downloadError) {
      setError(downloadError instanceof Error ? downloadError.message : t('updatePanel.errors.downloadFailed'));
    }
  }, [isConnected, request, t]);

  const handleResetSource = useCallback(async () => {
    if (resettingSource) return;
    setResettingSource(true);
    setError(null);
    try {
      const payload = await request<UpdaterConfigPayload>('updater.reset_source');
      setConfig(payload);
    } catch (resetError) {
      setError(resetError instanceof Error ? resetError.message : t('updatePanel.errors.resetSourceFailed'));
    } finally {
      setResettingSource(false);
    }
  }, [request, resettingSource, t]);

  const handleInstall = useCallback(async () => {
    const installerPath = normalizeString(status?.downloaded_path);
    const api = (window as Window & { pywebview?: { api?: { install_update?: (path: string) => Promise<boolean> | boolean } } }).pywebview?.api;
    if (!installerPath || !api?.install_update) {
      setError(t('updatePanel.errors.installUnavailable'));
      return;
    }
    // Optimistically switch to "installing" so the UI reflects the in-progress
    // state before the desktop app closes the window.
    setStatus((prev) => ({ ...(prev ?? {}), state: 'installing', installing: true }));
    try {
      const ok = await api.install_update(installerPath);
      if (!ok) {
        setError(t('updatePanel.errors.installFailed'));
        setStatus((prev) => ({ ...(prev ?? {}), state: 'downloaded', installing: false }));
      }
    } catch (installError) {
      setError(installError instanceof Error ? installError.message : t('updatePanel.errors.installFailed'));
      setStatus((prev) => ({ ...(prev ?? {}), state: 'downloaded', installing: false }));
    }
  }, [status?.downloaded_path, t]);

  const handleUpgrade = useCallback(async () => {
    if (!isConnected) return;
    setError(null);
    try {
      const payload = await request<UpdateStatusPayload>('updater.upgrade');
      setStatus(payload);
      setError(normalizeString(payload?.error) || null);
    } catch (upgradeError) {
      setError(upgradeError instanceof Error ? upgradeError.message : t('updatePanel.errors.upgradeFailed'));
    }
  }, [isConnected, request, t]);

  const state = normalizeString(status?.state) || 'idle';
  const hasUpdate = normalizeBoolean(status?.has_update);
  const installMode = normalizeString(status?.install_mode) || 'desktop';
  const isPipMode = installMode === 'pip';
  const currentVersion = normalizeString(status?.current_version) || '-';
  const latestVersion = normalizeString(status?.latest_version) || '-';
  const releaseNotes = normalizeString(status?.release_notes);
  const publishedAt = formatPublishedAt(normalizeString(status?.published_at), i18n.language);
  const downloadedBytes = normalizeNumber(status?.downloaded_bytes);
  const totalBytes = normalizeNumber(status?.total_bytes);
  const currentActivity = normalizeString(status?.current_activity);
  const restartCommand = normalizeString(status?.restart_command);
  const progress = useMemo(() => {
    if (totalBytes <= 0) return 0;
    return Math.max(0, Math.min(100, Math.round((downloadedBytes / totalBytes) * 100)));
  }, [downloadedBytes, totalBytes]);
  const canDownload = isConnected && hasUpdate && state !== 'downloading' && state !== 'downloaded';
  const canInstall = state === 'downloaded' && normalizeString(status?.downloaded_path).length > 0;
  const canUpgradePip = isPipMode && hasUpdate && state !== 'upgrading' && state !== 'restart_pending' && state !== 'restarting';
  const canRestartPip = isPipMode && state === 'restart_pending';
  const platformSupported = status == null ? true : normalizeBoolean(status.platform_supported);

  return (
    <div className="flex-1 min-h-0">
      <div className="card main-panel-card w-full h-full flex flex-col gap-5">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold">{t('updatePanel.title')}</h2>
            <p className="text-sm text-text-muted mt-1">{t('updatePanel.subtitle')}</p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => void handleCheck()} className="btn secondary" disabled={!isConnected || checking || state === 'downloading'}>
              {checking ? t('updatePanel.checking') : t('updatePanel.checkNow')}
            </button>
            {isPipMode ? (
              <>
                <button onClick={() => void handleDownload()} className="btn primary" disabled={!canUpgradePip}>
                  {state === 'upgrading' ? t('updatePanel.downloading') : t('updatePanel.pipUpgrade')}
                </button>
                <button onClick={() => void handleUpgrade()} className="btn secondary" disabled={!canRestartPip}>
                  {t('updatePanel.pipRestart')}
                </button>
              </>
            ) : (
              <>
                <button onClick={() => void handleDownload()} className="btn primary" disabled={!canDownload}>
                  {state === 'downloading' ? t('updatePanel.downloading') : t('updatePanel.downloadAndInstall')}
                </button>
                <button onClick={() => void handleInstall()} className="btn secondary" disabled={!canInstall}>
                  {t('updatePanel.installNow')}
                </button>
              </>
            )}
          </div>
        </div>

        {!platformSupported && (
          <div className="rounded-xl border border-warn/30 bg-warn/10 px-4 py-3 text-sm text-text">
            {t('updatePanel.unsupported')}
          </div>
        )}

        {error && (
          <div className="rounded-xl border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-text">
            {error}
          </div>
        )}

        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <div className="rounded-xl border border-border bg-panel-strong/70 px-4 py-3">
            <div className="text-xs uppercase tracking-wide text-text-muted">{t('updatePanel.currentVersion')}</div>
            <div className="mt-2 font-semibold text-text">{currentVersion}</div>
          </div>
          <div className="rounded-xl border border-border bg-panel-strong/70 px-4 py-3">
            <div className="text-xs uppercase tracking-wide text-text-muted">{t('updatePanel.latestVersion')}</div>
            <div className="mt-2 font-semibold text-text">{hasUpdate ? latestVersion : '-'}</div>
            {hasUpdate && normalizeString(status?.matched_asset) && (
              <div className="mt-1 text-xs font-mono text-text-muted">{normalizeString(status?.matched_asset)}</div>
            )}
          </div>
          <div className="rounded-xl border border-border bg-panel-strong/70 px-4 py-3">
            <div className="text-xs uppercase tracking-wide text-text-muted">{t('updatePanel.state')}</div>
            <div className="mt-2 font-semibold text-text">{t(`updatePanel.states.${state}`, { defaultValue: state })}</div>
          </div>
          <div className="rounded-xl border border-border bg-panel-strong/70 px-4 py-3">
            <div className="text-xs uppercase tracking-wide text-text-muted">{t('updatePanel.publishedAt')}</div>
            <div className="mt-2 font-semibold text-text">{hasUpdate ? publishedAt : '-'}</div>
          </div>
        </div>

        {(state === 'downloading' || state === 'upgrading' || (isPipMode && state === 'restart_pending')) && (
          <div className="rounded-xl border border-accent/30 bg-accent/10 px-4 py-4">
            <div className="flex items-center justify-between gap-3 text-sm text-text">
              <span>{t('updatePanel.downloadProgress')}</span>
              <span className="mono">{progress}%{isPipMode ? '' : ` · ${formatBytes(downloadedBytes)} / ${formatBytes(totalBytes)}`}</span>
            </div>
            <div className="mt-3 h-2 overflow-hidden rounded-full bg-secondary/80">
              <div className="h-full rounded-full bg-accent  " style={{ width: `${progress}%` }} />
            </div>
            {currentActivity && (
              <div className="mt-2 text-xs font-mono text-text-muted truncate" title={currentActivity}>
                {currentActivity}
              </div>
            )}
            {isPipMode && state === 'restart_pending' && (
              <div className="mt-3 text-sm text-text">
                {t('updatePanel.restartPendingHint')}
                {restartCommand && (
                  <div className="mt-2">
                    <code className="block rounded bg-secondary/60 px-2 py-1.5 text-xs font-mono break-all select-all">{restartCommand}</code>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {canInstall && (
          <div className="rounded-xl border border-ok/30 bg-ok/10 px-4 py-3 text-sm text-text">
            {t('updatePanel.readyToInstall')}
          </div>
        )}

        <div className="flex-1 flex flex-col min-h-0 rounded-xl border border-border bg-panel-strong/60 p-4">
          <div className="text-xs uppercase tracking-wide text-text-muted">{t('updatePanel.releaseNotes')}</div>
          <pre className="mt-3 flex-1 min-h-0 overflow-auto whitespace-pre-wrap break-words font-sans text-sm text-text">
            {loading ? t('common.loading') : hasUpdate ? (releaseNotes || t('updatePanel.noReleaseNotes')) : '-'}
          </pre>
        </div>

        <div className="rounded-xl border border-border bg-panel-strong/60 p-4">
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-sm font-semibold text-text">{t('updatePanel.configTitle')}</div>
            </div>
            <button onClick={() => void handleResetSource()} className="btn secondary" disabled={resettingSource}>
              {resettingSource ? t('common.loading') : t('updatePanel.restoreDefaults')}
            </button>
          </div>

          <div className="mt-4 grid gap-4 md:grid-cols-2">
            <label className="card !p-4">
              <div className="text-xs uppercase tracking-wide text-text-muted">{t('updatePanel.fields.releaseApiType')}</div>
              <div className="mt-3 text-sm font-mono text-text">
                {normalizeString(config?.release_api_type) || 'gitcode'}
              </div>
            </label>

            <label className="card !p-4 md:col-span-2">
              <div className="text-xs uppercase tracking-wide text-text-muted">{t('updatePanel.fields.releaseApiUrl')}</div>
              <input
                className="input mt-3 w-full"
                value={normalizeString(config?.release_api_url)}
                disabled
              />
            </label>
          </div>
        </div>
      </div>
    </div>
  );
}
