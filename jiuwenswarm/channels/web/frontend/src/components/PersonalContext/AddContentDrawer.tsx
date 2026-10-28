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

import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight, Loader2, X } from 'lucide-react';
import AddIcon from '../../assets/add.svg?react';
import ReduceIcon from '../../assets/reduce.svg?react';
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
  isFetchTaskRunningError,
  parseGithubRepoUrl,
  parseGitcodeRepoUrl,
  validateServiceId,
  validateToutiaoProfileUrl,
  validateZhihuColumnUrl,
} from '../../services/personalContextApi';
import { requestSettingsModule } from '../../features/settings/settingsNavigation';
import { selectProjectDirectory } from '../../features/workspace/projectDirectoryPicker';
import { selectLocalFiles } from '../../features/workspace/localFilePicker';
import { toast } from '../../components/ui/Toast/toastStore';
import { Input } from '../ui/Input/Input';
import { Select } from '../ui/Select/Select';
import { parseEdgeBookmarkFolderPaths } from './edgeBookmarkFolders';
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
  return { unit: 'hour', value: sec / FREQUENCY_SECONDS.hour };
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
  const [freqInput, setFreqInput] = useState(() => String(freqFromSeconds(editService?.interval_seconds ?? 3 * FREQUENCY_SECONDS.day).value));
  const freqValue = Number(freqInput);
  const [timeRange, setTimeRange] = useState<'week' | 'month' | 'quarter' | 'custom'>(() => timeRangeFromConfig(editService?.time_range).timeRange);
  const [customStart, setCustomStart] = useState(() => timeRangeFromConfig(editService?.time_range).customStart);
  const [customEnd, setCustomEnd] = useState(() => timeRangeFromConfig(editService?.time_range).customEnd);
  // 默认 20 条；填值须 [1,10000]
  const [maxItemsInput, setMaxItemsInput] = useState(() => String(editService?.max_items_per_run ?? 20));
  const [maxItemsBadInput, setMaxItemsBadInput] = useState(false);
  const maxItems = maxItemsInput === '' ? null : Number(maxItemsInput);
  const [advancedOpen, setAdvancedOpen] = useState(false);
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
  // 是否点过「添加/保存」。对齐 connector-market CreatePluginPage 的校验口径：
  // 必填为空的红色提示只在点过提交后出现（一进来不红）；格式非法仍然实时红。
  const [submitAttempted, setSubmitAttempted] = useState(false);
  const submitting = editService ? !!pendingWrites[`patch:${editService.service_id}`] : !!pendingWrites.create_service;

  // 飞书允许未授权时新建（先配 service 再授权）；其余 provider 仍需先授权。
  const requiresAuth = provider !== 'feishu';
  const authorized = isProviderAuthorized(provider);

  // —— 字段级校验 ——
  // 规则：填了但格式非法 → 实时红；必填为空 → 点过「添加」才红。
  const nameErrorKey = isEdit
    ? null
    : !name.trim()
      ? submitAttempted
        ? 'personalContext.addContent.nameRequired'
        : null
      : validateServiceId(name)
        ? 'personalContext.addContent.nameFormatError'
        : null;
  const frequencyErrorKey = (() => {
    const invalid =
      !Number.isFinite(freqValue) ||
      !Number.isInteger(freqValue) ||
      freqValue < 1 ||
      freqValue > maxFreqValue(freqUnit);
    if (!invalid) return null;
    // 清空重输的过程态不算错，点过「添加」后才红
    if (freqInput.trim() === '' && !submitAttempted) return null;
    return 'personalContext.addContent.frequencyRangeError';
  })();
  const maxItemsErrorKey =
    maxItemsBadInput ||
    (maxItems !== null &&
      (!Number.isFinite(maxItems) ||
        !Number.isInteger(maxItems) ||
        maxItems < MAX_ITEMS_MIN ||
        maxItems > MAX_ITEMS_MAX))
      ? 'personalContext.addContent.maxItemsRangeError'
      : null;
  // 分支来源：必填为空 → 点过「添加」才报；填了但格式非法 → 实时报。
  const sourceErrorKey = (() => {
    switch (provider) {
      case 'local_files':
        return rootDir.trim() ? null : submitAttempted ? 'personalContext.addContent.localFiles.rootDirRequired' : null;
      case 'zhihu_reader':
        return columnUrl.trim()
          ? validateZhihuColumnUrl(columnUrl)
            ? 'personalContext.addContent.zhihu.columnUrlHint'
            : null
          : submitAttempted
            ? 'personalContext.addContent.zhihu.columnUrlHint'
            : null;
      case 'toutiao_reader':
        return profileUrl.trim()
          ? validateToutiaoProfileUrl(profileUrl)
            ? 'personalContext.addContent.toutiao.profileUrlHint'
            : null
          : submitAttempted
            ? 'personalContext.addContent.toutiao.profileUrlHint'
            : null;
      case 'github': {
        if (!githubRepoUrl.trim()) return submitAttempted ? 'personalContext.addContent.github.sourceInvalid' : null;
        if ('error' in parseGithubRepoUrl(githubRepoUrl)) return 'personalContext.addContent.github.sourceInvalid';
        return githubResources.length > 0
          ? null
          : submitAttempted
            ? 'personalContext.addContent.github.resourcesRequired'
            : null;
      }
      case 'gitcode': {
        if (!gitcodeRepoUrl.trim()) return submitAttempted ? 'personalContext.addContent.gitcode.sourceInvalid' : null;
        if ('error' in parseGitcodeRepoUrl(gitcodeRepoUrl)) return 'personalContext.addContent.gitcode.sourceInvalid';
        return gitcodeResources.length > 0
          ? null
          : submitAttempted
            ? 'personalContext.addContent.gitcode.resourcesRequired'
            : null;
      }
      case 'feishu':
        if (feishuMode === 'wiki_space') {
          return feishuWikiSpaceId.trim()
            ? null
            : submitAttempted
              ? 'personalContext.addContent.feishu.wikiSpaceIdRequired'
              : null;
        }
        return feishuResources.length > 0
          ? null
          : submitAttempted
            ? 'personalContext.addContent.feishu.resourcesRequired'
            : null;
      case 'browser_bookmarks':
        return null;
      default:
        return null;
    }
  })();

  // 底部灰字提示：只承载与具体字段无关的全局阻塞（未开启采集 / 来源未授权）。
  // 字段级错误一律在各自输入框下方红字展示，不在底部重复。
  const configBlockKey = !isConfigured
    ? 'personalContext.addContent.notConfigured'
    : !isEdit && requiresAuth && !authorized
      ? 'personalContext.addContent.providerUnauthorized'
      : null;

  // 提交门禁（严格口径）：按钮禁用与 handleSubmit 提前 return 共用同一判定，避免两处漂移。
  // 不能复用 frequencyErrorKey / sourceErrorKey 等字段级错误——它们带 submitAttempted
  // 过程态豁免（必填为空先不红），按钮禁用必须按「是否真的能提交」判定。
  const submitBlocked =
    !isConfigured ||
    (!isEdit && requiresAuth && !authorized) ||
    (!isEdit && (!name.trim() || !!validateServiceId(name))) ||
    !Number.isFinite(freqValue) ||
    !Number.isInteger(freqValue) ||
    freqValue < 1 ||
    freqValue > maxFreqValue(freqUnit) ||
    maxItemsBadInput ||
    (maxItems !== null && (maxItems < MAX_ITEMS_MIN || maxItems > MAX_ITEMS_MAX)) ||
    (timeRange === 'custom' && (!customStart || !customEnd)) ||
    (provider === 'local_files' && !rootDir.trim()) ||
    (provider === 'zhihu_reader' && !!validateZhihuColumnUrl(columnUrl)) ||
    (provider === 'toutiao_reader' && !!validateToutiaoProfileUrl(profileUrl)) ||
    (provider === 'github' && ('error' in parseGithubRepoUrl(githubRepoUrl) || githubResources.length === 0)) ||
    (provider === 'gitcode' && ('error' in parseGitcodeRepoUrl(gitcodeRepoUrl) || gitcodeResources.length === 0)) ||
    (provider === 'feishu' &&
      (feishuMode === 'wiki_space' ? !feishuWikiSpaceId.trim() : feishuResources.length === 0));

  const handleSubmit = async () => {
    setSubmitAttempted(true);
    setError(null);
    // 门禁集中在 submitBlocked（与按钮禁用同一判定），此处不再重复逐项校验。
    if (submitBlocked) return;

    let source: Record<string, unknown> = {};

    if (provider === 'local_files') {
      source = { root_dir: rootDir.trim() };
    } else if (provider === 'zhihu_reader') {
      source = { column_url: columnUrl.trim() };
    } else if (provider === 'toutiao_reader') {
      source = { profile_url: profileUrl.trim() };
    } else if (provider === 'browser_bookmarks') {
      const s: Record<string, unknown> = { include_subfolders: true, fetch_page_content: true };
      if (edgeProfile.trim()) s.profile = edgeProfile.trim();
      if (edgeBookmarksPath.trim()) s.bookmarks_path = edgeBookmarksPath.trim();
      const folders = parseEdgeBookmarkFolderPaths(edgeFolderList);
      if (folders.length) s.bookmark_folder_paths = folders;
      source = s;
    } else if (provider === 'github') {
      const parsed = parseGithubRepoUrl(githubRepoUrl);
      if ('error' in parsed) return; // 正常已被 submitBlocked 拦截，此处仅供 TS 收窄
      source = { owner: parsed.owner, repo: parsed.repo, resources: [...githubResources] };
    } else if (provider === 'gitcode') {
      const parsed = parseGitcodeRepoUrl(gitcodeRepoUrl);
      if ('error' in parsed) return; // 正常已被 submitBlocked 拦截，此处仅供 TS 收窄
      source = { owner: parsed.owner, repo: parsed.repo, resources: [...gitcodeResources] };
    } else if (provider === 'feishu') {
      // 飞书先建 service（未授权也可建），建完后需去设置页授权。
      // 后端 _normalize_service_source feishu 分支：mode ∈ {account, wiki_space}；
      // account 需 resources ⊆ {docs,tasks,calendar}；wiki_space 需 wiki_space_id。
      // 飞书不接受 credentials（授权走 OAuth 设备流，非 token 注入）。
      if (feishuMode === 'wiki_space') {
        source = { mode: 'wiki_space', wiki_space_id: feishuWikiSpaceId.trim() };
      } else {
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
      if (isFetchTaskRunningError(e)) {
        toast.open({ content: t('personalContext.services.fetchTaskRunning'), variant: 'warning' });
        return;
      }
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
        data-testid="personal-context-add-content-drawer"
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
            <label htmlFor="personal-context-content-name" data-testid="personal-context-content-name-label">{t('personalContext.addContent.nameLabel')}</label>
            {isEdit ? (
              <div className="pc-drawer__locked">{name}</div>
            ) : (
              <Input
                id="personal-context-content-name"
                data-testid="personal-context-content-name"
                invalid={!!nameErrorKey}
                aria-describedby={nameErrorKey ? 'personal-context-name-error' : undefined}
                value={name}
                onChange={setName}
                placeholder={t('personalContext.addContent.namePlaceholder')}
              />
            )}
            {nameErrorKey && <div id="personal-context-name-error" className="pc-drawer__field-error" data-testid="personal-context-name-error">{t(nameErrorKey)}</div>}
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
                        setSubmitAttempted(false);
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
            sourceErrorKey={sourceErrorKey}
            submitAttempted={submitAttempted}
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
          {sourceErrorKey && <div id="personal-context-source-error" className="pc-drawer__field-error" data-testid="personal-context-source-error">{t(sourceErrorKey)}</div>}

          {/* 高级配置 */}
          <button
            type="button"
            className={'pc-drawer__advanced-toggle' + (advancedOpen ? ' pc-drawer__advanced-toggle--open' : '')}
            data-testid="personal-context-advanced-toggle"
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
                <Select
                  aria-label={t('personalContext.addContent.timeRangeLabel')}
                  value={timeRange}
                  style={{ width: '100%' }}
                  options={(['week', 'month', 'quarter', 'custom'] as const).map((opt) => ({
                    value: opt,
                    label: t('personalContext.addContent.timeRange' + (opt === 'week' ? 'Week' : opt === 'month' ? 'Month' : opt === 'quarter' ? 'Quarter' : 'Custom')),
                    trailing: opt === 'custom' ? <ChevronRight size={14} /> : undefined,
                  }))}
                  onChange={(opt) => {
                    setTimeRange(opt as 'week' | 'month' | 'quarter' | 'custom');
                    // 选「自定义」直接展开日期范围弹窗；选固定档位收起
                    setCalendarOpen(opt === 'custom');
                  }}
                  data-testid="personal-context-time-range-select"
                />
                {timeRange === 'custom' && (
                  <button
                    type="button"
                    className={`ui-select${submitAttempted && (!customStart || !customEnd) ? ' ui-select--invalid' : ''}`}
                    data-testid="personal-context-date-range"
                    aria-invalid={submitAttempted && (!customStart || !customEnd)}
                    aria-describedby={
                      submitAttempted && (!customStart || !customEnd) ? 'personal-context-date-range-error' : undefined
                    }
                    style={{ width: '100%' }}
                    onClick={() => setCalendarOpen(true)}
                  >
                    {customStart && customEnd ? `${customStart} - ${customEnd}` : t('personalContext.addContent.dateRangePlaceholder')}
                  </button>
                )}
                {timeRange === 'custom' && submitAttempted && (!customStart || !customEnd) && (
                  <div
                    id="personal-context-date-range-error"
                    className="pc-drawer__field-error"
                    data-testid="personal-context-date-range-error"
                  >
                    {t('personalContext.addContent.dateRangeRequired')}
                  </div>
                )}
              </div>

              {/* 自动采集频率 */}
              <div className="pc-drawer__field">
                <label htmlFor="personal-context-frequency" data-testid="personal-context-frequency-label">{t('personalContext.addContent.frequencyLabel')}</label>
                <div className="pc-drawer__freq">
                  <div className="pc-drawer__stepper">
                    <Input
                      type="number"
                      id="personal-context-frequency"
                      data-testid="personal-context-frequency"
                      value={freqInput}
                      invalid={!!frequencyErrorKey}
                      aria-describedby={frequencyErrorKey ? 'personal-context-frequency-error' : undefined}
                      step={1}
                      min={1}
                      max={maxFreqValue(freqUnit)}
                      onChange={(v) => {
                        // 超上限立即落至上限，不等失焦；下限(0/空)保留过程态由组件失焦钳制兜底
                        const parsed = Number(v);
                        setFreqInput(v.trim() !== '' && parsed > maxFreqValue(freqUnit) ? String(maxFreqValue(freqUnit)) : v);
                      }}
                      prefix={
                        <button
                          type="button"
                          className="pc-drawer__step-btn"
                          data-testid="personal-context-frequency-decrease"
                          onClick={() => setFreqInput(String(Math.max(1, freqValue - 1)))}
                        >
                          <ReduceIcon width={16} height={16} aria-hidden="true" />
                        </button>
                      }
                      suffix={
                        <button
                          type="button"
                          className="pc-drawer__step-btn"
                          data-testid="personal-context-frequency-increase"
                          onClick={() => setFreqInput(String(clampFreq(freqValue + 1, freqUnit)))}
                        >
                          <AddIcon width={16} height={16} aria-hidden="true" />
                        </button>
                      }
                    />
                  </div>
                  <div className="pc-drawer__freq-pills">
                    <button
                      type="button"
                      className={'pc-drawer__freq-pill' + (freqUnit === 'hour' ? ' pc-drawer__freq-pill--active' : '')}
                      data-testid="personal-context-frequency-hour"
                      onClick={() => setFreqUnit('hour')}
                    >
                      {t('personalContext.addContent.unitHour')}
                    </button>
                    <button
                      type="button"
                      className={'pc-drawer__freq-pill' + (freqUnit === 'day' ? ' pc-drawer__freq-pill--active' : '')}
                      data-testid="personal-context-frequency-day"
                      onClick={() => setFreqUnit('day')}
                    >
                      {t('personalContext.addContent.unitDay')}
                    </button>
                  </div>
                </div>
                {frequencyErrorKey && <div id="personal-context-frequency-error" className="pc-drawer__field-error" data-testid="personal-context-frequency-error">{t(frequencyErrorKey, { max: maxFreqValue(freqUnit) })}</div>}
              </div>

              {/* 单次最多采集条数 */}
              <div className="pc-drawer__field">
                <label htmlFor="personal-context-max-items" data-testid="personal-context-max-items-label">{t('personalContext.addContent.maxItemsLabel')}</label>
                <div className="pc-drawer__stepper">
                  <Input
                    type="number"
                    id="personal-context-max-items"
                    data-testid="personal-context-max-items"
                    value={maxItemsInput}
                    invalid={!!maxItemsErrorKey}
                    aria-describedby={maxItemsErrorKey ? 'personal-context-max-items-error' : undefined}
                    step={1}
                    min={MAX_ITEMS_MIN}
                    max={MAX_ITEMS_MAX}
                    placeholder="20"
                    onChange={setMaxItemsInput}
                    // badInput 只由 onInput 维护：同一原生 input 事件里 React 先派发 onInput、
                    // 后派发 onChange，若在 onChange 里重置，会把 onInput 刚写入的 true 抹掉
                    // （如 "5"→"5a"），非法输入被静默放行。+/- 按钮路径在其 onClick 里显式清标志。
                    onInput={(e) => setMaxItemsBadInput(e.currentTarget.validity?.badInput ?? false)}
                    prefix={
                      <button
                        type="button"
                        className="pc-drawer__step-btn"
                        data-testid="personal-context-max-items-decrease"
                        onClick={() => {
                          setMaxItemsInput(String(Math.max(MAX_ITEMS_MIN, (maxItems ?? 20) - 1)));
                          setMaxItemsBadInput(false);
                        }}
                      >
                        <ReduceIcon width={16} height={16} aria-hidden="true" />
                      </button>
                    }
                    suffix={
                      <button
                        type="button"
                        className="pc-drawer__step-btn"
                        data-testid="personal-context-max-items-increase"
                        onClick={() => {
                          setMaxItemsInput(String(Math.min(MAX_ITEMS_MAX, (maxItems ?? 20) + 1)));
                          setMaxItemsBadInput(false);
                        }}
                      >
                        <AddIcon width={16} height={16} aria-hidden="true" />
                      </button>
                    }
                  />
                </div>
                {maxItemsErrorKey && <div id="personal-context-max-items-error" className="pc-drawer__field-error" data-testid="personal-context-max-items-error">{t(maxItemsErrorKey)}</div>}
              </div>
              </div>
          )}
          {error && <div className="pc-drawer__error" data-testid="personal-context-content-error" role="alert">{error}</div>}
        </div>

        <footer className="pc-drawer__foot">
          {configBlockKey && (
            <div className="pc-drawer__foot-hint" data-testid="personal-context-submit-hint">
              {t(configBlockKey)}
            </div>
          )}
          <button type="button" className="pc-drawer__foot-btn pc-drawer__foot-btn--secondary" onClick={onClose} disabled={submitting}>
            {t('personalContext.services.cancel')}
          </button>
          <button
            type="button"
            className="pc-drawer__foot-btn pc-drawer__foot-btn--primary"
            data-testid="personal-context-content-submit"
            onClick={handleSubmit}
            disabled={submitting || submitBlocked}
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
  sourceErrorKey: string | null;
  /** 是否点过「添加」：必填为空的红色校验只在点过后出现（一进来不红）。 */
  submitAttempted: boolean;
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
            <Input
              value={start}
              data-testid="personal-context-calendar-start-input"
              onChange={setStart}
              placeholder="YYYY-MM-DD"
            />
            <Input
              value={end}
              data-testid="personal-context-calendar-end-input"
              onChange={setEnd}
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
          <Input value={v} onChange={(next) => update(i, next)} placeholder={placeholder} />
          <button type="button" className="pc-drawer__chip-close" onClick={() => remove(i)} aria-label="remove">
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
  invalid?: boolean;
}

