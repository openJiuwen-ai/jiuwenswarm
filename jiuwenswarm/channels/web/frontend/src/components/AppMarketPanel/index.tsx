import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { AppWindow, ArrowLeft, Circle, CircleCheck, Download, Ellipsis, HardDrive, Info, Loader2, LogOut, Pencil, Pin, PinOff, Play, RefreshCw, Square, Trash2, ExternalLink, Upload } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import ReactMarkdown from 'react-markdown';
import { webRequest } from '../../services/webClient';
import { openAssetPublish } from '../../features/assetPublishEvents';
import { matchesAssetName } from '../../features/assetNameSearch';
import { Pagination } from '../common/Pagination';
import { EntityHeader } from '../ui/EntityHeader/EntityHeader';
import { PageCard } from '../ui/PageCard/PageCard';
import { PageHeader } from '../ui/PageHeader/PageHeader';
import { PageToolbar } from '../ui/PageToolbar/PageToolbar';
import { PageToolbarSearch } from '../ui/PageToolbarSearch/PageToolbarSearch';
import { Tabs } from '../ui/Tabs/Tabs';
import { Button } from '../ui/Button/Button';
import { toast, type ToastVariant } from '../ui/Toast/toastStore';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '../ui/DropdownMenu/DropdownMenu';
import { AppMarketBrowserPane } from './AppMarketBrowserPane';
import {
  appMarketStartTimeoutMs, closeAppMarketView, hasAppMarketView, readAppMarketInstances, openAppMarketView, watchAppMarketInstances,
  type InstanceInfo,
} from './appMarketViews';
import { webAppViews } from './webAppViews';
import { AppMarketResources } from './AppMarketResources';
import { AppMarketAiRoles } from './AppMarketAiRoles';
import { marketResources } from './marketResources';
import './AppMarketPanel.css';

const PANE_HINT_DISMISSED = 'app-market-pane-hint-dismissed';
// The plaza search runs on the server; debounce keystrokes the same way the
// skills page does so typing does not fire one Hub request per character.
const MARKET_SEARCH_DEBOUNCE_MS = 300;

function showNotice(content: string, variant: ToastVariant = 'success') {
  toast.open({
    id: 'app-market-action',
    position: 'right',
    variant,
    content,
    duration: variant === 'error' || variant === 'warning' ? 8 : 3,
    testId: 'app-market-action-notice',
    dataVariant: variant,
  });
}

const CLEANUP_NOTICE_KEYS: Record<string, string> = {
  unverified_directory: 'appMarket.cleanupKeptUnverified',
  protected_directory: 'appMarket.cleanupKeptProtected',
  directory_in_use: 'appMarket.cleanupKeptInUse',
  filesystem_busy: 'appMarket.cleanupFilesystemBusy',
};

interface AppInfo {
  app_id: string;
  name: string;
  version: string;
  description: string;
  icon_url?: string;
  entry_path?: string;
  source_session?: string;
  startup_timeout_seconds?: number;
  created_at?: string;
  pinned?: boolean;
  source?: string;
  hub_asset_id?: string;
  artifact_sha256?: string;
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
  version?: string;
  description?: string;
}

export type AppMarketEditHandler = (target: AppMarketEditTarget, isCurrent: () => boolean) => void | Promise<void>;

type AppState = 'stopped' | 'starting' | 'running' | 'stopping';
type PendingAction = 'starting' | 'stopping' | 'deleting' | 'converting' | 'pinning';
type Tab = 'all' | 'market';
type LoadState = 'loading' | 'success' | 'error';
type MarketDetailState = { state: LoadState; detail?: MarketAppDetail; version?: string };
type DetailTarget = { kind: 'installed'; id: string } | { kind: 'market'; id: string };

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

function DetailRow({ label, value, mono = false }: { label: string; value: ReactNode; mono?: boolean }) {
  return (
    <div className="app-market-detail__row">
      <dt className="app-market-detail__label">{label}</dt>
      <dd className={`app-market-detail__value${mono ? ' app-market-detail__value--mono' : ''}`}>
        {value || '—'}
      </dd>
    </div>
  );
}

