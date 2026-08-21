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
  type FeishuMode,
  type FeishuResource,
  type GithubResource,
  FEISHU_MODES,
  FEISHU_RESOURCE_LABEL_KEYS,
  FEISHU_RESOURCES,
  FREQUENCY_OPTIONS,
  FREQUENCY_SECONDS,
  GITHUB_RESOURCE_LABEL_KEYS,
  GITHUB_RESOURCES,
  MAX_ITEMS_MAX,
  MAX_ITEMS_MIN,
  PROVIDER_LABEL_KEYS,
  PROVIDER_ORDER,
  getGithubToken,
  parseGithubRepoUrl,
  validateServiceId,
  validateToutiaoProfileUrl,
  validateZhihuColumnUrl,
} from '../../services/personalContextApi';
import localFilesIcon from '../../assets/settings/channels/local-files.svg';
import edgeBookmarksIcon from '../../assets/settings/channels/edge-bookmarks.svg';
import zhihuIcon from '../../assets/settings/channels/zhihu.svg';
import toutiaoIcon from '../../assets/settings/channels/toutiao.svg';
import feishuIcon from '../../assets/settings/channels/feishu.svg';
import githubIcon from '../../assets/settings/channels/GitHub.svg';
import './AddContentDrawer.css';

const PROVIDER_ICON: Record<FetchProvider, string> = {
  local_files: localFilesIcon,
  browser_bookmarks: edgeBookmarksIcon,
  zhihu_reader: zhihuIcon,
  toutiao_reader: toutiaoIcon,
  feishu: feishuIcon,
  github: githubIcon,
};

interface AddContentDrawerProps {
  /** 从内容页级联分类带入的预选 provider；缺省回退到首个。 */
  initialProvider?: FetchProvider;
  onClose: () => void;
  onCreated: () => void;
}

