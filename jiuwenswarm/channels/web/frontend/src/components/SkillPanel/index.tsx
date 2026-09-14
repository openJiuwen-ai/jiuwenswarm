/**
 * SkillPanel 组件
 *
 * Skills 管理面板（编排层）：
 * - 类型见 ./types.ts；纯函数工具见 ./skillPanelUtils.ts；小组件见 ./SkillPanelWidgets.tsx
 * - 数据域 hooks：useHubMarketplace / useSkillFilesTab / useSymphonyGraph / useIndexRecommendation /
 *   useEvolution / usePublishSkill / useOAuthLogin / useSkillToasts
 * - 视图：SkillGraphTab / MarketplaceView / SkillDetailView；弹窗：UploadSkillModal / DocToSkillModal /
 *   PublishDrawer / OAuthLoginModal
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import MoreIcon from '../../assets/work-mode/more-rimless.svg?react';
import NewConversationIcon from '../../assets/new_conversation.svg?react';
import { PageCard, PageHeader, PageToolbar, PageToolbarSearch, Tabs } from '../ui';
import { webRequest } from '../../services/webClient';
import { SourceManagerModal } from '../../features/SourceManagerModal';
import { SkillNetSearchModal } from '../../features/SkillNetSearchModal';
import { ClawHubSearchModal } from '../../features/ClawHubSearchModal';
import { TeamSkillsHubModal } from '../../features/TeamSkillsHubModal';
import { normalizeSkillNetUrl } from '../../utils/skillNetUrl';
import { getSkillAvatar } from '../../utils/skillAvatar';
import { computeMySkills, filterEnabledMySkills } from '../../utils/mySkills';
import { getStoredOAuthToken } from '../../utils/gitcodeOAuth';
import { Switch } from '../Switch';
import {
  MARKETPLACE_CATEGORIES,
  SKILLS_FETCH_TIMEOUT_REFRESH_MS,
  SKILLS_FETCH_TIMEOUT_NORMAL_MS,
  coerceStringList,
  normalizeSkillItem,
  transformSkillContentImages,
} from './skillPanelUtils';
import { FilterDropdown, MySkillGoTryButton, TopAnchorTooltip } from './SkillPanelWidgets';
import { SkillToasts } from './SkillToasts';
import { SkillGraphTab } from './SkillGraphTab';
import { MarketplaceView } from './MarketplaceView';
import { SkillDetailView } from './SkillDetailView';
import { UploadSkillModal } from './UploadSkillModal';
import { DocToSkillModal } from './DocToSkillModal';
import { PublishDrawer } from './PublishDrawer';
import { OAuthLoginModal } from './OAuthLoginModal';
import { useSkillToasts } from './useSkillToasts';
import { useHubMarketplace, type MarketplaceSubView } from './useHubMarketplace';
import { useSymphonyGraph } from './useSymphonyGraph';
import { useIndexRecommendation } from './useIndexRecommendation';
import { useEvolution } from './useEvolution';
import { useSkillFilesTab } from './useSkillFilesTab';
import { useOAuthLogin } from './useOAuthLogin';
import { usePublishSkill } from './usePublishSkill';
import type {
  InstalledPluginItem,
  LoadState,
  MarketplacePluginItem,
  SkillDetail,
  SkillItem,
  SkillPanelProps,
  SkillVersion,
  SkillVersionsListResponse,
  SkillRebuildResponse,
} from './types';

function errorToMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

const MY_SKILLS_EMPTY_KEY: Record<'all' | 'enabled' | 'disabled' | 'builtin', string> = {
  all: 'skills.noMatches',
  enabled: 'skills.noEnabledSkills',
  disabled: 'skills.noDisabledSkills',
  builtin: 'skills.noBuiltinSkills',
};

function MySkillsGroupHeader({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-between mb-3">
      <span className="font-bold text-text-strong text-[16px]">{label}</span>
    </div>
  );
}

function CreateSkillMenu({
  open,
  onToggle,
  uploadDisabled,
  onUploadLocal,
  onDocToSkill,
  onCreateViaChat,
}: {
  open: boolean;
  onToggle: (open: boolean) => void;
  uploadDisabled: boolean;
  onUploadLocal: () => void;
  onDocToSkill: () => void;
  onCreateViaChat: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="relative">
      <button
        onClick={() => onToggle(!open)}
        className="flex items-center justify-center gap-1 h-8 w-[96px] rounded-[16px] text-sm text-text-inverse bg-control-emphasis hover:opacity-80"
        data-testid="skill-panel-create-btn"
      >
        {t('skills.actions.create')}
        <svg
          className={`w-3.5 h-3.5 transition-transform ${open ? 'rotate-180' : ''}`}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
          strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => onToggle(false)} />
          <div className="dropdown-menu">
            <button
              onClick={onUploadLocal}
              disabled={uploadDisabled}
              className="flex items-center w-full px-3 py-2 text-sm text-left text-text hover:bg-secondary disabled:opacity-60 disabled:cursor-not-allowed"
              data-testid="skill-panel-create-menu-item"
              data-variant="upload-local"
            >
              {t('skills.actions.uploadLocalSkill')}
            </button>
            <button
              onClick={onDocToSkill}
              className="flex items-center w-full px-3 py-2 text-sm text-left text-text hover:bg-secondary"
              data-testid="skill-panel-create-menu-item"
              data-variant="doc-to-skill"
            >
              {t('skills.actions.documentToSkill')}
            </button>
            <button
              onClick={onCreateViaChat}
              className="flex items-center w-full px-3 py-2 text-sm text-left text-text hover:bg-secondary"
              data-testid="skill-panel-create-menu-item"
              data-variant="via-chat"
            >
              {t('skills.actions.createViaChat')}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

export function SkillPanel({
  sessionId,
  isConnected,
  symphonyEnabled,
  onSymphonyEnabledChange,
  onNavigateToSettings,
  isActive = false,
}: SkillPanelProps) {
  const { t } = useTranslation();
  // 导航与页签
  const [activeTab, setActiveTab] = useState<'my' | 'marketplace' | 'graph'>('marketplace');
  const [marketplaceSubView, setMarketplaceSubView] = useState<MarketplaceSubView>('list');
  const [mySkillsSubTab, setMySkillsSubTab] = useState<'all' | 'enabled' | 'disabled' | 'builtin'>('all');
  const [mySkillsPublishFilter, setMySkillsPublishFilter] = useState<'all' | 'published' | 'unpublished'>('all');
  const [marketplaceCategory, setMarketplaceCategory] = useState<(typeof MARKETPLACE_CATEGORIES)[number]>('all');
  const [detailTab, setDetailTab] = useState<'content' | 'files' | 'experience'>('content');

  // 列表与详情数据（含搜索筛选）
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [plugins, setPlugins] = useState<InstalledPluginItem[]>([]);
  const [search, setSearch] = useState('');
  const [selectedSkill, setSelectedSkill] = useState<SkillDetail | null>(null);
  const [listState, setListState] = useState<LoadState>('idle');
  const [detailState, setDetailState] = useState<LoadState>('idle');
  const [skillVersions, setSkillVersions] = useState<SkillVersion[]>([]);
  const [skillVersionsDefault, setSkillVersionsDefault] = useState<string | null>(null);
  const [versionsLoadState, setVersionsLoadState] = useState<LoadState>('idle');

  // 进行中的动作（按钮禁用/loading 依据）
  const [actionTarget, setActionTarget] = useState<string | null>(null);
  const [rebuildLoading, setRebuildLoading] = useState(false);
  const [knowledgeTaskCount, setKnowledgeTaskCount] = useState(0);

  // 弹窗与浮层开关
  const [sourceModalOpen, setSourceModalOpen] = useState(false);
  const [skillNetModalOpen, setSkillNetModalOpen] = useState(false);
  const [clawHubModalOpen, setClawHubModalOpen] = useState(false);
  const [teamSkillsHubModalOpen, setTeamSkillsHubModalOpen] = useState(false);
  const [uploadSkillModalOpen, setUploadSkillModalOpen] = useState(false);
  const [docToSkillModalOpen, setDocToSkillModalOpen] = useState(false);
  const [publishFilterOpen, setPublishFilterOpen] = useState(false);
  const [enableFilterOpen, setEnableFilterOpen] = useState(false);
  const [createMenuOpen, setCreateMenuOpen] = useState(false);
  const [detailMenuOpen, setDetailMenuOpen] = useState(false);
  const [synthesizeTooltip, setSynthesizeTooltip] = useState<{ left: number; top: number } | null>(null);

  // 挂载/激活时序标记
  const prevIsActiveRef = useRef(isActive);
  const mountedRef = useRef(false);

  const { message, messageType, setMessage, setMessageType, showMessage, cleanMessage } = useSkillToasts();
  const searchKeyword = search.trim();
  const withSession = useCallback(
    <T extends Record<string, unknown> = Record<string, unknown>>(params?: T): T & { session_id: string } => ({
      ...(params || ({} as T)),
      session_id: sessionId,
    }),
    [sessionId],
  );

  const showErrorToast = useCallback(
    (error: unknown, fallbackKey: string) => {
      showMessage('error', errorToMessage(error) || t(fallbackKey));
    },
    [showMessage, t],
  );

  const fetchSkills = useCallback(async (refreshMarketplaces = false) => {
    setListState('loading');
    try {
      const data = await webRequest<{
        skills?: SkillItem[];
        plugins?: InstalledPluginItem[];
      }>(
        'skills.list',
        {
          with_installed: true,
          ...(refreshMarketplaces ? { refresh_marketplaces: true } : {}),
        },
        {
          timeoutMs: refreshMarketplaces ? SKILLS_FETCH_TIMEOUT_REFRESH_MS : SKILLS_FETCH_TIMEOUT_NORMAL_MS,
        },
      );
      setSkills((data.skills || []).map(normalizeSkillItem));
      setPlugins(data.plugins || []);
      setListState('success');
    } catch (error) {
      console.error(error);
      setListState('error');
    }
  }, []);

  const handleSkillsInstalled = useCallback(async () => {
    await fetchSkills();
  }, [fetchSkills]);

  const {
    hubSkills,
    hubLoading,
    teamSkills,
    featuredSkills,
    selectedHubSkill,
    hubDetail,
    hubDetailState,
    setSelectedHubSkill,
    setHubDetail,
    setHubDetailState,
    fetchHubSkillDetail,
    invalidateHubFetch,
    pauseHubFetching,
  } = useHubMarketplace({ activeTab, searchKeyword, marketplaceCategory, setMarketplaceSubView, withSession });

  const {
    skillGraphPanelRef,
    graphReading,
    symphonyEnabledDraft,
    symphonySaving,
    symphonySaveError,
    graphActionError,
    clearGraphActionError,
    updateSymphonyEnabled,
    updateGraphReading,
  } = useSymphonyGraph({ isConnected, symphonyEnabled, onSymphonyEnabledChange });

  const {
    indexRecommendationVisible,
    indexRecommendationBuilding,
    setIndexRecommendationVisible,
    startRetrievalIndexBuild,
    buildRecommendedIndex,
  } = useIndexRecommendation({ isActive, activeTab, isConnected, withSession, showMessage });

  const {
    filesLoadState,
    filePreview,
    filePreviewPath,
    filePreviewStatus,
    previewTreeNodes,
    fetchSkillFiles,
    fetchFilePreview,
    resetFilesTab,
  } = useSkillFilesTab({ withSession });

  const {
    sortedEvolutionEntries,
    evolutionListState,
    evolutionMessage,
    evolutionMessageType,
    evolutionFormatError,
    handleEvolutionContentChange,
    handleEvolutionDeleteEntry,
  } = useEvolution({ selectedSkill, detailTab, withSession, fetchSkills });

  const { oauthLoginOpen, setOauthLoginOpen, oauthError, setOauthError, oauthLoadingProvider, handleOAuthLogin } =
    useOAuthLogin({ selectedSkill });

  const publish = usePublishSkill({
    selectedSkill,
    withSession,
    fetchSkills,
    showMessage,
    setActionTarget,
    setOauthLoginOpen,
  });
  const { setPublishDrawerOpen } = publish;

  const installedSkillMap = useMemo(() => {
    const map = new Map<string, InstalledPluginItem>();
    plugins.forEach((plugin) => {
      plugin.skills.forEach((skill) => {
        const skillName = typeof skill === 'string' ? skill : skill.name;
        if (!map.has(skillName)) {
          map.set(skillName, plugin);
        }
      });
    });
    return map;
  }, [plugins]);

  const installedSkillNames = useMemo(() => new Set(installedSkillMap.keys()), [installedSkillMap]);

  /** 已安装技能的来源 URL（规范化），与 SkillNet 搜索结果的 skill_url 匹配 */
  const installedSkillOrigins = useMemo(() => {
    const set = new Set<string>();
    for (const s of skills) {
      const o = s.origin?.trim();
      if (o) {
        set.add(normalizeSkillNetUrl(o));
      }
    }
    return set;
  }, [skills]);

  const filteredSkills = useMemo(() => {
    let result = skills;
    if (activeTab === 'my') {
      // 2026-08-21：抽成 utils/mySkills.ts 的 computeMySkills，跟"手动创建插件"的"添加技能"
      // 弹窗共用同一份"我的技能"判定规则，见该文件头注释。这里原本是"候选集过滤+排除内置未装"
      // 两步（第二步挪到了下面 visibleSkills 里），computeMySkills 已经把两步合并，语义不变
      // （排除条件不依赖搜索关键字，跟下面的关键字过滤谁先谁后结果一样）。
      result = computeMySkills(result, installedSkillNames);
    }
    const keyword = searchKeyword.toLowerCase();
    if (!keyword) return result;
    return result.filter((skill) => {
      const haystack = [
        skill.name,
        skill.display_name,
        skill.description,
        skill.author,
        coerceStringList(skill.tags).join(' '),
      ]
        .join(' ')
        .toLowerCase();
      return haystack.includes(keyword);
    });
  }, [skills, searchKeyword, activeTab, installedSkillNames]);

  const visibleSkills = useMemo(() => {
    return [...filteredSkills].sort((a, b) => {
      const aSkillNet = a.source === 'skillnet' ? 1 : 0;
      const bSkillNet = b.source === 'skillnet' ? 1 : 0;
      if (aSkillNet !== bSkillNet) {
        return bSkillNet - aSkillNet;
      }
      return a.name.localeCompare(b.name);
    });
  }, [filteredSkills]);

  const fetchSkillDetail = useCallback(
    async (skillName: string, version?: string) => {
      setDetailState('loading');
      try {
        const data = await webRequest<SkillDetail>(
          'skills.get',
          withSession({ name: skillName, ...(version ? { version } : {}) }),
        );
        setSelectedSkill(normalizeSkillItem(data));
        setDetailTab('content');
        setDetailState('success');
        resetFilesTab();
      } catch (error) {
        console.error(error);
        setDetailState('error');
      }
    },
    [withSession, resetFilesTab],
  );

  const handleInstallHubSkill = useCallback(
    async (skill: MarketplacePluginItem) => {
      const installKey = skill.identifier || skill.asset_id;
      setActionTarget(`install:${installKey}`);
      try {
        type InstallPayload = {
          success: boolean;
          pending?: boolean;
          skill?: { name: string };
          detail?: string;
          detail_key?: string;
          message?: string;
        };

        const buildParams = (force: boolean) =>
          withSession({
            source: skill.source || 'teamskillshub',
            identifier: skill.identifier || skill.asset_id,
            force,
            ...(skill.owner_handle ? { owner_handle: skill.owner_handle } : {}),
            ...(skill.display_name || skill.name ? { display_name: skill.display_name || skill.name } : {}),
          });

        const alreadyInstalledKeys = new Set([
          'skills.clawhub.errors.skillAlreadyInstalled',
          'skills.skillNet.errors.skillAlreadyInstalled',
        ]);

        let force = false;
        let data: InstallPayload;
        while (true) {
          data = await webRequest<InstallPayload>('skills.online_search.install', buildParams(force), {
            timeoutMs: 60000,
          });
          if (!data.success && !force && data.detail_key && alreadyInstalledKeys.has(data.detail_key)) {
            const confirmText =
              data.detail_key === 'skills.clawhub.errors.skillAlreadyInstalled'
                ? t('skills.clawhub.replaceConfirm', {
                    name: skill.display_name || skill.name,
                  })
                : `${data.detail || t('skills.errors.installFailed')}\n${t('skills.overwriteConfirm')}`;
            const overwrite = window.confirm(confirmText);
            if (!overwrite) return;
            force = true;
            continue;
          }
          break;
        }

        if (!data.success) {
          throw new Error(
            (data.detail_key ? t(data.detail_key) : null) ||
              data.detail ||
              data.message ||
              t('skills.errors.installFailed'),
          );
        }
        if (data.pending) {
          throw new Error(t('skills.errors.installFailedHint'));
        }
        showMessage('success', t('skills.messages.installed', { name: data.skill?.name || skill.name }));
        await fetchSkills();
      } catch (error) {
        console.error(error);
        showErrorToast(error, 'skills.errors.installFailedHint');
      } finally {
        setActionTarget(null);
      }
    },
    [withSession, fetchSkills, t, showMessage, showErrorToast],
  );

  const versionsRequestSeqRef = useRef(0);

  const fetchSkillVersions = useCallback(
    async (skillName: string) => {
      const seq = ++versionsRequestSeqRef.current;
      setVersionsLoadState('loading');
      try {
        const data = await webRequest<SkillVersionsListResponse>(
          'skills.versions.list',
          withSession({ name: skillName }),
        );
        if (seq !== versionsRequestSeqRef.current) return;
        setSkillVersions(data.versions || []);
        setSkillVersionsDefault(data.default_version);
        setVersionsLoadState('success');
      } catch (error) {
        console.error(error);
        if (seq !== versionsRequestSeqRef.current) return;
        setVersionsLoadState('error');
      }
    },
    [withSession],
  );

  // 进入“我的技能”详情时自动加载版本列表（原刷新按钮已移除），切换技能时先清空旧数据再重新拉取
  useEffect(() => {
    if (!selectedSkill) return;
    setSkillVersions([]);
    setSkillVersionsDefault(null);
    fetchSkillVersions(selectedSkill.name);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedSkill?.name]);

  const handleRebuild = useCallback(
    async (skillName: string, version: string | null) => {
      setRebuildLoading(true);
      showMessage('loading', t('skills.messages.rebuilding'));
      try {
        const data = await webRequest<SkillRebuildResponse>(
          'skills.rebuild',
          withSession({ name: skillName, version }),
          // 重建会同步跑静默 Agent，给足前端等待窗口（与 Gateway unary 默认 600s / 知识转技能同量级）。
          { timeoutMs: 600_000 },
        );
        if (data.success) {
          showMessage('success', t('skills.messages.rebuildCompleted'));
          fetchSkillDetail(skillName);
          fetchSkillVersions(skillName);
        }
      } catch (error) {
        console.error(error);
        const detail = error instanceof Error ? error.message : String(error);
        showMessage('error', t('skills.messages.rebuildFailed', { detail }));
      } finally {
        setRebuildLoading(false);
      }
    },
    [withSession, fetchSkillDetail, fetchSkillVersions, showMessage, t],
  );

  // 当左边栏切换到技能页面时，或切换到"我的技能"页签时，调用 list 接口
  useEffect(() => {
    const prevIsActive = prevIsActiveRef.current;
    const isInitialMount = !mountedRef.current;
    mountedRef.current = true;

    // 场景1：从其他页面切换到技能页面（isActive 变为 true），或首次挂载且已激活
    if (isActive && (!prevIsActive || isInitialMount)) {
      fetchSkills();
    }

    // 场景2：在技能页面内切换到"我的技能"页签（isActive 保持 true，activeTab 变化）
    if (isActive && prevIsActive && activeTab === 'my') {
      fetchSkills();
    }

    // 更新 ref
    prevIsActiveRef.current = isActive;
  }, [isActive, activeTab, fetchSkills]);

  // OAuth 回调后恢复技能详情页 + 打开发布抽屉
  // 在 isActive 变为 true 后执行（确保 WebSocket 已连接、SkillPanel 已挂载）
  useEffect(() => {
    if (!isActive) return;
    const oauthRedirect = sessionStorage.getItem('oauth_redirect');
    const skillName = sessionStorage.getItem('oauth_redirect_skill');
    if (oauthRedirect === 'publish' && skillName) {
      sessionStorage.removeItem('oauth_redirect');
      sessionStorage.removeItem('oauth_redirect_skill');
      const oauthError = sessionStorage.getItem('oauth_error');
      if (oauthError) {
        sessionStorage.removeItem('oauth_error');
        sessionStorage.removeItem('oauth_redirect_nav');
        setOauthError(oauthError);
        setOauthLoginOpen(true);
        return;
      }
      setActiveTab('my');
      fetchSkillDetail(skillName).then(() => {
        setPublishDrawerOpen(true);
      });
    }
  }, [isActive, fetchSkillDetail, setOauthError, setOauthLoginOpen, setPublishDrawerOpen]);

  const handleOpenSkill = useCallback(
    (skillName: string) => {
      fetchSkillDetail(skillName);
    },
    [fetchSkillDetail],
  );

  const handleBackToList = useCallback(() => {
    setSelectedSkill(null);
    setDetailState('idle');
  }, []);

  // 新建会话并将技能选中到输入框
  const handleGoToChat = useCallback((skillName: string, skillType?: string) => {
    window.dispatchEvent(
      new CustomEvent('jiuwen:new-conversation', {
        detail: { skillName, ...(skillType === 'swarm_skill' ? { mode: 'team' as const } : {}) },
      }),
    );
  }, []);

  const renderHubSkillAction = useCallback(
    (skill: MarketplacePluginItem) => {
      if (installedSkillMap.has(skill.name)) {
        return {
          icon: <NewConversationIcon aria-hidden />,
          onClick: () => handleGoToChat(skill.name, skill.plugin_type === 'swarmskill' ? 'swarm_skill' : undefined),
          tooltip: t('skills.actions.goTry'),
        };
      }
      return {
        icon: (
          <svg fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 5v14M5 12h14" />
          </svg>
        ),
        onClick: () => handleInstallHubSkill(skill),
        disabled: actionTarget === `install:${skill.identifier || skill.asset_id}`,
        tooltip: t('skills.actions.install'),
      };
    },
    [installedSkillMap, handleGoToChat, handleInstallHubSkill, actionTarget, t],
  );

  // 新建会话：skill-creator（所有 Skill Creator 统一入口）chip + "帮我修改这个技能" + 该技能 chip
  const handleEditSkill = useCallback(
    (skillName: string, skillType?: string) => {
      window.dispatchEvent(
        new CustomEvent('jiuwen:new-conversation', {
          detail: {
            skillName: 'skill-creator',
            suffixText: t('skills.chatPrompts.editSkill'),
            secondSkillName: skillName,
            ...(skillType === 'swarm_skill' ? { mode: 'team' as const } : {}),
            metadata: {
              scene: 'edit_skill',
              target_skill: skillName,
              ...(skillType ? { target_skill_type: skillType } : {}),
            },
          },
        }),
      );
    },
    [t],
  );

  // 通过聊天创建：新建会话，选中 skill-creator（统一入口）并在 chip 后追加创建提示文字
  const handleCreateViaChat = useCallback(() => {
    window.dispatchEvent(
      new CustomEvent('jiuwen:new-conversation', {
        detail: {
          skillName: 'skill-creator',
          suffixText: t('skills.chatPrompts.createSkill'),
          metadata: { scene: 'create_skill' },
        },
      }),
    );
  }, [t]);

  const createUploadError = useCallback(
    (code: string | undefined, status: number) => {
      let message = t('skills.errors.uploadFailed');
      // 服务端写入失败也使用 SKILL_INVALID_PACKAGE，须先区分 HTTP 状态。
      if (status === 400) {
        if (code === 'SKILL_UNSAFE_PATH') {
          message = t('skills.errors.uploadUnsafePath');
        } else if (code === 'SKILL_INVALID_PACKAGE') {
          message = t('skills.errors.uploadInvalidRequest');
        }
      }
      return Object.assign(new Error(message), { code, status });
    },
    [t],
  );

  const uploadTempFile = useCallback(
    async (file: File): Promise<string> => {
      const form = new FormData();
      form.append('file', file);
      const uploadResp = await fetch('/file-api/skills/upload-temp', { method: 'POST', body: form });
      const uploadData = await uploadResp.json();
      if (!uploadResp.ok || !uploadData.path) {
        throw createUploadError(uploadData.code, uploadResp.status);
      }
      return uploadData.path as string;
    },
    [createUploadError],
  );

  /** 上传技能 .zip 包：先上传到临时目录，再通过 WebSocket 调用 skills.import_upload */
  const handleSkillUpload = useCallback(
    async (file: File) => {
      setActionTarget('import_local');
      setMessage(null);
      setMessageType(null);
      try {
        // Step 1: 上传文件到服务端临时目录
        const tempPath = await uploadTempFile(file);

        // Step 2: 通过 WebSocket 调用 skills.import_upload
        const doImport = async (overwrite: boolean) => {
          const data = await webRequest<{
            success: boolean;
            detail?: string;
            message?: string;
            skill?: { name?: string };
            code?: string;
          }>(
            'skills.import_upload',
            withSession({
              path: tempPath,
              overwrite,
            }),
          );
          if (!data.success) {
            const err = new Error(data.detail || data.message || t('skills.errors.importFailed')) as Error & {
              code?: string;
            };
            err.code = data.code;
            throw err;
          }
          return data;
        };

        let data;
        try {
          data = await doImport(false);
        } catch (error) {
          const code = (error as Error & { code?: string }).code;
          if (code === 'SKILL_ALREADY_EXISTS' || code === 'SKILL_IMPORT_OVERWRITE_REQUIRED') {
            const msg = error instanceof Error ? error.message : String(error);
            const overwrite = window.confirm(`${msg}\n${t('skills.overwriteConfirm')}`);
            if (!overwrite) return;
            data = await doImport(true);
          } else {
            throw error;
          }
        }

        showMessage('success', t('skills.messages.imported', { name: data.skill?.name || file.name }));
        await fetchSkills();
        if (data.skill?.name) {
          await fetchSkillDetail(data.skill.name);
        }
      } catch (error) {
        console.error(error);
        showErrorToast(error, 'skills.errors.importFailedHint');
      } finally {
        setActionTarget(null);
      }
    },
    [uploadTempFile, fetchSkills, fetchSkillDetail, t, withSession, showMessage, setMessage, setMessageType],
  );

  /** 知识转技能：先上传文件到临时目录（如有），再通过 WebSocket 调用 skills.create_from_knowledge */
  const handleCreateFromKnowledge = useCallback(
    async (params: { file?: File | null; link?: string; skillDescription?: string }) => {
      setActionTarget('import_local');
      setKnowledgeTaskCount((prev) => prev + 1);
      // 进度由常驻 knowledge banner 展示，避免被广场安装等其它 toast 覆盖。
      setMessage(null);
      setMessageType(null);
      try {
        let filePath = '';
        let link = '';

        if (params.file) {
          filePath = await uploadTempFile(params.file);
        } else if (params.link) {
          link = params.link;
        } else {
          showMessage('error', t('skills.errors.importFailed'));
          return;
        }

        type KnowledgeResult = {
          success: boolean;
          detail?: string;
          message?: string;
          code?: string;
          skill_name?: string;
          skill?: { name?: string };
        };

        const doCreate = async () => {
          const data = await webRequest<KnowledgeResult>(
            'skills.create_from_knowledge',
            withSession({
              ...(filePath ? { file_path: filePath } : { link }),
              skill_description: params.skillDescription || '',
            }),
            // 知识转技能可能较久，给足前端等待窗口（与 Gateway 默认 unary 600s 同量级）。
            { timeoutMs: 600000 },
          );
          return data;
        };

        let data = await doCreate();
        if (!data.success) {
          const skillName = data.skill_name || data.skill?.name || '';
          if (data.code === 'SKILL_ALREADY_EXISTS' || data.code === 'SKILL_IMPORT_OVERWRITE_REQUIRED') {
            showMessage(
              'error',
              skillName
                ? t('skills.errors.knowledgeSkillExists', { name: skillName })
                : data.detail || data.message || t('skills.errors.knowledgeSkillExistsGeneric'),
            );
            return;
          }
          throw new Error(data.detail || data.message || t('skills.errors.importFailed'));
        }

        showMessage('success', t('skills.messages.knowledgeSkillCreated'));
        await fetchSkills();
      } catch (error) {
        console.error(error);
        const code = (error as { code?: string } | null)?.code;
        const payload = (error as { payload?: { skill_name?: string; code?: string } } | null)?.payload;
        const skillName = payload?.skill_name || '';
        if (
          code === 'SKILL_ALREADY_EXISTS' ||
          code === 'SKILL_IMPORT_OVERWRITE_REQUIRED' ||
          payload?.code === 'SKILL_ALREADY_EXISTS'
        ) {
          showMessage(
            'error',
            skillName
              ? t('skills.errors.knowledgeSkillExists', { name: skillName })
              : errorToMessage(error) || t('skills.errors.knowledgeSkillExistsGeneric'),
          );
          return;
        }
        showErrorToast(error, 'skills.errors.importFailedHint');
      } finally {
        let remaining = 0;
        setKnowledgeTaskCount((prev) => {
          remaining = Math.max(0, prev - 1);
          return remaining;
        });
        if (remaining <= 0) {
          setActionTarget(null);
        }
      }
    },
    [uploadTempFile, fetchSkills, t, withSession, showMessage, setMessage, setMessageType],
  );

  const handleMarketplaceCategoryChange = useCallback(
    (nextCategory: (typeof MARKETPLACE_CATEGORIES)[number]) => {
      if (nextCategory === marketplaceCategory) return;

      invalidateHubFetch();
      setSearch('');
      setMarketplaceCategory(nextCategory);
    },
    [marketplaceCategory, invalidateHubFetch],
  );

  const handleSearchChange = useCallback(
    (nextSearch: string) => {
      const nextKeyword = nextSearch.trim();

      if (activeTab === 'marketplace' && nextKeyword !== search.trim()) {
        invalidateHubFetch();
      }

      setSearch(nextSearch);
    },
    [activeTab, search, invalidateHubFetch],
  );

  const handleUninstall = useCallback(
    async (pluginName: string) => {
      if (!pluginName) return;
      const confirmed = window.confirm(t('skills.uninstallConfirm', { pluginName }));
      if (!confirmed) return;

      setActionTarget(pluginName);
      setMessage(null);
      setMessageType(null);
      try {
        const data = await webRequest<{
          success: boolean;
          detail?: string;
          message?: string;
        }>(
          'skills.uninstall',
          withSession({
            name: pluginName,
          }),
        );
        if (!data.success) {
          throw new Error(data.detail || data.message || t('skills.errors.uninstallFailed'));
        }
        showMessage('success', t('skills.messages.uninstalled', { pluginName }));
        await fetchSkills();
        handleBackToList();
      } catch (error) {
        console.error(error);
        showErrorToast(error, 'skills.errors.uninstallFailedHint');
      } finally {
        setActionTarget(null);
      }
    },
    [fetchSkills, handleBackToList, t, withSession, showMessage, setMessage, setMessageType, showErrorToast],
  );

  // 2026-08-25：改用 utils/mySkills.ts 的共享 isSkillInstalled/filterEnabledMySkills，跟"手动创建
  // 插件"的"添加技能"弹窗（CreatePluginPage.tsx）共用同一份"已启用"判定规则，见该文件头注释。
  const mySkillsFiltered = useMemo(() => {
    let filtered = visibleSkills;
    switch (mySkillsSubTab) {
      case 'enabled':
        filtered = filterEnabledMySkills(visibleSkills, installedSkillNames);
        break;
      case 'disabled':
        filtered = filtered.filter((s) => s.enabled === false);
        break;
      case 'builtin':
        filtered = filtered.filter((s) => s.source === 'builtin');
        break;
      default:
        break;
    }
    // 发布状态筛选
    if (mySkillsPublishFilter === 'published') {
      filtered = filtered.filter((s) => s.published === true);
    } else if (mySkillsPublishFilter === 'unpublished') {
      filtered = filtered.filter((s) => s.published !== true);
    }
    return filtered;
  }, [visibleSkills, mySkillsSubTab, mySkillsPublishFilter, installedSkillNames]);

  // 内置/非内置分组（用于"我的技能"列表分组展示）
  const builtinSkills = useMemo(() => mySkillsFiltered.filter((s) => s.source === 'builtin'), [mySkillsFiltered]);
  const otherSkills = useMemo(() => mySkillsFiltered.filter((s) => s.source !== 'builtin'), [mySkillsFiltered]);

  /** 判断技能是否为技能包（技能名与所属插件名一致） */
  const isSkillPackage = useCallback(
    (skill: SkillItem): boolean => {
      const plugin = installedSkillMap.get(skill.name);
      return Boolean(plugin && plugin.plugin_name === skill.name && plugin.skills.length > 1);
    },
    [installedSkillMap],
  );

  const toggleSkillDisabled = useCallback(
    async (skillName: string) => {
      const skill = skills.find((s) => s.name === skillName);
      const newEnabled = skill?.enabled === false ? true : false;

      const toggleKey = `toggle:${skillName}`;
      setActionTarget(toggleKey);

      try {
        const result = await webRequest<{
          success: boolean;
          name: string;
          enabled: boolean;
          detail?: string;
        }>('skills.toggle', withSession({ name: skillName, enabled: newEnabled }));

        if (!result.success) {
          throw new Error(result.detail || 'Failed to toggle skill');
        }

        setSkills((prev) => prev.map((s) => (s.name === skillName ? { ...s, enabled: newEnabled } : s)));

        if (selectedSkill && selectedSkill.name === skillName) {
          setSelectedSkill({ ...selectedSkill, enabled: newEnabled });
        }
      } catch (error) {
        console.error('Failed to toggle skill enabled:', error);
        showMessage('error', t('skills.setEnabledError'));
      } finally {
        setActionTarget(null);
      }
    },
    [skills, selectedSkill, withSession, showMessage, t],
  );

  const renderMySkillCard = (skill: SkillItem) => {
    const avatar = getSkillAvatar(skill.name);
    const displayName = skill.display_name || skill.name;
    const isDisabled = skill.enabled === false;
    const isToggling = actionTarget === `toggle:${skill.name}`;
    const isPackage = isSkillPackage(skill);
    const listKey = skill.path || `${skill.source || 'local'}:${skill.name}`;

    const titleEndContent = skill.has_evolutions ? (
      <button
        onClick={(e) => {
          e.stopPropagation();
          handleOpenSkill(skill.name);
          setDetailTab('experience');
        }}
        className="relative shrink-0 w-5 h-5 flex items-center justify-center text-text-muted hover:text-text"
        title={t('skills.actions.viewEvolution')}
        data-testid="skill-panel-my-skill-card-evolution-btn"
      >
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          className="lucide lucide-bell-dot-icon lucide-bell-dot"
        >
          <path d="M10.268 21a2 2 0 0 0 3.464 0" />
          <path d="M11.68 2.009A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673c-.824-.85-1.678-1.731-2.21-3.348" />
          <circle cx="18" cy="5" r="3" />
        </svg>
      </button>
    ) : undefined;

    const labelTags: string[] = [];
    if (skill.skill_type === 'swarm_skill') {
      labelTags.push(t('skills.skillTypes.team'));
    } else if (skill.skill_type === 'multimodal_skill') {
      labelTags.push(t('skills.skillTypes.multimodal'));
    }
    if (skill.source === 'builtin') {
      labelTags.push(t('skills.mySkillsTabs.builtin'));
    }
    if (activeTab === 'my') {
      labelTags.push(t('skills.publishFilter.unpublished'));
    }

    const actionContent = (
      <div className="flex items-center gap-1.5 shrink-0" onClick={(e) => e.stopPropagation()}>
        <div className="page-card-actions-hover flex items-center gap-1.5">
          <div className="relative group">
            <button
              type="button"
              className="w-7 h-7 flex items-center justify-center rounded-md hover:bg-secondary text-text-muted hover:text-text"
              data-testid="skill-panel-my-skill-card-menu"
            >
              <MoreIcon aria-hidden />
            </button>
            <div className="hidden group-hover:block">
              <div className="dropdown-menu">
                {!isPackage ? (
                  <button
                    onClick={
                      isDisabled
                        ? undefined
                        : (e: React.MouseEvent) => {
                            e.stopPropagation();
                            handleEditSkill(skill.name, skill.skill_type);
                          }
                    }
                    disabled={isDisabled}
                    className="flex items-center w-full px-3 py-2 text-sm text-left text-text hover:bg-secondary disabled:opacity-40 disabled:cursor-not-allowed"
                    data-testid="skill-panel-my-skill-card-menu-edit"
                  >
                    {t('skills.actions.edit')}
                  </button>
                ) : null}
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    const plugin = installedSkillMap.get(skill.name);
                    handleUninstall(plugin?.plugin_name || skill.name);
                  }}
                  className="flex items-center w-full px-3 py-2 text-sm text-left text-text hover:bg-secondary"
                  data-testid="skill-panel-my-skill-card-menu-uninstall"
                >
                  {t('skills.actions.uninstall')}
                </button>
              </div>
            </div>
          </div>
          <MySkillGoTryButton
            disabled={isDisabled}
            onGo={() => handleGoToChat(skill.name, skill.skill_type)}
            tooltip={t('skills.actions.goTry')}
          />
        </div>
        <Switch checked={!isDisabled} onChange={() => toggleSkillDisabled(skill.name)} disabled={isToggling} />
      </div>
    );

    return (
      <PageCard
        key={listKey}
        onClick={() => handleOpenSkill(skill.name)}
        testId="skill-panel-my-skill-card"
        variant={listKey}
        avatar={avatar}
        title={displayName}
        titleEnd={titleEndContent}
        label={labelTags}
        actionSlot={actionContent}
        description={skill.description || t('skills.noDescription')}
      />
    );
  };

  const detailContent = useMemo(
    () => (selectedSkill?.content ? transformSkillContentImages(selectedSkill.content, selectedSkill.file_path) : ''),
    [selectedSkill],
  );

  const handleSelectHubSkill = useCallback(
    (skill: MarketplacePluginItem) => {
      setSelectedHubSkill(skill);
      fetchHubSkillDetail(skill);
    },
    [setSelectedHubSkill, fetchHubSkillDetail],
  );

  const handleBackToHubDetail = useCallback(() => {
    setMarketplaceSubView('list');
    setSelectedHubSkill(null);
    setHubDetail(null);
    setHubDetailState('idle');
  }, [setMarketplaceSubView, setSelectedHubSkill, setHubDetail, setHubDetailState]);

  const handleMainTabChange = useCallback(
    (tab: 'my' | 'marketplace' | 'graph') => {
      if (tab === 'marketplace') {
        // 与 setActiveTab 同批清空搜索，避免广场首帧沿用「我的技能」关键词
        if (activeTab === 'my') {
          setSearch('');
        }
        setActiveTab('marketplace');
      } else if (tab === 'my') {
        // 进入“我的技能”时始终清除其他页面遗留的搜索词
        if (activeTab !== 'my') {
          setSearch('');
        }

        // 只有从技能广场离开时才需要终止广场请求
        if (activeTab === 'marketplace') {
          pauseHubFetching();
        }

        setActiveTab('my');
      } else {
        setActiveTab('graph');
      }
    },
    [activeTab, pauseHubFetching],
  );

  const renderFixedHeader = () => (
    // 固定区（header/toolbar）：page-shell 限宽 1400px 居中，与下方滚动列共用内容线
    <div className="page-shell flex-none">
      <PageHeader title={t('skills.title')} subtitle={t('skills.subtitle')}>
        <button
          onClick={() => setSourceModalOpen(true)}
          className="flex items-center gap-1.5 px-1 py-1.5 rounded-lg text-sm text-text-muted hover:text-text hover:bg-secondary/50"
          data-testid="skill-panel-source-manager-btn"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z"
            />
          </svg>
          {t('skills.actions.sourceManager')}
        </button>
        <button
          onClick={() => {
            if (activeTab === 'graph') {
              const started = skillGraphPanelRef.current?.refresh() ?? false;
              if (started) {
                updateGraphReading(true);
              }
            } else if (activeTab === 'my' || activeTab === 'marketplace') {
              setSearch('');
              fetchSkills(true);
            }
          }}
          className={`flex items-center gap-1.5 pl-[18px] pr-[24px] py-1.5 rounded-lg text-sm text-text-muted ${
            activeTab === 'graph' && graphReading
              ? 'cursor-not-allowed opacity-70'
              : 'hover:text-text hover:bg-secondary/50'
          }`}
          disabled={activeTab === 'graph' && graphReading}
          data-testid="skill-panel-refresh-btn"
        >
          <svg
            className={`w-4 h-4 ${activeTab === 'graph' && graphReading ? 'animate-spin' : ''}`}
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8" />
            <path d="M21 3v5h-5" />
          </svg>
          {activeTab === 'graph' && graphReading ? t('skills.graph.status.reading') : t('common.refresh')}
        </button>
      </PageHeader>

      <PageToolbar testId="skill-panel-toolbar">
        <Tabs
          wrapperTestId="skill-panel-toolbar-tabs"
          itemTestId="skill-panel-tab"
          className="h-[34px] text-base"
          value={activeTab}
          onChange={handleMainTabChange}
          items={[
            { value: 'marketplace', label: t('skills.tabs.marketplace') },
            { value: 'my', label: t('skills.tabs.mySkills') },
            { value: 'graph', label: t('skills.tabs.skillGraph') },
          ]}
        />
        <div className="flex items-center gap-3" data-testid="skill-panel-toolbar-actions">
          {activeTab === 'my' && (
            <>
              {/* 已发布/未发布筛选 */}
              <FilterDropdown
                open={publishFilterOpen}
                onToggle={(v) => {
                  setPublishFilterOpen(v);
                  setEnableFilterOpen(false);
                }}
                onClose={() => setPublishFilterOpen(false)}
                value={mySkillsPublishFilter}
                onChange={(v) => {
                  setMySkillsPublishFilter(v);
                  setPublishFilterOpen(false);
                }}
                options={[
                  { value: 'all', label: t('skills.publishFilter.all') },
                  { value: 'published', label: t('skills.publishFilter.published') },
                  { value: 'unpublished', label: t('skills.publishFilter.unpublished') },
                ]}
                style={{ width: mySkillsPublishFilter === 'all' ? '40px' : '55px' }}
                testId="skill-panel-filter-publish"
              />
              {/* 启用/禁用筛选 */}
              <FilterDropdown
                open={enableFilterOpen}
                onToggle={(v) => {
                  setEnableFilterOpen(v);
                  setPublishFilterOpen(false);
                }}
                onClose={() => setEnableFilterOpen(false)}
                value={mySkillsSubTab}
                onChange={(v) => {
                  setMySkillsSubTab(v);
                  setEnableFilterOpen(false);
                }}
                options={[
                  { value: 'all', label: t('skills.mySkillsTabs.all') },
                  { value: 'enabled', label: t('skills.mySkillsTabs.enabled') },
                  { value: 'disabled', label: t('skills.mySkillsTabs.disabled') },
                  { value: 'builtin', label: t('skills.mySkillsTabs.builtin') },
                ]}
                style={{ width: '40px' }}
                testId="skill-panel-filter-enable"
              />
            </>
          )}
          {(activeTab === 'my' || activeTab === 'marketplace') && (
            <PageToolbarSearch
              value={search}
              onChange={(e) => handleSearchChange(e.target.value)}
              onClear={() => handleSearchChange('')}
              placeholder={t('skills.searchPlaceholder')}
              inputTestId="skill-panel-search-input"
              className="focus-visible:border-[color:var(--color-control-emphasis)]"
            />
          )}
          {activeTab === 'my' && (
            <CreateSkillMenu
              open={createMenuOpen}
              onToggle={setCreateMenuOpen}
              uploadDisabled={actionTarget === 'import_local'}
              onUploadLocal={() => {
                setCreateMenuOpen(false);
                setUploadSkillModalOpen(true);
              }}
              onDocToSkill={() => {
                setCreateMenuOpen(false);
                setDocToSkillModalOpen(true);
              }}
              onCreateViaChat={() => {
                setCreateMenuOpen(false);
                handleCreateViaChat();
              }}
            />
          )}
        </div>
      </PageToolbar>
    </div>
  );

  const renderModals = () => (
    <>
      <SourceManagerModal
        open={sourceModalOpen}
        sessionId={sessionId}
        onClose={() => setSourceModalOpen(false)}
        onNavigateToSettings={() => {
          setSourceModalOpen(false);
          onNavigateToSettings?.();
        }}
      />
      <SkillNetSearchModal
        open={skillNetModalOpen}
        sessionId={sessionId}
        installedSkillNames={installedSkillNames}
        installedSkillOrigins={installedSkillOrigins}
        onClose={() => setSkillNetModalOpen(false)}
        onInstalled={handleSkillsInstalled}
        onNavigateToSettings={() => {
          setSkillNetModalOpen(false);
          onNavigateToSettings?.();
        }}
      />
      <ClawHubSearchModal
        open={clawHubModalOpen}
        sessionId={sessionId}
        installedSkillNames={installedSkillNames}
        installedSkillOrigins={installedSkillOrigins}
        onClose={() => setClawHubModalOpen(false)}
        onInstalled={handleSkillsInstalled}
      />
      <TeamSkillsHubModal
        open={teamSkillsHubModalOpen}
        sessionId={sessionId}
        installedSkillNames={installedSkillNames}
        onClose={() => setTeamSkillsHubModalOpen(false)}
        onInstalled={handleSkillsInstalled}
      />
      {/* 上传技能弹窗 */}
      {uploadSkillModalOpen && (
        <UploadSkillModal
          actionTarget={actionTarget}
          onUpload={handleSkillUpload}
          onClose={() => setUploadSkillModalOpen(false)}
        />
      )}
      {/* 知识转技能弹窗 */}
      {docToSkillModalOpen && (
        <DocToSkillModal
          onCreateFromKnowledge={handleCreateFromKnowledge}
          onClose={() => setDocToSkillModalOpen(false)}
        />
      )}
      {synthesizeTooltip && <TopAnchorTooltip pos={synthesizeTooltip} text={t('skills.actions.synthesizeTooltip')} />}
      {/* 发布技能右侧弹窗 */}
      {publish.publishDrawerOpen && selectedSkill && getStoredOAuthToken() && (
        <PublishDrawer selectedSkill={selectedSkill} publish={publish} actionTarget={actionTarget} />
      )}
      {/* OAuth 登录弹窗 */}
      {oauthLoginOpen && (
        <OAuthLoginModal
          oauthError={oauthError}
          oauthLoadingProvider={oauthLoadingProvider}
          onLogin={handleOAuthLogin}
          onClose={() => setOauthLoginOpen(false)}
        />
      )}
    </>
  );

  // 全局提示：操作结果 toast + 知识任务进行中横幅
  const renderToasts = () => (
    <SkillToasts
      knowledgeTaskCount={knowledgeTaskCount}
      message={message}
      messageType={messageType}
      cleanMessage={cleanMessage}
      onCloseMessage={() => setMessage(null)}
    />
  );

  // 技能总谱页签：图谱画布 + 总谱设置
  const renderGraphTab = () => (
    <SkillGraphTab
      isConnected={isConnected}
      indexRecommendationVisible={indexRecommendationVisible}
      indexRecommendationBuilding={indexRecommendationBuilding}
      onDismissRecommendation={() => setIndexRecommendationVisible(false)}
      onBuildRecommendedIndex={() => void buildRecommendedIndex()}
      symphonySaveError={symphonySaveError}
      symphonySaving={symphonySaving}
      symphonyEnabledDraft={symphonyEnabledDraft}
      onUpdateSymphonyEnabled={(enabled) => void updateSymphonyEnabled(enabled)}
      skillGraphPanelRef={skillGraphPanelRef}
      onGraphReadingChange={updateGraphReading}
      onStartRetrievalIndexBuild={startRetrievalIndexBuild}
      graphActionError={graphActionError}
      onExternalErrorClear={clearGraphActionError}
    />
  );

  // 技能广场页签：hub 技能详情 或 列表视图
  const renderMarketplace = () =>
    marketplaceSubView === 'detail' && selectedHubSkill ? (
      <SkillDetailView
        mode="hub"
        detailState={hubDetailState}
        installedSkillMap={installedSkillMap}
        hubSkill={selectedHubSkill}
        hubDetail={hubDetail}
        actionTarget={actionTarget}
        onInstallHubSkill={handleInstallHubSkill}
        onGoToChat={handleGoToChat}
        onBackToHubDetail={handleBackToHubDetail}
      />
    ) : (
      <MarketplaceView
        teamSkills={teamSkills}
        featuredSkills={featuredSkills}
        hubSkills={hubSkills}
        hubLoading={hubLoading}
        searchKeyword={searchKeyword}
        marketplaceCategory={marketplaceCategory}
        onSelectHubSkill={handleSelectHubSkill}
        renderHubSkillAction={renderHubSkillAction}
        onCategoryChange={handleMarketplaceCategoryChange}
      />
    );

  // 我的技能页签：错误提示条 / 已安装技能详情 / 技能列表（空态 + 内置分组）
  const renderMySkills = () => (
    <>
      {message && messageType === 'error' && (
        <div
          className="page-shell mt-3 px-3 py-2 rounded-md bg-secondary text-sm text-danger"
          data-testid="skill-panel-my-error"
        >
          {message}
        </div>
      )}
      {selectedSkill ? (
        <SkillDetailView
          mode="installed"
          selectedSkill={selectedSkill}
          detailState={detailState}
          detailContent={detailContent}
          detailTab={detailTab}
          setDetailTab={setDetailTab}
          detailMenuOpen={detailMenuOpen}
          setDetailMenuOpen={setDetailMenuOpen}
          installedSkillMap={installedSkillMap}
          actionTarget={actionTarget}
          skillVersions={skillVersions}
          skillVersionsDefault={skillVersionsDefault}
          versionsLoadState={versionsLoadState}
          onFetchSkillDetail={fetchSkillDetail}
          filesLoadState={filesLoadState}
          filePreviewPath={filePreviewPath}
          filePreviewStatus={filePreviewStatus}
          previewTreeNodes={previewTreeNodes}
          filePreview={filePreview}
          onSelectSkillFile={(filePath) => {
            if (selectedSkill) fetchFilePreview(selectedSkill.name, filePath);
          }}
          onFetchSkillFiles={fetchSkillFiles}
          evolutionMessage={evolutionMessage}
          evolutionMessageType={evolutionMessageType}
          evolutionFormatError={evolutionFormatError}
          evolutionListState={evolutionListState}
          sortedEvolutionEntries={sortedEvolutionEntries}
          onEvolutionContentChange={handleEvolutionContentChange}
          onEvolutionDeleteEntry={handleEvolutionDeleteEntry}
          rebuildLoading={rebuildLoading}
          onRebuild={handleRebuild}
          setSynthesizeTooltip={setSynthesizeTooltip}
          onBackToList={handleBackToList}
          onEditSkill={handleEditSkill}
          onUninstall={handleUninstall}
          onToggleSkillDisabled={toggleSkillDisabled}
          onGoToChat={handleGoToChat}
        />
      ) : (
        <>
          {listState === 'success' && mySkillsFiltered.length === 0 ? (
            <div className="page-shell mt-4 text-sm text-text-muted">{t(MY_SKILLS_EMPTY_KEY[mySkillsSubTab])}</div>
          ) : null}
          {listState !== 'success' || mySkillsFiltered.length > 0 ? (
            <div className="page-scroll pt-4 flex-1 min-h-0 overflow-y-auto">
              {listState === 'loading' && (
                <div className="text-sm text-text-muted" data-testid="skill-panel-my-list-loading">
                  {t('common.loading')}
                </div>
              )}
              {listState === 'error' && (
                <div data-testid="skill-panel-my-list-error" className="col-span-3 text-sm text-text-muted">
                  {t('skills.listError')}
                </div>
              )}
              {listState === 'success' && (
                <>
                  {otherSkills.length > 0 && (
                    <>
                      {builtinSkills.length > 0 && <MySkillsGroupHeader label={t('skills.mySkillsGroups.added')} />}
                      <div className="card-grid-auto mb-6">{otherSkills.map(renderMySkillCard)}</div>
                    </>
                  )}
                  {builtinSkills.length > 0 && (
                    <>
                      <MySkillsGroupHeader label={t('skills.mySkillsGroups.builtin')} />
                      <div className="card-grid-auto">{builtinSkills.map(renderMySkillCard)}</div>
                    </>
                  )}
                </>
              )}
            </div>
          ) : null}
        </>
      )}
    </>
  );

  // 是否处于详情视图（我的技能详情 / 广场详情）：为真时隐藏顶部固定区
  const inDetailView =
    (activeTab === 'my' && !!selectedSkill) || (activeTab === 'marketplace' && marketplaceSubView === 'detail');

  return (
    <>
      {/* 全局提示：操作结果 toast + 知识任务进行中横幅 */}
      {renderToasts()}
      <div className="app-page-body">
        <div className="page-content" data-testid="skill-panel-content">
          {/* 固定区：页面标题 + 页签工具栏（进入详情视图时隐藏） */}
          {!inDetailView && renderFixedHeader()}
          {/* 技能总谱页签 */}
          {activeTab === 'graph' && renderGraphTab()}
          {/* 技能广场页签：列表视图 或 hub 技能详情 */}
          {activeTab === 'marketplace' && renderMarketplace()}
          {/* 我的技能页签：错误提示 / 已安装技能详情 / 技能列表 */}
          {activeTab === 'my' && renderMySkills()}
        </div>
        {/* 弹窗与浮层：来源管理/SkillNet/ClawHub/团队技能中心/上传/知识转技能/发布抽屉/OAuth 登录/合成提示 */}
        {renderModals()}
      </div>
    </>
  );
}
