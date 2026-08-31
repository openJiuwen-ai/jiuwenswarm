/**
 * PersonalContextServicesPanel — 「上下文内容」子页（级联布局）。
 *
 * 左侧：6 个内容源分类（本地文件夹 / Edge 收藏夹 / 知乎专栏 / 今日头条 / 飞书 / GitHub），
 *   每项显示该分类已创建的采集内容数量，点击切换右侧内容。
 * 右侧：当前分类的内容卡片列表。
 *   - 空：暂无内容 + 添加内容按钮（预选当前分类 provider）。
 *   - 非空：每张卡片 = 内容名(header) + 左对齐[采集频率+采集状态(进度条/徽章)] +
 *           右对齐[启动/停止/编辑/删除 + 自动采集开关]。
 * 数据走 usePersonalContextStore；状态轮询 5s。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, PlayCircle } from 'lucide-react';
import { Switch } from '../Switch';
import { usePersonalContextStore } from '../../stores';
import {
  type FetchProvider,
  type FetchRunProgress,
  type FetchServiceConfig,
  type FetchServiceState,
  PROVIDER_LABEL_KEYS,
  PROVIDER_ORDER,
  FREQUENCY_SECONDS,
  isProviderAuthorized,
  pcApi,
} from '../../services/personalContextApi';
import { AddContentDrawer } from './AddContentDrawer';
import localFilesIcon from '../../assets/settings/channels/local-files.svg';
import edgeBookmarksIcon from '../../assets/settings/channels/edge-bookmarks.svg';
import zhihuIcon from '../../assets/settings/channels/zhihu.svg';
import toutiaoIcon from '../../assets/settings/channels/toutiao.svg';
import feishuIcon from '../../assets/settings/channels/feishu.svg';
import githubIcon from '../../assets/settings/channels/GitHub.svg';
import './ServicesPanel.css';

const POLL_INTERVAL_MS = 5000;

const PROVIDER_ICON: Record<FetchProvider, string> = {
  local_files: localFilesIcon,
  browser_bookmarks: edgeBookmarksIcon,
  zhihu_reader: zhihuIcon,
  toutiao_reader: toutiaoIcon,
  feishu: feishuIcon,
  github: githubIcon,
};

interface PersonalContextServicesPanelProps {
  isConnected: boolean;
  isActive: boolean;
  onBackToGraph: () => void;
}

export function PersonalContextServicesPanel({
  isConnected,
  isActive,
  onBackToGraph,
}: PersonalContextServicesPanelProps) {
  const { t } = useTranslation();
  const {
    config,
    graph,
    status,
    loadingServices,
    pendingWrites,
    loadServices,
    loadStatus,
    setServiceEnabled,
    deleteService,
    runOne,
    stopRun,
    authByProvider,
    loadAuthStatus,
    authorizeProvider,
  } = usePersonalContextStore();
  const [error, setError] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState<FetchServiceConfig | null>(null);
  const [selectedProvider, setSelectedProvider] = useState<FetchProvider>(PROVIDER_ORDER[0]);
  /** 用户是否手动点过分类；为 true 后不再自动切换默认分类。 */
  const [userTouched, setUserTouched] = useState(false);
  const pollRef = useRef<number | null>(null);

  // 轮询同时刷新服务列表与运行态进度（fetch_service_states / fetch_run_progress），
  // 否则任务页进度会停留在进页快照（见 ServicesPanel.refresh 旧实现只刷 loadServices）。
  const refresh = useCallback(() => {
    void loadServices().catch((e: unknown) => {
      setError(e instanceof Error ? e.message : String(e));
    });
    void loadStatus().catch(() => {
      // 进度刷新失败不阻断列表展示
    });
    // 同步飞书授权状态（设备流异步，后端需用户完成浏览器授权后才会变 authorized）
    void loadAuthStatus('feishu').catch(() => {});
  }, [loadServices, loadStatus, loadAuthStatus]);

  useEffect(() => {
    if (!isConnected || !isActive) return;
    refresh();
    pollRef.current = window.setInterval(refresh, POLL_INTERVAL_MS);
    return () => {
      if (pollRef.current != null) window.clearInterval(pollRef.current);
      pollRef.current = null;
    };
  }, [isConnected, isActive, refresh]);

  const services = config.fetch_services;

  // 默认选中首个有内容的分类；用户手动选过则不再自动切换。
  useEffect(() => {
    if (!services.length || userTouched) return;
    const hasContent = (p: FetchProvider) => services.some((s) => s.provider === p);
    if (!hasContent(selectedProvider)) {
      const firstWithContent = PROVIDER_ORDER.find(hasContent);
      if (firstWithContent) setSelectedProvider(firstWithContent);
    }
  }, [services, selectedProvider, userTouched]);

  const handleRun = useCallback(
    (serviceId: string) => {
      setError(null);
      void runOne(serviceId)
        .then(() => setError(t('personalContext.services.runSubmitted')))
        .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
    },
    [runOne, t],
  );

  const handleStop = useCallback(
    (serviceId: string) => {
      setError(null);
      void stopRun(serviceId).catch((e: unknown) => {
        setError(e instanceof Error ? e.message : String(e));
      });
    },
    [stopRun],
  );

  const handleToggle = useCallback(
    (serviceId: string, enabled: boolean) => {
      setError(null);
      void setServiceEnabled(serviceId, enabled).catch((e: unknown) => {
        setError(e instanceof Error ? e.message : String(e));
      });
    },
    [setServiceEnabled],
  );

  const handleDelete = useCallback(
    async (serviceId: string, state?: FetchServiceState) => {
      setError(null);
      if (!window.confirm(t('personalContext.services.deleteConfirm'))) return;
      // 后端要求 STOPPED 才能删；非 STOPPED 先停（走 store 有 pending+乐观更新）
      try {
        if (state && state !== 'STOPPED') {
          await setServiceEnabled(serviceId, false);
        }
        await deleteService(serviceId);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [deleteService, setServiceEnabled, t],
  );

  const openDrawer = useCallback(() => setDrawerOpen(true), []);

  const handleAuthorize = useCallback((provider: FetchProvider) => {
    if (provider !== 'feishu') return;
    setError(null);
    void authorizeProvider('feishu').then((result) => {
      if (result?.verification_url) {
        window.open(result.verification_url, '_blank', 'noopener,noreferrer');
      }
    }).catch((e: unknown) => {
      setError(e instanceof Error ? e.message : String(e));
    });
  }, [authorizeProvider]);

  const feishuState = authByProvider.feishu?.state;

  // 飞书授权中时加快轮询，直到变 authorized/failed
  useEffect(() => {
    if (!isConnected || feishuState !== 'authorizing') return;
    const id = window.setInterval(() => void loadAuthStatus('feishu'), 5000);
    return () => window.clearInterval(id);
  }, [isConnected, feishuState, loadAuthStatus]);

  const categoryServices = services.filter((s) => s.provider === selectedProvider);

  return (
    <div className="pc-services" data-testid="personal-context-services">
      {error && <div className="pc-services__error" role="alert">{error}</div>}

      {loadingServices && services.length === 0 ? (
        <div className="pc-services__loading"><Loader2 className="spin" size={20} /></div>
      ) : (
        <>
        <div className="pc-services__head">
          <button type="button" className="pc-services__back" onClick={onBackToGraph}>
            <span className="pc-services__back-icon">&lt;</span>
            <span>{t('personalContext.services.backToGraph')}</span>
          </button>
          <div className="pc-services__head-row">
            <h3 className="pc-services__head-title">{t('personalContext.services.addKnowledge')}</h3>
            <button
              type="button"
              className="pc-services__add-btn"
              onClick={openDrawer}
              disabled={!isConnected}
            >
              {t('personalContext.services.addContent')}
            </button>
          </div>
        </div>

        <div className="pc-services__stats">
          <div className="pc-services__stat-card">
            <span className="pc-services__stat-label">{t('personalContext.services.statSources')}</span>
            <span className="pc-services__stat-number">{services.length}</span>
          </div>
          <div className="pc-services__stat-card">
            <span className="pc-services__stat-label">{t('personalContext.services.statKnowledge')}</span>
            <span className="pc-services__stat-number">{graph?.nodes.length ?? 0}</span>
          </div>
          <div className="pc-services__stat-card">
            <span className="pc-services__stat-label">{t('personalContext.services.statCollecting')}</span>
            <span className="pc-services__stat-number">{Object.values(status?.fetch_run_progress ?? {}).filter((p) => p.run_state === 'running').length}</span>
          </div>
        </div>

                <div className="pc-services__layout">
          {/* 左侧：内容源分类 */}
          <aside className="pc-services__categories">
            <div className="pc-services__categories-title">{t('personalContext.services.knowledgeSource')}</div>
            {PROVIDER_ORDER.map((p) => {
              const count = services.filter((s) => s.provider === p).length;
              const active = p === selectedProvider;
              const authorized = isProviderAuthorized(p, authByProvider);
              return (
                <button
                  key={p}
                  type="button"
                  className={`pc-services__cat${active ? ' is-active' : ''}${!authorized ? ' is-disabled' : ''}`}
                  onClick={() => { setSelectedProvider(p); setUserTouched(true); }}
                >
                  <span className="pc-services__cat-icon">
                    <img src={PROVIDER_ICON[p]} alt="" />
                  </span>
                  <span className="pc-services__cat-name">{t(PROVIDER_LABEL_KEYS[p])}</span>
                  {authorized ? (
                    <span className="pc-services__cat-count">{count}</span>
                  ) : (
                    <button type="button" className="pc-services__cat-authorize" onClick={(e) => { e.stopPropagation(); handleAuthorize(p); }}>{t('personalContext.authorization.authorize')}</button>
                  )}
                </button>
              );
            })}
          </aside>

          {/* 右侧：当前分类内容 */}
          <section className="pc-services__list">
            <div className="pc-services__list-head">
              <div className="pc-services__list-head-left">
                <span className="pc-services__list-icon">
                  <img src={PROVIDER_ICON[selectedProvider]} alt="" />
                </span>
                <h3 className="pc-services__list-title">{t(PROVIDER_LABEL_KEYS[selectedProvider])}</h3>
              </div>
              <button
                type="button"
                className="pc-services__collect-all"
                onClick={() => categoryServices.forEach((s) => handleRun(s.service_id))}
                disabled={!isConnected || categoryServices.length === 0}
              >
                <PlayCircle size={16} />
                {t('personalContext.services.collectAll')}
              </button>
            </div>

            {categoryServices.length === 0 ? (
              <div className="pc-services__empty">
                <NoDataIcon />
                <div className="pc-services__empty-text">
                  {t('personalContext.services.noContentInCategory')}
                </div>
                <button
                  type="button"
                  className="pc-services__add"
                  onClick={openDrawer}
                  disabled={!isConnected}
                >
                  {t('personalContext.services.addContent')}
                </button>
              </div>
            ) : (
              <div className="pc-services__cards">
                {categoryServices.map((s) => (
                  <ServiceCard
                    key={s.service_id}
                    service={s}
                    state={status?.fetch_service_states[s.service_id] ?? 'STOPPED'}
                    lastError={status?.fetch_service_errors[s.service_id] ?? null}
                    progress={status?.fetch_run_progress[s.service_id]}
                    pending={
                      !!pendingWrites[`svc:${s.service_id}`] ||
                      !!pendingWrites[`run:${s.service_id}`] ||
                      !!pendingWrites[`stop:${s.service_id}`] ||
                      !!pendingWrites[`del:${s.service_id}`]
                    }
                    onRun={handleRun}
                    onStop={handleStop}
                    onToggle={handleToggle}
                    onDelete={handleDelete}
                    onEdit={setEditing}
                  />
                ))}
              </div>
            )}
          </section>
        </div>
        </>
      )}

      {drawerOpen && (
        <AddContentDrawer
          initialProvider={selectedProvider}
          onClose={() => setDrawerOpen(false)}
          onCreated={() => setDrawerOpen(false)}
        />
      )}

      {editing && (
        <ServiceEditModal
          service={editing}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); refresh(); }}
        />
      )}
    </div>
  );
}

