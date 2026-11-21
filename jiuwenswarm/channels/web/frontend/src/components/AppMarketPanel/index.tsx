import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AppWindow, Circle, CircleCheck, Download, Loader2, LogOut, Pencil, Play, RefreshCw, Square, Trash2, ExternalLink } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import { openAssetPublish } from '../../features/assetPublishEvents';
import { Pagination } from '../common/Pagination';
import { CategoryTabs } from '../ui/CategoryTabs/CategoryTabs';
import { PageCard } from '../ui/PageCard/PageCard';
import { Button } from '../ui/Button/Button';
import { AppMarketBrowserPane } from './AppMarketBrowserPane';
import {
  appMarketStartTimeoutMs, closeAppMarketView, hasAppMarketView, readAppMarketInstances, openAppMarketView, watchAppMarketInstances,
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
  startup_timeout_seconds?: number;
  source?: string;
  hub_asset_id?: string;
  artifact_sha256?: string;
  requires_project?: boolean;
  project_id?: string;
  project_dir?: string;
  work_dir_missing?: boolean;
}

interface MarketAppInfo {
  asset_id: string;
  name: string;
  display_name: string;
  short_desc: string;
  public_latest_version: string;
  icon_uri: string;
  tags: string[];
  installed: boolean;
  installed_version: string;
  installed_app_id: string;
  work_dir_missing?: boolean;
}

interface MarketAppDetail extends MarketAppInfo {
  version: string;
  detail_desc: string;
  file_size?: number | null;
  source_size?: number | null;
  requires_project?: boolean | null;
  permissions?: string[];
  has_server_script?: boolean | null;
  checksum_sha256?: string;
  installed_checksum_sha256?: string;
}

