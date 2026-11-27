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
  release_notes?: unknown;
  published_at?: unknown;
  downloaded_path?: unknown;
  downloaded_bytes?: unknown;
  total_bytes?: unknown;
  error?: unknown;
  platform_supported?: unknown;
}

interface UpdaterConfigPayload {
  enabled?: unknown;
  repo_owner?: unknown;
  repo_name?: unknown;
  release_api_url?: unknown;
  asset_name_pattern?: unknown;
  sha256_name_pattern?: unknown;
  timeout_seconds?: unknown;
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

export function UpdatePanel({ isConnected, request }: UpdatePanelProps) {
  const { t, i18n } = useTranslation();
  const [status, setStatus] = useState<UpdateStatusPayload | null>(null);
  const [config, setConfig] = useState<UpdaterConfigPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [checking, setChecking] = useState(false);
  const [savingConfig, setSavingConfig] = useState(false);
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
    if (normalizeString(status?.state) !== 'downloading') {
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
    if (!isConnected || checking) return;
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
  }, [checking, isConnected, request, t]);

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

  const handleConfigChange = useCallback((key: keyof UpdaterConfigPayload, value: string | boolean) => {
    setConfig((prev) => ({ ...(prev ?? {}), [key]: value }));
  }, []);

  const handleSaveConfig = useCallback(async () => {
    if (!config || savingConfig) {
      return;
    }
    setSavingConfig(true);
    setError(null);
    try {
      const payload = await request<UpdaterConfigPayload>('updater.set_conf', {
        enabled: normalizeBoolean(config.enabled),
        repo_owner: normalizeString(config.repo_owner),
        repo_name: normalizeString(config.repo_name),
        release_api_url: normalizeString(config.release_api_url),
        asset_name_pattern: normalizeString(config.asset_name_pattern),
        sha256_name_pattern: normalizeString(config.sha256_name_pattern),
        timeout_seconds: normalizeNumber(config.timeout_seconds),
      });
      setConfig(payload);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : t('updatePanel.errors.saveConfigFailed'));
    } finally {
      setSavingConfig(false);
    }
  }, [config, request, savingConfig, t]);

  const handleInstall = useCallback(async () => {
    const installerPath = normalizeString(status?.downloaded_path);
    const api = (window as Window & { pywebview?: { api?: { install_update?: (path: string) => Promise<boolean> | boolean } } }).pywebview?.api;
    if (!installerPath || !api?.install_update) {
      setError(t('updatePanel.errors.installUnavailable'));
      return;
    }
    try {
      const ok = await api.install_update(installerPath);
      if (!ok) {
        setError(t('updatePanel.errors.installFailed'));
      }
    } catch (installError) {
      setError(installError instanceof Error ? installError.message : t('updatePanel.errors.installFailed'));
    }
  }, [status?.downloaded_path, t]);

  const state = normalizeString(status?.state) || 'idle';
  const hasUpdate = normalizeBoolean(status?.has_update);
  const currentVersion = normalizeString(status?.current_version) || '-';
  const latestVersion = normalizeString(status?.latest_version) || '-';
  const releaseNotes = normalizeString(status?.release_notes);
  const publishedAt = formatPublishedAt(normalizeString(status?.published_at), i18n.language);
  const downloadedBytes = normalizeNumber(status?.downloaded_bytes);
  const totalBytes = normalizeNumber(status?.total_bytes);
  const progress = useMemo(() => {
    if (totalBytes <= 0) return 0;
    return Math.max(0, Math.min(100, Math.round((downloadedBytes / totalBytes) * 100)));
  }, [downloadedBytes, totalBytes]);
  const canDownload = isConnected && hasUpdate && state !== 'downloading' && state !== 'downloaded';
  const canInstall = state === 'downloaded' && normalizeString(status?.downloaded_path).length > 0;
  const platformSupported = status == null ? true : normalizeBoolean(status.platform_supported);
  const configEnabled = normalizeBoolean(config?.enabled);

  return (
    <div className="flex-1 min-h-0">
      <div className="card w-full h-full flex flex-col gap-5">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold">{t('updatePanel.title')}</h2>
            <p className="text-sm text-text-muted mt-1">{t('updatePanel.subtitle')}</p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => void handleCheck()} className="btn secondary" disabled={!isConnected || checking}>
              {checking ? t('updatePanel.checking') : t('updatePanel.checkNow')}
            </button>
            <button onClick={() => void handleDownload()} className="btn primary" disabled={!canDownload}>
              {state === 'downloading' ? t('updatePanel.downloading') : t('updatePanel.downloadAndInstall')}
            </button>
            <button onClick={() => void handleInstall()} className="btn secondary" disabled={!canInstall}>
              {t('updatePanel.installNow')}
            </button>
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
            <div className="mt-2 font-semibold text-text">{latestVersion}</div>
          </div>
          <div className="rounded-xl border border-border bg-panel-strong/70 px-4 py-3">
            <div className="text-xs uppercase tracking-wide text-text-muted">{t('updatePanel.state')}</div>
            <div className="mt-2 font-semibold text-text">{t(`updatePanel.states.${state}`, { defaultValue: state })}</div>
          </div>
          <div className="rounded-xl border border-border bg-panel-strong/70 px-4 py-3">
            <div className="text-xs uppercase tracking-wide text-text-muted">{t('updatePanel.publishedAt')}</div>
            <div className="mt-2 font-semibold text-text">{publishedAt}</div>
          </div>
        </div>

        {state === 'downloading' && (
          <div className="rounded-xl border border-accent/30 bg-accent/10 px-4 py-4">
            <div className="flex items-center justify-between gap-3 text-sm text-text">
              <span>{t('updatePanel.downloadProgress')}</span>
              <span className="mono">{progress}% · {formatBytes(downloadedBytes)} / {formatBytes(totalBytes)}</span>
            </div>
            <div className="mt-3 h-2 overflow-hidden rounded-full bg-secondary/80">
              <div className="h-full rounded-full bg-accent transition-all duration-200" style={{ width: `${progress}%` }} />
            </div>
          </div>
        )}

        {canInstall && (
          <div className="rounded-xl border border-ok/30 bg-ok/10 px-4 py-3 text-sm text-text">
            {t('updatePanel.readyToInstall')}
          </div>
        )}

        <div className="flex-1 min-h-0 rounded-xl border border-border bg-panel-strong/60 p-4">
          <div className="text-xs uppercase tracking-wide text-text-muted">{t('updatePanel.releaseNotes')}</div>
          <pre className="mt-3 max-h-[28rem] overflow-auto whitespace-pre-wrap break-words font-sans text-sm text-text">
            {loading ? t('common.loading') : releaseNotes || t('updatePanel.noReleaseNotes')}
          </pre>
        </div>

        <div className="rounded-xl border border-border bg-panel-strong/60 p-4">
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-sm font-semibold text-text">{t('updatePanel.configTitle')}</div>
              <p className="mt-1 text-sm text-text-muted">{t('updatePanel.configSubtitle')}</p>
            </div>
            <button onClick={() => void handleSaveConfig()} className="btn secondary" disabled={savingConfig || !config}>
              {savingConfig ? t('common.saving') : t('common.save')}
            </button>
          </div>

          <div className="mt-4 grid gap-4 md:grid-cols-2">
            <label className="card !p-4">
              <div className="text-xs uppercase tracking-wide text-text-muted">{t('updatePanel.fields.enabled')}</div>
              <div className="mt-3 flex items-center gap-3">
                <input
                  type="checkbox"
                  checked={configEnabled}
                  onChange={(event) => handleConfigChange('enabled', event.target.checked)}
                />
                <span className="text-sm text-text">{configEnabled ? t('common.ok') : t('common.cancel')}</span>
              </div>
            </label>

            <label className="card !p-4">
              <div className="text-xs uppercase tracking-wide text-text-muted">{t('updatePanel.fields.timeoutSeconds')}</div>
              <input
                className="input mt-3"
                value={String(normalizeNumber(config?.timeout_seconds) || 20)}
                onChange={(event) => handleConfigChange('timeout_seconds', event.target.value)}
              />
            </label>

            <label className="card !p-4 md:col-span-2">
              <div className="text-xs uppercase tracking-wide text-text-muted">{t('updatePanel.fields.releaseApiUrl')}</div>
              <input
                className="input mt-3"
                value={normalizeString(config?.release_api_url)}
                onChange={(event) => handleConfigChange('release_api_url', event.target.value)}
                placeholder="http://127.0.0.1:8000/latest.json"
              />
            </label>

            <label className="card !p-4">
              <div className="text-xs uppercase tracking-wide text-text-muted">{t('updatePanel.fields.repoOwner')}</div>
              <input
                className="input mt-3"
                value={normalizeString(config?.repo_owner)}
                onChange={(event) => handleConfigChange('repo_owner', event.target.value)}
              />
            </label>

            <label className="card !p-4">
              <div className="text-xs uppercase tracking-wide text-text-muted">{t('updatePanel.fields.repoName')}</div>
              <input
                className="input mt-3"
                value={normalizeString(config?.repo_name)}
                onChange={(event) => handleConfigChange('repo_name', event.target.value)}
              />
            </label>

            <label className="card !p-4">
              <div className="text-xs uppercase tracking-wide text-text-muted">{t('updatePanel.fields.assetPattern')}</div>
              <input
                className="input mt-3"
                value={normalizeString(config?.asset_name_pattern)}
                onChange={(event) => handleConfigChange('asset_name_pattern', event.target.value)}
              />
            </label>

            <label className="card !p-4">
              <div className="text-xs uppercase tracking-wide text-text-muted">{t('updatePanel.fields.sha256Pattern')}</div>
              <input
                className="input mt-3"
                value={normalizeString(config?.sha256_name_pattern)}
                onChange={(event) => handleConfigChange('sha256_name_pattern', event.target.value)}
              />
            </label>
          </div>
        </div>
      </div>
    </div>
  );
}