function NoDataIcon() {
  return (
    <svg className="pc-services__empty-icon" viewBox="0 0 80 80" width="80" height="80" fill="none">
      <path d="M12.5 24.875L69.68 24.875C71.7897 24.875 73.5 26.5853 73.5 28.695L73.5 70.1063C73.5 72.1877 71.8127 73.875 69.7313 73.875L16.2687 73.875C14.1873 73.875 12.5 72.1877 12.5 70.1063L12.5 24.875Z" fill="rgb(240,240,240)" fillRule="evenodd" />
      <path d="M8.81909 25.375L56.5 25.375L56.5 70.1063C56.5 72.1877 54.8127 73.875 52.7313 73.875L11.2687 73.875C9.18729 73.875 7.5 72.0814 7.5 70L7.5 39.5L0.5 39.5C0.5 38.7715 8.09061 25.375 8.81909 25.375Z" fill="rgb(255,255,255)" fillRule="evenodd" />
      <path d="M56.2949 25.3749L70.0906 25.3749C70.7734 25.3749 71.6071 25.2443 71.94 25.8404L78.7165 37.9733C78.9702 38.4276 79.4536 39.1212 78.9993 39.3749C78.8588 39.4534 78.0547 39.3749 77.8939 39.3749L66.2236 39.3749C64.8088 39.3749 63.5131 38.5825 62.8687 37.3229L56.2949 25.3749Z" fill="rgb(255,255,255)" fillRule="evenodd" />
      <path d="M73.619 39.3911L73.619 70.0621C73.619 72.2368 71.8548 73.9996 69.6786 73.9996L52.5977 73.9996L52.5977 73.1246L69.6786 73.1246C71.3712 73.1246 72.7434 71.7535 72.7434 70.0621L72.7434 39.3911L73.619 39.3911Z" fill="rgb(128,128,128)" fillRule="nonzero" />
      <path d="M56.9202 25.0005L56.9202 70.0621C56.9202 72.2367 55.156 73.9996 52.9798 73.9996L10.9483 73.9996C8.77201 73.9996 7.00781 72.2367 7.00781 70.0621L7.00781 39.3911L7.88347 39.3911L7.88347 70.0621C7.88347 71.7535 9.25563 73.1246 10.9483 73.1246L52.9798 73.1246C54.6724 73.1246 56.0446 71.7535 56.0446 70.0621L56.0446 25.8755L10.6716 25.8755L10.6716 25.0005L56.9202 25.0005Z" fill="rgb(128,128,128)" fillRule="nonzero" />
      <path d="M47.6109 0C48.2047 0 48.7566 0.312949 49.0717 0.828254L56.9113 13.6494C57.1639 14.0625 57.0416 14.6069 56.6382 14.8655C56.5011 14.9534 56.3427 15 56.1809 15L11.3545 15C10.1852 15 9.0957 14.3931 8.4605 13.3879L0 0L47.6109 0ZM47.3915 1L1.86778 1L9.3849 12.8125C9.85608 13.5529 10.6643 14 11.5316 14L55.8678 14L48.1139 1.40676C47.9581 1.15369 47.6851 1 47.3915 1Z" fill="rgb(128,128,128)" fillRule="nonzero" transform="matrix(-1,0,0,1,57.043,25)" />
      <path d="M70.7516 25C71.315 25 71.8397 25.3031 72.1441 25.8045L79.8692 38.5272C80.1161 38.9339 80.0045 39.4752 79.62 39.7363C79.4866 39.8269 79.3314 39.875 79.1729 39.875L66.1499 39.875C64.977 39.875 63.8917 39.2186 63.2971 38.1496L55.9824 25L70.7516 25ZM70.7285 26L57.8004 26L64.1868 37.7183C64.6196 38.5124 65.4095 39 66.2631 39L78.9004 39L71.4041 26.3984C71.2564 26.1501 71.0019 26 70.7285 26Z" fill="rgb(128,128,128)" fillRule="nonzero" />
      <g>
        <rect width="14" height="4" x="13.5" y="56" rx="1.886225" fill="rgb(20,118,255)" />
        <path d="M23.5625 63C23.8041 63 24 63.2239 24 63.5C24 63.7761 23.8041 64 23.5625 64L13.9375 64C13.6959 64 13.5 63.7761 13.5 63.5C13.5 63.2239 13.6959 63 13.9375 63L23.5625 63Z" fill="rgb(128,128,128)" fillRule="nonzero" />
        <rect width="5" height="1" x="13.5" y="66" rx="0.5" fill="rgb(128,128,128)" />
      </g>
      <path d="M39.9993 6C40.2773 6 40.5026 6.19725 40.5026 6.44058L40.5026 12.6086C40.5026 12.852 40.2773 13.0492 39.9993 13.0492C39.7214 13.0492 39.4961 12.852 39.4961 12.6086L39.4961 6.44058C39.4961 6.19725 39.7214 6 39.9993 6Z" fill="rgb(128,128,128)" fillRule="nonzero" />
      <path d="M0.503237 0C0.78117 0 1.00647 0.197253 1.00647 0.440575L1.00647 6.60863C1.00647 6.85195 0.78117 7.04921 0.503237 7.04921C0.225307 7.04921 0 6.85195 0 6.60863L0 0.440575C0 0.197253 0.225307 0 0.503237 0Z" fill="rgb(128,128,128)" fillRule="nonzero" transform="matrix(0.866025,0.5,-0.5,0.866025,56.1172,8.39185)" />
      <path d="M0.503238 0C0.781168 0 1.00648 0.197253 1.00648 0.440575L1.00648 6.60863C1.00648 6.85195 0.781168 7.0492 0.503238 7.0492C0.225307 7.0492 0 6.85195 0 6.60863L0 0.440575C0 0.197253 0.225307 0 0.503238 0Z" fill="rgb(128,128,128)" fillRule="nonzero" transform="matrix(0.866025,-0.5,0.5,0.866025,23.0117,8.89502)" />
    </svg>
  );
}

