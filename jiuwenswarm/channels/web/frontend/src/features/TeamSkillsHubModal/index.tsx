/**
 * Team Skills Hub（teamskillshub）在线检索弹窗：从 Hub 检索并安装技能。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import { getSkillAvatar } from '../../utils/skillAvatar';

/** 与后端 TEAM_SKILLS_HUB_BASE_URL 默认值一致（info 请求失败时的回退） */
const DEFAULT_TEAMSKILLS_HUB_BASE_URL = 'https://teamskills.openjiuwen.com';

type LoadState = 'idle' | 'loading' | 'success' | 'error';

type TeamSkillsHubSkillItem = {
  source_id: string;
  skill_id?: string;
  version_id: string;
  name: string;
  display_name?: string;
  summary?: string;
  version?: string;
  updated_at?: string | number;
};

type SkillUpdateStatus = {
  source_id: string;
  name: string;
  skill_id?: string;
  current_version_id?: string;
  latest_version_id?: string;
  has_update: boolean;
  remote_status?: string;
};

type SkillSourceDescriptor = {
  source?: string;
  source_id: string;
  enabled: boolean;
  capabilities?: string[];
};

function skillSourceIdentity(item: TeamSkillsHubSkillItem, fallbackSourceId: string): string {
  return `${item.source_id || fallbackSourceId}:${item.skill_id || item.name}`;
}

interface TeamSkillsHubModalProps {
  open: boolean;
  embedded?: boolean;
  sessionId: string;
  /** 外部传入的搜索关键词 */
  externalSearchQuery?: string;
  /** 已安装技能的来源标识（用于精确匹配，避免同名误判） */
  installedSkillOrigins?: ReadonlySet<string>;
  /** 视图模式：列表或平铺 */
  viewMode?: 'list' | 'grid';
  onClose: () => void;
  onInstalled?: (skillName: string) => void | Promise<void>;
}

