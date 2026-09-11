/**
 * AddContentDrawer — 「添加内容」抽屉。
 *
 * 由 PersonalContextServicesPanel 右上角按钮触发。
 * 通用字段（名称/来源/自动采集/频率/单次条数）+ 6 个 provider 分支表单。
 * 飞书占位（下一步开发）；GitHub 需先在设置页授权（localStorage PAT）。
 * 提交走 usePersonalContextStore.createService。
 *
 * 后端 source 校验对齐 openjiuwen config.py:_normalize_service_source。
 */

import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, Loader2, X } from 'lucide-react';
import { usePersonalContextStore } from '../../stores';
import {
  type FetchProvider,
  type FetchServiceConfig,
  type FeishuMode,
  type FeishuResource,
  type GithubResource,
  type GitcodeResource,
  type TimeRange,
  FEISHU_MODES,
  FEISHU_RESOURCE_LABEL_KEYS,
  FEISHU_RESOURCES,
  FREQUENCY_SECONDS,
  GITHUB_RESOURCE_LABEL_KEYS,
  GITHUB_RESOURCES,
  GITCODE_RESOURCE_LABEL_KEYS,
  GITCODE_RESOURCES,
  INTERVAL_MAX_SECONDS,
  MAX_ITEMS_MAX,
  MAX_ITEMS_MIN,
  PROVIDER_LABEL_KEYS,
  PROVIDER_ORDER,
  parseGithubRepoUrl,
  parseGitcodeRepoUrl,
  validateServiceId,
  validateToutiaoProfileUrl,
  validateZhihuColumnUrl,
} from '../../services/personalContextApi';
import { requestSettingsModule } from '../../features/settings/settingsNavigation';
import type { WebError } from '../../types/websocket';
import localFilesIcon from '../../assets/settings/channels/local-files.svg';
import edgeBookmarksIcon from '../../assets/settings/channels/edge-bookmarks.svg';
import zhihuIcon from '../../assets/settings/channels/zhihu.svg';
import toutiaoIcon from '../../assets/settings/channels/toutiao.svg';
import feishuIcon from '../../assets/settings/channels/feishu.svg';
import githubIcon from '../../assets/settings/channels/GitHub.svg';
import gitcodeIcon from '../../assets/settings/channels/gitcode.png';
import './AddContentDrawer.css';

const PROVIDER_ICON: Record<FetchProvider, string> = {
  local_files: localFilesIcon,
  browser_bookmarks: edgeBookmarksIcon,
  zhihu_reader: zhihuIcon,
  toutiao_reader: toutiaoIcon,
  feishu: feishuIcon,
  github: githubIcon,
  gitcode: gitcodeIcon,
};

interface AddContentDrawerProps {
  /** 从内容页级联分类带入的预选 provider；缺省回退到首个。 */
  initialProvider?: FetchProvider;
  /** 传入则进入「编辑」模式：名称/来源锁定，只改参数，提交走保存而非新建。 */
  editService?: FetchServiceConfig | null;
  onClose: () => void;
  onCreated: () => void;
}

/** 由 interval_seconds 反推频率单位与数值（创建时用 day=86400 才回填得干净）。 */
function freqFromSeconds(sec: number): { unit: 'hour' | 'day'; value: number } {
  if (sec % FREQUENCY_SECONDS.day === 0) {
    return { unit: 'day', value: Math.max(1, sec / FREQUENCY_SECONDS.day) };
  }
  return { unit: 'hour', value: Math.max(1, Math.round(sec / FREQUENCY_SECONDS.hour)) };
}

/** 给定频率单位下的数值上限，对齐后端 interval_seconds le=31_536_000（day=365 / hour=8760）。 */
function maxFreqValue(unit: 'hour' | 'day'): number {
  return Math.floor(INTERVAL_MAX_SECONDS / FREQUENCY_SECONDS[unit]);
}

/** 把频率数值钳制到 [1, 单位上限]，单位切换时也据此收敛（day↔hour 上限不同）。 */
function clampFreq(value: number, unit: 'hour' | 'day'): number {
  return Math.min(maxFreqValue(unit), Math.max(1, value));
}

function isRequestTimeout(e: unknown): boolean {
  return (e as WebError | undefined)?.code === 'REQUEST_TIMEOUT';
}

/**
 * 由后端 time_range 反推时间范围表单态。
 * 新建任务（无 time_range）默认「最近三个月」（recent 90 天），而非自定义。
 */
function timeRangeFromConfig(tr?: TimeRange): {
  timeRange: 'week' | 'month' | 'quarter' | 'custom';
  customStart: string;
  customEnd: string;
} {
  if (tr?.mode === 'fixed') {
    return {
      timeRange: 'custom',
      customStart: rfc3339ToLocalDate(tr.start_at),
      customEnd: rfc3339ToLocalDate(tr.end_at, true),
    };
  }
  if (tr?.mode === 'recent') {
    if (tr.recent_days === 7) return { timeRange: 'week', customStart: '', customEnd: '' };
    if (tr.recent_days === 30) return { timeRange: 'month', customStart: '', customEnd: '' };
    if (tr.recent_days === 90) return { timeRange: 'quarter', customStart: '', customEnd: '' };
  }
  return { timeRange: 'quarter', customStart: '', customEnd: '' };
}