export function AddContentDrawer({ initialProvider, onClose, onCreated }: AddContentDrawerProps) {
  const { t } = useTranslation();
  const { config, createService, pendingWrites, isProviderAuthorized } = usePersonalContextStore();
  const isConfigured = config.collection_enabled === true;

  // 通用字段
  // 预选 provider；若未授权则回退到首个已授权的，避免 select 落到 disabled option。
  const [name, setName] = useState('');
  const [provider, setProvider] = useState<FetchProvider>(() => {
    const init = initialProvider ?? 'local_files';
    return isProviderAuthorized(init) ? init : PROVIDER_ORDER.find((p) => isProviderAuthorized(p)) ?? 'local_files';
  });
  const [freqUnit, setFreqUnit] = useState<'hour' | 'day'>('day');
  const [freqValue, setFreqValue] = useState(3);
  const [timeRange, setTimeRange] = useState<'week' | 'month' | 'quarter' | 'custom'>('week');
  const [customStart, setCustomStart] = useState('');
  const [customEnd, setCustomEnd] = useState('');
  // 默认 20 条；填值须 [1,10000]
  const [maxItems, setMaxItems] = useState<number | null>(20);
const [advancedOpen, setAdvancedOpen] = useState(false);
  const [timeDropdownOpen, setTimeDropdownOpen] = useState(false);
  const [calendarOpen, setCalendarOpen] = useState(false);

  // 分支字段
  const [rootDir, setRootDir] = useState('');
  const [columnUrl, setColumnUrl] = useState('');
  const [profileUrl, setProfileUrl] = useState('');
  const [edgeProfile, setEdgeProfile] = useState('');
  const [edgeBookmarksPath, setEdgeBookmarksPath] = useState('');
  const [edgeFolderList, setEdgeFolderList] = useState<string[]>([]);
  const [githubRepoUrl, setGithubRepoUrl] = useState('');
  const [feishuDocIdList, setFeishuDocIdList] = useState<string[]>([]);
  const [githubResources, setGithubResources] = useState<GithubResource[]>(['readme']);
  // 飞书：先建 service 再去设置页授权（后端授权需 service 已存在以派生 scope）。
  const [feishuMode, setFeishuMode] = useState<FeishuMode>('account');
  const [feishuResources, setFeishuResources] = useState<FeishuResource[]>(['docs']);
  const [feishuWikiSpaceId, setFeishuWikiSpaceId] = useState('');
  const [feishuCalendarStart, setFeishuCalendarStart] = useState('');
  const [feishuCalendarEnd, setFeishuCalendarEnd] = useState('');
  const [feishuCalendarOpen, setFeishuCalendarOpen] = useState(false);
  const [feishuWikiDir, setFeishuWikiDir] = useState('');

  const [error, setError] = useState<string | null>(null);
  const submitting = !!pendingWrites.create_service;

  // 飞书允许未授权时新建（先配 service 再授权）；其余 provider 仍需先授权。
  const requiresAuth = provider !== 'feishu';
  const authorized = isProviderAuthorized(provider);

  const canSubmit = useMemo(() => {
    if (!isConfigured) return false;
    if (!name.trim() || submitting) return false;
    if (requiresAuth && !authorized) return false;
    if (validateServiceId(name)) return false;
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
    return false;
  }, [isConfigured, name, submitting, requiresAuth, authorized, provider, feishuMode, feishuResources, feishuWikiSpaceId, rootDir, columnUrl, profileUrl, githubRepoUrl, githubResources, timeRange, customStart, customEnd]);

  const handleSubmit = async () => {
    setError(null);
    if (!isConfigured) {
      setError(t('personalContext.addContent.notConfigured'));
      return;
    }
    if (requiresAuth && !authorized) {
      setError(t('personalContext.addContent.providerUnauthorized'));
      return;
    }
    const idErr = validateServiceId(name);
    if (idErr) {
      setError(idErr);
      return;
    }
    if (maxItems !== null && (maxItems < MAX_ITEMS_MIN || maxItems > MAX_ITEMS_MAX)) {
      setError(t('personalContext.addContent.maxItemsRangeError'));
      return;
    }

    let source: Record<string, unknown> = {};
    let credentials: Record<string, string> = {};

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
      // GitHub PAT 从设置页授权的 localStorage 取（后端无 GitHub 授权接口）
      const token = getGithubToken() ?? '';
      if (!token) {
        setError(t('personalContext.addContent.github.tokenMissing'));
        return;
      }
      source = { owner: parsed.owner, repo: parsed.repo, resources: [...githubResources] };
      credentials = { token };
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

    try {
      await createService({
        service_id: name.trim(),
        provider,
        enabled: true,
        interval_seconds: freqValue * FREQUENCY_SECONDS[freqUnit],
        max_items_per_run: maxItems ?? 20,
        time_range: (() => {
          if (timeRange === 'custom' && customStart && customEnd) {
            return { mode: 'fixed', start_at: customStart, end_at: customEnd };
          }
          if (timeRange === 'custom') return { mode: 'all' };
          const recentDays = timeRange === 'week' ? 7 : timeRange === 'month' ? 30 : 90;
          return { mode: 'recent', recent_days: recentDays };
        })(),
        source,
        credentials,
      });
      onCreated();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
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
          <h3 className="pc-drawer__title">{t('personalContext.addContent.title')}</h3>
          <button type="button" className="pc-drawer__close" onClick={onClose} aria-label="close">
            <X size={18} />
          </button>
        </header>

        <div className="pc-drawer__body">
          {/* 采集内容名称 */}
          <div className="pc-drawer__field">
            <label>{t('personalContext.addContent.nameLabel')}</label>
            <input
              className="pc-drawer__input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t('personalContext.addContent.namePlaceholder')}
            />
          </div>

          {/* 内容采集来源 */}
          <div className="pc-drawer__field">
            <label>{t('personalContext.addContent.providerLabel')}</label>
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
                    onClick={() => { if (!disabled) { setProvider(p); setError(null); } }}
                    disabled={disabled}
                  >
                    <span className="pc-drawer__provider-icon">
                      <img src={PROVIDER_ICON[p]} alt="" />
                    </span>
                    <span className="pc-drawer__provider-name">{t(PROVIDER_LABEL_KEYS[p])}</span>
                    {disabled && (
                      <span
                        className="pc-drawer__provider-authorize"
                        onClick={(e) => { e.stopPropagation(); window.dispatchEvent(new CustomEvent('jiuwen:nav', { detail: 'personalContextSettings' })); onClose(); }}
                      >
                        {t('personalContext.authorization.authorize')}
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
            {provider === 'feishu' && !authorized && (
              <div className="pc-drawer__field-hint">
                {t('personalContext.addContent.feishu.authorizeAfterCreate')}
                <button
                  type="button"
                  className="pc-drawer__link"
                  onClick={() => { window.dispatchEvent(new CustomEvent('jiuwen:nav', { detail: 'personalContextSettings' })); onClose(); }}
                >
                  {t('personalContext.addContent.goAuthorize')}
                </button>
              </div>
            )}
          </div>

          {/* provider 分支表单 */}          {/* provider 分支表单 */}
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
            feishuMode={feishuMode} setFeishuMode={setFeishuMode}
            feishuResources={feishuResources} setFeishuResources={setFeishuResources}
            feishuWikiSpaceId={feishuWikiSpaceId} setFeishuWikiSpaceId={setFeishuWikiSpaceId}
            feishuCalendarStart={feishuCalendarStart} setFeishuCalendarStart={setFeishuCalendarStart}
            feishuCalendarEnd={feishuCalendarEnd} setFeishuCalendarEnd={setFeishuCalendarEnd}
            feishuCalendarOpen={feishuCalendarOpen} setFeishuCalendarOpen={setFeishuCalendarOpen}
            feishuDocIdList={feishuDocIdList} setFeishuDocIdList={setFeishuDocIdList}
            feishuWikiDir={feishuWikiDir} setFeishuWikiDir={setFeishuWikiDir}
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
                      onChange={(e) => setFreqValue(Math.max(1, Number(e.target.value) || 1))}
                    />
                    <button
                      type="button"
                      className="pc-drawer__spinner-btn"
                      onClick={() => setFreqValue(freqValue + 1)}
                    >
                      {'+'}
                    </button>
                  </div>
                  <button
                    type="button"
                    className={'pc-drawer__freq-pill' + (freqUnit === 'hour' ? ' pc-drawer__freq-pill--active' : '')}
                    onClick={() => setFreqUnit('hour')}
                  >
                    {t('personalContext.addContent.unitHour')}
                  </button>
                  <button
                    type="button"
                    className={'pc-drawer__freq-pill' + (freqUnit === 'day' ? ' pc-drawer__freq-pill--active' : '')}
                    onClick={() => setFreqUnit('day')}
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
          <button type="button" className="pc-drawer__foot-btn pc-drawer__foot-btn--secondary" onClick={onClose} disabled={submitting}>
            {t('personalContext.services.cancel')}
          </button>
          <button
            type="button"
            className="pc-drawer__foot-btn pc-drawer__foot-btn--primary"
            onClick={handleSubmit}
            disabled={!canSubmit}
          >
            {submitting ? <Loader2 className="spin" size={14} /> : t('personalContext.addContent.submit')}
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
  feishuMode: FeishuMode; setFeishuMode: (v: FeishuMode) => void;
  feishuResources: FeishuResource[]; setFeishuResources: (v: FeishuResource[]) => void;
  feishuWikiSpaceId: string; setFeishuWikiSpaceId: (v: string) => void;
  feishuCalendarStart: string; setFeishuCalendarStart: (v: string) => void;
  feishuCalendarEnd: string; setFeishuCalendarEnd: (v: string) => void;
  feishuCalendarOpen: boolean; setFeishuCalendarOpen: (v: boolean) => void;
  feishuDocIdList: string[]; setFeishuDocIdList: (v: string[]) => void;
  feishuWikiDir: string; setFeishuWikiDir: (v: string) => void;
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

            {/* Wiki 目录 */}
            <div className="pc-drawer__field">
              <label>{t('personalContext.addContent.feishu.wikiDirLabel')}</label>
              <input
                className="pc-drawer__input"
                value={props.feishuWikiDir}
                onChange={(e) => props.setFeishuWikiDir(e.target.value)}
                placeholder={t('personalContext.addContent.feishu.wikiDirPlaceholder')}
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

  return null;
}
