import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AppWindow, Circle, CircleCheck, Loader2, LogOut, Pencil, Play, RefreshCw, Square, Trash2, ExternalLink } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import { CategoryTabs } from '../ui/CategoryTabs/CategoryTabs';
import { PageCard } from '../ui/PageCard/PageCard';
import { Button } from '../ui/Button/Button';
import { AppMarketBrowserPane } from './AppMarketBrowserPane';
import {
  closeAppMarketView, hasAppMarketView, readAppMarketInstances, openAppMarketView, watchAppMarketInstances,
  type InstanceInfo,
} from './appMarketViews';
import { webAppViews } from './webAppViews';
import { AppMarketResources } from './AppMarketResources';
import './AppMarketPanel.css';

const PANE_HINT_DISMISSED = 'app-market-pane-hint-dismissed';

interface AppInfo {
  app_id: string;
  name: string;
  version: string;
  description: string;
  icon_url?: string;
  entry_path?: string;
  source_session?: string;
}

export interface AppMarketEditTarget {
  appId: string;
  name: string;
  instanceId: string;
  entryPath: string;
  sourceSession: string;
}

export type AppMarketEditHandler = (target: AppMarketEditTarget, isCurrent: () => boolean) => void | Promise<void>;

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
      className={`inline-flex shrink-0 items-center gap-1 text-xs ${color}`}
    >
      <Icon size={14} aria-hidden="true" className={state === 'starting' ? 'animate-spin' : undefined} />
      <span data-testid="app-market-status-text">{label}</span>
    </span>
  );
}

