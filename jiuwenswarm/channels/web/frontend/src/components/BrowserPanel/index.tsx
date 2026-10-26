import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Switch } from '../Switch';

interface BrowserPathPayload {
  chrome_path?: unknown;
  headless?: unknown;
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

function normalizeHeadless(payload: unknown): boolean {
  if (!payload || typeof payload !== 'object') return true;
  const data = payload as BrowserPathPayload;
  return typeof data.headless === 'boolean' ? data.headless : true;
}

export function BrowserPanel({ isConnected, request }: BrowserPanelProps) {
  const { t } = useTranslation();
  const [chromePath, setChromePath] = useState('');
  const [initialPath, setInitialPath] = useState('');
  const [headless, setHeadless] = useState(true);
  const [initialHeadless, setInitialHeadless] = useState(true);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const hasChanges = useMemo(
    () => chromePath !== initialPath || headless !== initialHeadless,
    [chromePath, initialPath, headless, initialHeadless]
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
      const headlessValue = normalizeHeadless(payload);
      setChromePath(value);
      setInitialPath(value);
      setHeadless(headlessValue);
      setInitialHeadless(headlessValue);
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
      const payload = await request<BrowserPathPayload>('path.set', { chrome_path: nextPath, headless });
      const savedPath = normalizeChromePath(payload) || nextPath;
      const savedHeadless = normalizeHeadless(payload);
      setChromePath(savedPath);
      setInitialPath(savedPath);
      setHeadless(savedHeadless);
      setInitialHeadless(savedHeadless);
      setSuccess(t('browser.success.pathSaved'));
    } catch (saveError) {
      const message = saveError instanceof Error ? saveError.message : t('browser.errors.savePath');
      setError(message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex-1 min-h-0">
      <div className="card main-panel-card w-full h-full flex flex-col">
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
              disabled={saving || loading}
              className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {loading ? t('common.refreshing') : t('browser.refreshPath')}
            </button>
          </div>
        </div>

        {error ? (
          <div className="mb-4 rounded-md border border-[var(--color-border-danger)] bg-danger-subtle px-3 py-2 text-sm text-danger">
            {error}
          </div>
        ) : null}
        {success ? (
          <div className="mb-4 rounded-md border border-[var(--color-border-success)] bg-ok-subtle px-3 py-2 text-sm text-ok">
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
                }}
                placeholder={t('browser.examplePath')}
                className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent"
                disabled={loading || saving}
              />
            </label>

            <div className="flex items-center justify-between gap-4 py-1">
              <div>
                <span className="text-xs uppercase tracking-wide text-text-muted">{t('browser.showBrowser')}</span>
                <p className="text-xs text-text-muted mt-0.5">{t('browser.showBrowserDesc')}</p>
              </div>
              <Switch
                checked={!headless}
                onChange={(val) => setHeadless(!val)}
                disabled={loading || saving}
              />
            </div>

            <div className="flex items-center gap-2">
              <button
                type="button"
                className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                onClick={() => {
                  setChromePath(initialPath);
                  setHeadless(initialHeadless);
                  clearFeedback();
                }}
                disabled={!hasChanges || saving}
              >
                {t('common.cancel')}
              </button>
              <button
                type="button"
                className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                onClick={() => void handleSave()}
                disabled={!isConnected || !hasChanges || saving || loading}
              >
                {saving ? t('common.saving') : t('browser.savePath')}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
