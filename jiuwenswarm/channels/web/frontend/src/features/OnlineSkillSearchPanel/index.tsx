import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import { getSkillAvatar } from '../../utils/skillAvatar';
import { isClawHubOriginInstalled, normalizeSkillNetUrl } from '../../utils/skillNetUrl';

type OnlineSource = 'skillnet' | 'clawhub';
type LoadState = 'idle' | 'loading' | 'success' | 'error';

type OnlineSearchItem = {
  source: OnlineSource;
  name: string;
  description: string;
  identifier: string;
  version: string;
  author: string;
  /** ClawHub publisher; required for download when slug is shared by multiple owners. */
  owner_handle?: string;
  native_score?: number;
  category: string;
  updated_at: number;
  source_rank: number;
  fusion_score: number;
};

const onlineItemKey = (item: OnlineSearchItem) =>
  item.source === 'clawhub' && item.owner_handle
    ? `${item.source}:${item.owner_handle}/${item.identifier}`
    : `${item.source}:${item.identifier}`;

type OnlineSourceStatus = {
  source: OnlineSource;
  status: 'success' | 'error' | 'skipped';
  count: number;
  detail?: string;
  detail_key?: string;
};

type OnlineSearchResponse = {
  success: boolean;
  partial?: boolean;
  detail?: string;
  items?: OnlineSearchItem[];
  sources?: OnlineSourceStatus[];
};

type InstallResponse = {
  success: boolean;
  status?: string;
  detail?: string;
  detail_key?: string;
  skill?: { name?: string };
};

interface OnlineSkillSearchPanelProps {
  sessionId: string;
  externalSearchQuery: string;
  installedSkillNames?: ReadonlySet<string>;
  installedSkillOrigins?: ReadonlySet<string>;
  viewMode?: 'list' | 'grid';
  onInstalled?: (skillName: string) => void | Promise<void>;
}

const createAbortError = () => new DOMException('Skill installation polling aborted', 'AbortError');

const throwIfAborted = (signal: AbortSignal) => {
  if (signal.aborted) throw createAbortError();
};

const sleep = (delayMs: number, signal: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    if (signal.aborted) {
      reject(createAbortError());
      return;
    }

    const onAbort = () => {
      window.clearTimeout(timeoutId);
      reject(createAbortError());
    };
    const timeoutId = window.setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, delayMs);
    signal.addEventListener('abort', onAbort, { once: true });
  });