export function AppMarketPanel({ onEdit }: { onEdit?: AppMarketEditHandler } = {}) {
  const { t } = useTranslation();
  const isElectron = !!window.jiuwenDesktop?.isElectron;
  const useEmbeddedPane = !isElectron || !!window.jiuwenDesktop?.setMarketAppViewport;
  const hintKey = !useEmbeddedPane ? 'app-market-desktop-window-hint-dismissed' : PANE_HINT_DISMISSED;
  const [windowHintVisible, setWindowHintVisible] = useState(() => {
    try { return localStorage.getItem(hintKey) !== '1'; } catch { return true; }
  });
  const [activeTab, setActiveTab] = useState<Tab>('all');
  const [activeApp, setActiveApp] = useState<(AppInfo & { instanceId: string }) | null>(null);
  const [paneExpanded, setPaneExpanded] = useState(false);
  const openRevision = useRef(0);
  const [apps, setApps] = useState<AppInfo[]>([]);
  const [instances, setInstances] = useState<InstanceInfo[]>([]);
  const [listState, setListState] = useState<LoadState>('loading');
  const [pending, setPending] = useState<Record<string, PendingAction>>({});
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);
  const actionLocks = useRef(new Set<string>());
  const instanceRevision = useRef(0);
  // Newest user intent for which page should be shown. Starting an app opens
  // it only while this token is still current, so a slow start cannot replace
  // a page the user opened or dismissed after clicking start.
  const viewIntent = useRef(0);

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
    const next = await readAppMarketInstances();
    setActiveApp(current => current && hasAppMarketView(current.app_id, current.instanceId) ? current : null);
    // An older poll must not overwrite a start/stop response received in the meantime.
    if (next && revision === instanceRevision.current) setInstances(next);
  }, []);

  const showError = useCallback(
    (key: string, error: unknown) => {
      const detail = errorDetail(error);
      setMessage({ text: detail ? `${t(key)}: ${detail}` : t(key), error: true });
    },
    [t],
  );

  const openApplication = async (app: AppInfo, instance: InstanceInfo, edit = false) => {
    if (!instance.url) return;
    const revision = ++openRevision.current;
    if (!edit && useEmbeddedPane && activeApp?.app_id === app.app_id
      && activeApp.instanceId === instance.instance_id && hasAppMarketView(app.app_id, instance.instance_id)) {
      return;
    }
    const intent = viewIntent.current;
    const isCurrent = () => revision === openRevision.current && intent === viewIntent.current;
    try {
      const desktop = window.jiuwenDesktop;
      if (desktop?.isElectron) {
        if (!desktop.openMarketApp) throw new Error(t('appMarket.desktopUpdateRequired'));
        if (edit && !desktop.setMarketAppViewport) throw new Error(t('appMarket.desktopUpdateRequired'));
      }
      if (!await openAppMarketView(instance, app.name, edit || useEmbeddedPane)) throw new Error(t('appMarket.openFailed'));
      if (!edit && isCurrent()) setActiveApp({ ...app, instanceId: instance.instance_id });
      if (edit && isCurrent() && onEdit && app.entry_path) {
        await onEdit({ appId: app.app_id, name: app.name, instanceId: instance.instance_id,
          entryPath: app.entry_path, sourceSession: app.source_session || '' }, isCurrent);
      }
    } catch (error) {
      if (!isCurrent()) return;
      showError('appMarket.openFailed', error);
    }
  };

  const hideApplication = () => {
    ++viewIntent.current;
    ++openRevision.current;
    setActiveApp(null);
  };

  const closeApplication = async (app: AppInfo, instanceId?: string, errorKey = 'appMarket.panelCloseFailed') => {
    ++viewIntent.current;
    try {
      const closed = await closeAppMarketView(app.app_id, instanceId, () => {
        if (!isElectron) return webAppViews.close(app.app_id);
        const desktop = window.jiuwenDesktop;
        if (!desktop?.closeMarketApp) throw new Error(t('appMarket.desktopUpdateRequired'));
        return desktop.closeMarketApp(app.app_id);
      });
      if (!closed) throw new Error(t(errorKey));
      setActiveApp(current => current?.app_id === app.app_id && (!instanceId || current.instanceId === instanceId)
        ? null : current);
    } catch (error) { showError(errorKey, error); }
  };

  useEffect(() => () => { ++openRevision.current; ++viewIntent.current; }, []);

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

  useEffect(() => watchAppMarketInstances(fetchInstances), [fetchInstances]);

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

  const handleStart = async (app: AppInfo, edit = false) => {
    if (!beginAction(app.app_id, 'starting')) return;
    const intent = ++viewIntent.current;
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
      // Page loading must not keep the running application's operation lock.
      // Do not steal the pane if the user opened or dismissed another page
      // while this start was in flight.
      if (intent === viewIntent.current) void openApplication(app, instance, edit);
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
      await closeApplication(app, instance.instance_id, 'appMarket.closeWindowFailed');
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
      await closeApplication(app);
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
    <div className={`app-market-layout${useEmbeddedPane && activeApp && paneExpanded ? ' app-market-layout-expanded' : ''}`} data-testid="app-market-panel">
      <div className="app-market-list flex min-h-0 flex-1 flex-col gap-5" data-testid="app-market-list">
      <div className="page-shell flex shrink-0 flex-wrap items-center justify-between gap-4">
        <CategoryTabs<Tab>
          items={[
            { value: 'all', label: t('appMarket.allApps') },
            { value: 'running', label: t('appMarket.running') },
          ]}
          value={activeTab}
          onChange={setActiveTab}
        />
        <div className="ml-auto flex items-center gap-2" data-testid="app-market-resource-actions">
        <AppMarketResources />
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
      </div>
      {windowHintVisible && (
        <div
          className="page-shell flex items-center justify-between gap-3 rounded-md bg-secondary px-3 py-2 text-sm text-text-muted"
          data-testid="app-market-pane-hint"
        >
            <p data-testid="app-market-pane-hint-text">{t(useEmbeddedPane ? 'appMarket.paneHint' : 'appMarket.desktopWindowHint')}</p>
          <Button
            variant="quiet"
            size="sm"
            className="shrink-0"
            data-testid="app-market-pane-hint-dismiss"
            onClick={() => {
              setWindowHintVisible(false);
              try { localStorage.setItem(hintKey, '1'); } catch { /* The hint can still close for this visit. */ }
            }}
          >
            {t('appMarket.gotIt')}
          </Button>
        </div>
      )}
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
              const toggleLabel = t(state === 'running'
                ? 'appMarket.stopApp'
                : state === 'stopped' ? 'appMarket.start' : `appMarket.states.${state}`);
              return (
                <PageCard
                  key={app.app_id}
                  testId="app-market-card"
                  variant={app.app_id}
                  className="app-market-card cursor-default"
                  avatar={{ name: app.name, iconUrl: app.icon_url, testId: 'app-market-card-avatar' }}
                  title={app.name}
                  label={[`v${app.version}`]}
                  description={app.description}
                  titleEnd={<StateIcon state={state} label={t(`appMarket.states.${state}`)} />}
                  actionSlot={
                    <div className="flex shrink-0 items-center gap-2">
                      {onEdit && app.entry_path && (
                        <Button variant="secondary" size="sm" icon={<Pencil size={16} aria-hidden="true" />}
                          data-testid="app-market-edit" title={t('appMarket.edit')} aria-label={t('appMarket.edit')}
                          disabled={busy} onClick={() => {
                            if (state === 'running' && instance?.url) {
                              ++viewIntent.current;
                              void openApplication(app, instance, true);
                            } else if (state === 'stopped') {
                              void handleStart(app, true);
                            }
                          }}>
                          {t('appMarket.edit')}
                        </Button>
                      )}
                      {state === 'running' && instance?.url && (
                        <Button
                          variant="secondary"
                          size="sm"
                          data-testid="app-market-open"
                          title={t('appMarket.openRestore')}
                          aria-label={t('appMarket.openRestore')}
                          disabled={busy}
                          icon={isElectron ? <AppWindow size={16} aria-hidden="true" /> : <ExternalLink size={16} aria-hidden="true" />}
                          onClick={() => {
                            ++viewIntent.current;
                            void openApplication(app, instance);
                          }}
                        >
                          {t('appMarket.openRestore')}
                        </Button>
                      )}
                      <button
                        type="button"
                        className="page-card-action"
                        data-testid="app-market-toggle"
                        data-variant={state}
                        title={toggleLabel}
                        aria-label={toggleLabel}
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
      {useEmbeddedPane && activeApp && (
        <AppMarketBrowserPane key={activeApp.instanceId} appId={activeApp.app_id} name={activeApp.name}
          controls={{ expanded: paneExpanded, onExpand: () => setPaneExpanded(current => !current),
            onHide: hideApplication, onClose: () => void closeApplication(activeApp, activeApp.instanceId) }}
          onError={error => showError('appMarket.openFailed', error)} />
      )}
    </div>
  );
}