/** 下拉通用：open 时监听 document mousedown，点击容器外部任意处触发 onDismiss。 */
function useOutsideDismiss(
  containerRef: { current: HTMLElement | null },
  open: boolean,
  onDismiss: () => void,
): void {
  const onDismissRef = useRef(onDismiss);
  onDismissRef.current = onDismiss;
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        onDismissRef.current();
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [containerRef, open]);
}

function MultiSelectDropdown<T extends string>({ options, selected, labelKey, onToggle, placeholder, invalid }: MultiSelectDropdownProps<T>) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  // 点击多选下拉外部任意处自动收起（选项连续勾选时保持展开）
  useOutsideDismiss(containerRef, open, () => setOpen(false));
  return (
    <div className="pc-drawer__multi-select" ref={containerRef}>
      <button
        type="button"
        className="pc-drawer__multi-select-trigger"
        data-testid="personal-context-resource-select"
        aria-invalid={invalid}
        aria-describedby={invalid ? 'personal-context-source-error' : undefined}
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
  /** 路径/文件选择器失败时给出反馈；取消选择保持静默。 */
  const notifyPickerFailure = (result: {
    ok: boolean;
    reason?: 'unsupported' | 'cancelled' | 'failed';
    message?: string;
  }) => {
    if (result.ok || result.reason === 'cancelled') return;
    const content =
      result.reason === 'unsupported'
        ? t('chat.inputAttachment.filePickerUnsupported')
        : result.message || t('chat.inputAttachment.filePickerFailed');
    toast.open({ content, variant: 'error' });
  };

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
                invalid={props.submitAttempted && props.feishuResources.length === 0}
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
              <Input
                value={props.feishuWikiSpaceId}
                data-testid="personal-context-feishu-wiki-space"
                invalid={!!props.sourceErrorKey}
                aria-describedby={props.sourceErrorKey ? 'personal-context-source-error' : undefined}
                onChange={props.setFeishuWikiSpaceId}
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
        <Input
          value={props.rootDir}
          data-testid="personal-context-local-root-directory"
          invalid={!!props.sourceErrorKey}
          aria-describedby={props.sourceErrorKey ? 'personal-context-source-error' : undefined}
          readOnly
          placeholder={t('personalContext.addContent.localFiles.rootDirPlaceholder')}
          suffix={
            <button
              type="button"
              className="pc-drawer__path-btn"
              onClick={async () => {
                const result = await selectProjectDirectory({ initialDir: props.rootDir || undefined });
                if (result.ok && result.path) props.setRootDir(result.path);
                else notifyPickerFailure(result);
              }}
              aria-label="browse"
            >
              <svg
                viewBox="0 0 24 24"
                width="16"
                height="16"
                fill="none"
                stroke="currentColor"
                strokeWidth={1.5}
                aria-hidden="true"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"
                />
              </svg>
            </button>
          }
        />
      </div>
    );
  }

  if (provider === 'zhihu_reader') {
    return (
      <div className="pc-drawer__field">
        <label>{t('personalContext.addContent.zhihu.columnUrlLabel')}</label>
        <Input
          value={props.columnUrl}
          data-testid="personal-context-zhihu-column-url"
          invalid={!!props.sourceErrorKey}
          aria-describedby={props.sourceErrorKey ? 'personal-context-source-error' : undefined}
          onChange={props.setColumnUrl}
          placeholder={t('personalContext.addContent.zhihu.columnUrlPlaceholder')}
        />
      </div>
    );
  }

  if (provider === 'toutiao_reader') {
    return (
      <div className="pc-drawer__field">
        <label>{t('personalContext.addContent.toutiao.profileUrlLabel')}</label>
        <Input
          value={props.profileUrl}
          data-testid="personal-context-toutiao-profile-url"
          invalid={!!props.sourceErrorKey}
          aria-describedby={props.sourceErrorKey ? 'personal-context-source-error' : undefined}
          onChange={props.setProfileUrl}
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
          <Input
            value={props.edgeProfile}
            data-testid="personal-context-edge-profile"
            onChange={props.setEdgeProfile}
            placeholder={t('personalContext.addContent.edge.profilePlaceholder')}
            suffix={
              <button
                type="button"
                className="pc-drawer__path-btn"
                onClick={async () => {
                  const result = await selectProjectDirectory({});
                  if (result.ok && result.path) {
                    const segments = result.path.replace(/\/+$/, '').split(/[\\/]/);
                    const name = segments[segments.length - 1];
                    if (name) props.setEdgeProfile(name);
                  } else {
                    notifyPickerFailure(result);
                  }
                }}
                aria-label="browse"
              >
                <svg
                  viewBox="0 0 24 24"
                  width="16"
                  height="16"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={1.5}
                  aria-hidden="true"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"
                  />
                </svg>
              </button>
            }
          />
          <div className="pc-drawer__field-hint">{t('personalContext.addContent.edge.profileHint')}</div>
        </div>
        <div className="pc-drawer__field">
          <label>{t('personalContext.addContent.edge.bookmarksPathLabel')}</label>
          <Input
            value={props.edgeBookmarksPath}
            data-testid="personal-context-edge-bookmarks-path"
            readOnly
            placeholder={t('personalContext.addContent.edge.bookmarksPathPlaceholder')}
            suffix={
              <button
                type="button"
                className="pc-drawer__path-btn"
                onClick={async () => {
                  const result = await selectLocalFiles(false);
                  if (result.ok && result.files[0]?.path) props.setEdgeBookmarksPath(result.files[0].path);
                  else notifyPickerFailure(result);
                }}
                aria-label="browse"
              >
                <svg
                  viewBox="0 0 24 24"
                  width="16"
                  height="16"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={1.5}
                  aria-hidden="true"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M15.5 3H8a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7.5L15.5 3z"
                  />
                  <path strokeLinecap="round" strokeLinejoin="round" d="M15 3v5h5" />
                </svg>
              </button>
            }
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
    // 仓库链接：空 → 点过「添加」才红；填了但非法 → 实时红
    const urlInvalid = !props.githubRepoUrl.trim()
      ? props.submitAttempted
      : 'error' in parseGithubRepoUrl(props.githubRepoUrl);
    return (
      <div className="pc-drawer__fields-group">
        <div className="pc-drawer__field">
          <label>{t('personalContext.addContent.github.repoUrlLabel')}</label>
          <Input
            value={props.githubRepoUrl}
            data-testid="personal-context-github-repository-url"
            invalid={urlInvalid}
            aria-describedby={urlInvalid ? 'personal-context-source-error' : undefined}
            onChange={props.setGithubRepoUrl}
            placeholder={t('personalContext.addContent.github.repoUrlPlaceholder')}
          />
        </div>
        <div className="pc-drawer__field">
          <label>{t('personalContext.addContent.github.resourcesLabel')}</label>
          <MultiSelectDropdown
            options={GITHUB_RESOURCES}
            invalid={props.submitAttempted && props.githubResources.length === 0}
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
    // 仓库链接：空 → 点过「添加」才红；填了但非法 → 实时红
    const urlInvalid = !props.gitcodeRepoUrl.trim()
      ? props.submitAttempted
      : 'error' in parseGitcodeRepoUrl(props.gitcodeRepoUrl);
    return (
      <div className="pc-drawer__fields-group">
        <div className="pc-drawer__field">
          <label>{t('personalContext.addContent.gitcode.repoUrlLabel')}</label>
          <Input
            value={props.gitcodeRepoUrl}
            data-testid="personal-context-gitcode-repository-url"
            invalid={urlInvalid}
            aria-describedby={urlInvalid ? 'personal-context-source-error' : undefined}
            onChange={props.setGitcodeRepoUrl}
            placeholder={t('personalContext.addContent.gitcode.repoUrlPlaceholder')}
          />
        </div>
        <div className="pc-drawer__field">
          <label>{t('personalContext.addContent.gitcode.resourcesLabel')}</label>
          <MultiSelectDropdown
            options={GITCODE_RESOURCES}
            invalid={props.submitAttempted && props.gitcodeResources.length === 0}
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