function formatBytes(value?: number | null): string {
  if (!value || value <= 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB'];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

/** Size suffix for the install/update confirm prompt; empty when the Hub omits the size. */
function confirmSizeSuffix(value?: number | null): string {
  if (!value || value <= 0) return '';
  // Keep the separator inside the value so the prompt reads "v1.2.3 4.0 KB" instead of
  // "v1.2.34.0 KB", and so a missing size leaves no dangling space before the punctuation.
  return ` ${formatBytes(value)}`;
}

function appDirectory(app: AppInfo): string {
  const entry = (app.entry_path || '').trim();
  if (!entry) return '';
  const separator = Math.max(entry.lastIndexOf('/'), entry.lastIndexOf('\\'));
  return separator > 0 ? entry.slice(0, separator) : '';
}

function appDescriptionParts(description: string) {
  const [summary, ...details] = description.trim().split(/\r?\n[ \t]*\r?\n/);
  return { summary, introduction: details.join('\n\n') };
}

function DetailPageShell({ backLabel, onBack, children }: {
  backLabel: string;
  onBack: () => void;
  children: ReactNode;
}) {
  return (
    <div className="app-market-detail-page" data-testid="app-market-detail-page">
      <div>
        <Button
          variant="quiet"
          size="sm"
          icon={<ArrowLeft size={16} aria-hidden="true" />}
          data-testid="app-market-detail-back"
          onClick={onBack}
        >
          {backLabel}
        </Button>
      </div>
      <div className="app-market-detail-card">{children}</div>
    </div>
  );
}

function InstalledAppDetailPage({ app, state, onBack, actions, memoryLabel }: {
  app: AppInfo;
  state: AppState;
  onBack: () => void;
  actions?: ReactNode;
  memoryLabel?: string;
}) {
  const { t } = useTranslation();
  const { summary, introduction } = appDescriptionParts(app.description || '');
  return (
    <DetailPageShell backLabel={t('appMarket.backToMyApps')} onBack={onBack}>
      <EntityHeader
        variant="card"
        avatar={{ name: app.name, iconUrl: app.icon_url }}
        title={app.name}
        tags={[`v${app.version}`, ...(memoryLabel ? [memoryLabel] : [])]}
        titleEnd={state !== 'stopped' ? <StateIcon state={state} label={t(`appMarket.states.${state}`)} /> : undefined}
        actions={actions}
      />
      {summary && <p className="app-market-detail__description">{summary}</p>}
      {introduction && (
        <div className="app-market-detail__introduction" data-testid="app-market-detail-introduction">
          <ReactMarkdown
            allowedElements={['p', 'blockquote', 'a']}
            unwrapDisallowed
            skipHtml
            components={{
              a: ({ href, children }) => href?.startsWith('https://') ? (
                <a href={href} className="text-text-link underline" target="_blank" rel="noopener noreferrer">
                  {children}
                </a>
              ) : <>{children}</>,
            }}
          >
            {introduction}
          </ReactMarkdown>
        </div>
      )}
      <dl className="app-market-detail__grid">
        <DetailRow label={t('appMarket.detail.source')} value={t(app.source === 'hub' ? 'appMarket.sourceHub' : 'appMarket.sourceLocal')} />
        <DetailRow label={t('appMarket.detail.status')} value={t(`appMarket.states.${state}`)} />
        <DetailRow label={t('appMarket.detail.directory')} value={appDirectory(app)} mono />
      </dl>
      <AppMarketAiRoles key={`${app.app_id}/${app.version}`} appId={app.app_id} />
    </DetailPageShell>
  );
}

function MarketAppDetailPage({ item, detailState, onBack }: {
  item: MarketAppInfo;
  detailState?: MarketDetailState;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  return (
    <DetailPageShell backLabel={t('appMarket.backToMarket')} onBack={onBack}>
      <EntityHeader
        variant="card"
        avatar={{ name: item.display_name, iconUrl: item.icon_uri }}
        title={item.display_name}
        tags={[`v${item.public_latest_version}`, ...(item.installed ? [t('appMarket.installedBadge')] : [])]}
      />
      {detailState?.state === 'loading' || !detailState ? (
        <p className="text-sm text-text-muted">{t('appMarket.loading')}</p>
      ) : detailState.state === 'error' ? (
        <p className="text-sm text-text-muted">{t('appMarket.detailLoadFailed')}</p>
      ) : detailState.detail ? (
        <>
          {detailState.detail.detail_desc || item.short_desc ? (
            <p className="app-market-detail__description">{detailState.detail.detail_desc || item.short_desc}</p>
          ) : null}
          <dl className="app-market-detail__grid" data-testid="app-market-detail-grid">
            <DetailRow label={t('appMarket.detail.version')} value={`v${detailState.detail.version || item.public_latest_version}`} />
            <DetailRow label={t('appMarket.detail.packageSize')} value={formatBytes(detailState.detail.file_size)} />
            <DetailRow label={t('appMarket.detail.sourceSize')} value={formatBytes(detailState.detail.source_size)} />
            <DetailRow
              label={t('appMarket.detail.permissions')}
              value={detailState.detail.permissions?.length ? detailState.detail.permissions.join(', ') : '—'}
            />
            <DetailRow
              label={t('appMarket.detail.serverScript')}
              value={t(detailState.detail.has_server_script ? 'appMarket.yes' : 'appMarket.no')}
            />
          </dl>
        </>
      ) : null}
    </DetailPageShell>
  );
}

export function AppMarketPanel({
  onEdit,
  showPageHeader = false,
}: {
  onEdit?: AppMarketEditHandler;
  showPageHeader?: boolean;
} = {}) {
  const { t } = useTranslation();
  const isElectron = !!window.jiuwenDesktop?.isElectron;
  const useEmbeddedPane = !isElectron || !!window.jiuwenDesktop?.setMarketAppViewport;
  const hintKey = !useEmbeddedPane ? 'app-market-desktop-window-hint-dismissed' : PANE_HINT_DISMISSED;
  const [windowHintVisible, setWindowHintVisible] = useState(() => {
    try { return localStorage.getItem(hintKey) !== '1'; } catch { return true; }
  });
  const [activeTab, setActiveTab] = useState<Tab>('all');
  // Shared between both tabs: My Apps filters the local list, the plaza sends
  // the trimmed keyword to Hub as `query`.
  const [search, setSearch] = useState('');
  const [marketQuery, setMarketQuery] = useState('');
  const appliedMarketQuery = useRef('');
  const [activeApp, setActiveApp] = useState<(AppInfo & { instanceId: string }) | null>(null);
  const [openMenuAppId, setOpenMenuAppId] = useState<string | null>(null);
  const [detailTarget, setDetailTarget] = useState<DetailTarget | null>(null);
  const [marketDetails, setMarketDetails] = useState<Record<string, MarketDetailState>>({});
  const [paneExpanded, setPaneExpanded] = useState(false);
  const openRevision = useRef(0);
  const [apps, setApps] = useState<AppInfo[]>([]);
  const [instances, setInstances] = useState<InstanceInfo[]>([]);
  const [listState, setListState] = useState<LoadState>('loading');
  const [pending, setPending] = useState<Record<string, PendingAction>>({});
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

  const installedDetail = detailTarget?.kind === 'installed'
    ? apps.find((app) => app.app_id === detailTarget.id)
    : undefined;
  const marketDetail = detailTarget?.kind === 'market'
    ? marketApps.find((app) => app.asset_id === detailTarget.id)
    : undefined;
  const detailOpen = Boolean(installedDetail || marketDetail);
  const searchKeyword = search.trim();

  const resourceSnapshot = useSyncExternalStore(marketResources.subscribeCards, marketResources.getSnapshot);
  const appMemoryLabel = useCallback((appId: string) => {
    const memory = resourceSnapshot?.serviceApps.find(item => item.appId === appId)?.memoryBytes;
    return memory == null ? '' : t('appMarket.cardMemory', { size: `${Math.round(memory / 1048576)} MiB` });
  }, [resourceSnapshot, t]);

  const instanceMap = useMemo(() => {    const map = new Map<string, InstanceInfo>();
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
      const code = (error as { code?: string } | null)?.code;
      if (code === 'app_run_disabled') {
        showNotice(t('appMarket.runDisabled'), 'error');
        return;
      }
      if (code === 'app_project_unavailable') {
        showNotice(t('appMarket.projectUnavailable'), 'error');
        return;
      }
      const detail = errorDetail(error);
      showNotice(detail ? `${t(key)}: ${detail}` : t(key), 'error');
    },
    [t],
  );

  const fetchMarket = useCallback(async () => {
    const revision = ++marketRevision.current;
    setMarketState('loading');
    try {
      const data = await webRequest<{ items?: MarketAppInfo[]; total: number }>('app_market.market_list', {
        query: marketQuery,
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
  }, [marketPage, marketPageSize, marketQuery, showError]);

  // Market details are cached per asset_id and only reused while the plaza list
  // still reports the same version; installs, updates and Hub releases bump
  // public_latest_version, so a mismatch forces a fresh market_detail request
  // and the detail page never disagrees with the card label.
  const openMarketDetail = useCallback(async (item: MarketAppInfo) => {
    setDetailTarget({ kind: 'market', id: item.asset_id });
    const known = marketDetails[item.asset_id];
    // The version pin also covers in-flight loading entries, so a matching
    // version never starts a duplicate request.
    if (known && known.state !== 'error' && known.version === item.public_latest_version) return;
    const requestedVersion = item.public_latest_version;
    setMarketDetails((current) => ({
      ...current,
      [item.asset_id]: { state: 'loading', version: requestedVersion },
    }));
    try {
      const detail = await webRequest<MarketAppDetail>('app_market.market_detail', {
        asset_id: item.asset_id,
        version: requestedVersion,
      });
      setMarketDetails((current) => {
        // A newer request or invalidation replaced this entry while the request
        // was in flight; the stale response must not overwrite it.
        const currentEntry = current[item.asset_id];
        if (currentEntry && currentEntry.version !== requestedVersion) return current;
        return {
          ...current,
          [item.asset_id]: { state: 'success', detail, version: requestedVersion },
        };
      });
    } catch {
      setMarketDetails((current) => {
        const currentEntry = current[item.asset_id];
        if (currentEntry && currentEntry.version !== requestedVersion) return current;
        // Keep the version pin on failed entries too, so a later response for
        // the same request cannot overwrite a newer entry (mirrors success).
        return { ...current, [item.asset_id]: { state: 'error', version: requestedVersion } };
      });
    }
  }, [marketDetails]);

  const invalidateMarketDetail = (assetId: string) => {
    setMarketDetails((current) => {
      if (!(assetId in current)) return current;
      const next = { ...current };
      delete next[assetId];
      return next;
    });
  };

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
    showNotice(text);
    // Refresh the lists first, then drop the cached detail: a detail page that
    // is open while the install finishes is re-requested against the refreshed
    // list version instead of being left on the loading placeholder.
    await Promise.all([fetchApps(), fetchMarket()]);
    invalidateMarketDetail(key);
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
        showNotice(t('appMarket.alreadyInstalled'), 'info');
        return;
      }
      const size = confirmSizeSuffix(detail.source_size);
      const warnings = [
        detail.has_server_script ? t('appMarket.installServerWarning') : '',
        detail.permissions?.length
          ? t('appMarket.installPermissions', { list: detail.permissions.join(', ') })
          : '',
        t('appMarket.installProjectNote'),
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
      showNotice(t('appMarket.installDirRequired'), 'error');
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
      showNotice(t('appMarket.installCancelling'), 'info');
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
        showNotice(t('appMarket.alreadyLatest'), 'info');
        return;
      }
      if (!window.confirm(t('appMarket.updateConfirm', {
        name: app.name,
        version: detail.version,
        size: confirmSizeSuffix(detail.source_size),
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
      showNotice(text);
      // Same contract as runInstall: refresh the lists first, then drop the
      // cached detail, so a refetch while the update finished resolves against
      // the refreshed list version. A failed or cancelled update keeps the
      // cache intact instead of discarding still-valid entries.
      await Promise.all([fetchApps(), fetchInstances()]);
      invalidateMarketDetail(app.hub_asset_id);
    } catch (error) {
      showError('appMarket.updateFailed', error);
    } finally {
      setInstallingAsset('');
      setInstallJob(null);
    }
  };

  const openApplication = async (app: AppInfo, instance: InstanceInfo, edit = false) => {
    if (!instance.url) return false;
    const revision = ++openRevision.current;
    if (!edit && useEmbeddedPane && activeApp?.app_id === app.app_id
      && activeApp.instanceId === instance.instance_id && hasAppMarketView(app.app_id, instance.instance_id)) {
      return true;
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
      if (!isCurrent()) return false;
      if (!edit) setActiveApp({ ...app, instanceId: instance.instance_id });
      if (edit && onEdit && app.entry_path) {
        await onEdit({ appId: app.app_id, name: app.name, instanceId: instance.instance_id,
          entryPath: app.entry_path, sourceSession: app.source_session || '',
          version: app.version, description: app.description }, isCurrent);
      }
      return true;
    } catch (error) {
      if (isCurrent()) showError('appMarket.openFailed', error);
      return false;
    }
  };

  const hideApplication = () => {
    ++viewIntent.current;
    ++openRevision.current;
    setActiveApp(null);
  };

  const showApplication = async (app: AppInfo, instance: InstanceInfo) => {
    ++viewIntent.current;
    if (await openApplication(app, instance)) {
      setDetailTarget(current => current === detailTarget ? null : current);
    }
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
    if (detailTarget && !detailOpen) {
      setDetailTarget((current) => current === detailTarget ? null : current);
    }
  }, [detailTarget, detailOpen]);

  useEffect(() => {
    if (activeTab === 'market') void fetchMarket();
  }, [activeTab, fetchMarket]);

  // An invalidation can remove the cached entry while its detail page is open
  // (for example an install finishing in the background). Re-request it so the
  // page cannot stay on the loading placeholder forever. Entries that exist,
  // including failed ones, are left untouched so errors are not retried in a loop.
  useEffect(() => {
    if (!marketDetail) return;
    if (marketDetails[marketDetail.asset_id]) return;
    void openMarketDetail(marketDetail);
  }, [marketDetail, marketDetails, openMarketDetail]);

  // The plaza is paginated by Hub, so filtering has to happen server-side.
  // Debounce typing and return to the first page only when the keyword really
  // changed; re-entering the tab with the same keyword keeps the current page.
  useEffect(() => {
    const keyword = search.trim();
    const timer = window.setTimeout(() => {
      if (appliedMarketQuery.current === keyword) return;
      appliedMarketQuery.current = keyword;
      setMarketPage(1);
      setMarketQuery(keyword);
    }, MARKET_SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [search]);

  const beginAction = (appId: string, action: PendingAction) => {
    if (actionLocks.current.has(appId)) return false;
    actionLocks.current.add(appId);
    instanceRevision.current += 1;
    setPending((current) => ({ ...current, [appId]: action }));
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
      showNotice(t('appMarket.startSuccess'));
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
      showNotice(t('appMarket.stopSuccess'));
      await closeApplication(app, instance.instance_id, 'appMarket.closeWindowFailed');
      await fetchInstances();
    } catch (error) {
      showError('appMarket.stopFailed', error);
    } finally {
      finishAction(app.app_id);
    }
  };

  const removeInstalledApp = (appId: string) => {
    instanceRevision.current += 1;
    setApps((current) => current.filter((item) => item.app_id !== appId));
    setInstances((current) => current.filter((item) => item.app_id !== appId));
    setDetailTarget((current) => current?.kind === 'installed' && current.id === appId ? null : current);
  };

  const handleRemove = async (app: AppInfo, removeFiles: boolean) => {
    if (actionLocks.current.has(app.app_id)) return;
    if (removeFiles && !window.confirm(t('appMarket.deleteCleanConfirm', { name: app.name }))) return;
    if (!beginAction(app.app_id, 'deleting')) return;
    try {
      const result = await webRequest<{
        cleanup_warning?: string;
        cleanup_code?: string;
        work_dir?: string;
        preserved_dir?: string;
      }>(
        app.source === 'hub' ? 'app_market.uninstall' : 'app_market.delete',
        {
          app_id: app.app_id,
          remove_files: removeFiles,
          // A local app directory may be the user's project directory, so the
          // server requires the caller to name it back before deleting files.
          ...(removeFiles && app.source !== 'hub' ? { confirm_work_dir: appDirectory(app) } : {}),
        },
        // Large local trees (node_modules) can take minutes to remove.
        removeFiles ? { timeoutMs: 180000 } : undefined,
      );
      const cleanupError = result?.cleanup_warning;
      const cleanupKey = cleanupError ? CLEANUP_NOTICE_KEYS[result?.cleanup_code || ''] : '';
      removeInstalledApp(app.app_id);
      if (app.source === 'hub' && app.hub_asset_id) invalidateMarketDetail(app.hub_asset_id);
      if (cleanupError) {
        const directory = result?.work_dir || appDirectory(app);
        const message = cleanupKey
          ? t(cleanupKey, { directory })
          : t('appMarket.cleanupFailed', { directory, error: cleanupError });
        const archive = result?.preserved_dir;
        showNotice(archive ? t('appMarket.cleanupWithArchive', { message, directory: archive }) : message, 'error');
      } else if (removeFiles && result?.preserved_dir) {
        showNotice(t('appMarket.deleteCleanArchived', { directory: result.preserved_dir }), 'success');
      } else {
        showNotice(t(removeFiles ? 'appMarket.deleteCleanSuccess' : 'appMarket.removeKeepSuccess'), 'success');
      }
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
      if (app.hub_asset_id) invalidateMarketDetail(app.hub_asset_id);
      await fetchApps();
      showNotice(t('appMarket.convertSuccess', { name: app.name }));
    } catch (error) {
      showError('appMarket.convertFailed', error);
    } finally {
      finishAction(app.app_id);
    }
  };

  const handlePin = async (app: AppInfo) => {
    if (actionLocks.current.has(app.app_id) || !beginAction(app.app_id, 'pinning')) return;
    try {
      await webRequest('app_market.pin', { app_id: app.app_id, pinned: !app.pinned });
      await fetchApps();
      showNotice(t(app.pinned ? 'appMarket.unpinSuccess' : 'appMarket.pinSuccess', { name: app.name }));
    } catch (error) {
      showError('appMarket.pinFailed', error);
    } finally {
      finishAction(app.app_id);
    }
  };

  const handleReinstall = async (app: AppInfo) => {
    if (actionLocks.current.has(app.app_id) || !window.confirm(t('appMarket.reinstallConfirm', { name: app.name }))) return;
    if (!beginAction(app.app_id, 'deleting')) return;
    try {
      await webRequest('app_market.delete', { app_id: app.app_id });
      if (app.hub_asset_id) invalidateMarketDetail(app.hub_asset_id);
      removeInstalledApp(app.app_id);
      await fetchMarket();
      setActiveTab('market');
      showNotice(t('appMarket.reinstallHint'), 'info');
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
  const renderAppActions = (
    app: AppInfo,
    state: AppState,
    instance: InstanceInfo | undefined,
    missing: boolean,
    busy: boolean,
  ) => {
    const toggleLabel = t(state === 'running'
      ? 'appMarket.stopApp'
      : state === 'stopped' ? 'appMarket.start' : `appMarket.states.${state}`);
    return (
      <div className="app-market-card-actions" onClick={(event) => event.stopPropagation()}>
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
        <DropdownMenu
          open={openMenuAppId === app.app_id}
          onOpenChange={(open) => setOpenMenuAppId(open ? app.app_id : null)}
        >
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className="page-card-action"
              data-testid="app-market-more"
              title={t('appMarket.moreActions', { name: app.name })}
              aria-label={t('appMarket.moreActions', { name: app.name })}
            >
              <Ellipsis size={16} aria-hidden="true" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent side="bottom" align="end">
            <DropdownMenuItem
              icon={app.pinned ? <PinOff size={16} aria-hidden="true" /> : <Pin size={16} aria-hidden="true" />}
              testId="app-market-pin"
              disabled={busy}
              onSelect={() => void handlePin(app)}
            >
              {t(app.pinned ? 'appMarket.unpin' : 'appMarket.pin')}
            </DropdownMenuItem>
            {!missing && state === 'running' && instance?.url ? (
              <>
                <DropdownMenuItem
                  icon={isElectron ? <AppWindow size={16} aria-hidden="true" /> : <ExternalLink size={16} aria-hidden="true" />}
                  testId="app-market-open"
                  onSelect={() => void showApplication(app, instance)}
                >
                  {t('appMarket.openRestore')}
                </DropdownMenuItem>
                {detailTarget?.id !== app.app_id && (
                  <DropdownMenuItem
                    icon={<Info size={16} aria-hidden="true" />}
                    testId="app-market-view-details"
                    onSelect={() => setDetailTarget({ kind: 'installed', id: app.app_id })}
                  >
                    {t('appMarket.viewDetails')}
                  </DropdownMenuItem>
                )}
              </>
            ) : null}
            {!missing && app.source !== 'hub' && onEdit && app.entry_path ? (
              <DropdownMenuItem
                icon={<Pencil size={16} aria-hidden="true" />}
                testId="app-market-edit"
                disabled={busy}
                onSelect={() => {
                  if (state === 'running' && instance?.url) {
                    ++viewIntent.current;
                    void openApplication(app, instance, true);
                  } else if (state === 'stopped') {
                    void handleStart(app, true);
                  }
                }}
              >
                {t('appMarket.edit')}
              </DropdownMenuItem>
            ) : null}
            {!missing && app.source === 'hub' && app.hub_asset_id ? (
              <DropdownMenuItem
                icon={<RefreshCw size={16} aria-hidden="true" />}
                testId="app-market-update"
                disabled={busy || Boolean(installingAsset) || Boolean(installJob)}
                onSelect={() => void handleUpdateApp(app)}
              >
                {t('appMarket.update')}
              </DropdownMenuItem>
            ) : null}
            {!missing && app.source === 'hub' ? (
              <DropdownMenuItem
                icon={<HardDrive size={16} aria-hidden="true" />}
                testId="app-market-convert-local"
                disabled={busy}
                onSelect={() => void handleConvertToLocal(app)}
              >
                {t('appMarket.convertToLocal')}
              </DropdownMenuItem>
            ) : null}
            {!missing && app.source === 'local' ? (
              <DropdownMenuItem
                icon={<Upload size={16} aria-hidden="true" />}
                testId="app-market-publish"
                disabled={busy}
                onSelect={() => openAssetPublish({ kind: 'app', local_id: app.app_id, avatar_url: app.icon_url })}
              >
                {t('appMarket.publishToHub')}
              </DropdownMenuItem>
            ) : null}
            {missing && app.source === 'hub' ? (
              <DropdownMenuItem
                icon={<Download size={16} aria-hidden="true" />}
                testId="app-market-reinstall"
                disabled={busy}
                onSelect={() => void handleReinstall(app)}
              >
                {t('appMarket.reinstall')}
              </DropdownMenuItem>
            ) : null}
            <DropdownMenuItem
              icon={<HardDrive size={16} aria-hidden="true" />}
              testId="app-market-remove"
              disabled={busy}
              onSelect={() => void handleRemove(app, false)}
            >
              {t('appMarket.removeKeepFiles')}
            </DropdownMenuItem>
            <DropdownMenuItem
              danger
              icon={<Trash2 size={16} aria-hidden="true" />}
              testId="app-market-delete"
              disabled={busy}
              onSelect={() => void handleRemove(app, true)}
            >
              {t('appMarket.deleteCleanFiles')}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    );
  };

  // Running apps are the ones users act on; put them first and order them by
  // start time (newest first). Everything else keeps the server order.
  const displayApps = useMemo(() => {
    const rank: Record<AppState, number> = { running: 0, starting: 1, stopping: 2, stopped: 3 };
    const stateOf = (appId: string): AppState => {
      const action = pending[appId];
      if (action === 'starting' || action === 'stopping') return action;
      return appState(instanceMap.get(appId)?.state);
    };
    return apps
      .filter((app) => matchesAssetName(searchKeyword, [app.name, app.description || '']))
      .map((app, index) => ({ app, index, state: stateOf(app.app_id) }))
      .sort((left, right) => {
        const rankDelta = rank[left.state] - rank[right.state];
        if (rankDelta !== 0) return rankDelta;
        if (left.state === 'running') {
          const leftInstance = instanceMap.get(left.app.app_id);
          const rightInstance = instanceMap.get(right.app.app_id);
          const leftTime = leftInstance?.started_at || leftInstance?.created_at || '';
          const rightTime = rightInstance?.started_at || rightInstance?.created_at || '';
          const timeDelta = rightTime.localeCompare(leftTime);
          if (timeDelta !== 0) return timeDelta;
        }
        return left.index - right.index;
      })
      .map(({ app }) => app);
  }, [apps, instanceMap, pending, searchKeyword]);

  const showListHeader = showPageHeader && !activeApp;
  const resourceActions = (
    <div className="flex min-w-0 flex-wrap items-center justify-end gap-2" data-testid="app-market-resource-actions">
      <AppMarketResources />
      <button
        type="button"
        className="page-card-action shrink-0"
        title={t('common.refresh')}
        aria-label={t('common.refresh')}
        data-testid="app-market-refresh"
        disabled={listState === 'loading'}
        onClick={() => {
          if (activeTab === 'market') void fetchMarket();
          else void refresh();
        }}
      >
        <RefreshCw size={16} aria-hidden="true" />
      </button>
    </div>
  );
  const selectTab = (value: Tab) => {
    hideApplication();
    setPaneExpanded(false);
    setDetailTarget(null);
    setActiveTab(value);
  };

  return (
    <div className={`app-market-layout${useEmbeddedPane && activeApp && paneExpanded ? ' app-market-layout-expanded' : ''}`} data-testid="app-market-panel">
      <div className="app-market-list flex min-h-0 flex-1 flex-col" data-testid="app-market-list">
      {!detailOpen && (
        <div className="page-shell flex-none">
          {showListHeader ? (
            <PageHeader
              title={t('appMarket.title')}
              subtitle={t('appMarket.subtitle')}
              titleTestId="app-market-page-title"
              subtitleTestId="app-market-page-subtitle"
            >
              {resourceActions}
            </PageHeader>
          ) : resourceActions}
          <PageToolbar className="flex-wrap" testId="app-market-toolbar">
            <Tabs<Tab>
              wrapperTestId="app-market-tabs"
              className="page-tabs"
              role="tablist"
              items={[
                { value: 'market', label: t('appMarket.market'), testId: 'app-market-tab-plaza' },
                { value: 'all', label: t('appMarket.myApps'), testId: 'app-market-tab-my-apps' },
              ]}
              value={activeTab}
              onChange={selectTab}
              onReselect={activeApp ? selectTab : undefined}
            />
            <div className="ml-auto flex flex-wrap items-center justify-end gap-3" data-testid="app-market-toolbar-actions">
              <PageToolbarSearch
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                onClear={() => setSearch('')}
                placeholder={t('appMarket.searchPlaceholder')}
                aria-label={t('appMarket.searchPlaceholder')}
                inputTestId="app-market-search-input"
                wrapperTestId="app-market-search"
                measureSelector=".app-market-list"
                className="focus-visible:border-[color:var(--color-control-emphasis)]"
              />
            </div>
          </PageToolbar>
          {windowHintVisible && (
            <div
              className="flex items-center justify-between gap-3 rounded-md bg-secondary px-3 py-2 text-sm text-text-muted"
              data-testid="app-market-pane-hint"
            >
              <p data-testid="app-market-pane-hint-text">
                {t(useEmbeddedPane ? 'appMarket.paneHint' : 'appMarket.desktopWindowHint')}
              </p>
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
        </div>
      )}
      {installJob && (
        <div
          className="page-shell mt-4 flex shrink-0 items-center justify-between gap-3 rounded-md bg-secondary px-3 py-2 text-sm text-text-muted"
          data-testid="app-market-install-progress"
        >
          <span role="status">
            {t('appMarket.installPhase', {
              phase: t(`appMarket.installPhases.${installJob.phase}`),
            })}
          </span>
          <Button variant="quiet" size="sm" data-testid="app-market-install-cancel" onClick={() => void cancelInstallJob()}>
            {t('appMarket.cancel')}
          </Button>
        </div>
      )}
      {installDirDialog && (
        <div
          className="page-shell mt-4 shrink-0 rounded-md bg-secondary px-3 py-2 text-sm text-text-muted"
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
        {installedDetail ? (() => {
          const app = installedDetail;
          const state = getState(app.app_id);
          const instance = instanceMap.get(app.app_id);
          const missing = Boolean(app.work_dir_missing);
          const busy = !!pending[app.app_id] || state === 'starting' || state === 'stopping';
          const memoryLabel = state === 'running' ? appMemoryLabel(app.app_id) : '';
          return (
            <InstalledAppDetailPage
              app={app}
              state={state}
              onBack={() => setDetailTarget(null)}
              actions={renderAppActions(app, state, instance, missing, busy)}
              memoryLabel={memoryLabel}
            />
          );
        })() : marketDetail ? (
          <MarketAppDetailPage
            item={marketDetail}
            detailState={marketDetails[marketDetail.asset_id]}
            onBack={() => setDetailTarget(null)}
          />
        ) : (
          <>
        {activeTab === 'market' && marketState === 'loading' && (
          <p className="text-sm text-text-muted">{t('appMarket.loading')}</p>
        )}
        {activeTab === 'market' && marketState === 'error' && (
          <p className="text-sm text-text-muted">{t('appMarket.marketLoadFailed')}</p>
        )}
        {activeTab === 'market' && marketState === 'success' && marketApps.length === 0 && (
          <p className="text-sm text-text-muted">
            {t(searchKeyword ? 'common.noMatches' : 'appMarket.noMarketApps')}
          </p>
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
                  onClick={() => void openMarketDetail(item)}
                  actionSlot={
                    <div className="app-market-card-actions" onClick={(event) => event.stopPropagation()}>
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
                    </div>
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
            {t(searchKeyword ? 'common.noMatches' : 'appMarket.noApps')}
          </p>
        )}
        {activeTab !== 'market' && listState === 'success' && (
          <div className="card-grid-auto">
            {displayApps.map((app) => {
              const instance = instanceMap.get(app.app_id);
              const state = getState(app.app_id);
              const missing = Boolean(app.work_dir_missing);
              const busy = !!pending[app.app_id] || state === 'starting' || state === 'stopping';
              const memoryLabel = state === 'running' ? appMemoryLabel(app.app_id) : '';
              return (
                <PageCard
                  key={app.app_id}
                  testId="app-market-card"
                  variant={app.app_id}
                  className="app-market-card"
                  avatar={{ name: app.name, iconUrl: app.icon_url, testId: 'app-market-card-avatar' }}
                  title={app.name}
                  label={[
                    `v${app.version}`,
                    ...(memoryLabel ? [memoryLabel] : []),
                    ...(missing ? [t('appMarket.workDirMissing')] : []),
                  ]}
                  description={appDescriptionParts(app.description || '').summary}
                  titleEnd={state !== 'stopped' ? <StateIcon state={state} label={t(`appMarket.states.${state}`)} /> : undefined}
                  onClick={() => {
                    if (!missing && state === 'running' && instance?.url) void showApplication(app, instance);
                    else setDetailTarget({ kind: 'installed', id: app.app_id });
                  }}
                  actionSlot={renderAppActions(app, state, instance, missing, busy)}
                />
              );
            })}
          </div>
        )}
          </>
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