const STATUS_RING_PATH = 'M8 3C5.24 3 3 5.24 3 8C3 10.76 5.24 13 8 13C10.76 13 13 10.76 13 8C13 5.24 10.76 3 8 3ZM8 11C6.34 11 5 9.66 5 8C5 6.34 6.34 5 8 5C9.66 5 11 6.34 11 8C11 9.66 9.66 11 8 11Z';
const STATUS_WAITING_PATHS = [
  'M11 11L5 11L5 9.03003L3 9.03003L3 11C3 12.1 3.9 13 5 13L11 13C12.1 13 13 12.1 13 11L13 9.03003L11 9.03003L11 11Z',
  'M11 3L5 3C3.9 3 3 3.9 3 5L3 7.03L5 7.03L5 5L11 5L11 7.03L13 7.03L13 5C13 3.9 12.1 3 11 3Z',
];
const STATUS_RING_COLORS: Record<string, string> = {
  stateStopped: '#C2C2C2',
  stateCompleted: '#5CB300',
  stateFailed: '#F23030',
  stateCollecting: '#5CB300',
};

function StatusIcon({ statusKey }: { statusKey: string }) {
  if (statusKey === 'stateWaiting') {
    return (
      <svg className="pc-services__status-svg" viewBox="0 0 16 16" width="14" height="14" fill="none">
        <path d={STATUS_WAITING_PATHS[0]} fill="#0BB8B2" fillRule="nonzero" />
        <path d={STATUS_WAITING_PATHS[1]} fill="#0BB8B2" fillRule="nonzero" />
      </svg>
    );
  }
  const color = STATUS_RING_COLORS[statusKey] ?? '#C2C2C2';
  const pulse = statusKey === 'stateCollecting' ? ' pc-services__status-svg--pulse' : '';
  return (
    <svg className={`pc-services__status-svg${pulse}`} viewBox="0 0 16 16" width="14" height="14" fill="none">
      <path d={STATUS_RING_PATH} fill={color} fillRule="nonzero" />
    </svg>
  );
}

