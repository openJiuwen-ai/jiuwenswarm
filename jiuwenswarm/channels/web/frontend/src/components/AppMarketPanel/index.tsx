import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Circle, CircleCheck, Loader2, LogOut, Play, RefreshCw, Square, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import { openFileInDesktopBrowser } from '../../features/desktopBrowserFile';
import { CategoryTabs } from '../ui/CategoryTabs/CategoryTabs';
import { PageCard } from '../ui/PageCard/PageCard';

interface AppInfo {
  app_id: string;
  name: string;
  version: string;
  description: string;
  icon_url?: string;
}

interface InstanceInfo {
  instance_id: string;
  app_id: string;
  port: number;
  state: string;
  detail: string;
  url?: string;
  created_at: string;
  started_at?: string | null;
}

type AppState = 'stopped' | 'starting' | 'running' | 'stopping';
type PendingAction = 'starting' | 'stopping' | 'deleting';
type Tab = 'all' | 'running';
type LoadState = 'loading' | 'success' | 'error';

function appState(state?: string): AppState {
  return state === 'starting' || state === 'running' || state === 'stopping' ? state : 'stopped';
}

function errorDetail(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === 'string') return error;
  return '';
}

function openApplication(app: AppInfo, instance: InstanceInfo) {
  const url = instance.url;
  if (!url) return;
  // The desktop helper accepts previewable filenames; the app name may have no extension.
  if (!openFileInDesktopBrowser({ name: `${app.name}.html`, download_url: url })) {
    window.open(url, '_blank', 'noopener,noreferrer');
  }
}

function StateIcon({ state, label }: { state: AppState; label: string }) {
  const Icon =
    state === 'running' ? CircleCheck : state === 'starting' ? Loader2 : state === 'stopping' ? LogOut : Circle;
  const color = state === 'running' ? 'text-ok' : state === 'stopped' ? 'text-text-muted' : 'text-warn';
  return (
    <span
      role="img"
      title={label}
      aria-label={label}
      data-testid="app-market-status"
      className={`inline-flex shrink-0 ${color}`}
    >
      <Icon size={14} aria-hidden="true" className={state === 'starting' ? 'animate-spin' : undefined} />
    </span>
  );
}