export function OnlineSkillSearchPanel({
  sessionId,
  externalSearchQuery,
  installedSkillNames,
  installedSkillOrigins,
  viewMode = 'list',
  onInstalled,
}: OnlineSkillSearchPanelProps) {
  const { t } = useTranslation();
  const [items, setItems] = useState<OnlineSearchItem[]>([]);
  const [loadState, setLoadState] = useState<LoadState>('idle');
  const [partial, setPartial] = useState(false);
  const [sourceStatuses, setSourceStatuses] = useState<OnlineSourceStatus[]>([]);
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const [installingKey, setInstallingKey] = useState<string | null>(null);
  const [installedKeys, setInstalledKeys] = useState<Set<string>>(() => new Set());
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const [enabledSources, setEnabledSources] = useState<Set<OnlineSource>>(
    () => new Set<OnlineSource>(['skillnet', 'clawhub'])
  );
  const requestSequenceRef = useRef(0);
  const installingKeyRef = useRef<string | null>(null);
  const installAbortControllerRef = useRef<AbortController | null>(null);

  const withSession = useCallback(
    (params?: Record<string, unknown>) => ({
      ...(params || {}),
      session_id: sessionId,
    }),
    [sessionId]
  );

  useEffect(
    () => () => {
      const abortController = installAbortControllerRef.current;
      installAbortControllerRef.current = null;
      installingKeyRef.current = null;
      abortController?.abort();
    },
    []
  );

  useEffect(() => {
    const query = externalSearchQuery.trim();
    const requestSequence = ++requestSequenceRef.current;
    setMessage(null);
    if (!query) {
      setItems([]);
      setPartial(false);
      setSourceStatuses([]);
      setExpandedKey(null);
      setLoadState('idle');
      return;
    }

    setLoadState('loading');
    setItems([]);
    setPartial(false);
    setSourceStatuses([]);
    setExpandedKey(null);
    void (async () => {
      try {
        const data = await webRequest<OnlineSearchResponse>('skills.online_search.search', withSession({ q: query, limit: 20 }), { timeoutMs: 45_000 });
        if (requestSequence !== requestSequenceRef.current) return;
        setItems(data.items || []);
        setPartial(Boolean(data.partial));
        setSourceStatuses(data.sources || []);
        if (!data.success) {
          throw new Error(data.detail || t('skills.onlineSearch.searchFailed'));
        }
        setLoadState('success');
      } catch (error) {
        if (requestSequence !== requestSequenceRef.current) return;
        console.error(error);
        setItems([]);
        setLoadState('error');
        setMessage({
          type: 'error',
          text: error instanceof Error ? error.message : t('skills.onlineSearch.searchFailed'),
        });
      }
    })();
  }, [externalSearchQuery, t, withSession]);

  const isInstalled = useCallback(
    (item: OnlineSearchItem) => {
      const key = onlineItemKey(item);
      if (installedKeys.has(key)) return true;
      if (item.source === 'skillnet') {
        return item.identifier ? (installedSkillOrigins?.has(normalizeSkillNetUrl(item.identifier)) ?? false) : (installedSkillNames?.has(item.name) ?? false);
      }
      // ClawHub：按 owner+slug 精确匹配，避免同名 slug 全部显示已安装
      return isClawHubOriginInstalled(item.identifier, item.owner_handle, installedSkillOrigins);
    },
    [installedKeys, installedSkillNames, installedSkillOrigins]
  );

  const installSkillNet = useCallback(
    async (item: OnlineSearchItem, force: boolean, signal: AbortSignal): Promise<InstallResponse> => {
      throwIfAborted(signal);
      const data = await webRequest<{
        success: boolean;
        pending?: boolean;
        install_id?: string;
        detail?: string;
        detail_key?: string;
        skill?: { name?: string };
      }>('skills.skillnet.install', withSession({ url: item.identifier, force }));
      throwIfAborted(signal);
      if (!data.success || !data.pending || !data.install_id) return data;

      const startedAt = Date.now();
      while (Date.now() - startedAt < 15 * 60 * 1000) {
        const status = await webRequest<InstallResponse>('skills.skillnet.install_status', withSession({ install_id: data.install_id }));
        throwIfAborted(signal);
        if (status.status === 'done' && status.success) return status;
        if (status.status === 'failed' || (!status.success && status.status !== 'pending')) return status;
        await sleep(800, signal);
      }
      return { success: false, detail: t('skills.skillNet.installTimeout') };
    },
    [t, withSession]
  );

  const handleInstall = useCallback(
    async (item: OnlineSearchItem) => {
      const key = onlineItemKey(item);
      if (installingKeyRef.current) return;

      const abortController = new AbortController();
      installingKeyRef.current = key;
      installAbortControllerRef.current = abortController;
      setInstallingKey(key);
      setMessage(null);
      try {
        let force = false;
        let skillName = item.name || item.identifier;
        while (true) {
          if (item.source === 'skillnet') {
            const data = await installSkillNet(item, force, abortController.signal);
            if (!data.success) {
              if (!force && data.detail_key === 'skills.skillNet.errors.skillAlreadyInstalled') {
                if (window.confirm(t('skills.skillNet.replaceConfirm', { name: item.name }))) {
                  force = true;
                  continue;
                }
                return;
              }
              throw new Error(data.detail_key ? t(data.detail_key) : data.detail || t('skills.onlineSearch.installFailed'));
            }
            skillName = data.skill?.name || skillName;
          } else {
            const data = await webRequest<InstallResponse>(
              'skills.clawhub.download',
              withSession({
                slug: item.identifier,
                ...(item.owner_handle ? { owner_handle: item.owner_handle } : {}),
                ...(item.name ? { display_name: item.name } : {}),
                force,
              })
            );
            throwIfAborted(abortController.signal);
            if (!data.success) {
              if (!force && data.detail_key === 'skills.clawhub.errors.skillAlreadyInstalled') {
                if (window.confirm(t('skills.clawhub.replaceConfirm', { name: item.name }))) {
                  force = true;
                  continue;
                }
                return;
              }
              throw new Error(data.detail_key ? t(data.detail_key) : data.detail || t('skills.onlineSearch.installFailed'));
            }
            skillName = data.skill?.name || skillName;
          }
          break;
        }

        throwIfAborted(abortController.signal);
        setInstalledKeys(current => new Set([...current, key]));
        setMessage({ type: 'success', text: t('skills.onlineSearch.installed', { name: skillName }) });
        await onInstalled?.(skillName);
      } catch (error) {
        if (abortController.signal.aborted) return;
        console.error(error);
        setMessage({
          type: 'error',
          text: error instanceof Error ? error.message : t('skills.onlineSearch.installFailed'),
        });
      } finally {
        if (installAbortControllerRef.current === abortController) {
          installAbortControllerRef.current = null;
          installingKeyRef.current = null;
          if (!abortController.signal.aborted) setInstallingKey(null);
        }
      }
    },
    [installSkillNet, onInstalled, t, withSession]
  );

  const toggleSourceFilter = useCallback((source: OnlineSource) => {
    setEnabledSources(prev => {
      const next = new Set(prev);
      if (next.has(source)) {
        if (next.size <= 1) return prev;
        next.delete(source);
      } else {
        next.add(source);
      }
      return next;
    });
  }, []);

  const visibleItems = items.filter(item => enabledSources.has(item.source));

  return (
    <div data-testid='online-skill-search-panel' className='flex h-full min-h-0 flex-col'>
      {message?.type === 'success' ? (
        <div
          data-testid='online-skill-search-panel-message' data-variant='success'
          className='fixed right-4 top-4 z-[9999] flex h-[40px] w-[564px] items-center gap-3 rounded-[4px] px-4 text-sm text-text shadow-lg'
          style={{ backgroundColor: 'var(--color-feedback-success-toast)' }}
        >
          <span className='flex h-4 w-4 flex-shrink-0 items-center justify-center rounded-full bg-[var(--color-feedback-success-indicator)]'>
            <svg className='h-3 w-3 text-text-inverse' fill='none' stroke='currentColor' viewBox='0 0 24 24'>
              <path strokeLinecap='round' strokeLinejoin='round' strokeWidth={3} d='M5 13l4 4L19 7' />
            </svg>
          </span>
          {message.text}
        </div>
      ) : null}
      {message?.type === 'error' ? (
        <div data-testid='online-skill-search-panel-message' data-variant='error' className='mb-3 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger'>{message.text}</div>
      ) : null}
      {partial ? (
        <div data-testid='online-skill-search-panel-message' data-variant='partial' className='mb-3 rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn'>{t('skills.onlineSearch.partialFailure')}</div>
      ) : null}
      {sourceStatuses.length > 0 ? (
        <div data-testid='online-skill-search-panel-source-filter' className='mb-3 flex flex-wrap items-center gap-2 text-xs'>
          {sourceStatuses.map(sourceStatus => {
            const sourceLabel = sourceStatus.source === 'skillnet' ? 'SkillNet' : 'ClawHub';
            const statusLabel = t(`skills.onlineSearch.sourceStatus.${sourceStatus.status}`, { count: sourceStatus.count });
            const detail = sourceStatus.detail_key ? t(sourceStatus.detail_key) : sourceStatus.detail;
            const selected = enabledSources.has(sourceStatus.source);
            const isLastSelected = selected && enabledSources.size <= 1;
            const statusClass = !selected
              ? 'border-border bg-secondary text-text-muted opacity-60'
              : sourceStatus.status === 'success'
                ? 'border-[color:var(--color-border-success)] bg-ok-subtle text-ok'
                : sourceStatus.status === 'error'
                  ? 'border-danger/40 bg-danger/10 text-danger'
                  : 'border-border bg-secondary text-text-muted';
            return (
              <button
                key={sourceStatus.source}
                type='button'
                data-testid={`online-skill-search-panel-source-filter-button-${sourceStatus.source}`}
                aria-pressed={selected}
                title={
                  isLastSelected
                    ? t('skills.onlineSearch.sourceFilterKeepOne')
                    : detail || t('skills.onlineSearch.sourceFilterToggle')
                }
                onClick={() => toggleSourceFilter(sourceStatus.source)}
                className={`rounded-full border px-2 py-1 transition-opacity ${statusClass} ${
                  isLastSelected ? 'cursor-default' : 'cursor-pointer hover:opacity-90'
                }`}
              >
                {sourceLabel}: {statusLabel}
              </button>
            );
          })}
          <span
            data-testid='online-skill-search-panel-source-filter-help'
            className='inline-flex h-4 w-4 items-center justify-center rounded-full border border-border text-[10px] font-normal text-text-muted cursor-help'
            title={t('skills.onlineSearch.sourceFilterHelp') ?? undefined}
            aria-label={t('skills.onlineSearch.sourceFilterHelp')}
          >
            ?
          </span>
        </div>
      ) : null}

      <div data-testid='online-skill-search-panel-results' className='min-h-0 flex-1 overflow-auto'>
        {loadState === 'loading' ? <div data-testid='online-skill-search-panel-loading' className='flex h-full items-center justify-center text-text-muted'>{t('common.loading')}</div> : null}
        {loadState === 'error' && !message ? <div data-testid='online-skill-search-panel-error-text' className='text-sm text-text-muted'>{t('skills.onlineSearch.searchFailed')}</div> : null}
        {loadState === 'success' && items.length === 0 ? <div data-testid='online-skill-search-panel-no-results' className='text-sm text-text-muted'>{t('skills.onlineSearch.noResults')}</div> : null}
        {loadState === 'success' && items.length > 0 && visibleItems.length === 0 ? (
          <div data-testid='online-skill-search-panel-no-filtered-results' className='text-sm text-text-muted'>{t('skills.onlineSearch.noFilteredResults')}</div>
        ) : null}
        {loadState === 'success' && visibleItems.length > 0 ? (
          <div data-testid='online-skill-search-panel-list' data-variant={viewMode} className={`mt-4 min-h-0 flex-1 overflow-y-auto ${viewMode === 'grid' ? 'flex flex-wrap content-start gap-4' : 'space-y-3'}`}>
            {visibleItems.map(item => {
              const key = onlineItemKey(item);
              const installed = isInstalled(item);
              const installing = installingKey === key;
              const isSkillNet = item.source === 'skillnet';
              const isExpanded = isSkillNet && expandedKey === key;
              const avatar = getSkillAvatar(item.name || item.identifier || '?');
              const installLabel = installing
                ? isSkillNet
                  ? t('skills.skillNet.installingInProgress')
                  : t('skills.clawhub.installing')
                : isSkillNet
                  ? t('skills.skillNet.installFromResult')
                  : t('skills.actions.install');
              const installControl = installed ? (
                <span data-testid='online-skill-search-panel-item-install-status' data-variant='installed' className='flex h-[28px] items-center whitespace-nowrap rounded-2xl border border-[color:var(--color-border-success)] bg-ok-subtle px-4 text-sm text-ok'>
                  {t('skills.status.installed')}
                </span>
              ) : (
                <button
                  type='button'
                  onClick={() => void handleInstall(item)}
                  disabled={Boolean(installingKey)}
                  data-testid='online-skill-search-panel-item-install-button'
                  className={`h-[28px] min-w-[76px] whitespace-nowrap rounded-[24px] border border-text px-3 text-sm text-text hover:bg-secondary/50 ${
                    installingKey ? 'cursor-not-allowed text-text-muted' : ''
                  }`}
                >
                  {installLabel}
                </button>
              );
              return (
                <div
                  key={key}
                  data-testid='online-skill-search-panel-item' data-variant={viewMode}
                  className={`rounded-lg border border-border bg-panel p-4 ${viewMode === 'grid' ? 'flex flex-col' : 'flex items-start justify-between gap-4'}`}
                  style={viewMode === 'grid' ? { width: '496px', height: isExpanded ? 'auto' : '168px', flexShrink: 0 } : undefined}
                >
                  {viewMode === 'list' ? (
                    <>
                      <div className='flex min-w-0 flex-1 items-center gap-3'>
                        <div data-testid='online-skill-search-panel-item-avatar' className={`flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-lg ${avatar.color} font-semibold text-text-inverse`}>
                          {avatar.firstChar}
                        </div>
                        <div className='min-w-0 flex-1'>
                          <div className='flex min-w-0 items-center gap-2'>
                            <div data-testid='online-skill-search-panel-item-name' className='min-w-0 truncate text-base font-semibold text-text-strong'>{item.name}</div>
                            <span data-testid='online-skill-search-panel-item-source-badge' data-variant={item.source} className='flex-shrink-0 rounded-full border border-border bg-secondary px-2 py-0.5 text-xs font-normal text-text-muted'>
                              {isSkillNet ? 'SkillNet' : 'ClawHub'}
                            </span>
                          </div>
                          <div data-testid='online-skill-search-panel-item-description' className='mt-1 line-clamp-3 text-sm text-text-muted'>{item.description || t('skills.noDescription')}</div>
                          {isSkillNet ? (
                            <div data-testid='online-skill-search-panel-item-meta' className='mt-1 text-xs text-text-muted'>
                              {t('skills.skillNet.meta', { author: item.author || 'unknown', stars: item.native_score || 0 })}
                            </div>
                          ) : null}
                          {isExpanded ? (
                            <div data-testid='online-skill-search-panel-item-detail' data-variant='expanded' className='mt-2 space-y-1 break-all text-xs text-text-muted'>
                              <div>
                                {t('skills.skillNet.category')}: {item.category || 'unknown'}
                              </div>
                              <div>
                                {t('skills.skillNet.url')}:{' '}
                                <a data-testid='online-skill-search-panel-item-url' href={item.identifier} target='_blank' rel='noreferrer' className='text-accent hover:underline'>
                                  {item.identifier}
                                </a>
                              </div>
                            </div>
                          ) : null}
                        </div>
                      </div>
                      <div className={`flex flex-shrink-0 flex-col items-end ${isSkillNet ? 'gap-1' : 'gap-2'}`}>
                        {installControl}
                        {isSkillNet ? (
                          <button
                            type='button'
                            onClick={() => setExpandedKey(current => (current === key ? null : key))}
                            data-testid='online-skill-search-panel-item-toggle-detail'
                            className='whitespace-nowrap text-xs text-link hover:underline'
                          >
                            {isExpanded ? t('skills.skillNet.hideDetail') : t('skills.skillNet.showDetail')}
                          </button>
                        ) : null}
                      </div>
                    </>
                  ) : (
                    <>
                      <div className='flex flex-shrink-0 items-start gap-3'>
                        <div
                          data-testid='online-skill-search-panel-item-avatar'
                          className={`flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-lg ${avatar.color} text-sm font-semibold text-text-inverse`}
                        >
                          {avatar.firstChar}
                        </div>
                        <div className='min-w-0 flex-1'>
                          <div className='flex min-w-0 items-center gap-2'>
                            <div data-testid='online-skill-search-panel-item-name' className='min-w-0 truncate text-sm font-semibold text-text-strong'>{item.name}</div>
                            <span data-testid='online-skill-search-panel-item-source-badge' data-variant={item.source} className='flex-shrink-0 rounded-full border border-border bg-secondary px-2 py-0.5 text-xs font-normal text-text-muted'>
                              {isSkillNet ? 'SkillNet' : 'ClawHub'}
                            </span>
                          </div>
                          <div data-testid='online-skill-search-panel-item-description' className='mt-1 line-clamp-2 text-xs text-text-muted'>{item.description || t('skills.noDescription')}</div>
                        </div>
                      </div>
                      <div className='mt-2 flex flex-shrink-0 flex-wrap gap-1.5 text-xs text-text-muted'>
                        <span data-testid='online-skill-search-panel-item-meta' className='truncate rounded-full border border-border bg-secondary px-2 py-0.5'>
                          {isSkillNet
                            ? t('skills.skillNet.meta', { author: item.author || 'unknown', stars: item.native_score || 0 })
                            : item.updated_at
                              ? t('skills.clawhub.updatedAt', { date: new Date(item.updated_at).toLocaleDateString() })
                              : `v${item.version || 'latest'}`}
                        </span>
                      </div>
                      {isExpanded ? (
                        <div data-testid='online-skill-search-panel-item-detail' data-variant='expanded' className='mt-2 flex-shrink-0 space-y-1 break-all text-xs text-text-muted'>
                          <div className='truncate'>
                            {t('skills.skillNet.category')}: {item.category || 'unknown'}
                          </div>
                          <div className='truncate'>
                            {t('skills.skillNet.url')}:{' '}
                            <a data-testid='online-skill-search-panel-item-url' href={item.identifier} target='_blank' rel='noreferrer' className='text-accent hover:underline'>
                              {item.identifier}
                            </a>
                          </div>
                        </div>
                      ) : null}
                      <div className='mt-auto flex w-full flex-shrink-0 items-center gap-2 pt-2'>
                        <div className='flex flex-1 gap-1.5'>
                          {isSkillNet ? (
                            <button
                              type='button'
                              onClick={() => setExpandedKey(current => (current === key ? null : key))}
                              data-testid='online-skill-search-panel-item-toggle-detail'
                              className='whitespace-nowrap text-xs text-link hover:underline'
                            >
                              {isExpanded ? t('skills.skillNet.hideDetail') : t('skills.skillNet.showDetail')}
                            </button>
                          ) : null}
                        </div>
                        <div className='ml-auto flex-shrink-0'>{installControl}</div>
                      </div>
                    </>
                  )}
                </div>
              );
            })}
          </div>
        ) : null}
      </div>
    </div>
  );
}