interface InstallJobInfo {
  job_id: string;
  kind: string;
  app_id: string;
  state: string;
  phase: string;
  cancellable: boolean;
  error?: { code: string; message: string } | null;
  result?: Record<string, unknown> | null;
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
type PendingAction = 'starting' | 'stopping' | 'deleting' | 'converting';
type Tab = 'all' | 'running' | 'market';
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
  const [marketPage, setMarketPage] = useState(1);
  const [marketPageSize, setMarketPageSize] = useState(50);
  const [marketTotal, setMarketTotal] = useState(0);
  const marketRevision = useRef(0);
  const [marketApps, setMarketApps] = useState<MarketAppInfo[]>([]);
  const [marketState, setMarketState] = useState<LoadState>('loading');
  const [installJob, setInstallJob] = useState<{ jobId: string; assetId: string; phase: string } | null>(null);
  const [installingAsset, setInstallingAsset] = useState('');
  // Directory input replaces window.prompt, which embedded Chromium WebViews
  // (and Electron) reject with "prompt() is not supported".
  const [installDirDialog, setInstallDirDialog] = useState<{
    item: MarketAppInfo;
    detail: MarketAppDetail;
    value: string;
  } | null>(null);
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
      if ((error as { code?: string } | null)?.code === 'app_run_disabled') {
        setMessage({ text: t('appMarket.runDisabled'), error: true });
        return;
      }
      const detail = errorDetail(error);
      setMessage({ text: detail ? `${t(key)}: ${detail}` : t(key), error: true });
    },
    [t],
  );

  const fetchMarket = useCallback(async () => {
    const revision = ++marketRevision.current;
    setMarketState('loading');
    try {
      const data = await webRequest<{ items?: MarketAppInfo[]; total: number }>('app_market.market_list', {
        query: '',
        page: marketPage,
        page_size: marketPageSize,
      });
      if (revision !== marketRevision.current) return;
      const lastPage = Math.max(1, Math.ceil(data.total / marketPageSize));
      if (marketPage > lastPage) {
        setMarketPage(lastPage);
        return;
      }
      setMarketTotal(data.total);
      setMarketApps(data.items || []);
      setMarketState('success');
    } catch (error) {
      if (revision !== marketRevision.current) return;
      setMarketState('error');
      showError('appMarket.marketLoadFailed', error);
    }
  }, [marketPage, marketPageSize, showError]);

  const waitForInstallJob = useCallback(async (jobId: string, assetId: string) => {
    for (let attempt = 0; attempt < 900; attempt += 1) {
      const job = await webRequest<InstallJobInfo>('app_market.install_status', { job_id: jobId });
      setInstallJob({ jobId, assetId, phase: job.phase });
      if (job.state === 'done') return job;
      if (job.state === 'failed') {
        throw new Error(job.error?.message || t('appMarket.installFailed'));
      }
      if (job.state === 'cancelled') throw new Error(t('appMarket.installCancelled'));
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    throw new Error(t('appMarket.installTimeout'));
  }, [t]);

  const runInstall = async (
    item: MarketAppInfo,
    detail: MarketAppDetail,
    installDir: string,
  ) => {
    const key = item.asset_id;
    let start: { job_id: string };
    if (item.installed && item.installed_app_id) {
      start = await webRequest<{ job_id: string }>('app_market.update', {
        app_id: item.installed_app_id,
        version: detail.version,
        expected_sha256: detail.checksum_sha256 || '',
      });
    } else {
      start = await webRequest<{ job_id: string }>('app_market.install', {
        asset_id: item.asset_id,
        version: detail.version,
        install_dir: installDir,
        expected_sha256: detail.checksum_sha256 || '',
      });
    }
    const job = start.job_id ? await waitForInstallJob(start.job_id, key) : undefined;
    const backup = job?.result?.backup_dir;
    const text = typeof backup === 'string'
      ? t('appMarket.updateBackupSuccess', { name: detail.display_name, path: backup })
      : t('appMarket.installSuccess', { name: detail.display_name });
    setMessage({ text, error: false });
    await Promise.all([fetchApps(), fetchMarket()]);
    setActiveTab('all');
    window.dispatchEvent(new Event('app-market-apps-changed'));
  };

  const handleInstall = async (item: MarketAppInfo) => {
    if (installJob || installDirDialog) return;
    let openedDirDialog = false;
    const isUpdate = item.installed && !item.work_dir_missing;
    const staleRegistration = item.installed && Boolean(item.installed_app_id) && Boolean(item.work_dir_missing);
    try {
      setInstallingAsset(item.asset_id);
      const detail = await webRequest<MarketAppDetail>('app_market.market_detail', {
        asset_id: item.asset_id,
        version: item.public_latest_version,
      });
      if (!staleRegistration && item.installed && item.installed_version === detail.version
        && detail.checksum_sha256 && detail.installed_checksum_sha256 === detail.checksum_sha256) {
        setMessage({ text: t('appMarket.alreadyInstalled'), error: false });
        return;
      }
      const size = detail.source_size ? `${(detail.source_size / 1024 / 1024).toFixed(1)} MB` : '';
      const warnings = [
        detail.has_server_script ? t('appMarket.installServerWarning') : '',
        detail.permissions?.length
          ? t('appMarket.installPermissions', { list: detail.permissions.join(', ') })
          : '',
        detail.requires_project ? t('appMarket.installProjectNote') : '',
      ].filter(Boolean).join('\n');
      const prompt = `${t(isUpdate ? 'appMarket.updateConfirm' : 'appMarket.installConfirm', {
        name: detail.display_name,
        version: detail.version,
        size,
      })}${warnings ? `\n${warnings}` : ''}`;
      if (!window.confirm(prompt)) return;

      if (isUpdate && item.installed_app_id) {
        await runInstall(item, detail, '');
      } else {
        let target = item;
        // A registered app whose directory disappeared must be deregistered
        // before the install RPC can create a fresh copy.
        if (staleRegistration) {
          await webRequest('app_market.delete', { app_id: item.installed_app_id });
          target = { ...item, installed: false, work_dir_missing: false, installed_app_id: '', installed_version: '' };
        }
        const dirDefault = await webRequest<{ path: string }>('app_market.install_dir_default');
        openedDirDialog = true;
        setInstallDirDialog({ item: target, detail, value: dirDefault.path });
      }
    } catch (error) {
      showError(isUpdate ? 'appMarket.updateFailed' : 'appMarket.installFailed', error);
    } finally {
      if (!openedDirDialog) {
        setInstallingAsset('');
        setInstallJob(null);
      }
    }
  };

  const confirmInstallDir = async () => {
    if (!installDirDialog) return;
    const { item, detail, value } = installDirDialog;
    const installDir = value.trim();
    if (!installDir) {
      setMessage({ text: t('appMarket.installDirRequired'), error: true });
      return;
    }
    setInstallDirDialog(null);
    try {
      await runInstall(item, detail, installDir);
    } catch (error) {
      showError(item.installed ? 'appMarket.updateFailed' : 'appMarket.installFailed', error);
    } finally {
      setInstallingAsset('');
      setInstallJob(null);
    }
  };

  const cancelInstallDir = () => {
    setInstallDirDialog(null);
    setInstallingAsset('');
    setInstallJob(null);
  };

  const cancelInstallJob = async () => {
    if (!installJob) return;
    try {
      await webRequest('app_market.install_cancel', { job_id: installJob.jobId });
      setMessage({ text: t('appMarket.installCancelling'), error: false });
    } catch (error) {
      showError('appMarket.installFailed', error);
    }
  };

  const handleUpdateApp = async (app: AppInfo) => {
    if (!app.hub_asset_id || installJob) return;
    try {
      setInstallingAsset(app.app_id);
      const detail = await webRequest<MarketAppDetail>('app_market.market_detail', {
        asset_id: app.hub_asset_id,
      });
      if (detail.version === app.version && detail.checksum_sha256 && detail.checksum_sha256 === app.artifact_sha256) {
        setMessage({ text: t('appMarket.alreadyLatest'), error: false });
        return;
      }
      if (!window.confirm(t('appMarket.updateConfirm', {
        name: app.name,
        version: detail.version,
        size: detail.source_size ? `${(detail.source_size / 1024 / 1024).toFixed(1)} MB` : '',
      }))) return;
      const start = await webRequest<{ job_id: string }>('app_market.update', {
        app_id: app.app_id,
        version: detail.version,
        expected_sha256: detail.checksum_sha256 || '',
      });
      const job = start.job_id ? await waitForInstallJob(start.job_id, app.app_id) : undefined;
      const backup = job?.result?.backup_dir;
      const text = typeof backup === 'string'
        ? t('appMarket.updateBackupSuccess', { name: app.name, path: backup })
        : t('appMarket.updateSuccess', { name: app.name });
      setMessage({ text, error: false });
      await Promise.all([fetchApps(), fetchInstances()]);
    } catch (error) {
      showError('appMarket.updateFailed', error);
    } finally {
      setInstallingAsset('');
      setInstallJob(null);
    }
  };

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

  useEffect(() => {
    if (activeTab === 'market') void fetchMarket();
  }, [activeTab, fetchMarket]);

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
    const requestOptions = { timeoutMs: appMarketStartTimeoutMs(app.startup_timeout_seconds) };
    try {
      let instance: InstanceInfo;
      try {
        instance = await webRequest<InstanceInfo>(
          'app_market.start',
          { app_id: app.app_id },
          requestOptions,
        );
      } catch (error) {
        const code = (error as { code?: string }).code;
        const payload = (error as { payload?: { artifact_sha256?: string } }).payload;
        const sha = payload?.artifact_sha256;
        if (code !== 'app_run_consent_required' || !sha) throw error;
        if (!window.confirm(t('appMarket.runConsentConfirm', { name: app.name }))) {
          throw new Error(t('appMarket.runConsentDeclined'));
        }
        instance = await webRequest<InstanceInfo>(
          'app_market.start',
          { app_id: app.app_id, run_consent: { artifact_sha256: sha } },
          requestOptions,
        );
      }
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
      let messageKey = 'appMarket.deleteSuccess';
      let directory = '';
      if (app.source === 'hub') {
        const removeFiles = window.confirm(t('appMarket.uninstallRemoveFiles'));
        const result = await webRequest<{ cleanup_warning?: string; work_dir?: string }>('app_market.uninstall', {
          app_id: app.app_id,
          remove_files: removeFiles,
        });
        if (result?.cleanup_warning) {
          messageKey = 'appMarket.uninstallCleanupWarning';
          directory = result.work_dir || '';
        }
      } else {
        await webRequest('app_market.delete', { app_id: app.app_id });
      }
      instanceRevision.current += 1;
      setApps((current) => current.filter((item) => item.app_id !== app.app_id));
      setInstances((current) => current.filter((item) => item.app_id !== app.app_id));
      setMessage({ text: t(messageKey, { directory }), error: false });
      await closeApplication(app);
      window.dispatchEvent(new Event('app-market-apps-changed'));
    } catch (error) {
      showError('appMarket.deleteFailed', error);
    } finally {
      finishAction(app.app_id);
    }
  };

  const handleConvertToLocal = async (app: AppInfo) => {
    if (actionLocks.current.has(app.app_id) || !window.confirm(t('appMarket.convertConfirm', { name: app.name }))) return;
    if (!beginAction(app.app_id, 'converting')) return;
    try {
      await webRequest('app_market.convert_to_local', { app_id: app.app_id });
      await fetchApps();
      setMessage({ text: t('appMarket.convertSuccess', { name: app.name }), error: false });
    } catch (error) {
      showError('appMarket.convertFailed', error);
    } finally {
      finishAction(app.app_id);
    }
  };

  const handleReinstall = async (app: AppInfo) => {
    if (actionLocks.current.has(app.app_id) || !window.confirm(t('appMarket.reinstallConfirm', { name: app.name }))) return;
    if (!beginAction(app.app_id, 'deleting')) return;
    try {
      await webRequest('app_market.delete', { app_id: app.app_id });
      setApps((current) => current.filter((item) => item.app_id !== app.app_id));
      setInstances((current) => current.filter((item) => item.app_id !== app.app_id));
      await fetchMarket();
      setActiveTab('market');
      setMessage({ text: t('appMarket.reinstallHint'), error: false });
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
            { value: 'market', label: t('appMarket.market') },
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
            if (activeTab === 'market') void fetchMarket();
            else void refresh();
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
      {installJob && (
        <div
          className="page-shell flex items-center justify-between gap-3 rounded-md bg-secondary px-3 py-2 text-sm text-text-muted"
          data-testid="app-market-install-progress"
        >
          <span>
            {t('appMarket.installPhase', {
              phase: t(`appMarket.installPhases.${installJob.phase}`),
            })}
          </span>
          <Button variant="quiet" size="sm" onClick={() => void cancelInstallJob()}>
            {t('appMarket.cancel')}
          </Button>
        </div>
      )}
      {installDirDialog && (
        <div
          className="page-shell rounded-md bg-secondary px-3 py-2 text-sm text-text-muted"
          data-testid="app-market-install-dir"
        >
          <label className="mb-1 block text-sm text-text-muted" htmlFor="app-market-install-dir-input">
            {t('appMarket.installDirLabel')}
          </label>
          <input
            id="app-market-install-dir-input"
            type="text"
            value={installDirDialog.value}
            onChange={(event) => setInstallDirDialog({ ...installDirDialog, value: event.target.value })}
            onKeyDown={(event) => {
              if (event.key === 'Enter') void confirmInstallDir();
              if (event.key === 'Escape') cancelInstallDir();
            }}
            className="w-full rounded-md border border-border bg-card px-2 py-1.5 text-sm"
            data-testid="app-market-install-dir-input"
            autoFocus
          />
          <div className="mt-2 flex items-center justify-end gap-2">
            <Button variant="quiet" size="sm" onClick={cancelInstallDir}>
              {t('appMarket.cancel')}
            </Button>
            <Button variant="secondary" size="sm" onClick={() => void confirmInstallDir()}>
              {t('appMarket.installDirConfirm')}
            </Button>
          </div>
          <p className="mt-1 text-xs text-text-muted">{t('appMarket.installDirHint')}</p>
        </div>
      )}
      <div className="page-scroll min-h-0 flex-1 overflow-y-auto">
        {activeTab === 'market' && marketState === 'loading' && (
          <p className="text-sm text-text-muted">{t('appMarket.loading')}</p>
        )}
        {activeTab === 'market' && marketState === 'error' && (
          <p className="text-sm text-text-muted">{t('appMarket.marketLoadFailed')}</p>
        )}
        {activeTab === 'market' && marketState === 'success' && marketApps.length === 0 && (
          <p className="text-sm text-text-muted">{t('appMarket.noMarketApps')}</p>
        )}
        {activeTab === 'market' && marketState === 'success' && marketApps.length > 0 && (
          <div className="card-grid-auto">
            {marketApps.map((item) => {
              return (
                <PageCard
                  key={item.asset_id}
                  testId="app-market-market-card"
                  avatar={{ name: item.display_name, iconUrl: item.icon_uri }}
                  title={item.display_name}
                  label={[
                    `v${item.public_latest_version}`,
                    ...(item.installed ? [t('appMarket.installedBadge')] : []),
                    ...(item.installed && item.work_dir_missing ? [t('appMarket.workDirMissing')] : []),
                  ]}
                  description={item.short_desc}
                  actionSlot={
                    <Button
                      variant="secondary"
                      size="sm"
                      data-testid="app-market-install"
                      icon={<Download size={16} aria-hidden="true" />}
                      disabled={Boolean(installingAsset) || Boolean(installJob)}
                      onClick={() => void handleInstall(item)}
                    >
                      {t(item.installed
                        ? (item.work_dir_missing ? 'appMarket.reinstall' : 'appMarket.update')
                        : 'appMarket.install')}
                    </Button>
                  }
                />
              );
            })}
          </div>
        )}
        {activeTab === 'market' && marketState === 'success' && marketTotal > marketPageSize && (
          <Pagination page={marketPage} pageSize={marketPageSize} total={marketTotal}
            totalPages={Math.ceil(marketTotal / marketPageSize)} onPageChange={setMarketPage}
            onPageSizeChange={(size) => { setMarketPage(1); setMarketPageSize(size); }} />
        )}
        {activeTab !== 'market' && listState === 'loading' && <p className="text-sm text-text-muted">{t('appMarket.loading')}</p>}
        {activeTab !== 'market' && listState === 'success' && displayApps.length === 0 && (
          <p className="text-sm text-text-muted">
            {t(activeTab === 'all' ? 'appMarket.noApps' : 'appMarket.noInstances')}
          </p>
        )}
        {activeTab !== 'market' && listState === 'success' && (
          <div className="card-grid-auto">
            {displayApps.map((app) => {
              const instance = instanceMap.get(app.app_id);
              const state = getState(app.app_id);
              const missing = Boolean(app.work_dir_missing);
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
                  label={[`v${app.version}`, ...(missing ? [t('appMarket.workDirMissing')] : [])]}
                  description={app.description}
                  titleEnd={<StateIcon state={state} label={t(`appMarket.states.${state}`)} />}
                  actionSlot={
                    <div className="flex shrink-0 items-center gap-2">
                      {missing && app.source === 'hub' && (
                        <Button variant="secondary" size="sm" data-testid="app-market-reinstall"
                          disabled={busy} onClick={() => void handleReinstall(app)}>
                          {t('appMarket.reinstall')}
                        </Button>
                      )}
                      {!missing && app.source === 'hub' && (
                        <Button
                          variant="secondary"
                          size="sm"
                          data-testid="app-market-convert-local"
                          disabled={busy}
                          onClick={() => void handleConvertToLocal(app)}
                        >
                          {t('appMarket.convertToLocal')}
                        </Button>
                      )}
                      {!missing && app.source !== 'hub' && onEdit && app.entry_path && (
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
                      {!missing && app.source === 'hub' && app.hub_asset_id && (
                        <Button variant="secondary" size="sm" data-testid="app-market-update"
                          disabled={busy || Boolean(installingAsset) || Boolean(installJob)}
                          onClick={() => void handleUpdateApp(app)}>
                          {t('appMarket.update')}
                        </Button>
                      )}
                      {!missing && app.source === 'local' && (
                        <Button
                          variant="secondary"
                          size="sm"
                          data-testid="app-market-publish"
                          disabled={busy}
                          onClick={() => openAssetPublish({ kind: 'app', local_id: app.app_id, avatar_url: app.icon_url })}
                        >
                          {t('appMarket.publishToHub')}
                        </Button>
                      )}
                      {!missing && state === 'running' && instance?.url && (
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
                        disabled={busy || missing}
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