export function AppMarketPanel() {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState<Tab>('all');
  const [apps, setApps] = useState<AppInfo[]>([]);
  const [instances, setInstances] = useState<InstanceInfo[]>([]);
  const [listState, setListState] = useState<LoadState>('loading');
  const [pending, setPending] = useState<Record<string, PendingAction>>({});
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);
  const actionLocks = useRef(new Set<string>());
  const instanceRevision = useRef(0);

  const instanceMap = useMemo(() => {
    const map = new Map<string, InstanceInfo>();
    const rank: Record<AppState, number> = { running: 3, starting: 2, stopping: 1, stopped: 0 };
    for (const instance of instances) {
      const current = map.get(instance.app_id);
      const priority = rank[appState(instance.state)];
      const currentPriority = current ? rank[appState(current.state)] : -1;
      if (
        !current ||
        priority > currentPriority ||
        (priority === currentPriority &&
          (instance.started_at || instance.created_at || '') > (current.started_at || current.created_at || ''))
      ) {
        map.set(instance.app_id, instance);
      }
    }
    return map;
  }, [instances]);

  const fetchApps = useCallback(async () => {
    const data = await webRequest<{ apps?: AppInfo[] }>('app_market.list', {});
    setApps(data.apps || []);
  }, []);

  const fetchInstances = useCallback(async () => {
    const revision = instanceRevision.current;
    const data = await webRequest<{ instances?: InstanceInfo[] }>('app_market.instances', {});
    // An older poll must not overwrite a start/stop response received in the meantime.
    if (revision === instanceRevision.current) setInstances(data.instances || []);
  }, []);

  const showError = useCallback(
    (key: string, error: unknown) => {
      const detail = errorDetail(error);
      setMessage({ text: detail ? `${t(key)}: ${detail}` : t(key), error: true });
    },
    [t],
  );

  const refresh = useCallback(async () => {
    setListState('loading');
    try {
      await Promise.all([fetchApps(), fetchInstances()]);
      setListState('success');
    } catch (error) {
      setListState('error');
      showError('appMarket.empty', error);
    }
  }, [fetchApps, fetchInstances, showError]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const interval = setInterval(() => {
      // Silent like the marketplace list fetches: a failed poll must not
      // toast every 3 seconds; the next tick recovers once the request layer
      // is available again.
      void fetchInstances().catch(() => undefined);
    }, 3000);
    return () => clearInterval(interval);
  }, [fetchInstances]);

  const beginAction = (appId: string, action: PendingAction) => {
    if (actionLocks.current.has(appId)) return false;
    actionLocks.current.add(appId);
    instanceRevision.current += 1;
    setPending((current) => ({ ...current, [appId]: action }));
    setMessage(null);
    return true;
  };

  const finishAction = (appId: string) => {
    actionLocks.current.delete(appId);
    setPending((current) => {
      const next = { ...current };
      delete next[appId];
      return next;
    });
  };

  const handleStart = async (app: AppInfo) => {
    if (!beginAction(app.app_id, 'starting')) return;
    try {
      const instance = await webRequest<InstanceInfo>(
        'app_market.start',
        { app_id: app.app_id },
        { timeoutMs: 120000 },
      );
      if (instance.state !== 'running') throw new Error(instance.detail || t('appMarket.startFailed'));
      instanceRevision.current += 1;
      setInstances((current) => [...current.filter((item) => item.app_id !== app.app_id), instance]);
      setMessage({ text: t('appMarket.startSuccess'), error: false });
      openApplication(app, instance);
    } catch (error) {
      showError('appMarket.startFailed', error);
      // Refresh without holding the optimistic state after a failed start.
      void fetchInstances().catch(() => undefined);
    } finally {
      finishAction(app.app_id);
    }
  };

  const handleStop = async (app: AppInfo, instance: InstanceInfo) => {
    if (!beginAction(app.app_id, 'stopping')) return;
    try {
      // Stopping first finishes/cleans WorkSwarm role sessions and then
      // terminates the app process; that can exceed the normal 15s RPC limit.
      await webRequest('app_market.stop', { instance_id: instance.instance_id }, { timeoutMs: 180000 });
      instanceRevision.current += 1;
      setInstances((current) =>
        current.map((item) => (item.instance_id === instance.instance_id ? { ...item, state: 'stopped' } : item)),
      );
      setMessage({ text: t('appMarket.stopSuccess'), error: false });
      await fetchInstances();
    } catch (error) {
      showError('appMarket.stopFailed', error);
    } finally {
      finishAction(app.app_id);
    }
  };

  const handleDelete = async (app: AppInfo) => {
    if (actionLocks.current.has(app.app_id) || !window.confirm(t('appMarket.deleteConfirm'))) return;
    if (!beginAction(app.app_id, 'deleting')) return;
    try {
      await webRequest('app_market.delete', { app_id: app.app_id });
      instanceRevision.current += 1;
      setApps((current) => current.filter((item) => item.app_id !== app.app_id));
      setInstances((current) => current.filter((item) => item.app_id !== app.app_id));
      setMessage({ text: t('appMarket.deleteSuccess'), error: false });
      window.dispatchEvent(new Event('app-market-apps-changed'));
    } catch (error) {
      showError('appMarket.deleteFailed', error);
    } finally {
      finishAction(app.app_id);
    }
  };

  const getState = (appId: string) => {
    const action = pending[appId];
    return action === 'starting' || action === 'stopping' ? action : appState(instanceMap.get(appId)?.state);
  };
  const displayApps = activeTab === 'all' ? apps : apps.filter((app) => getState(app.app_id) !== 'stopped');

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-5" data-testid="app-market-panel">
      <div className="page-shell flex shrink-0 items-center justify-between gap-4">
        <CategoryTabs<Tab>
          items={[
            { value: 'all', label: t('appMarket.allApps') },
            { value: 'running', label: t('appMarket.running') },
          ]}
          value={activeTab}
          onChange={setActiveTab}
        />
        <button
          type="button"
          className="page-card-action shrink-0"
          title={t('common.refresh')}
          aria-label={t('common.refresh')}
          disabled={listState === 'loading'}
          onClick={() => {
            setMessage(null);
            void refresh();
          }}
        >
          <RefreshCw size={16} aria-hidden="true" />
        </button>
      </div>
      {message && (
        <div
          role={message.error ? 'alert' : 'status'}
          className={`page-shell rounded-md bg-secondary px-3 py-2 text-sm ${message.error ? 'text-danger' : 'text-text'}`}
        >
          {message.text}
        </div>
      )}
      <div className="page-scroll min-h-0 flex-1 overflow-y-auto">
        {listState === 'loading' && <p className="text-sm text-text-muted">{t('appMarket.loading')}</p>}
        {listState === 'success' && displayApps.length === 0 && (
          <p className="text-sm text-text-muted">
            {t(activeTab === 'all' ? 'appMarket.noApps' : 'appMarket.noInstances')}
          </p>
        )}
        {listState === 'success' && (
          <div className="card-grid-auto">
            {displayApps.map((app) => {
              const instance = instanceMap.get(app.app_id);
              const state = getState(app.app_id);
              const busy = !!pending[app.app_id] || state === 'starting' || state === 'stopping';
              return (
                <PageCard
                  key={app.app_id}
                  testId="app-market-card"
                  className="cursor-default"
                  avatar={{ name: app.name, iconUrl: app.icon_url, testId: 'app-market-card-avatar' }}
                  title={app.name}
                  label={[`v${app.version}`]}
                  description={app.description}
                  titleEnd={<StateIcon state={state} label={t(`appMarket.states.${state}`)} />}
                  actionSlot={
                    <div className="flex shrink-0 items-center gap-2">
                      <button
                        type="button"
                        className="page-card-action"
                        data-testid="app-market-toggle"
                        data-variant={state}
                        title={t(state === 'running' ? 'appMarket.stop' : state === 'stopped' ? 'appMarket.start' : `appMarket.states.${state}`)}
                        aria-label={t(state === 'running' ? 'appMarket.stop' : state === 'stopped' ? 'appMarket.start' : `appMarket.states.${state}`)}
                        aria-busy={state === 'starting' || state === 'stopping' || undefined}
                        disabled={busy}
                        onClick={() => {
                          if (state === 'running' && instance) void handleStop(app, instance);
                          else if (state === 'stopped') void handleStart(app);
                        }}
                      >
                        {state === 'starting' || state === 'stopping' ? (
                          <Loader2 size={16} className="animate-spin" aria-hidden="true" />
                        ) : state === 'running' ? (
                          <Square size={16} aria-hidden="true" />
                        ) : (
                          <Play size={16} aria-hidden="true" />
                        )}
                      </button>
                      <button
                        type="button"
                        className="page-card-action hover:text-danger"
                        data-testid="app-market-delete"
                        title={t('appMarket.delete')}
                        aria-label={t('appMarket.delete')}
                        disabled={busy}
                        onClick={() => void handleDelete(app)}
                      >
                        <Trash2 size={16} aria-hidden="true" />
                      </button>
                    </div>
                  }
                />
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