/** 单张内容卡片：名称 header + 左[频率+状态] + 右[启动/停止/编辑/删除 + 自动采集开关]。 */
function ServiceCard({
  service,
  state,
  lastError,
  progress,
  pending,
  onRun,
  onStop,
  onToggle,
  onDelete,
  onEdit,
}: {
  service: FetchServiceConfig;
  state: FetchServiceState;
  lastError: string | null;
  progress?: FetchRunProgress;
  pending: boolean;
  onRun: (id: string) => void;
  onStop: (id: string) => void;
  onToggle: (id: string, enabled: boolean) => void;
  onDelete: (id: string, state?: FetchServiceState) => void;
  onEdit: (s: FetchServiceConfig) => void;
}) {
  const { t } = useTranslation();
  const runState = progress?.run_state;
  const serviceRunning = state === 'STARTING' || state === 'RUNNING' || state === 'STOPPING';
  const isCollecting = runState != null
    ? runState === 'running'
    : serviceRunning && state !== 'RUNNING';
  const isFailed = runState != null
    ? runState === 'failed'
    : state === 'FAILED' || (!!lastError && state === 'STOPPED');

  const statusKey = isCollecting
    ? 'stateCollecting'
    : isFailed
      ? 'stateFailed'
      : runState === 'succeeded'
        ? 'stateCompleted'
        : !service.enabled
          ? 'stateStopped'
          : 'stateWaiting';

  const percent = progress?.progress_percent;
  const hasProgress = isCollecting && typeof percent === 'number' && percent > 0;

  const interval = service.interval_seconds;
  const isHour = interval % FREQUENCY_SECONDS.hour === 0;
  const isDay = interval % FREQUENCY_SECONDS.day === 0;
  const freqText = isDay
    ? t('personalContext.services.intervalDay', { n: Math.floor(interval / FREQUENCY_SECONDS.day) })
    : isHour
      ? t('personalContext.services.intervalHour', { n: Math.floor(interval / FREQUENCY_SECONDS.hour) })
      : `${interval}s`;

  return (
    <div className="pc-services__card">
      <div className="pc-services__card-head" title={service.service_id}>
        {service.service_id}
      </div>
      <div className="pc-services__card-body">
        {/* 左：采样周期 */}
        <div className="pc-services__card-freq">
          {freqText}
        </div>

        {/* 中：采集状态 */}
        <div className="pc-services__card-status">
          <StatusIcon statusKey={statusKey} />
          <span
            className={`pc-services__status-text pc-services__status-text--${statusKey}`}
            title={isFailed && lastError ? lastError : undefined}
          >
            {t(`personalContext.services.${statusKey}`)}
          </span>
          {isFailed && lastError ? (
            <svg className="pc-services__status-hint" viewBox="0 0 16 16" width="14" height="14" fill="none" role="img" aria-label={lastError}>
              <title>{lastError}</title>
              <path d="M8 1.5C4.41 1.5 1.5 4.41 1.5 8C1.5 11.59 4.41 14.5 8 14.5C11.59 14.5 14.5 11.59 14.5 8C14.5 4.41 11.59 1.5 8 1.5Z" fill="#F23030" />
              <path d="M8 4.5L8 8.5" stroke="#FFFFFF" strokeWidth="1.6" strokeLinecap="round" />
              <path d="M8 11L8 11.01" stroke="#FFFFFF" strokeWidth="1.8" strokeLinecap="round" />
            </svg>
          ) : null}
          {hasProgress && (
            <>
              <div className="pc-services__progress">
                <div
                  className="pc-services__progress-bar"
                  style={{ width: `${Math.min(percent!, 100)}%` }}
                />
              </div>
              <span className="pc-services__progress-text">{Math.round(percent!)}%</span>
            </>
          )}
        </div>

        {/* 右：操作按钮 + 开关 */}
        <div className="pc-services__card-actions">
          {isCollecting ? (
            <button type="button" className="pc-services__action-link" onClick={() => onStop(service.service_id)} disabled={pending}>
              {t('personalContext.services.actionStop')}
            </button>
          ) : (
            <button type="button" className="pc-services__action-link" onClick={() => onRun(service.service_id)} disabled={pending}>
              {t('personalContext.services.actionRunNow')}
            </button>
          )}
          <button type="button" className="pc-services__action-link" onClick={() => onEdit(service)}>
            {t('personalContext.services.actionEdit')}
          </button>
          <button type="button" className="pc-services__action-link pc-services__action-link--danger" onClick={() => onDelete(service.service_id, state)} disabled={pending}>
            {t('personalContext.services.actionDelete')}
          </button>
          <Switch
            checked={service.enabled}
            onChange={(enabled: boolean) => onToggle(service.service_id, enabled)}
            disabled={pending}
            title={service.enabled ? t('personalContext.services.autoOffHint') : t('personalContext.services.autoOnHint')}
          />
        </div>
      </div>
    </div>
  );
}
function ServiceEditModal({
  service,
  onClose,
  onSaved,
}: {
  service: FetchServiceConfig;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const [intervalSeconds, setIntervalSeconds] = useState(service.interval_seconds);
  const [maxItems, setMaxItems] = useState<number | null>(service.max_items_per_run);
  const [sourceJson, setSourceJson] = useState(JSON.stringify(service.source, null, 2));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    setError(null);
    if (!Number.isFinite(intervalSeconds) || intervalSeconds <= 0 || intervalSeconds > 31536000) {
      setError(t('personalContext.services.intervalRangeError'));
      return;
    }
    let source = service.source;
    if (sourceJson.trim()) {
      try {
        source = JSON.parse(sourceJson);
        if (!source || typeof source !== 'object' || Array.isArray(source)) {
          setError(t('personalContext.services.sourceJsonObjectError'));
          return;
        }
      } catch {
        setError(t('personalContext.services.sourceJsonInvalidError'));
        return;
      }
    }
    setSaving(true);
    try {
      await pcApi.patchService(service.service_id, {
        interval_seconds: intervalSeconds,
        max_items_per_run: maxItems,
        source,
      });
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="pc-services__modal-overlay" onClick={onClose}>
      <div className="pc-services__modal" onClick={(e) => e.stopPropagation()}>
        <h3 className="pc-services__modal-title">
          {t('personalContext.services.actionEdit')} — {service.service_id}
        </h3>
        <div className="pc-services__field">
          <label>{t('personalContext.services.intervalSeconds')}</label>
          <input
            type="number"
            className="pc-services__input"
            value={intervalSeconds}
            onChange={(e) => setIntervalSeconds(Number(e.target.value))}
          />
        </div>
        <div className="pc-services__field">
          <label>{t('personalContext.services.maxItems')}</label>
          <input
            type="number"
            className="pc-services__input"
            value={maxItems ?? ''}
            onChange={(e) => setMaxItems(e.target.value ? Number(e.target.value) : null)}
          />
        </div>
        <div className="pc-services__field">
          <label>source (JSON)</label>
          <textarea
            className="pc-services__textarea"
            rows={4}
            value={sourceJson}
            onChange={(e) => setSourceJson(e.target.value)}
          />
        </div>
        {error && <div className="pc-services__error">{error}</div>}
        <div className="pc-services__modal-actions">
          <button type="button" className="btn" onClick={onClose} disabled={saving}>
            {t('personalContext.services.cancel')}
          </button>
          <button type="button" className="btn primary" onClick={handleSave} disabled={saving}>
            {saving ? <Loader2 className="spin" size={14} /> : t('personalContext.services.save')}
          </button>
        </div>
      </div>
    </div>
  );
}