export function AddContentDrawer({ initialProvider, editService, onClose, onCreated }: AddContentDrawerProps) {
  const { t } = useTranslation();
  const { config, createService, updateService, pendingWrites, isProviderAuthorized } = usePersonalContextStore();
  const isConfigured = config.collection_enabled === true;
  const isEdit = !!editService;

  // 通用字段
  const [name, setName] = useState(editService?.service_id ?? '');
  const [provider, setProvider] = useState<FetchProvider>(() => {
    if (editService) return editService.provider;
    const init = initialProvider ?? 'local_files';
    return isProviderAuthorized(init) ? init : PROVIDER_ORDER.find((p) => isProviderAuthorized(p)) ?? 'local_files';
  });
  const [freqUnit, setFreqUnit] = useState<'hour' | 'day'>(() => freqFromSeconds(editService?.interval_seconds ?? 3 * FREQUENCY_SECONDS.day).unit);
  const [freqValue, setFreqValue] = useState(() => freqFromSeconds(editService?.interval_seconds ?? 3 * FREQUENCY_SECONDS.day).value);
  const [timeRange, setTimeRange] = useState<'week' | 'month' | 'quarter' | 'custom'>(() => timeRangeFromConfig(editService?.time_range).timeRange);
  const [customStart, setCustomStart] = useState(() => timeRangeFromConfig(editService?.time_range).customStart);
  const [customEnd, setCustomEnd] = useState(() => timeRangeFromConfig(editService?.time_range).customEnd);
  // 默认 20 条；填值须 [1,10000]
  const [maxItems, setMaxItems] = useState<number | null>(editService?.max_items_per_run ?? 20);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [timeDropdownOpen, setTimeDropdownOpen] = useState(false);
  const [calendarOpen, setCalendarOpen] = useState(false);

  // 分支字段
  const [rootDir, setRootDir] = useState(() => (editService?.provider === 'local_files' ? String(editService.source.root_dir ?? '') : ''));
  const [columnUrl, setColumnUrl] = useState(() => (editService?.provider === 'zhihu_reader' ? String(editService.source.column_url ?? '') : ''));
  const [profileUrl, setProfileUrl] = useState(() => (editService?.provider === 'toutiao_reader' ? String(editService.source.profile_url ?? '') : ''));
  const [edgeProfile, setEdgeProfile] = useState(() => (editService?.provider === 'browser_bookmarks' ? String(editService.source.profile ?? '') : ''));
  const [edgeBookmarksPath, setEdgeBookmarksPath] = useState(() => (editService?.provider === 'browser_bookmarks' ? String(editService.source.bookmarks_path ?? '') : ''));
  const [edgeFolderList, setEdgeFolderList] = useState<string[]>(() => (editService?.provider === 'browser_bookmarks' && Array.isArray(editService.source.bookmark_folder_paths) ? (editService.source.bookmark_folder_paths as string[]) : []));
  const [githubRepoUrl, setGithubRepoUrl] = useState(() => {
    if (editService?.provider !== 'github') return '';
    const owner = String(editService.source.owner ?? '');
    const repo = String(editService.source.repo ?? '');
    return owner && repo ? `https://github.com/${owner}/${repo}` : '';
  });
  const [feishuDocIdList, setFeishuDocIdList] = useState<string[]>(() => (editService?.provider === 'feishu' && Array.isArray(editService.source.document_ids) ? (editService.source.document_ids as string[]) : []));
  const [githubResources, setGithubResources] = useState<GithubResource[]>(() => (editService?.provider === 'github' && Array.isArray(editService.source.resources) ? (editService.source.resources as GithubResource[]) : ['readme']));
  const [gitcodeRepoUrl, setGitcodeRepoUrl] = useState(() => {
    if (editService?.provider !== 'gitcode') return '';
    const owner = String(editService.source.owner ?? '');
    const repo = String(editService.source.repo ?? '');
    return owner && repo ? `https://gitcode.com/${owner}/${repo}` : '';
  });
  const [gitcodeResources, setGitcodeResources] = useState<GitcodeResource[]>(() => (editService?.provider === 'gitcode' && Array.isArray(editService.source.resources) ? (editService.source.resources as GitcodeResource[]) : ['readme']));
  // 飞书：先建 service 再去设置页授权（后端授权需 service 已存在以派生 scope）。
  const [feishuMode, setFeishuMode] = useState<FeishuMode>(() => (editService?.provider === 'feishu' && editService.source.mode === 'wiki_space' ? 'wiki_space' : 'account'));
  const [feishuResources, setFeishuResources] = useState<FeishuResource[]>(() => (editService?.provider === 'feishu' && Array.isArray(editService.source.resources) ? (editService.source.resources as FeishuResource[]) : ['docs']));
  const [feishuWikiSpaceId, setFeishuWikiSpaceId] = useState(() => (editService?.provider === 'feishu' ? String(editService.source.wiki_space_id ?? '') : ''));
  const [feishuCalendarStart, setFeishuCalendarStart] = useState(() => (editService?.provider === 'feishu' ? String(editService.source.start ?? '') : ''));
  const [feishuCalendarEnd, setFeishuCalendarEnd] = useState(() => (editService?.provider === 'feishu' ? String(editService.source.end ?? '') : ''));
  const [feishuCalendarOpen, setFeishuCalendarOpen] = useState(false);

  const [error, setError] = useState<string | null>(null);
  const submitting = editService ? !!pendingWrites[`patch:${editService.service_id}`] : !!pendingWrites.create_service;

  // 飞书允许未授权时新建（先配 service 再授权）；其余 provider 仍需先授权。
  const requiresAuth = provider !== 'feishu';
  const authorized = isProviderAuthorized(provider);

  // 分支表单是否满足提交条件（编辑与新建共用）
  const branchValid = useMemo(() => {
    if (provider === 'feishu') {
      if (feishuMode === 'wiki_space') return !!feishuWikiSpaceId.trim();
      return feishuResources.length > 0; // account 模式需至少选一项资源
    }
    if (provider === 'local_files') return !!rootDir.trim();
    if (provider === 'zhihu_reader') return !validateZhihuColumnUrl(columnUrl);
    if (provider === 'toutiao_reader') return !validateToutiaoProfileUrl(profileUrl);
    if (provider === 'browser_bookmarks') return true; // 全可空
    if (provider === 'github') {
      const parsed = parseGithubRepoUrl(githubRepoUrl);
      return !('error' in parsed) && !!parsed.owner && githubResources.length > 0;
    }
    if (provider === 'gitcode') {
      const parsed = parseGitcodeRepoUrl(gitcodeRepoUrl);
      return !('error' in parsed) && !!parsed.owner && gitcodeResources.length > 0;
    }
    return false;
  }, [provider, feishuMode, feishuResources, feishuWikiSpaceId, rootDir, columnUrl, profileUrl, githubRepoUrl, githubResources, gitcodeRepoUrl, gitcodeResources]);

  // 「添加」按钮被禁用时给出可见原因。顺序与 canSubmit 完全一致，返回 i18n key。
  // 没有它按钮只会静默置灰，用户无法判断是名称格式、来源地址还是未授权的问题。
  const submitBlockReasonKey = useMemo(() => {
    if (submitting) return null;
    if (!isConfigured) return 'personalContext.addContent.notConfigured';
    if (!branchValid) {
      if (provider === 'local_files') return 'personalContext.addContent.localFiles.rootDirHint';
      if (provider === 'zhihu_reader') return 'personalContext.addContent.zhihu.columnUrlHint';
      if (provider === 'toutiao_reader') return 'personalContext.addContent.toutiao.profileUrlHint';
      if (provider === 'github') return 'personalContext.addContent.github.sourceInvalid';
      if (provider === 'gitcode') return 'personalContext.addContent.gitcode.sourceInvalid';
      if (provider === 'feishu') {
        return feishuMode === 'wiki_space'
          ? 'personalContext.addContent.feishu.wikiSpaceIdRequired'
          : 'personalContext.addContent.feishu.resourcesRequired';
      }
      return null;
    }
    if (maxItems !== null && (maxItems < MAX_ITEMS_MIN || maxItems > MAX_ITEMS_MAX)) {
      return 'personalContext.addContent.maxItemsRangeError';
    }
    if (timeRange === 'custom' && (!customStart || !customEnd)) {
      return 'personalContext.addContent.dateRangeRequired';
    }
    if (isEdit) return null; // 编辑：名称/来源已锁定
    if (!name.trim()) return 'personalContext.addContent.nameRequired';
    if (requiresAuth && !authorized) return 'personalContext.addContent.providerUnauthorized';
    if (validateServiceId(name)) return 'personalContext.addContent.nameFormatError';
    return null;
  }, [
    isConfigured,
    submitting,
    branchValid,
    isEdit,
    name,
    requiresAuth,
    authorized,
    provider,
    feishuMode,
    maxItems,
    timeRange,
    customStart,
    customEnd,
  ]);

  const canSubmit = useMemo(() => {
    return !submitting && submitBlockReasonKey === null;
  }, [submitting, submitBlockReasonKey]);

  const handleSubmit = async () => {
    setError(null);
    if (!isConfigured) {
      setError(t('personalContext.addContent.notConfigured'));
      return;
    }
    if (!isEdit) {
      if (requiresAuth && !authorized) {
        setError(t('personalContext.addContent.providerUnauthorized'));
        return;
      }
      const idErr = validateServiceId(name);
      if (idErr) {
        setError(idErr);
        return;
      }
    }
    if (maxItems !== null && (maxItems < MAX_ITEMS_MIN || maxItems > MAX_ITEMS_MAX)) {
      setError(t('personalContext.addContent.maxItemsRangeError'));
      return;
    }
    // 选「自定义」但没挑具体起止日期时不允许创建/保存（否则会静默退化成 mode=all 全量）。
    if (timeRange === 'custom' && (!customStart || !customEnd)) {
      setError(t('personalContext.addContent.dateRangeRequired'));
      return;
    }

    let source: Record<string, unknown> = {};

    if (provider === 'local_files') {
      if (!rootDir.trim()) {
        setError(t('personalContext.addContent.localFiles.rootDirRequired'));
        return;
      }
      source = { root_dir: rootDir.trim() };
    } else if (provider === 'zhihu_reader') {
      const err = validateZhihuColumnUrl(columnUrl);
      if (err) { setError(err); return; }
      source = { column_url: columnUrl.trim() };
    } else if (provider === 'toutiao_reader') {
      const err = validateToutiaoProfileUrl(profileUrl);
      if (err) { setError(err); return; }
      source = { profile_url: profileUrl.trim() };
    } else if (provider === 'browser_bookmarks') {
      const s: Record<string, unknown> = { include_subfolders: true, fetch_page_content: true };
      if (edgeProfile.trim()) s.profile = edgeProfile.trim();
      if (edgeBookmarksPath.trim()) s.bookmarks_path = edgeBookmarksPath.trim();
      const folders = edgeFolderList.map((f) => f.trim()).filter(Boolean);
      if (folders.length) s.bookmark_folder_paths = folders;
      source = s;
    } else if (provider === 'github') {
      const parsed = parseGithubRepoUrl(githubRepoUrl);
      if ('error' in parsed) { setError(parsed.error); return; }
      if (githubResources.length === 0) {
        setError(t('personalContext.addContent.github.resourcesRequired'));
        return;
      }
      source = { owner: parsed.owner, repo: parsed.repo, resources: [...githubResources] };
    } else if (provider === 'gitcode') {
      const parsed = parseGitcodeRepoUrl(gitcodeRepoUrl);
      if ('error' in parsed) { setError(parsed.error); return; }
      if (gitcodeResources.length === 0) {
        setError(t('personalContext.addContent.gitcode.resourcesRequired'));
        return;
      }
      source = { owner: parsed.owner, repo: parsed.repo, resources: [...gitcodeResources] };
    } else if (provider === 'feishu') {
      // 飞书先建 service（未授权也可建），建完后需去设置页授权。
      // 后端 _normalize_service_source feishu 分支：mode ∈ {account, wiki_space}；
      // account 需 resources ⊆ {docs,tasks,calendar}；wiki_space 需 wiki_space_id。
      // 飞书不接受 credentials（授权走 OAuth 设备流，非 token 注入）。
      if (feishuMode === 'wiki_space') {
        if (!feishuWikiSpaceId.trim()) {
          setError(t('personalContext.addContent.feishu.wikiSpaceIdRequired'));
          return;
        }
        source = { mode: 'wiki_space', wiki_space_id: feishuWikiSpaceId.trim() };
      } else {
        if (feishuResources.length === 0) {
          setError(t('personalContext.addContent.feishu.resourcesRequired'));
          return;
        }
        const accountSource: Record<string, unknown> = { mode: 'account', resources: [...feishuResources] };
        const docIds = feishuDocIdList.map((d) => d.trim()).filter(Boolean);
        if (docIds.length && feishuResources.includes('docs')) {
          accountSource.document_ids = docIds;
        }
        if (feishuResources.includes('calendar') && feishuCalendarStart && feishuCalendarEnd) {
          accountSource.start = feishuCalendarStart;
          accountSource.end = feishuCalendarEnd;
        }
        source = accountSource;
      }
    }

    const intervalSeconds = freqValue * FREQUENCY_SECONDS[freqUnit];
    const timeRangePayload: TimeRange = (() => {
      if (timeRange === 'custom' && customStart && customEnd) {
        return {
          mode: 'fixed',
          start_at: dateToStartRFC3339(customStart),
          end_at: dateToEndRFC3339(customEnd),
        };
      }
      const recentDays = timeRange === 'week' ? 7 : timeRange === 'month' ? 30 : 90;
      return { mode: 'recent', recent_days: recentDays };
    })();

    try {
      if (editService) {
        await updateService(editService.service_id, {
          interval_seconds: intervalSeconds,
          max_items_per_run: maxItems,
          time_range: timeRangePayload,
          source,
        });
      } else {
        await createService({
          service_id: name.trim(),
          provider,
          enabled: true,
          interval_seconds: intervalSeconds,
          max_items_per_run: maxItems,
          time_range: timeRangePayload,
          source,
        });
      }
      onCreated();
    } catch (e) {
      setError(
        isRequestTimeout(e)
          ? t('personalContext.addContent.createTimeout')
          : e instanceof Error
            ? e.message
            : String(e),
      );
    }
  };

  return (
    <div className="pc-drawer-overlay" onClick={onClose} data-testid="pc-add-content-overlay">
      <aside
        className="pc-drawer"
        onClick={(e) => e.stopPropagation()}
        data-testid="pc-add-content-drawer"
      >
        <header className="pc-drawer__head">
          <h3 className="pc-drawer__title">{t(isEdit ? 'personalContext.addContent.editTitle' : 'personalContext.addContent.title')}</h3>
          <button type="button" className="pc-drawer__close" onClick={onClose} aria-label="close">
            <X size={18} />
          </button>
        </header>

        <div className="pc-drawer__body">
          {/* 采集内容名称 */}
          <div className="pc-drawer__field">
            <label>{t('personalContext.addContent.nameLabel')}</label>
            {isEdit ? (
              <div className="pc-drawer__locked">{name}</div>
            ) : (
              <input
                className="pc-drawer__input"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={t('personalContext.addContent.namePlaceholder')}
              />
            )}
          </div>

          {/* 内容采集来源 */}
          <div className="pc-drawer__field">
            <label>{t('personalContext.addContent.providerLabel')}</label>
            {isEdit ? (
              <div className="pc-drawer__provider-locked">
                <span className={'pc-drawer__provider-icon' + (provider === 'gitcode' ? ' pc-drawer__provider-icon--gitcode' : '')}>
                  <img src={PROVIDER_ICON[provider]} alt="" />
                </span>
                <span className="pc-drawer__provider-name">{t(PROVIDER_LABEL_KEYS[provider])}</span>
              </div>
            ) : (
              <div className="pc-drawer__provider-grid">
                {PROVIDER_ORDER.map((p) => {
                  const authed = isProviderAuthorized(p);
                  const disabled = p !== 'feishu' && !authed;
                  const active = provider === p;
                  return (
                    <button
                      key={p}
                      type="button"
                      className={'pc-drawer__provider-card' + (active ? ' pc-drawer__provider-card--active' : '') + (disabled ? ' pc-drawer__provider-card--disabled' : '')}
                      onClick={() => {
                        if (disabled) {
                          // 未授权（GitHub/GitCode）→ 跳设置页授权
                          requestSettingsModule('personalContext');
                          onClose();
                          return;
                        }
                        setProvider(p);
                        setError(null);
                      }}
                    >
                      <span className={'pc-drawer__provider-icon' + (p === 'gitcode' ? ' pc-drawer__provider-icon--gitcode' : '')}>
                        <img src={PROVIDER_ICON[p]} alt="" />
                      </span>
                      <span className="pc-drawer__provider-name">{t(PROVIDER_LABEL_KEYS[p])}</span>
                      {disabled && (
                        <span className="pc-drawer__provider-authorize">
                          {t('personalContext.authorization.goAuthorize')}
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
            )}
            {!isEdit && provider === 'feishu' && !authorized && (
              <div className="pc-drawer__field-hint">
                {t('personalContext.addContent.feishu.authorizeAfterCreate')}
                <button
                  type="button"
                  className="pc-drawer__link"
                  onClick={() => { requestSettingsModule('personalContext'); onClose(); }}
                >
                  {t('personalContext.addContent.goAuthorize')}
                </button>
              </div>
            )}
          </div>

          {/* provider 分支表单 */}
          <ProviderFields
            provider={provider}
            rootDir={rootDir} setRootDir={setRootDir}
            columnUrl={columnUrl} setColumnUrl={setColumnUrl}
            profileUrl={profileUrl} setProfileUrl={setProfileUrl}
            edgeProfile={edgeProfile} setEdgeProfile={setEdgeProfile}
            edgeBookmarksPath={edgeBookmarksPath} setEdgeBookmarksPath={setEdgeBookmarksPath}
            edgeFolderList={edgeFolderList} setEdgeFolderList={setEdgeFolderList}
            githubRepoUrl={githubRepoUrl} setGithubRepoUrl={setGithubRepoUrl}
            githubResources={githubResources} setGithubResources={setGithubResources}
            gitcodeRepoUrl={gitcodeRepoUrl} setGitcodeRepoUrl={setGitcodeRepoUrl}
            gitcodeResources={gitcodeResources} setGitcodeResources={setGitcodeResources}
            feishuMode={feishuMode} setFeishuMode={setFeishuMode}
            feishuResources={feishuResources} setFeishuResources={setFeishuResources}
            feishuWikiSpaceId={feishuWikiSpaceId} setFeishuWikiSpaceId={setFeishuWikiSpaceId}
            feishuCalendarStart={feishuCalendarStart} setFeishuCalendarStart={setFeishuCalendarStart}
            feishuCalendarEnd={feishuCalendarEnd} setFeishuCalendarEnd={setFeishuCalendarEnd}
            feishuCalendarOpen={feishuCalendarOpen} setFeishuCalendarOpen={setFeishuCalendarOpen}
            feishuDocIdList={feishuDocIdList} setFeishuDocIdList={setFeishuDocIdList}
          />

          {/* 高级配置 */}
          <button
            type="button"
            className={'pc-drawer__advanced-toggle' + (advancedOpen ? ' pc-drawer__advanced-toggle--open' : '')}
            onClick={() => setAdvancedOpen(!advancedOpen)}
          >
            {t('personalContext.addContent.advancedConfig')}
            <ChevronDown size={16} />
          </button>
          {advancedOpen && (
            <div className="pc-drawer__advanced-body">
              {/* 采集时间 */}
              <div className="pc-drawer__field">
                <label>{t('personalContext.addContent.timeRangeLabel')}</label>
                <div className="pc-drawer__custom-select" >
                  <button
                    type="button"
                    className="pc-drawer__select-trigger"
                    onClick={() => setTimeDropdownOpen(!timeDropdownOpen)}
                  >
                    <span>{t('personalContext.addContent.timeRange' + (timeRange === 'week' ? 'Week' : timeRange === 'month' ? 'Month' : timeRange === 'quarter' ? 'Quarter' : 'Custom'))}</span>
                    <span className="pc-drawer__select-arrow">{'<'}</span>
                  </button>
                  {timeDropdownOpen && (
                    <div className="pc-drawer__select-menu">
                      {(['week', 'month', 'quarter', 'custom'] as const).map((opt) => (
                        <button
                          key={opt}
                          type="button"
                          className={'pc-drawer__select-option' + (timeRange === opt ? ' pc-drawer__select-option--active' : '')}
                          onClick={() => {
                            setTimeRange(opt);
                            if (opt === 'custom') {
                              setCalendarOpen(true);
                            } else {
                              setCalendarOpen(false);
                            }
                            setTimeDropdownOpen(false);
                          }}
                        >
                          <span>{t('personalContext.addContent.timeRange' + (opt === 'week' ? 'Week' : opt === 'month' ? 'Month' : opt === 'quarter' ? 'Quarter' : 'Custom'))}</span>
                          {opt === 'custom' && <span className="pc-drawer__select-arrow-right">{'>'}</span>}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                {timeRange === 'custom' && (
                  <button
                    type="button"
                    className="pc-drawer__calendar-trigger"
                    onClick={() => setCalendarOpen(true)}
                  >
                    <span>{customStart && customEnd ? `${customStart} - ${customEnd}` : t('personalContext.addContent.dateRangePlaceholder')}</span>
                    <span className="pc-drawer__select-arrow">{'<'}</span>
                  </button>
                )}
              </div>

              {/* 自动采集频率 */}
              <div className="pc-drawer__field">
                <label>{t('personalContext.addContent.frequencyLabel')}</label>
                <div className="pc-drawer__freq">
                  <div className="pc-drawer__spinner">
                    <button
                      type="button"
                      className="pc-drawer__spinner-btn"
                      onClick={() => setFreqValue(Math.max(1, freqValue - 1))}
                    >
                      {'-'}
                    </button>
                    <input
                      type="number"
                      className="pc-drawer__spinner-input"
                      value={freqValue}
                      min={1}
                      max={maxFreqValue(freqUnit)}
                      onChange={(e) => setFreqValue(clampFreq(Number(e.target.value) || 1, freqUnit))}
                    />
                    <button
                      type="button"
                      className="pc-drawer__spinner-btn"
                      onClick={() => setFreqValue(clampFreq(freqValue + 1, freqUnit))}
                    >
                      {'+'}
                    </button>
                  </div>
                  <button
                    type="button"
                    className={'pc-drawer__freq-pill' + (freqUnit === 'hour' ? ' pc-drawer__freq-pill--active' : '')}
                    onClick={() => { setFreqUnit('hour'); setFreqValue(clampFreq(freqValue, 'hour')); }}
                  >
                    {t('personalContext.addContent.unitHour')}
                  </button>
                  <button
                    type="button"
                    className={'pc-drawer__freq-pill' + (freqUnit === 'day' ? ' pc-drawer__freq-pill--active' : '')}
                    onClick={() => { setFreqUnit('day'); setFreqValue(clampFreq(freqValue, 'day')); }}
                  >
                    {t('personalContext.addContent.unitDay')}
                  </button>
                </div>
              </div>

              {/* 单次最多采集条数 */}
              <div className="pc-drawer__field">
                <label>{t('personalContext.addContent.maxItemsLabel')}</label>
                <div className="pc-drawer__spinner pc-drawer__spinner--narrow">
                  <button
                    type="button"
                    className="pc-drawer__spinner-btn"
                    onClick={() => setMaxItems(Math.max(MAX_ITEMS_MIN, (maxItems ?? 20) - 1))}
                  >
                    {'-'}
                  </button>
                  <input
                    type="number"
                    className="pc-drawer__spinner-input"
                    value={maxItems ?? ''}
                    min={MAX_ITEMS_MIN}
                    max={MAX_ITEMS_MAX}
                    placeholder="20"
                    onChange={(e) => {
                      const raw = e.target.value;
                      if (raw === '') { setMaxItems(null); return; }
                      const v = Number(raw);
                      setMaxItems(Number.isNaN(v) ? null : v);
                    }}
                  />
                  <button
                    type="button"
                    className="pc-drawer__spinner-btn"
                    onClick={() => setMaxItems(Math.min(MAX_ITEMS_MAX, (maxItems ?? 20) + 1))}
                  >
                    {'+'}
                  </button>
                </div>
              </div>
              </div>
          )}
          {error && <div className="pc-drawer__error" role="alert">{error}</div>}
        </div>

        <footer className="pc-drawer__foot">
          {submitBlockReasonKey && <div className="pc-drawer__foot-hint">{t(submitBlockReasonKey)}</div>}
          <button type="button" className="pc-drawer__foot-btn pc-drawer__foot-btn--secondary" onClick={onClose} disabled={submitting}>
            {t('personalContext.services.cancel')}
          </button>
          <button
            type="button"
            className="pc-drawer__foot-btn pc-drawer__foot-btn--primary"
            onClick={handleSubmit}
            disabled={!canSubmit}
          >
            {submitting ? <Loader2 className="spin" size={14} /> : t(isEdit ? 'personalContext.addContent.save' : 'personalContext.addContent.submit')}
          </button>
        </footer>
        <CalendarRangeModal
          open={calendarOpen}
          onClose={() => setCalendarOpen(false)}
          start={customStart}
          end={customEnd}
          setStart={setCustomStart}
          setEnd={setCustomEnd}
        />
        <CalendarRangeModal
          open={feishuCalendarOpen}
          onClose={() => setFeishuCalendarOpen(false)}
          start={feishuCalendarStart}
          end={feishuCalendarEnd}
          setStart={setFeishuCalendarStart}
          setEnd={setFeishuCalendarEnd}
        />
      </aside>
    </div>
  );
}

interface ProviderFieldsProps {
  provider: FetchProvider;
  rootDir: string; setRootDir: (v: string) => void;
  columnUrl: string; setColumnUrl: (v: string) => void;
  profileUrl: string; setProfileUrl: (v: string) => void;
  edgeProfile: string; setEdgeProfile: (v: string) => void;
  edgeBookmarksPath: string; setEdgeBookmarksPath: (v: string) => void;
  edgeFolderList: string[]; setEdgeFolderList: (v: string[]) => void;
  githubRepoUrl: string; setGithubRepoUrl: (v: string) => void;
  githubResources: GithubResource[]; setGithubResources: (v: GithubResource[]) => void;
  gitcodeRepoUrl: string; setGitcodeRepoUrl: (v: string) => void;
  gitcodeResources: GitcodeResource[]; setGitcodeResources: (v: GitcodeResource[]) => void;
  feishuMode: FeishuMode; setFeishuMode: (v: FeishuMode) => void;
  feishuResources: FeishuResource[]; setFeishuResources: (v: FeishuResource[]) => void;
  feishuWikiSpaceId: string; setFeishuWikiSpaceId: (v: string) => void;
  feishuCalendarStart: string; setFeishuCalendarStart: (v: string) => void;
  feishuCalendarEnd: string; setFeishuCalendarEnd: (v: string) => void;
  feishuCalendarOpen: boolean; setFeishuCalendarOpen: (v: boolean) => void;
  feishuDocIdList: string[]; setFeishuDocIdList: (v: string[]) => void;
}


const CAL_WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日'];
const CAL_MONTHS = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'];

function toISO(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function parseISO(s: string): Date | null {
  if (!s) return null;
  const [y, m, d] = s.split('-').map(Number);
  if (!y || !m || !d) return null;
  return new Date(y, m - 1, d);
}

/** 后端固定区间存的是带时区的 RFC 3339 时间戳，读回本地日期（end_at 为开区间，回显需减一天）。 */
function rfc3339ToLocalDate(ts: string, exclusiveEnd = false): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '';
  if (exclusiveEnd) d.setDate(d.getDate() - 1);
  return toISO(d);
}

/** 把日历选的「YYYY-MM-DD」转成后端 _normalize_rfc3339 要求的带时区时间戳（当日 00:00，UTC 表达）。 */
function dateToStartRFC3339(dateStr: string): string {
  const [y, m, d] = dateStr.split('-').map(Number);
  return new Date(y, m - 1, d, 0, 0, 0).toISOString();
}

/** end_at 语义为开区间：取所选日期次日的 00:00，这样整日都被包含。 */
function dateToEndRFC3339(dateStr: string): string {
  const [y, m, d] = dateStr.split('-').map(Number);
  return new Date(y, m - 1, d + 1, 0, 0, 0).toISOString();
}

function addMonths(d: Date, n: number): Date {
  return new Date(d.getFullYear(), d.getMonth() + n, 1);
}

function monthTitle(d: Date): string {
  return `${d.getFullYear()}年 ${CAL_MONTHS[d.getMonth()]}`;
}

function monthGrid(monthDate: Date): Date[] {
  const first = new Date(monthDate.getFullYear(), monthDate.getMonth(), 1);
  // 周一为一周首日：把周日(0)映射到末尾
  const offset = (first.getDay() + 6) % 7;
  const start = new Date(first);
  start.setDate(first.getDate() - offset);
  const days: Date[] = [];
  for (let i = 0; i < 42; i++) {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    days.push(d);
  }
  return days;
}

interface CalendarRangeModalProps {
  open: boolean;
  onClose: () => void;
  start: string;
  end: string;
  setStart: (v: string) => void;
  setEnd: (v: string) => void;
}

function CalendarRangeModal({ open, onClose, start, end, setStart, setEnd }: CalendarRangeModalProps) {
  const { t } = useTranslation();
  const [leftMonth, setLeftMonth] = useState(() => {
    const base = parseISO(start) || new Date();
    return new Date(base.getFullYear(), base.getMonth(), 1);
  });
  if (!open) return null;

  const startDate = parseISO(start);
  const endDate = parseISO(end);

  const pick = (iso: string) => {
    const picked = parseISO(iso);
    if (!picked) return;
    if (!start || (start && end)) {
      // 开始新一轮选择：设起点，清空终点
      setStart(iso);
      setEnd('');
      return;
    }
    // 已有起点、无终点
    if (picked < startDate!) {
      setStart(iso);
      setEnd('');
      return;
    }
    setEnd(iso);
  };

  const renderMonth = (monthDate: Date) => {
    const days = monthGrid(monthDate);
    return (
      <div className="pc-cal__month">
        <div className="pc-cal__month-title">{monthTitle(monthDate)}</div>
        <div className="pc-cal__weekdays">
          {CAL_WEEKDAYS.map((w) => (
            <span key={w} className="pc-cal__weekday">{w}</span>
          ))}
        </div>
        <div className="pc-cal__days">
          {days.map((d, i) => {
            const iso = toISO(d);
            const inMonth = d.getMonth() === monthDate.getMonth();
            const isStart = start === iso;
            const isEnd = end === iso;
            const inRange = startDate && endDate && d > startDate && d < endDate;
            const cls =
              'pc-cal__day' +
              (!inMonth ? ' pc-cal__day--off' : '') +
              (isStart ? ' pc-cal__day--start' : '') +
              (isEnd ? ' pc-cal__day--end' : '') +
              (inRange ? ' pc-cal__day--inrange' : '');
            return (
              <button
                key={i}
                type="button"
                className={cls}
                onClick={() => pick(iso)}
              >
                {d.getDate()}
              </button>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <div className="pc-drawer-overlay pc-drawer-overlay--modal" onClick={onClose}>
      <div className="pc-drawer__calendar-modal" onClick={(e) => e.stopPropagation()}>
        <div className="pc-drawer__calendar-modal-head">
          <span>{t('personalContext.addContent.dateRangeTitle')}</span>
          <button type="button" className="pc-drawer__close" onClick={onClose} aria-label="close">
            <X size={18} />
          </button>
        </div>
        <div className="pc-cal__range-display">
          <span className="pc-cal__nav-spacer" />
          <div className="pc-cal__range-fields">
            <input
              className="pc-cal__field-input"
              value={start}
              onChange={(e) => setStart(e.target.value)}
              placeholder="YYYY-MM-DD"
            />
            <input
              className="pc-cal__field-input"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
              placeholder="YYYY-MM-DD"
            />
          </div>
          <span className="pc-cal__nav-spacer" />
        </div>
        <div className="pc-cal__nav">
          <button type="button" className="pc-cal__nav-btn" onClick={() => setLeftMonth(addMonths(leftMonth, -1))}>{'<'}</button>
          <div className="pc-cal__months">
            {renderMonth(leftMonth)}
            {renderMonth(addMonths(leftMonth, 1))}
          </div>
          <button type="button" className="pc-cal__nav-btn" onClick={() => setLeftMonth(addMonths(leftMonth, 1))}>{'>'}</button>
        </div>
        <div className="pc-drawer__calendar-actions">
          <button type="button" className="pc-drawer__calendar-confirm" onClick={onClose}>
            {t('common.confirm')}
          </button>
        </div>
      </div>
    </div>
  );
}

interface MultiTextInputProps {
  values: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
}

function MultiTextInput({ values, onChange, placeholder }: MultiTextInputProps) {
  const { t } = useTranslation();
  const update = (i: number, v: string) => onChange(values.map((x, j) => (j === i ? v : x)));
  const remove = (i: number) => onChange(values.filter((_, j) => j !== i));
  const add = () => onChange([...values, '']);
  return (
    <div className="pc-drawer__multi-input">
      {values.map((v, i) => (
        <div key={i} className="pc-drawer__multi-row">
          <input
            className="pc-drawer__input"
            value={v}
            onChange={(e) => update(i, e.target.value)}
            placeholder={placeholder}
          />
          <button
            type="button"
            className="pc-drawer__chip-close"
            onClick={() => remove(i)}
            aria-label="remove"
          >
            <X size={12} />
          </button>
        </div>
      ))}
      <button type="button" className="pc-drawer__link pc-drawer__add-url" onClick={add}>
        {t('personalContext.addContent.addUrl')}
      </button>
    </div>
  );
}

interface MultiSelectDropdownProps<T extends string> {
  options: readonly T[];
  selected: T[];
  labelKey: (opt: T) => string;
  onToggle: (opt: T) => void;
  placeholder: string;
}

function MultiSelectDropdown<T extends string>({ options, selected, labelKey, onToggle, placeholder }: MultiSelectDropdownProps<T>) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <div className="pc-drawer__multi-select">
      <button
        type="button"
        className="pc-drawer__multi-select-trigger"
        onClick={() => setOpen(!open)}
      >
        <span className="pc-drawer__multi-select-tags">
          {selected.length > 0 ? (
            options.filter(o => selected.includes(o)).map((opt) => (
              <span key={opt} className="pc-drawer__chip pc-drawer__chip--active">
                {t(labelKey(opt))}
                <span
                  className="pc-drawer__chip-close"
                  onClick={(e) => { e.stopPropagation(); onToggle(opt); }}
                  role="button"
                  tabIndex={0}
                >
                  <X size={12} />
                </span>
              </span>
            ))
          ) : (
            <span className="pc-drawer__multi-select-placeholder">{placeholder}</span>
          )}
        </span>
        <span className="pc-drawer__multi-select-arrow">{'<'}{''}</span>
      </button>
      {open && (
        <div className="pc-drawer__multi-select-menu">
          {options.map((opt) => {
            const active = selected.includes(opt);
            return (
              <button
                key={opt}
                type="button"
                className={'pc-drawer__multi-select-option' + (active ? ' pc-drawer__multi-select-option--active' : '')}
                onClick={() => onToggle(opt)}
              >
                <span className="pc-drawer__multi-select-check">{active ? '✓' : ''}</span>
                {t(labelKey(opt))}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ProviderFields(props: ProviderFieldsProps) {
  const { t } = useTranslation();
  const { provider } = props;

  if (provider === 'feishu') {
    const toggleResource = (r: FeishuResource) => {
      const has = props.feishuResources.includes(r);
      const next = has ? props.feishuResources.filter((x) => x !== r) : [...props.feishuResources, r];
      props.setFeishuResources(next);
    };
    return (
      <div className="pc-drawer__fields-group">
        {/* 飞书内容类型 — pill 切换 */}
        <div className="pc-drawer__field">
          <label>{t('personalContext.addContent.feishu.modeLabel')}</label>
          <div className="pc-drawer__mode-pills">
            {FEISHU_MODES.map((m) => (
              <button
                key={m}
                type="button"
                className={'pc-drawer__mode-pill' + (props.feishuMode === m ? ' pc-drawer__mode-pill--active' : '')}
                onClick={() => props.setFeishuMode(m)}
              >
                {t('personalContext.addContent.feishu.mode_' + m)}
              </button>
            ))}
          </div>
        </div>

        {props.feishuMode === 'account' ? (
          <>
            {/* 要采集的账号内容 — chips */}
            <div className="pc-drawer__field">
              <label>{t('personalContext.addContent.feishu.resourcesLabel')}</label>
              <MultiSelectDropdown
                options={FEISHU_RESOURCES}
                selected={props.feishuResources}
                labelKey={(r) => FEISHU_RESOURCE_LABEL_KEYS[r]}
                onToggle={toggleResource}
                placeholder={t('personalContext.addContent.feishu.resourcesHint')}
              />
            </div>

            {/* 日历时间段 — 仅选了日历才显示，点击打开日期范围弹窗 */}
            {props.feishuResources.includes('calendar') && (
              <div className="pc-drawer__field">
                <label>{t('personalContext.addContent.feishu.calendarTimeLabel')}</label>
                <button
                  type="button"
                  className="pc-drawer__calendar-trigger"
                  onClick={() => props.setFeishuCalendarOpen(true)}
                >
                  <span>{props.feishuCalendarStart && props.feishuCalendarEnd ? `${props.feishuCalendarStart} - ${props.feishuCalendarEnd}` : t('personalContext.addContent.feishu.calendarTimePlaceholder')}</span>
                  <span className="pc-drawer__select-arrow">{'<'}</span>
                </button>
              </div>
            )}

            {/* 指定文档（可选） */}
            <div className="pc-drawer__field">
              <label>{t('personalContext.addContent.feishu.docPathLabel')}</label>
              <MultiTextInput
                values={props.feishuDocIdList}
                onChange={props.setFeishuDocIdList}
                placeholder={t('personalContext.addContent.feishu.docPathPlaceholder')}
              />
            </div>
          </>
        ) : (
          <>
            {/* Wiki 空间名称 */}
            <div className="pc-drawer__field">
              <label>{t('personalContext.addContent.feishu.wikiSpaceIdLabel')}</label>
              <input
                className="pc-drawer__input"
                value={props.feishuWikiSpaceId}
                onChange={(e) => props.setFeishuWikiSpaceId(e.target.value)}
                placeholder={t('personalContext.addContent.feishu.wikiSpaceIdPlaceholder')}
              />
            </div>
          </>
        )}
      </div>
    );
  }

  if (provider === 'local_files') {
    return (
      <div className="pc-drawer__field">
        <label>{t('personalContext.addContent.localFiles.rootDirLabel')}</label>
        <input
          className="pc-drawer__input"
          value={props.rootDir}
          onChange={(e) => props.setRootDir(e.target.value)}
          placeholder={t('personalContext.addContent.localFiles.rootDirPlaceholder')}
        />
      </div>
    );
  }

  if (provider === 'zhihu_reader') {
    return (
      <div className="pc-drawer__field">
        <label>{t('personalContext.addContent.zhihu.columnUrlLabel')}</label>
        <input
          className="pc-drawer__input"
          value={props.columnUrl}
          onChange={(e) => props.setColumnUrl(e.target.value)}
          placeholder={t('personalContext.addContent.zhihu.columnUrlPlaceholder')}
        />
      </div>
    );
  }

  if (provider === 'toutiao_reader') {
    return (
      <div className="pc-drawer__field">
        <label>{t('personalContext.addContent.toutiao.profileUrlLabel')}</label>
        <input
          className="pc-drawer__input"
          value={props.profileUrl}
          onChange={(e) => props.setProfileUrl(e.target.value)}
          placeholder={t('personalContext.addContent.toutiao.profileUrlPlaceholder')}
        />
      </div>
    );
  }

  if (provider === 'browser_bookmarks') {
    return (
      <div className="pc-drawer__fields-group">
        <div className="pc-drawer__field">
          <label>{t('personalContext.addContent.edge.profileLabel')}</label>
          <select
            className="pc-drawer__select"
            value={props.edgeProfile}
            onChange={(e) => props.setEdgeProfile(e.target.value)}
          >
            <option value="">{t('personalContext.addContent.edge.profilePlaceholder')}</option>
            <option value="Default">{t('personalContext.addContent.edge.profileDefault')}</option>
          </select>
        </div>
        <div className="pc-drawer__field">
          <label>{t('personalContext.addContent.edge.bookmarksPathLabel')}</label>
          <input
            className="pc-drawer__input"
            value={props.edgeBookmarksPath}
            onChange={(e) => props.setEdgeBookmarksPath(e.target.value)}
            placeholder={t('personalContext.addContent.edge.bookmarksPathPlaceholder')}
          />
        </div>
        <div className="pc-drawer__field">
          <label>{t('personalContext.addContent.edge.foldersLabel')}</label>
          <MultiTextInput
            values={props.edgeFolderList}
            onChange={props.setEdgeFolderList}
            placeholder={t('personalContext.addContent.edge.foldersPlaceholder')}
          />
        </div>
      </div>
    );
  }

  if (provider === 'github') {
    const toggleResource = (r: GithubResource) => {
      const has = props.githubResources.includes(r);
      const next = has ? props.githubResources.filter((x) => x !== r) : [...props.githubResources, r];
      props.setGithubResources(next);
    };
    return (
      <div className="pc-drawer__fields-group">
        <div className="pc-drawer__field">
          <label>{t('personalContext.addContent.github.repoUrlLabel')}</label>
          <input
            className="pc-drawer__input"
            value={props.githubRepoUrl}
            onChange={(e) => props.setGithubRepoUrl(e.target.value)}
            placeholder={t('personalContext.addContent.github.repoUrlPlaceholder')}
          />
        </div>
        <div className="pc-drawer__field">
          <label>{t('personalContext.addContent.github.resourcesLabel')}</label>
          <MultiSelectDropdown
            options={GITHUB_RESOURCES}
            selected={props.githubResources}
            labelKey={(r) => GITHUB_RESOURCE_LABEL_KEYS[r]}
            onToggle={toggleResource}
            placeholder={t('personalContext.addContent.github.resourcesLabel')}
          />
        </div>
      </div>
    );
  }

  if (provider === 'gitcode') {
    const toggleResource = (r: GitcodeResource) => {
      const has = props.gitcodeResources.includes(r);
      const next = has ? props.gitcodeResources.filter((x) => x !== r) : [...props.gitcodeResources, r];
      props.setGitcodeResources(next);
    };
    return (
      <div className="pc-drawer__fields-group">
        <div className="pc-drawer__field">
          <label>{t('personalContext.addContent.gitcode.repoUrlLabel')}</label>
          <input
            className="pc-drawer__input"
            value={props.gitcodeRepoUrl}
            onChange={(e) => props.setGitcodeRepoUrl(e.target.value)}
            placeholder={t('personalContext.addContent.gitcode.repoUrlPlaceholder')}
          />
        </div>
        <div className="pc-drawer__field">
          <label>{t('personalContext.addContent.gitcode.resourcesLabel')}</label>
          <MultiSelectDropdown
            options={GITCODE_RESOURCES}
            selected={props.gitcodeResources}
            labelKey={(r) => GITCODE_RESOURCE_LABEL_KEYS[r]}
            onToggle={toggleResource}
            placeholder={t('personalContext.addContent.gitcode.resourcesLabel')}
          />
        </div>
      </div>
    );
  }

  return null;
}