export function TeamSkillsHubModal({
  open,
  embedded = false,
  sessionId,
  externalSearchQuery,
  installedSkillOrigins,
  viewMode = 'list',
  onClose,
  onInstalled,
}: TeamSkillsHubModalProps) {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<TeamSkillsHubSkillItem[]>([]);
  const [loadState, setLoadState] = useState<LoadState>('idle');
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const [installingSkillKey, setInstallingSkillKey] = useState<string | null>(null);
  // 与原 asset_id 语义一致：按来源和来源内 skill_id 唯一标识，避免同名跨源误判。
  const [installedSkillKeys, setInstalledSkillKeys] = useState<Set<string>>(new Set());
  const [updateStatuses, setUpdateStatuses] = useState<Map<string, SkillUpdateStatus>>(new Map());
  const [sourceId, setSourceId] = useState('');
  const [hubBaseUrl, setHubBaseUrl] = useState(DEFAULT_TEAMSKILLS_HUB_BASE_URL);
  const messageTimerRef = useRef<number | null>(null);

  const withSession = useCallback(
    (params?: Record<string, unknown>) => ({
      ...(params || {}),
      session_id: sessionId,
    }),
    [sessionId],
  );

  const showMessage = useCallback((type: 'success' | 'error', text: string) => {
    if (messageTimerRef.current !== null) {
      window.clearTimeout(messageTimerRef.current);
      messageTimerRef.current = null;
    }
    setMessage({ type, text });
    messageTimerRef.current = window.setTimeout(() => {
      setMessage(null);
      messageTimerRef.current = null;
    }, 3000);
  }, []);

  useEffect(
    () => () => {
      if (messageTimerRef.current !== null) {
        window.clearTimeout(messageTimerRef.current);
        messageTimerRef.current = null;
      }
    },
    [],
  );

  useEffect(() => {
    if (!open) return;
    setInstalledSkillKeys(new Set());
    setUpdateStatuses(new Map());
    setSourceId('');
    setHubBaseUrl(DEFAULT_TEAMSKILLS_HUB_BASE_URL);
    let cancelled = false;
    void (async () => {
      try {
        const data = await webRequest<{
          success: boolean;
          error_message?: string;
          providers?: SkillSourceDescriptor[];
        }>('skills.source.providers', withSession());
        const provider = (data.providers || []).find(item => item.enabled !== false && (item.capabilities || []).includes('search'));
        const selected = provider?.source_id || provider?.source || '';
        if (!data.success || !selected) {
          throw new Error(data.error_message || t('skills.teamskillshub.errors.searchFailed'));
        }
        if (!cancelled) setSourceId(selected);
      } catch (error) {
        if (cancelled) return;
        setResults([]);
        setLoadState('error');
        showMessage('error', error instanceof Error ? error.message : t('skills.teamskillshub.errors.searchFailed'));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, showMessage, t, withSession]);

  useEffect(() => {
    if (!open || !sourceId) return;
    let cancelled = false;
    void (async () => {
      try {
        const data = await webRequest<{
          success: boolean;
          items?: SkillUpdateStatus[];
        }>('skills.updates.check', withSession({ source_id: sourceId }));
        if (!cancelled && data.success) {
          setUpdateStatuses(new Map((data.items || []).map(item => [`${item.source_id}:${item.name}`, item])));
        }
      } catch (error) {
        console.debug('Skill update check skipped', error);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, sourceId, withSession]);

  useEffect(() => {
    if (embedded && externalSearchQuery !== undefined) {
      setQuery(externalSearchQuery);
    }
  }, [externalSearchQuery, embedded]);

  useEffect(() => {
    if (embedded && sourceId && query.trim()) {
      const q = query.trim();
      setLoadState('loading');
      setMessage(null);
      void (async () => {
        try {
          const data = await webRequest<{
            success: boolean;
            error_message?: string;
            items?: TeamSkillsHubSkillItem[];
          }>(
            'skills.source.search',
            withSession({
              source_id: sourceId,
              q,
              page: 1,
              page_size: 50,
            }),
          );
          if (!data.success) {
            throw new Error(data.error_message || t('skills.teamskillshub.errors.searchFailed'));
          }
          setResults(data.items || []);
          setLoadState('success');
        } catch (error) {
          console.error(error);
          setResults([]);
          setLoadState('error');
          showMessage('error', error instanceof Error ? error.message : t('skills.teamskillshub.errors.searchFailed'));
        }
      })();
    }
  }, [query, embedded, showMessage, sourceId, t, withSession]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    void (async () => {
      try {
        const data = await webRequest<{
          success?: boolean;
          market_base_url?: string;
        }>('skills.teamskillshub.info', withSession());
        const url = data.market_base_url?.trim();
        if (!cancelled && data.success && url) {
          try {
            // 确保为合法绝对 URL（与服务端配置的基地址一致）
            setHubBaseUrl(new URL(url).href.replace(/\/$/, ''));
          } catch {
            setHubBaseUrl(url.replace(/\/$/, ''));
          }
        }
      } catch {
        if (!cancelled) setHubBaseUrl(DEFAULT_TEAMSKILLS_HUB_BASE_URL);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, withSession]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    if (open) {
      window.addEventListener('keydown', handleKeyDown);
      return () => window.removeEventListener('keydown', handleKeyDown);
    }
  }, [open, onClose]);

  const handleSearch = useCallback(async () => {
    const q = query.trim();
    if (!q || !sourceId) return;
    setLoadState('loading');
    setMessage(null);
    try {
      const data = await webRequest<{
        success: boolean;
        error_message?: string;
        items?: TeamSkillsHubSkillItem[];
      }>(
        'skills.source.search',
        withSession({
          source_id: sourceId,
          q,
          page: 1,
          page_size: 50,
        }),
      );
      if (!data.success) {
        throw new Error(data.error_message || t('skills.teamskillshub.errors.searchFailed'));
      }
      setResults(data.items || []);
      setLoadState('success');
    } catch (error) {
      console.error(error);
      setResults([]);
      setLoadState('error');
      showMessage('error', error instanceof Error ? error.message : t('skills.teamskillshub.errors.searchFailed'));
    }
  }, [query, showMessage, sourceId, t, withSession]);

  const handleInstall = useCallback(
    async (item: TeamSkillsHubSkillItem) => {
      if (installingSkillKey) return;
      const itemKey = skillSourceIdentity(item, sourceId);
      setInstallingSkillKey(itemKey);
      setMessage(null);
      let force = false;
      try {
        while (true) {
          const data = await webRequest<{
            success: boolean;
            error_code?: string;
            error_message?: string;
            skill?: { name: string };
          }>(
            'skills.source.install',
            withSession({
              source_id: item.source_id || sourceId,
              skill_id: item.skill_id,
              version_id: item.version_id,
              force,
            }),
          );
          if (!data.success) {
            // 已安装冲突：弹确认框询问是否覆盖
            if (!force && data.error_code === 'skill_already_installed') {
              const confirmed = window.confirm(t('skills.teamskillshub.replaceConfirm', { name: item.name }));
              if (confirmed) {
                force = true;
                continue;
              }
              return;
            }
            throw new Error(data.error_message || t('skills.teamskillshub.errors.installFailed'));
          }
          // 安装成功
          const skillName = data.skill?.name || item.name;
          setInstalledSkillKeys(prev => new Set([...prev, itemKey]));
          showMessage('success', t('skills.teamskillshub.messages.installed', { name: skillName }));
          await onInstalled?.(skillName);
          break;
        }
      } catch (error) {
        console.error(error);
        showMessage('error', error instanceof Error ? error.message : t('skills.teamskillshub.errors.installFailed'));
      } finally {
        setInstallingSkillKey(null);
      }
    },
    [installingSkillKey, onInstalled, showMessage, sourceId, t, withSession],
  );

  const handleUpdate = useCallback(
    async (item: TeamSkillsHubSkillItem, status: SkillUpdateStatus) => {
      if (installingSkillKey) return;
      const targetVersionId = status.latest_version_id || item.version_id;
      setInstallingSkillKey(skillSourceIdentity(item, sourceId));
      setMessage(null);
      try {
        const data = await webRequest<{
          success: boolean;
          error_message?: string;
          skill?: { name?: string };
        }>(
          'skills.update',
          withSession({
            source_id: item.source_id || sourceId,
            skill_id: item.skill_id,
            target_version_id: targetVersionId,
            expected_current_version_id: status.current_version_id,
          }),
        );
        if (!data.success) {
          throw new Error(data.error_message || t('skills.teamskillshub.errors.updateFailed'));
        }
        setUpdateStatuses(previous => {
          const next = new Map(previous);
          next.set(`${item.source_id}:${item.name}`, {
            ...status,
            current_version_id: targetVersionId,
            latest_version_id: targetVersionId,
            has_update: false,
          });
          return next;
        });
        showMessage('success', t('skills.teamskillshub.messages.updated', { name: data.skill?.name || item.name }));
        await onInstalled?.(data.skill?.name || item.name);
      } catch (error) {
        console.error(error);
        showMessage('error', error instanceof Error ? error.message : t('skills.teamskillshub.errors.updateFailed'));
      } finally {
        setInstallingSkillKey(null);
      }
    },
    [installingSkillKey, onInstalled, showMessage, sourceId, t, withSession],
  );

  if (!open) return null;

  if (embedded) {
    return (
      <div className="flex flex-col h-full">
        <div className="overflow-auto flex-1 min-h-0">
          {message && message.type === 'success' && (
            <div
              className="fixed top-4 right-4 z-[9999] rounded-[4px] text-sm text-text shadow-lg flex items-center gap-3 px-4"
              style={{ backgroundColor: 'var(--color-feedback-success-toast)', width: '564px', height: '40px' }}
            >
              <span className="w-4 h-4 rounded-full bg-[var(--color-feedback-success-indicator)] flex items-center justify-center flex-shrink-0">
                <svg className="w-3 h-3 text-text-inverse" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                </svg>
              </span>
              {message.text.replace('√', '')}
              <button
                type="button"
                onClick={() => showMessage('success', '')}
                className="ml-auto w-6 h-6 flex items-center justify-center hover:bg-card/30 rounded-full "
              >
                <svg className="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          )}
          {message && message.type === 'error' && (
            <div className="mb-3 px-3 py-2.5 rounded-lg text-sm leading-snug border border-danger/40 bg-danger/10 text-danger">{message.text}</div>
          )}

          {loadState === 'loading' && <div className="flex items-center justify-center h-full text-text-muted">{t('common.loading')}</div>}
          {loadState === 'error' && <div className="text-sm text-text-muted">{t('skills.teamskillshub.errors.searchFailed')}</div>}
          {loadState === 'success' && (
            <div className={`mt-4 flex-1 min-h-0 overflow-y-auto ${viewMode === 'grid' ? 'flex flex-wrap gap-4 content-start' : 'space-y-3'}`}>
              {results.length === 0 ? (
                <div className="text-sm text-text-muted">{t('skills.teamskillshub.noResults')}</div>
              ) : (
                results.map(item => {
                  const updateStatus = updateStatuses.get(`${item.source_id}:${item.name}`);
                  const isInstalled =
                    Boolean(updateStatus) ||
                    installedSkillKeys.has(skillSourceIdentity(item, sourceId)) ||
                    (installedSkillOrigins?.has(`${item.source_id || sourceId}:${item.name}`) ?? false) ||
                    (item.skill_id != null &&
                      ((installedSkillOrigins?.has(`${item.source_id || sourceId}:${item.skill_id}`) ?? false) ||
                        (installedSkillOrigins?.has(`teamskillshub:${item.skill_id}`) ?? false)));
                  const isInstalling = installingSkillKey === skillSourceIdentity(item, sourceId);
                  const avatar = getSkillAvatar(item.name);
                  return (
                    <div
                      key={`${item.source_id}:${item.name}:${item.version_id}`}
                      className={`p-4 rounded-lg border border-border bg-panel ${viewMode === 'grid' ? 'flex flex-col' : 'flex items-start justify-between gap-4'}`}
                      style={viewMode === 'grid' ? { width: '496px', height: '168px', flexShrink: 0 } : undefined}
                    >
                      {viewMode === 'list' ? (
                        <>
                          <div className="flex items-center gap-3 min-w-0 flex-1">
                            <div
                              className={`w-10 h-10 rounded-lg ${avatar.color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold`}
                            >
                              {avatar.firstChar}
                            </div>
                            <div className="min-w-0 flex-1">
                              <div className="text-base font-semibold text-text-strong truncate">{item.name}</div>
                              <div className="text-sm text-text-muted mt-1 line-clamp-3">{item.summary || t('skills.noDescription')}</div>
                            </div>
                          </div>
                          <div className="flex flex-col items-end gap-2 flex-shrink-0">
                            {isInstalled && updateStatus?.has_update ? (
                              <button
                                type="button"
                                onClick={() => void handleUpdate(item, updateStatus)}
                                disabled={isInstalling}
                                className="min-w-[76px] h-[28px] px-3 rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50 whitespace-nowrap disabled:text-text-muted disabled:cursor-not-allowed"
                              >
                                {isInstalling ? t('skills.teamskillshub.updating') : t('common.update')}
                              </button>
                            ) : isInstalled ? (
                              <span className="px-4 h-[28px] flex items-center rounded-2xl text-sm whitespace-nowrap border border-[color:var(--color-border-success)] bg-ok-subtle text-ok">
                                {t('skills.status.installed')}
                              </span>
                            ) : (
                              <button
                                type="button"
                                onClick={() => void handleInstall(item)}
                                disabled={isInstalling}
                                className={`min-w-[76px] h-[28px] px-3 rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50  whitespace-nowrap ${
                                  isInstalling ? 'text-text-muted cursor-not-allowed' : 'text-text'
                                }`}
                              >
                                {isInstalling ? t('skills.teamskillshub.installing') : t('skills.actions.install')}
                              </button>
                            )}
                          </div>
                        </>
                      ) : (
                        <>
                          <div className="flex items-start gap-3 flex-shrink-0">
                            <div
                              className={`w-10 h-10 rounded-lg ${avatar.color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold text-sm`}
                            >
                              {avatar.firstChar}
                            </div>
                            <div className="min-w-0 flex-1">
                              <div className="text-sm font-semibold text-text-strong truncate">{item.name}</div>
                              <div className="text-xs text-text-muted mt-1 line-clamp-2">{item.summary || t('skills.noDescription')}</div>
                            </div>
                          </div>
                          <div className="flex flex-wrap gap-1.5 mt-2 flex-shrink-0 text-xs text-text-muted">
                            <span className="px-2 py-0.5 rounded-full bg-secondary border border-border truncate">
                              {t('skills.versionLabel')}: {item.version || 'latest'}
                            </span>
                          </div>
                          <div className="flex items-center mt-auto pt-2 gap-2 flex-shrink-0" style={{ width: '100%' }}>
                            <div className="flex gap-1.5 flex-1"></div>
                            <div className="flex-shrink-0 ml-auto">
                              {isInstalled && updateStatus?.has_update ? (
                                <button
                                  type="button"
                                  onClick={() => void handleUpdate(item, updateStatus)}
                                  disabled={isInstalling}
                                  className="min-w-[76px] h-[28px] px-3 rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50 whitespace-nowrap disabled:text-text-muted disabled:cursor-not-allowed"
                                >
                                  {isInstalling ? t('skills.teamskillshub.updating') : t('common.update')}
                                </button>
                              ) : isInstalled ? (
                                <span className="px-4 h-[28px] flex items-center rounded-2xl text-sm whitespace-nowrap border border-[color:var(--color-border-success)] bg-ok-subtle text-ok">
                                  {t('skills.status.installed')}
                                </span>
                              ) : (
                                <button
                                  type="button"
                                  onClick={() => void handleInstall(item)}
                                  disabled={isInstalling}
                                  className={`min-w-[76px] h-[28px] px-3 rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50  whitespace-nowrap ${
                                    isInstalling ? 'text-text-muted cursor-not-allowed' : 'text-text'
                                  }`}
                                >
                                  {isInstalling ? t('skills.teamskillshub.installing') : t('skills.actions.install')}
                                </button>
                              )}
                            </div>
                          </div>
                        </>
                      )}
                    </div>
                  );
                })
              )}
            </div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button type="button" className="absolute inset-0 bg-black/60" onClick={onClose} aria-label={t('common.close')} />
      <div className="relative w-full max-w-2xl max-h-[85vh] overflow-hidden rounded-xl border border-border bg-card shadow-2xl animate-rise flex flex-col">
        <div className="flex items-start justify-between gap-3 px-5 py-3 border-b border-border bg-panel flex-shrink-0">
          <div className="min-w-0 flex-1 space-y-1">
            <h3 className="text-base font-semibold text-text">{t('skills.teamskillshub.title')}</h3>
            <p className="text-[11px] leading-snug text-text-muted">
              <a
                href={hubBaseUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="font-medium text-accent underline decoration-accent/35 underline-offset-2 hover:text-accent-hover hover:decoration-accent/60"
                aria-label={t('skills.teamskillshub.titleHubAria')}
              >
                {t('skills.teamskillshub.titleHubLinkText')}
              </a>
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 rounded-2xl text-sm text-text border border-gray-400 hover:border-gray-600 hover:bg-secondary/50 "
          >
            {t('common.close')}
          </button>
        </div>

        <div className="p-5 overflow-auto flex-1 min-h-0">
          {message && message.type === 'success' && (
            <div
              className="fixed top-4 right-4 z-[9999] rounded-[4px] text-sm text-text shadow-lg flex items-center gap-3 px-4"
              style={{ backgroundColor: 'var(--color-feedback-success-toast)', width: '564px', height: '40px' }}
            >
              <span className="w-4 h-4 rounded-full bg-[var(--color-feedback-success-indicator)] flex items-center justify-center flex-shrink-0">
                <svg className="w-3 h-3 text-text-inverse" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                </svg>
              </span>
              {message.text.replace('√', '')}
              <button
                type="button"
                onClick={() => showMessage('success', '')}
                className="ml-auto w-6 h-6 flex items-center justify-center hover:bg-card/30 rounded-full "
              >
                <svg className="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          )}
          {message && message.type === 'error' && (
            <div className="mb-3 px-3 py-2.5 rounded-lg text-sm leading-snug border border-danger/40 bg-danger/10 text-danger">{message.text}</div>
          )}

          <div className="flex items-center gap-2">
            <input
              value={query}
              onChange={e => setQuery(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleSearch()}
              placeholder={t('skills.teamskillshub.searchPlaceholder')}
              className="flex-1 min-w-0 px-3 py-2 rounded-md bg-secondary border border-border text-sm text-text placeholder:text-text-muted"
            />
            <button
              type="button"
              onClick={() => void handleSearch()}
              disabled={loadState === 'loading' || !query.trim()}
              className={`px-4 py-2 rounded-2xl text-sm  border border-gray-400 hover:border-gray-600 hover:bg-secondary/50 ${
                loadState === 'loading' || !query.trim() ? 'text-text-muted cursor-not-allowed' : 'text-text'
              }`}
            >
              {loadState === 'loading' ? t('common.loading') : t('skills.teamskillshub.search')}
            </button>
          </div>

          {loadState === 'success' && (
            <div className="mt-4 flex min-h-0 max-h-[50vh] flex-col gap-2">
              <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-0.5">
                {results.length === 0 ? (
                  <div className="text-xs text-text-muted">{t('skills.teamskillshub.noResults')}</div>
                ) : (
                  results.map(item => {
                    const updateStatus = updateStatuses.get(`${item.source_id}:${item.name}`);
                    const isInstalled =
                      Boolean(updateStatus) ||
                      installedSkillKeys.has(skillSourceIdentity(item, sourceId)) ||
                      (installedSkillOrigins?.has(`${item.source_id || sourceId}:${item.name}`) ?? false) ||
                      (item.skill_id != null &&
                        ((installedSkillOrigins?.has(`${item.source_id || sourceId}:${item.skill_id}`) ?? false) ||
                          (installedSkillOrigins?.has(`teamskillshub:${item.skill_id}`) ?? false)));
                    const isInstalling = installingSkillKey === skillSourceIdentity(item, sourceId);
                    const avatar = getSkillAvatar(item.name);
                    return (
                      <div
                        key={`${item.source_id}:${item.name}:${item.version_id}`}
                        className="p-4 rounded-lg border border-border bg-panel flex items-start justify-between gap-4"
                      >
                        <div className="flex items-center gap-3 min-w-0 flex-1">
                          <div
                            className={`w-10 h-10 rounded-lg ${avatar.color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold`}
                          >
                            {avatar.firstChar}
                          </div>
                          <div className="min-w-0 flex-1">
                            <div className="text-base font-semibold text-text-strong truncate">{item.name}</div>
                            <div className="text-sm text-text-muted mt-1 line-clamp-3">{item.summary || t('skills.noDescription')}</div>
                            <div className="text-xs text-text-muted mt-1">
                              {t('skills.versionLabel')}: {item.version || 'latest'}
                            </div>
                          </div>
                        </div>
                        <div className="flex-shrink-0">
                          {isInstalled && updateStatus?.has_update ? (
                            <button
                              type="button"
                              onClick={() => void handleUpdate(item, updateStatus)}
                              disabled={isInstalling}
                              className="min-w-[76px] h-[28px] px-3 rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50 whitespace-nowrap disabled:text-text-muted disabled:cursor-not-allowed"
                            >
                              {isInstalling ? t('skills.teamskillshub.updating') : t('common.update')}
                            </button>
                          ) : isInstalled ? (
                            <span className="px-4 py-2 rounded-2xl text-sm whitespace-nowrap border border-[color:var(--color-border-success)] bg-ok-subtle text-ok">
                              {t('skills.status.installed')}
                            </span>
                          ) : (
                            <button
                              type="button"
                              onClick={() => void handleInstall(item)}
                              disabled={isInstalling}
                              className={`min-w-[76px] h-[28px] px-3 rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50  whitespace-nowrap ${
                                isInstalling ? 'text-text-muted cursor-not-allowed' : 'text-text'
                              }`}
                            >
                              {isInstalling ? t('skills.teamskillshub.installing') : t('skills.actions.install')}
                            </button>
                          )}
                        </div>
                      </div>
                    );
                  })
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
