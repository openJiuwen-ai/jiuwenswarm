import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

interface BrowserPathPayload {
  chrome_path?: unknown;
}

interface BrowserStartPayload {
  returncode?: unknown;
}

interface BrowserPanelProps {
  isConnected: boolean;
  request: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>;
}

function normalizeChromePath(payload: unknown): string {
  if (!payload || typeof payload !== 'object') return '';
  const data = payload as BrowserPathPayload;
  return typeof data.chrome_path === 'string' ? data.chrome_path : '';
}

function normalizeReturnCode(payload: unknown): number | null {
  if (!payload || typeof payload !== 'object') return null;
  const data = payload as BrowserStartPayload;
  const code = Number(data.returncode);
  return Number.isInteger(code) ? code : null;
}

export function BrowserPanel({ isConnected, request }: BrowserPanelProps) {
  const { t } = useTranslation();
  const [chromePath, setChromePath] = useState('');
  const [initialPath, setInitialPath] = useState('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [showPathError, setShowPathError] = useState(false);

  const hasChanges = useMemo(() => chromePath !== initialPath, [chromePath, initialPath]);
  const isPathValid = useMemo(() => chromePath.trim().length > 0, [chromePath]);
  const canStart = useMemo(
    () => isConnected && !starting && !saving && !loading && isPathValid && !hasChanges,
    [hasChanges, isConnected, isPathValid, loading, saving, starting]
  );

  const clearFeedback = () => {
    setError(null);
    setSuccess(null);
  };

  const loadPath = useCallback(async () => {
    setLoading(true);
    clearFeedback();
    try {
      const payload = await request<BrowserPathPayload>('path.get');
      const value = normalizeChromePath(payload);
      setChromePath(value);
      setInitialPath(value);
    } catch (loadError) {
      const message = loadError instanceof Error ? loadError.message : t('browser.errors.loadPath');
      setError(message);
    } finally {
      setLoading(false);
    }
  }, [request, t]);

  useEffect(() => {
    void loadPath();
  }, [loadPath]);

  useEffect(() => {
    if (!success) return;
    const timer = window.setTimeout(() => {
      setSuccess(null);
    }, 2500);
    return () => {
      window.clearTimeout(timer);
    };
  }, [success]);

  const handleSave = async () => {
    if (saving || !hasChanges || !isConnected) {
      return;
    }
    setSaving(true);
    clearFeedback();
    try {
      const nextPath = chromePath.trim();
      const payload = await request<BrowserPathPayload>('path.set', { chrome_path: nextPath });
      const savedPath = normalizeChromePath(payload) || nextPath;
      setChromePath(savedPath);
      setInitialPath(savedPath);
      setSuccess(t('browser.success.pathSaved'));
    } catch (saveError) {
      const message = saveError instanceof Error ? saveError.message : t('browser.errors.savePath');
      setError(message);
    } finally {
      setSaving(false);
    }
  };

  const handleStart = async () => {
    if (starting || !isConnected) {
      return;
    }
    if (!isPathValid) {
      setShowPathError(true);
      return;
    }
    if (hasChanges) {
      clearFeedback();
      setError(t('browser.errors.saveBeforeStart'));
      return;
    }
    setStarting(true);
    clearFeedback();
    setShowPathError(false);
    try {
      const payload = await request<BrowserStartPayload>('browser.start');
      const returncode = normalizeReturnCode(payload);
      if (returncode === null || returncode === 0) {
        setSuccess(t('browser.success.started'));
      } else {
        setError(t('browser.errors.startFailedWithCode', { code: returncode }));
      }
    } catch (startError) {
      const message = startError instanceof Error ? startError.message : t('browser.errors.startFailed');
      setError(t('browser.errors.startFailedWithMessage', { message }));
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="flex-1 min-h-0">
      <div className="card w-full h-full flex flex-col">
        <div className="flex items-center justify-between gap-4 mb-4">
          <div>
            <h2 className="text-lg font-semibold">{t('browser.title')}</h2>
            <p className="text-sm text-text-muted mt-1">
              {t('browser.subtitle')}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void loadPath()}
              disabled={saving || starting}
              className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {loading ? t('common.refreshing') : t('browser.refreshPath')}
            </button>
          </div>
        </div>

        {error ? (
          <div className="mb-4 rounded-md border border-[var(--border-danger)] bg-danger-subtle px-3 py-2 text-sm text-danger">
            {error}
          </div>
        ) : null}
        {success ? (
          <div className="mb-4 rounded-md border border-[var(--border-ok)] bg-ok-subtle px-3 py-2 text-sm text-ok">
            {success}
          </div>
        ) : null}

        <div className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm">
          <div className="px-4 py-3 border-b border-border bg-secondary/30">
            <span className="text-xs text-text-muted tracking-wider font-medium">{t('browser.pathConfigHelp')}</span>
          </div>
          <div className="p-4 space-y-4">
            <label className="block space-y-1.5">
              <span className="text-xs uppercase tracking-wide text-text-muted">chrome_path</span>
              <input
                type="text"
                value={chromePath}
                onChange={(event) => {
                  setChromePath(event.target.value);
                  if (error) setError(null);
                  if (showPathError) setShowPathError(false);
                }}
                placeholder={t('browser.examplePath')}
                className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent"
                disabled={loading || saving || starting}
              />
            </label>

            {showPathError && !isPathValid ? (
              <div className="text-xs text-danger">{t('browser.errors.pathRequired')}</div>
            ) : null}

            <div className="flex items-center gap-2">
              <button
                type="button"
                className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                onClick={() => {
                  setChromePath(initialPath);
                  clearFeedback();
                }}
                disabled={!hasChanges || saving || starting}
              >
                {t('common.cancel')}
              </button>
              <button
                type="button"
                className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                onClick={() => void handleSave()}
                disabled={!isConnected || !hasChanges || saving || starting || loading}
              >
                {saving ? t('common.saving') : t('browser.savePath')}
              </button>
              <button
                type="button"
                className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                onClick={() => void handleStart()}
                disabled={!canStart}
                title={
                  !isPathValid
                    ? t('browser.tooltips.fillPath')
                    : hasChanges
                      ? t('browser.tooltips.savePath')
                      : undefined
                }
              >
                {starting ? t('browser.starting') : t('browser.startService')}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
