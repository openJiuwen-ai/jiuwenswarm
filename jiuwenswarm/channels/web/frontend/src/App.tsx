// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

/**
 * App 主组件
 *
 * 应用主布局，整合所有组件
 */

import { useState, useCallback, useEffect, useRef, Component, ReactNode, useMemo, type PointerEvent as ReactPointerEvent } from 'react';
import { ChatPanel } from './components/ChatPanel';
import { SessionSidebar } from './components/SessionSidebar';
import { SkillPanel } from './components/SkillPanel';
import { AgentManagementPanel } from './components/AgentManagementPanel';
import { TeamPanel } from './components/TeamPanel';
import { SessionsPanel } from './components/SessionsPanel';
import CronPanel from './components/CronPanel';
import HeartbeatPanel from './components/HeartbeatPanel';
import { ToolPanel } from './components/ToolPanel';
import { UpdatePanel } from './components/UpdatePanel';
import { ExternalCliInstallDialog, type ExternalCliInstallStatuses } from './components/ExternalCliInstallDialog';
import { PersonalContextPanel } from './components/PersonalContext';
import { SettingsPage } from './features/settings/SettingsPage';
import type { SettingsPageDefinition } from './features/settings/registry/types';
import type { SettingsRequest } from './features/settings/services/settingsContract';
import {
  SETTINGS_MODULE_NAVIGATION_EVENT,
  requestSettingsModule,
  type SettingsModuleTarget,
} from './features/settings/settingsNavigation';
import { ConnectorMarketPanel } from './components/ConnectorMarket';
import {
  ShareImageDocument,
  exportShareImageNode,
  type ShareImageSnapshot,
} from './features/shareImageExport';
import type { CodeReviewTarget } from './features/code-mode/types';

import { FEATURE_APP_UPDATER_UI, FEATURE_PERSONAL_CONTEXT_UI } from './featureFlags';
import {
  beginHistoryRestore,
  fetchHistoryPage,
  HISTORY_GET_METHOD,
  mergeHistoryToolReplayItems,
  recoverSubagentToolHistory,
  type HistoryRestoreHandle,
  type HistoryHarnessReplayItem,
  type HistorySubagentReplayItem,
  type HistoryToolReplayItem,
  type FetchHistoryPageResult,
} from './features/historyRestore';
import { prefetchHistoryPages } from './features/historyPagination';
import { isPlanWireMode } from './features/planMode/wireMode';
import { queueOrAddGoalObjectiveMessage } from './features/goalPendingObjectiveBubble';
import { LoginPage } from './features/auth/LoginPage';
import { LogoutButton } from './features/auth/LogoutButton';
import {
  normalizeToolCallPayload,
  normalizeToolResultPayload,
} from './features/tool-events/toolEventNormalizer';
import { useWebSocket, mergePersistedGoalCompletionMessages, stampGoalObjectiveMessages, useResponsiveLayout, useResponsivePanelResize } from './hooks';
import { webRequest } from './services/webClient';
import { processOAuthCallback } from './utils/gitcodeOAuth';
import { useTeamPanelState } from './features/teamPanelState';
import { useSingleAgentPanelState } from './features/singleAgentPanelState';
import { AgentMode, MediaItem, UserAnswer, ModelEntry, type Session } from './types';
import type {
  ExternalCliAgentKind,
  ExternalCliDependencyInstallStatus,
} from './components/ExternalCliAgentsSection';
import {
  ensureSessionRuntimes,
  useSessionStore,
  useChatStore,
  useTodoStore,
  useGoalStore,
  useHarnessStore,
  usePlanStore,
  useWorkspaceStore,
  useCronStore,
  useSubagentStore,
} from './stores';
import { useChatRoute } from './multi-session/routing/useChatRoute';
import { ConversationSidebar, type NewConversationOptions } from './multi-session/sidebar/ConversationSidebar';
import { DeleteDialog } from './multi-session/dialogs/Dialogs';
import {
  NEW_CONVERSATION_ID,
  createConversationTitle,
  forgetCreatedConversation,
  isConversationMissing,
  registerCreatedConversation,
  resolveNewConversationEntrySettings,
  resetNewConversationRuntime,
} from './multi-session/state/newConversationLifecycle';
import { resolveNewConversationProjectDir } from './multi-session/state/newConversationProject';
import { toDisplaySessionTitle } from './utils/documentMessage';
import {
  getHiddenNavItemsForPlatform,
  resolveFrontendPlatform,
  type SidebarNavKey,
} from './utils/frontendPlatform';
import {
  createConversationSession,
  parsePersistSessionCommand,
} from './multi-session/state/createConversationSession';
import {
  resolvePendingPreviousSession,
  type PendingPreviousSession,
} from './multi-session/state/newConversationPreviousSession';
import { useTranslation } from 'react-i18next';
import {
  normalizeSubagentActivityEvent,
  normalizeSubagentStatusEvent,
  normalizeSubagentWaitResults,
} from './features/subagent/subagentNormalizer';
import {
  normalizeA2UIEnabled,
  setA2UIFeatureEnabled,
} from './features/a2ui/featureConfig';
import {
  buildA2UIClientEventContent,
  setA2UIActionHandler,
} from './features/a2ui/actionBridge';
import { saveBlob } from './utils/desktopSave';
import { generateUuidV4 } from './utils/uuid';
import {
  ModelSetupGuide,
  type ModelSetupGuideStep,
} from './features/modelSetupGuide/ModelSetupGuide';
import { isSetupGuideEnabled } from './features/modelSetupGuide/modelSetupGuideState';
import { isTeamAgentMode } from './features/planMode/wireMode';
import './App.css';

const CHAT_PANEL_DEFAULT_WIDTH_PCT = 33.33;
const CHAT_PANEL_MIN_WIDTH_PCT = 20;
const CHAT_PANEL_MAX_WIDTH_PCT = 70;

type ChatPanelResizeDrag = {
  pointerId: number;
  startX: number;
  startPct: number;
  containerWidth: number;
};
const PREVIEW_MODEL_SETUP_GUIDE = import.meta.env.DEV
  && new URLSearchParams(window.location.search).get('modelSetupGuide') === '1';

function shouldPreviewModelSetupGuide(): boolean {
  return PREVIEW_MODEL_SETUP_GUIDE;
}

function normalizeConfigBoolean(value: unknown): boolean {
  if (typeof value === 'boolean') {
    return value;
  }
  return ['1', 'true', 'yes', 'on', 'enabled'].includes(
    String(value ?? '').trim().toLowerCase(),
  );
}

type MainNavKey = SidebarNavKey | 'connectorMarket';

type LoadedHistoryPage = {
  pageIdx: number;
  totalPages: number;
  result: FetchHistoryPageResult | null;
};

function getWorkContextForSession(sessionId: string): {
  project_id?: string;
  project_dir?: string;
} {
  const sessionState = useSessionStore.getState();
  const workspaceState = useWorkspaceStore.getState();
  const session =
    sessionState.currentSession?.session_id === sessionId
      ? sessionState.currentSession
      : sessionState.sessions.find((item) => item.session_id === sessionId);
  const selectedProject = workspaceState.selectedProject;

  return {
    project_id: session?.project_id || selectedProject?.project_id || undefined,
    project_dir: session?.project_dir || selectedProject?.project_dir || undefined,
  };
}

function clearTeamRuntimeState(sessionId: string): void {
  const sessionStore = useSessionStore.getState();
  sessionStore.setTeamMembers(sessionId, []);
  sessionStore.setTeamTaskEvents(sessionId, []);
  sessionStore.setTeamTasks(sessionId, []);
  sessionStore.setTeamMemberExecutionEvents(sessionId, []);
  sessionStore.clearAllTeamMemberContextCompressionStatus(sessionId);
  sessionStore.setTeamHistoryMessages(sessionId, []);
  sessionStore.setTeamHumanShareCommands(sessionId, []);
}

function waitForNextPaint(): Promise<void> {
  return new Promise((resolve) => {
    requestAnimationFrame(() => resolve());
  });
}

// 错误边界组件
interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
}

class ErrorBoundary extends Component<
  { children: ReactNode },
  ErrorBoundaryState
> {
  constructor(props: { children: ReactNode }) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    console.error('React Error:', error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return <ErrorFallback error={this.state.error} />;
    }
    return this.props.children;
  }
}

function ErrorFallback({ error }: { error: Error | null }) {
  const { t } = useTranslation();
  return (
    <div className="flex items-center justify-center h-screen bg-bg text-text p-8" data-testid="app-error-fallback">
      <div className="max-w-2xl card" data-testid="app-error-fallback-card">
        <h1 className="text-2xl font-bold text-danger mb-4" data-testid="app-error-fallback-title">
          {t('app.errorTitle')}
        </h1>
        <p className="text-text-muted mb-4" data-testid="app-error-fallback-message">
          {error?.message || t('app.unknownError')}
        </p>
        <pre className="bg-secondary p-4 rounded-lg text-sm overflow-auto max-h-64 font-mono" data-testid="app-error-fallback-stack">
          {error?.stack}
        </pre>
        <button
          onClick={() => window.location.reload()}
          className="btn primary mt-4"
          data-testid="app-error-fallback-reload"
        >
          {t('app.reload')}
        </button>
      </div>
    </div>
  );
}

async function saveShareImage(blob: Blob, filename: string): Promise<boolean> {
  const outcome = await saveBlob(blob, filename);
  if (outcome === 'failed') {
    throw new Error('share_desktop_save_failed');
  }
  return outcome === 'saved';
}

function AppContent({
  settingsPageDefinition,
  resolveSettingsRequest,
}: {
  settingsPageDefinition: SettingsPageDefinition;
  resolveSettingsRequest: (openSourceRequest: SettingsRequest) => SettingsRequest;
}) {
  const { t, i18n } = useTranslation();
  const { route, navigate } = useChatRoute();
  const tRef = useRef(t);
  // 优先使用存储的会话 ID，避免每次刷新创建新会话
  const [sessionId, setSessionId] = useState<string>(() => {
    if (route.kind === 'chat-session') return route.sessionId;
    return 'new';
  });

  const [activeNav, setActiveNav] = useState<MainNavKey>('chat');
  const [serverConfig, setServerConfig] = useState<Record<string, unknown> | null>(null);
  const kvCacheAffinityEnabled = normalizeConfigBoolean(
    serverConfig?.kv_cache_affinity_enabled,
  );
  const [configError, setConfigError] = useState<string | null>(null);
  const [initialDataLoaded, setInitialDataLoaded] = useState(false);
  const [restartModalOpen, setRestartModalOpen] = useState(false);
  const [restartSuccess, setRestartSuccess] = useState(false);
  const [isExportingShare, setIsExportingShare] = useState(false);
  const [shareExportSnapshot, setShareExportSnapshot] = useState<ShareImageSnapshot | null>(null);
  const [restartSeenDisconnect, setRestartSeenDisconnect] = useState(false);
  const [appliedWithoutRestart, setAppliedWithoutRestart] = useState(false);
  const [saveToastVisible, setSaveToastVisible] = useState(false);
  const [proactiveToastVisible, setProactiveToastVisible] = useState(false);
  const [proactiveToastMessage, setProactiveToastMessage] = useState('');
  const [securityAlertVisible, setSecurityAlertVisible] = useState(false);
  const [securityAlertContent, setSecurityAlertContent] = useState('');
  const [externalCliInstallDialogOpen, setExternalCliInstallDialogOpen] = useState(false);
  const [externalCliInstallStatuses, setExternalCliInstallStatuses] = useState<ExternalCliInstallStatuses>({});
  const [hasVisitedSkills, setHasVisitedSkills] = useState(false);
  const [hasVisitedPersonalContext, setHasVisitedPersonalContext] = useState(false);
  const [requestedSettingsModuleId, setRequestedSettingsModuleId] = useState<SettingsModuleTarget | null>(null);
  const {
    isMobile,
    conversationSidebarCollapsed,
    setConversationSidebarCollapsed,
    conversationSidebarFloating,
    toolPanelHidden,
    setToolPanelHidden,
  } = useResponsiveLayout();

  const [modelSetupGuideStep, setModelSetupGuideStep] = useState<ModelSetupGuideStep | null>(null);
  const [modelSetupGuideManual, setModelSetupGuideManual] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Session | null>(null);
  const [dialogBusy, setDialogBusy] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [composerFocusNonce, setComposerFocusNonce] = useState(0);
  const [missingSessionId, setMissingSessionId] = useState<string | null>(null);
  const startupUpdateCheckRef = useRef(false);
  const modelSetupGuideEvaluatedRef = useRef(false);
  /** OAuth 回调恢复导航后标记，防止 fetchConfig 等后续逻辑覆盖 activeNav */
  const oauthNavRestoredRef = useRef(false);

  useEffect(() => {
    tRef.current = t;
  }, [t]);

  // OAuth 回调处理：页面加载时检测 URL 中的 code，自动换 token + 获取用户信息
  useEffect(() => {
    processOAuthCallback()
      .finally(() => {
        // 备份：OAuth 回调完成后再次确认导航（通常路由 effect 已设置）
        const nav = sessionStorage.getItem('oauth_redirect_nav');
        if (nav) {
          sessionStorage.removeItem('oauth_redirect_nav');
          oauthNavRestoredRef.current = true;
          setActiveNav(nav as MainNavKey);
          if (nav === 'skills') setHasVisitedSkills(true);
        }
        // 无论成功或失败都派发事件，SkillPanel 根据有无 oauth_error 决定显示错误或开抽屉
        window.dispatchEvent(new CustomEvent('oauth-callback-complete'));
      });
  }, []);

  useEffect(() => {
    if (activeNav === 'chat') {
      const { defaultModelName, setSelectedModelName } = useSessionStore.getState();
      const runtime = useSessionStore.getState().getRuntime(sessionId);
      if (defaultModelName && !runtime?.selectedModelName) {
        useSessionStore.getState().ensureRuntime(sessionId);
        setSelectedModelName(sessionId, defaultModelName);
      }
    }
  }, [activeNav, sessionId]);

  useEffect(() => {
    if (!FEATURE_APP_UPDATER_UI && activeNav === 'updatepanel') {
      setActiveNav('chat');
    }
  }, [activeNav]);

  useEffect(() => {
    if (!FEATURE_PERSONAL_CONTEXT_UI && (activeNav === 'personalContext' || activeNav === 'personalContextSettings')) {
      setActiveNav('chat');
    }
  }, [activeNav]);

  useEffect(() => {
    const handler = (e: Event) => {
      const nav = (e as CustomEvent<MainNavKey>).detail;
      if (nav) setActiveNav(nav);
    };
    window.addEventListener('jiuwen:nav', handler);
    return () => window.removeEventListener('jiuwen:nav', handler);
  }, []);

  useEffect(() => {
    const handler = (event: Event) => {
      const moduleId = (event as CustomEvent<SettingsModuleTarget>).detail;
      setRequestedSettingsModuleId(moduleId);
      setActiveNav('settings');
    };
    window.addEventListener(SETTINGS_MODULE_NAVIGATION_EVENT, handler);
    return () => window.removeEventListener(SETTINGS_MODULE_NAVIGATION_EVENT, handler);
  }, []);

  const restartAutoCloseTimerRef = useRef<number | null>(null);
  const saveToastTimerRef = useRef<number | null>(null);
  const proactiveToastTimerRef = useRef<number | null>(null);
  const settingsHasChangesRef = useRef(false);
  const [historyLoadingMore, setHistoryLoadingMore] = useState(false);
  const [historyPrepending, setHistoryPrepending] = useState(false);
  const [historyRetrySessions, setHistoryRetrySessions] = useState<ReadonlySet<string>>(
    () => new Set()
  );
  /** 仅用于强制重跑「首屏 history」effect：从会话列表恢复时若 sessionId 未变，也要重新拉 history 并恢复 historyPagerMeta */
  const [historyBootstrapKey, setHistoryBootstrapKey] = useState(0);
  const sessionIdRef = useRef(sessionId);
  const sessionRestoreQueueRef = useRef<Promise<void>>(Promise.resolve());
  const kvcViewIdRef = useRef(generateUuidV4());
  const kvcPreparedInputSessionRef = useRef<string | null>(null);
  const historyLoadingSessionsRef = useRef(new Set<string>());
  const historyRestoreHandlesRef = useRef(new Map<string, HistoryRestoreHandle>());
  const subagentHistoryRestoreHandlesRef = useRef(new Map<string, HistoryRestoreHandle>());
  const subagentHistoryRestoreRevisionRef = useRef(new Map<string, string>());
  const subagentToolReplayBySessionRef = useRef(new Map<string, HistoryToolReplayItem[]>());
  const historyPageHandlesRef = useRef(new Map<string, HistoryRestoreHandle>());
  const historyPagePromisesRef = useRef(new Map<string, Promise<LoadedHistoryPage | null>>());
  const historyPageCancelRef = useRef(new Map<string, () => void>());
  const historyBackgroundPrefetchTokensRef = useRef(new Map<string, number>());
  const creatingSessionRef = useRef(false);
  /** 离开新建任务页后，仍未发送的临时会话可以被再次打开。 */
  const pendingNewConversationRef = useRef(route.kind === 'chat-new');
  const sessionIdsCreatedInThisPageRef = useRef(new Set<string>());
  const shareExportRef = useRef<HTMLDivElement>(null);
  const shareExportFilenameRef = useRef('jiuwenswarm-share.png');
  const shareExportTokenRef = useRef(0);
  const preserveSelectedProjectOnChatNewRef = useRef(false);
  const newConversationProjectRef = useRef<Pick<Session, 'project_id' | 'project_dir'> | null>(null);
  const newConversationPreviousSessionRef = useRef<PendingPreviousSession | null>(null);
  /** 为 true 表示刚从「会话列表」恢复；history 为空时在 useEffect 的 onEmpty 中提示一次 */
  const historyRestoreFromPanelHintRef = useRef(false);
  const { loadProjects, setSelectedProject } = useWorkspaceStore();

  const setHistoryRetryAvailable = useCallback((sid: string, available: boolean) => {
    setHistoryRetrySessions((current) => {
      if (current.has(sid) === available) {
        return current;
      }
      const next = new Set(current);
      if (available) {
        next.add(sid);
      } else {
        next.delete(sid);
      }
      return next;
    });
  }, []);

  useEffect(() => {
    sessionIdRef.current = sessionId;
    // A new foreground visit gets one fresh input-intent opportunity. Merely
    // switching to the Session still does not prefetch; the first real editor
    // insertion below does.
    kvcPreparedInputSessionRef.current = null;
    setHistoryLoadingMore(false);
    setHistoryPrepending(historyLoadingSessionsRef.current.has(sessionId));
  }, [sessionId]);

  useEffect(() => {
    // A Session can stay mounted in its own browser tab/window while another
    // Session is used elsewhere. In that case `sessionId` never changes, so
    // the per-visit input latch above would otherwise remain consumed by the
    // Session's initial turn. Re-arm only when this page returns to the
    // foreground; focus/visibility alone still does not issue a prefetch.
    const rearmInputIntent = () => {
      kvcPreparedInputSessionRef.current = null;
    };
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        rearmInputIntent();
      }
    };

    window.addEventListener('focus', rearmInputIntent);
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      window.removeEventListener('focus', rearmInputIntent);
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, []);

  const {
    teamAreaExpanded,
    teamAreaActiveTab,
    teamAreaActiveDetailTab,
    teamAreaSelectedMemberId,
    teamAreaSelectedArtifactId,
    setTeamAreaExpanded,
    setTeamAreaActiveTab,
    setTeamAreaActiveDetailTab,
    setTeamAreaSelectedMemberId,
    setTeamAreaSelectedArtifactId,
  } = useTeamPanelState();
  const {
    singleAgentPanelExpanded,
    singleAgentPanelActiveTab,
    singleAgentPanelSelectedArtifactId,
    singleAgentPanelSelectedSubagentId,
    setSingleAgentPanelExpanded,
    setSingleAgentPanelActiveTab,
    setSingleAgentPanelSelectedArtifactId,
    setSingleAgentPanelSelectedSubagentId,
  } = useSingleAgentPanelState();

  useEffect(() => {
    const oauthNav = sessionStorage.getItem('oauth_redirect_nav');
    const targetNav = (oauthNav || 'chat') as MainNavKey;
    if (oauthNav === 'skills') setHasVisitedSkills(true);
    if (route.kind === 'chat-session') {
      sessionIdRef.current = route.sessionId;
      setSessionId(route.sessionId);
      setActiveNav(targetNav);
    } else if (route.kind === 'chat-new') {
      if (window.location.pathname !== '/chat/new') {
        if (oauthNav) {
          // OAuth 重定向：用 replaceState 改 URL 但不触发 route 变化，避免 effect 重跑覆盖 activeNav
          window.history.replaceState(null, '', '/chat/new');
        } else {
          navigate({ kind: 'chat-new' }, { replace: true });
        }
      }
      pendingNewConversationRef.current = true;
      if (preserveSelectedProjectOnChatNewRef.current) {
        preserveSelectedProjectOnChatNewRef.current = false;
      } else {
        useWorkspaceStore.getState().setSelectedProject(null);
      }
      sessionIdRef.current = 'new';
      setSessionId('new');
      setActiveNav(targetNav);
      if (!oauthNav) {
        setTeamAreaExpanded(false);
        setSingleAgentPanelExpanded(false);
      }
    }
  }, [navigate, route, setSingleAgentPanelExpanded, setTeamAreaExpanded, setHasVisitedSkills]);

  useEffect(() => {
    ensureSessionRuntimes(sessionId);
    useChatStore.getState().setActiveSessionId(sessionId);
    useSubagentStore.getState().hydrateRuntime(sessionId);
  }, [sessionId]);

  useEffect(() => {
    if (!initialDataLoaded) {
      return;
    }
    void loadProjects();
  }, [initialDataLoaded, loadProjects]);

  const {
    setCurrentSession,
    setAvailableModels,
    setMode,
    setTeamLeaderMemberIds,
  } = useSessionStore.getState();
  const sessions = useSessionStore((s) => s.sessions);
  const currentSession = useSessionStore((s) => s.currentSession);
  const routeSessionId = route.kind === 'chat-session' ? route.sessionId : null;
  const projects = useWorkspaceStore((s) => s.projects);
  const sessionTitle = useMemo(() => {
    const session = currentSession?.session_id === sessionId
      ? currentSession
      : sessions.find((s) => s.session_id === sessionId);
    const raw = session?.title?.trim() ?? '';
    return toDisplaySessionTitle(raw);
  }, [currentSession, sessions, sessionId]);
  const sessionProjectName = useMemo(() => {
    const session = currentSession?.session_id === sessionId
      ? currentSession
      : sessions.find((s) => s.session_id === sessionId);
    if (!session?.project_dir) return '';
    const project = projects.find((item) => !item.is_default && item.project_dir === session.project_dir);
    return project?.name?.trim() ?? '';
  }, [currentSession, projects, sessions, sessionId]);
  const sessionProject = useMemo(() => {
    const session = currentSession?.session_id === sessionId
      ? currentSession
      : sessions.find((item) => item.session_id === sessionId);
    if (!session) return null;
    return projects.find((project) => (
      (!project.is_default && project.project_id === session.project_id)
      || Boolean(project.project_dir && project.project_dir === session.project_dir)
    )) ?? null;
  }, [currentSession, projects, sessions, sessionId]);
  const mode = useSessionStore((s) => s.runtimes[sessionId]?.mode ?? 'agent');
  const teamTaskEvents = useSessionStore((s) => s.runtimes[sessionId]?.teamTaskEvents ?? []);
  const teamTasks = useSessionStore((s) => s.runtimes[sessionId]?.teamTasks ?? []);
  const teamMembers = useSessionStore((s) => s.runtimes[sessionId]?.teamMembers ?? []);
  const [chatPanelWidthPct, setChatPanelWidthPct] = useState(CHAT_PANEL_DEFAULT_WIDTH_PCT);
  const chatPanelResizeDragRef = useRef<ChatPanelResizeDrag | null>(null);
  const [codeReviewTarget, setCodeReviewTarget] = useState<CodeReviewTarget | null>(null);
  const [heartbeatPanelOpen, setHeartbeatPanelOpen] = useState(false);

  useEffect(() => {
    setCodeReviewTarget(null);
  }, [sessionId]);

  // 心跳面板是会话级功能，切换会话时收起，避免带着上一个会话的任务列表进入新会话
  useEffect(() => {
    setHeartbeatPanelOpen(false);
  }, [sessionId]);

  const handleToggleHeartbeatPanel = useCallback(() => {
    setHeartbeatPanelOpen((v) => !v);
  }, []);

  const handleToggleDetailPanel = useCallback((expanded: boolean | null) => {
    // 团队/代码审核面板和心跳面板互斥，共用右侧工作区同一栏
    setHeartbeatPanelOpen(false);
    if (expanded === null) {
      setToolPanelHidden(true);
      setTeamAreaExpanded(false);
      setSingleAgentPanelExpanded(false);
      return;
    }
    setToolPanelHidden(false);
    if (mode === 'team') {
      // 真正处于 Team 模式时不动 teamAreaActiveTab：下面这段"陈旧 team tab 切回
      // planning"的兜底只是给单 Agent 面板用的。曾经按某版交接文档建议去掉这层
      // mode 隔离，复核后确认那条建议的前提不成立（mode 是 zustand selector，
      // 渲染时始终最新，不存在"滞后短路"的竞态窗口），且会导致真正在 Team 模式、
      // 停留在 team tab 的用户每次收起/展开面板都被强制踢回 planning——teamArea
      // 组件把 'team' 当合法 tab，没有兜底。这个 early return 就是隔离本身。
      setTeamAreaExpanded(expanded);
      return;
    }
    if (expanded && teamAreaActiveTab === 'team') {
      setTeamAreaActiveTab('planning');
    }
    setSingleAgentPanelExpanded(expanded);
  }, [mode, setSingleAgentPanelExpanded, setTeamAreaActiveTab, setTeamAreaExpanded, teamAreaActiveTab]);

  const handleOpenCodeReview = useCallback((target: CodeReviewTarget) => {
    setHeartbeatPanelOpen(false);
    setCodeReviewTarget(target);
    setToolPanelHidden(false);
    if (mode === 'team') {
      setTeamAreaActiveTab('review');
      setTeamAreaExpanded(true);
    } else {
      setSingleAgentPanelActiveTab('review');
      setSingleAgentPanelExpanded(true);
    }
  }, [mode, setSingleAgentPanelActiveTab, setSingleAgentPanelExpanded, setTeamAreaActiveTab, setTeamAreaExpanded, setToolPanelHidden]);

  const handleDividerPointerDown = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || chatPanelResizeDragRef.current) return;
    const container = event.currentTarget.parentElement;
    if (!container) return;
    const containerWidth = container.getBoundingClientRect().width;
    if (containerWidth <= 0) return;

    event.preventDefault();
    document.body.classList.add('workspace-resize-active');
    event.currentTarget.setPointerCapture(event.pointerId);
    chatPanelResizeDragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startPct: chatPanelWidthPct,
      containerWidth,
    };
  }, [chatPanelWidthPct]);

  const handleDividerPointerMove = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = chatPanelResizeDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.startX;
    const nextWidthPct = drag.startPct + (dx / drag.containerWidth) * 100;
    const clampedWidthPct = Math.min(
      CHAT_PANEL_MAX_WIDTH_PCT,
      Math.max(CHAT_PANEL_MIN_WIDTH_PCT, nextWidthPct),
    );
    setChatPanelWidthPct(clampedWidthPct);
  }, []);

  const clearChatPanelResize = useCallback((pointerId?: number): boolean => {
    if (pointerId !== undefined && chatPanelResizeDragRef.current?.pointerId !== pointerId) return false;
    chatPanelResizeDragRef.current = null;
    document.body.classList.remove('workspace-resize-active');
    return true;
  }, []);

  const finishDividerResize = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (!clearChatPanelResize(event.pointerId)) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }, [clearChatPanelResize]);

  const clearMessages = useChatStore((s) => s.clearMessages);
  const addMessage = useChatStore((s) => s.addMessage);
  const addToolCall = useChatStore((s) => s.addToolCall);
  const addToolResult = useChatStore((s) => s.addToolResult);
  const settleHistoricalToolExecutions = useChatStore((s) => s.settleHistoricalToolExecutions);
  const prependMessages = useChatStore((s) => s.prependMessages);
  const isProcessing = useChatStore((s) => s.runtimes[sessionId]?.isProcessing ?? false);
  const isPaused = useChatStore((s) => s.runtimes[sessionId]?.isPaused ?? false);
  const hasPendingQuestion = useChatStore((s) => Boolean(s.runtimes[sessionId]?.pendingQuestion));
  const setProcessing = useChatStore((s) => s.setProcessing);
  const setThinking = useChatStore((s) => s.setThinking);
  const setLoadingHistory = useChatStore((s) => s.setLoadingHistory);
  const setHistoryPagerMeta = useChatStore((s) => s.setHistoryPagerMeta);
  /** 自「恢复会话」加载 history 后的分页元数据；与消息一样按 session 隔离。 */
  const historyPagerMeta = useChatStore((s) => s.runtimes[sessionId]?.historyPagerMeta ?? null);
  const setPaused = useChatStore((s) => s.setPaused);
  const messages = useChatStore((s) => s.runtimes[sessionId]?.messages ?? []);
  const isLoadingHistory = useChatStore((s) => s.runtimes[sessionId]?.isLoadingHistory ?? false);
  const replaceHistoryMessages = useChatStore((s) => s.replaceHistoryMessages);
  const restoreReasoningSegments = useChatStore((s) => s.restoreReasoningSegments);
  const isRestoringHistorySession = isLoadingHistory && !historyPagerMeta && messages.length === 0;
  const isRestoringTeamHistory = mode === 'team' && isRestoringHistorySession;

  const frontendPlatform = resolveFrontendPlatform(
    typeof window !== 'undefined' ? window.__JIWEN_PLATFORM__ : undefined,
    import.meta.env.VITE_PLATFORM,
    import.meta.env.MODE,
    typeof serverConfig?.runtime_platform === 'string' ? serverConfig.runtime_platform : undefined,
  );
  const hiddenNavItems = useMemo<MainNavKey[]>(() => {
    const base = getHiddenNavItemsForPlatform(frontendPlatform);
    if (FEATURE_PERSONAL_CONTEXT_UI) return base;
    // feature 关闭时移除全部个人上下文入口
    return [...base, 'personalContext', 'personalContextSettings'];
  }, [frontendPlatform]);

  useEffect(() => {
    if (!serverConfig) {
      if (sessionId) setTeamLeaderMemberIds(sessionId, []);
      return;
    }
    const leaderIds = Object.entries(serverConfig)
      .filter(([key]) => /^team_leader_member_name_\d+$/.test(key) || /^team_\d+_leader_member_name$/.test(key))
      .map(([, value]) => (typeof value === 'string' ? value.trim() : ''))
      .filter(Boolean);
    if (sessionId) setTeamLeaderMemberIds(sessionId, leaderIds);
  }, [serverConfig, sessionId, setTeamLeaderMemberIds]);

  const disposeInFlightHistoryHandles = useCallback((sid?: string) => {
    const cancelSession = (targetSid: string) => {
      const prevToken = historyBackgroundPrefetchTokensRef.current.get(targetSid) ?? 0;
      historyBackgroundPrefetchTokensRef.current.set(targetSid, prevToken + 1);
      historyLoadingSessionsRef.current.delete(targetSid);
      setHistoryRetryAvailable(targetSid, false);
      if (targetSid === sessionIdRef.current) {
        setHistoryPrepending(false);
        setHistoryLoadingMore(false);
      }
      setLoadingHistory(targetSid, false);
      historyRestoreHandlesRef.current.get(targetSid)?.dispose();
      historyRestoreHandlesRef.current.delete(targetSid);
      for (const [key, handle] of Array.from(subagentHistoryRestoreHandlesRef.current.entries())) {
        if (!key.startsWith(`${targetSid}:`)) continue;
        handle.dispose();
        subagentHistoryRestoreHandlesRef.current.delete(key);
      }
      for (const key of subagentHistoryRestoreRevisionRef.current.keys()) {
        if (key.startsWith(`${targetSid}:`)) {
          subagentHistoryRestoreRevisionRef.current.delete(key);
        }
      }
      subagentToolReplayBySessionRef.current.delete(targetSid);
      for (const [key, handle] of Array.from(historyPageHandlesRef.current.entries())) {
        if (!key.startsWith(`${targetSid}:`)) continue;
        handle.dispose();
        historyPageHandlesRef.current.delete(key);
        historyPagePromisesRef.current.delete(key);
        historyPageCancelRef.current.get(key)?.();
        historyPageCancelRef.current.delete(key);
      }
    };

    if (sid) {
      cancelSession(sid);
      return;
    }

    for (const targetSid of new Set([
      ...historyRestoreHandlesRef.current.keys(),
      ...Array.from(subagentHistoryRestoreHandlesRef.current.keys(), (key) => key.split(':', 1)[0]),
      ...Array.from(historyPageHandlesRef.current.keys(), (key) => key.split(':', 1)[0]),
      ...historyLoadingSessionsRef.current,
    ])) {
      cancelSession(targetSid);
    }
  }, [setHistoryRetryAvailable, setLoadingHistory]);

  useEffect(() => () => disposeInFlightHistoryHandles(), [disposeInFlightHistoryHandles]);
  const todos = useTodoStore((s) => s.runtimes[sessionId]?.todos ?? []);
  const subagentCount = useSubagentStore((s) => Object.keys(s.runtimes[sessionId]?.subagentsById ?? {}).length);
  const subagentStatusSignature = useSubagentStore((s) => Object.values(s.runtimes[sessionId]?.subagentsById ?? {})
    .sort((left, right) => left.subagent_id.localeCompare(right.subagent_id))
    .map((subagent) => `${subagent.subagent_id}:${subagent.status}:${subagent.turn_outcome ?? ''}:${subagent.closed_reason ?? ''}:${subagent.revision}:${subagent.updated_at}`)
    .join('|'));
  const clearTodos = useTodoStore((s) => s.clearTodos);
  const extensionReady = useHarnessStore((s) => s.runtimes[sessionId]?.extensionReady ?? null);
  const resetHarnessStore = useHarnessStore((s) => s.reset);
  const proactiveNotificationMessage = useHarnessStore((s) => s.proactiveNotificationMessage);
  const setProactiveNotification = useHarnessStore((s) => s.setProactiveNotification);

  const toolPanelHasContent = useMemo(() => {
    const hasMessages = messages.length > 0;
    const hasCodeEnvironment = sessionProject?.work_mode === 'code' && sessionId !== NEW_CONVERSATION_ID;
    switch (mode) {
      case 'auto_harness':
        return Boolean(extensionReady?.runtimePath) || hasMessages;
      case 'team':
        return isRestoringTeamHistory || teamTaskEvents.length > 0 || teamTasks.length > 0 || teamMembers.length > 0 || hasMessages || hasCodeEnvironment;
      default:
        return todos.length > 0
          || subagentCount > 0
          || hasMessages
          || hasCodeEnvironment;
    }
  }, [mode, todos.length, subagentCount, teamTaskEvents.length, teamTasks.length, teamMembers.length, extensionReady?.runtimePath, messages.length, isRestoringTeamHistory, sessionId, sessionProject?.work_mode]);
  // 单 agent 模式同样复用集群模式的展开布局（百分比宽度 + 可拖拽分割线），
  // 避免右侧面板与聊天面板平分空间导致宽度与集群模式不一致；auto_harness 走收起态分支。
  const panelExpanded = mode === 'team' ? teamAreaExpanded : singleAgentPanelExpanded;
  // 心跳面板打开时，团队/代码审核面板让出右侧工作区（两者互斥，不共同占用宽度）。
  const isTeamAreaExpanded = mode !== 'auto_harness' && panelExpanded && toolPanelHasContent && !heartbeatPanelOpen;

  const { shouldFullscreen } = useResponsivePanelResize({
    isTeamAreaExpanded,
    conversationSidebarCollapsed,
    setConversationSidebarCollapsed,
    setSingleAgentPanelExpanded,
    setTeamAreaExpanded,
    mode,
  });

  // WebSocket 连接 - provider 由后端配置决定 - provider 由后端配置决定，前端默认不在 URL query 传递
  const {
    isConnected,
    connectionState,
    request,
    persistMedia,
    persistDocuments,
    sendMessage,
    sendStructuredChatContent,
    pause,
    cancel,
    supplement,
    sendUserAnswer,
    setGoalObjective,
    pauseGoal,
    resumeGoal,
    clearGoal,
    refreshGoal,
    drainTaskQueueIfIdle,
  } = useWebSocket({
    activeSessionId: sessionId,
    onConnect: () => console.log('Connected'),
    onDisconnect: () => {
      console.log('Disconnected');
    },
    onError: (error) => {
      console.error('WebSocket error:', error);
    },
    onConfigChanged: () => {
      handleConfigChanged();
    },
    onModelsUpdated: () => {
      handleModelsRefresh();
    },
    onCronResultArrived: (cronSessionId: string, cronJobId: string) => {
      // 仅当用户当前停留在该任务的"立即执行"页面时才自动跳转：
      // - 多个任务同时返回结果时，不会互相跳转覆盖
      // - 用户已手动切走时不打扰
      // - 定时调度（非"立即执行"）不自动跳转
      // lastRunSessionId[jobId] 是点击"立即执行"时存入的会话 ID，
      // sessionIdRef.current 是当前会话，两者一致说明用户还在等这个任务的结果。
      if (cronJobId) {
        const lastSid = useCronStore.getState().lastRunSessionId[cronJobId] ?? '';
        if (lastSid && sessionIdRef.current === lastSid) {
          void handleRestoreSession(cronSessionId);
        }
      }
    },
  });
  const settingsRequest = useMemo(() => resolveSettingsRequest(request), [request, resolveSettingsRequest]);

  const applySubagentHistoryReplay = useCallback((sid: string, items: HistorySubagentReplayItem[]) => {
    const subagentStore = useSubagentStore.getState();
    for (const item of items) {
      if (item.kind === 'updated') {
        const event = normalizeSubagentStatusEvent({ ...item.payload, session_id: sid });
        if (!event || event.subagent.parent_session_id !== sid) continue;
        subagentStore.applyHistoryEvent(sid, event);
        continue;
      }
      if (item.kind === 'activity') {
        const event = normalizeSubagentActivityEvent({
          ...item.payload,
          event_type: 'chat.subagent_activity',
          session_id: sid,
        });
        if (!event) continue;
        subagentStore.applyHistoryEvent(sid, event);
        continue;
      }
      const subagentId = typeof item.payload.subagent_id === 'string' ? item.payload.subagent_id.trim() : '';
      const content = typeof item.payload.content === 'string' ? item.payload.content : '';
      if (subagentId && content.trim()) {
        const parentSessionId = typeof item.payload.parent_session_id === 'string'
          ? item.payload.parent_session_id
          : undefined;
        const taskId = typeof item.payload.task_id === 'string' ? item.payload.task_id : undefined;
        const atMs = Date.parse(item.at);
        subagentStore.applyTranscript(sid, {
          subagent_id: subagentId,
          content,
          ...(parentSessionId ? { parent_session_id: parentSessionId } : {}),
          ...(taskId ? { task_id: taskId } : {}),
          ...(Number.isFinite(atMs) ? { at_ms: atMs } : {}),
        });
      }
    }
  }, []);

  const restoreSubagentHistory = useCallback((sid: string) => {
    useSubagentStore.getState().hydrateRuntime(sid);
    const runtime = useSubagentStore.getState().getRuntime(sid);
    const subagentIds = Object.keys(runtime?.subagentsById ?? {});
    for (const subagentId of subagentIds) {
      const key = `${sid}:${subagentId}`;
      const subagent = runtime?.subagentsById[subagentId];
      if (!subagent) continue;
      const expectedRevision = subagent.revision;
      const expectedUpdatedAt = subagent.updated_at;
      const revisionMarker = `${subagent.status}:${subagent.turn_outcome ?? ''}:${subagent.closed_reason ?? ''}:${subagent.revision}:${subagent.updated_at}`;
      if (subagentHistoryRestoreRevisionRef.current.get(key) === revisionMarker) continue;
      if (subagentHistoryRestoreHandlesRef.current.has(key)) continue;
      subagentHistoryRestoreRevisionRef.current.set(key, revisionMarker);
      const pageHandles = new Set<HistoryRestoreHandle>();
      const pageSettlers = new Set<(page: LoadedHistoryPage | null) => void>();
      let disposed = false;
      const handle: HistoryRestoreHandle = {
        generation: 0,
        dispose: () => {
          if (disposed) return;
          disposed = true;
          useSubagentStore.getState().finishHistoryRestore(sid, subagentId);
          for (const settlePending of pageSettlers) {
            settlePending(null);
          }
          pageSettlers.clear();
          for (const pageHandle of pageHandles) {
            pageHandle.dispose();
          }
          pageHandles.clear();
        },
      };
      subagentHistoryRestoreHandlesRef.current.set(key, handle);
      useSubagentStore.getState().beginHistoryRestore(sid, subagentId);

      const fetchSubagentHistoryPage = (
        pageIdx: number,
        fallbackTotalPages: number,
      ): Promise<LoadedHistoryPage | null> => new Promise((resolve) => {
        if (disposed) {
          resolve(null);
          return;
        }

        let settled = false;
        let pageHandle: HistoryRestoreHandle | null = null;
        const settle = (page: LoadedHistoryPage | null) => {
          if (settled) return;
          settled = true;
          pageSettlers.delete(settle);
          if (pageHandle) pageHandles.delete(pageHandle);
          resolve(page);
        };
        pageSettlers.add(settle);

        pageHandle = fetchHistoryPage({
          sessionId: sid,
          subagentId,
          pageIdx,
          onReady: (result: FetchHistoryPageResult) => {
            settle({
              pageIdx,
              totalPages: result.totalPages ?? fallbackTotalPages,
              result,
            });
          },
          onEmpty: (totalPages) => {
            if (pageIdx > 1) {
              settle(null);
              return;
            }
            settle({
              pageIdx,
              totalPages: totalPages ?? fallbackTotalPages,
              result: null,
            });
          },
          onTimeout: () => {
            settle(null);
          },
          onError: (message) => console.warn('[subagent.history]', message),
        });
        pageHandles.add(pageHandle);
        void request(HISTORY_GET_METHOD, {
          session_id: sid,
          subagent_id: subagentId,
          page_idx: pageIdx,
        }).catch((error) => {
          pageHandle?.dispose();
          settle(null);
          console.warn('[subagent.history] request failed', error);
        });
      });

      const restorePages = async () => {
        const cleanup = () => {
          handle.dispose();
          if (subagentHistoryRestoreHandlesRef.current.get(key) === handle) {
            subagentHistoryRestoreHandlesRef.current.delete(key);
          }
        };
        let hasSubagentHistory = false;
        const applyPage = (page: LoadedHistoryPage) => {
          const items = page.result?.subagentReplay ?? [];
          if (items.length > 0) {
            hasSubagentHistory = true;
            applySubagentHistoryReplay(sid, items);
          }
        };
        const hasSubagentFinal = () => {
          const currentRuntime = useSubagentStore.getState().getRuntime(sid);
          return Object.values(currentRuntime?.turnsBySubagentId[subagentId] ?? {})
            .some(turn => turn.result?.source === 'transcript');
        };

        const firstPage = await fetchSubagentHistoryPage(1, 1);
        if (disposed || !firstPage) {
          cleanup();
          return;
        }
        applyPage(firstPage);

        const prefetchOutcome = await prefetchHistoryPages({
          initialLoadedPages: 1,
          initialTotalPages: firstPage.totalPages,
          isCurrent: () => !disposed,
          fetchPage: (pageIdx, totalPages) => fetchSubagentHistoryPage(pageIdx, totalPages),
          applyPage,
          waitForNextPaint: async () => {},
        });
        if (prefetchOutcome === 'completed' && firstPage.totalPages === 1 && !hasSubagentFinal()) {
          const fallbackPage = await fetchSubagentHistoryPage(2, 2);
          if (fallbackPage) {
            applyPage(fallbackPage);
            await prefetchHistoryPages({
              initialLoadedPages: 2,
              initialTotalPages: fallbackPage.totalPages,
              isCurrent: () => !disposed,
              fetchPage: (pageIdx, totalPages) => fetchSubagentHistoryPage(pageIdx, totalPages),
              applyPage,
              waitForNextPaint: async () => {},
            });
          }
        }
        if (disposed || prefetchOutcome !== 'completed') {
          cleanup();
          return;
        }
        if (!hasSubagentHistory && firstPage.result === null) {
          useSubagentStore.getState().dropCachedSubagent(
            sid,
            subagentId,
            expectedRevision,
            expectedUpdatedAt,
          );
        }
        cleanup();
      };

      void restorePages().catch((error) => {
        handle.dispose();
        if (subagentHistoryRestoreHandlesRef.current.get(key) === handle) {
          subagentHistoryRestoreHandlesRef.current.delete(key);
        }
        console.warn('[subagent.history] restore failed', error);
      });
    }
  }, [applySubagentHistoryReplay, request]);

  const applyRecoveredSubagentToolHistory = useCallback((sid: string, items: HistoryToolReplayItem[]) => {
    const subagentStore = useSubagentStore.getState();
    const mergedToolReplay = mergeHistoryToolReplayItems(
      subagentToolReplayBySessionRef.current.get(sid) ?? [],
      items,
    );
    subagentToolReplayBySessionRef.current.set(sid, mergedToolReplay);
    const recoveredItems = recoverSubagentToolHistory(mergedToolReplay, sid);
    for (const recovered of recoveredItems) {
      const event = normalizeSubagentStatusEvent({ ...recovered.subagent, session_id: sid });
      if (event && event.subagent.parent_session_id === sid) {
        subagentStore.applyHistoryEvent(sid, event);
        if (event.subagent.status === 'closed') {
          subagentStore.applyToolStatus(
            sid,
            event.subagent.subagent_id,
            'closed',
            event.subagent.updated_at,
            event.subagent.task_description,
          );
        }
      }
      const recoveredSubagentId = typeof recovered.subagent.subagent_id === 'string'
        ? recovered.subagent.subagent_id.trim()
        : '';
      if (!recoveredSubagentId) continue;
      for (const turn of recovered.turns ?? []) {
        subagentStore.applyTurn(
          sid,
          recoveredSubagentId,
          turn.task_id,
          turn.task_description,
          turn.started_at,
        );
      }
      if (recovered.result) {
        subagentStore.applyResult(sid, recovered.result);
      }
    }
    if (recoveredItems.length > 0) {
      setSingleAgentPanelActiveTab('subagents');
    }
    restoreSubagentHistory(sid);
  }, [restoreSubagentHistory, setSingleAgentPanelActiveTab]);

  const applyHistoryPageResult = useCallback((sid: string, result: FetchHistoryPageResult) => {
    // 只 stamp 徽章：merge 完成卡只适合整页 replace（首次 history 恢复）。
    // 这里若再 merge，localStorage 里的完成卡不在本页 messages 里就会被再次注入，
    // prepend 又不按 id 去重，导致完成卡重复。
    prependMessages(sid, stampGoalObjectiveMessages(sid, result.messages));
    for (const item of result.toolReplay) {
      if (item.kind === 'tool_call') {
        const n = normalizeToolCallPayload(item.payload);
        addToolCall(
          sid,
          {
            id: n.id,
            name: n.name,
            arguments: n.arguments,
            description: n.description,
            formatted_args: n.formatted_args,
            display_name: n.display_name,
            memberName: n.memberName,
          },
          { startedAt: item.at }
        );
      } else {
        const n = normalizeToolResultPayload(item.payload);
        addToolResult(
          sid,
          {
            toolName: n.toolName,
            result: n.result,
            success: n.success,
            ...(n.pending ? { pending: true } : {}),
            toolCallId: n.toolCallId,
            summary: n.summary,
            skillTree: n.skillTree,
            ...(n.timedOut ? { timedOut: true } : {}),
            ...(n.beamSearch ? { beamSearch: n.beamSearch } : {}),
          },
          { updatedAt: item.at }
        );
      }
    }
    if (result.subagentReplay.length > 0) {
      applySubagentHistoryReplay(sid, result.subagentReplay);
    }
    if (result.toolReplay.length > 0) {
      applyRecoveredSubagentToolHistory(sid, result.toolReplay);
    }
    settleHistoricalToolExecutions(sid);

    const harnessStore = useHarnessStore.getState();
    const harnessRuntime = harnessStore.getRuntime(sid);
    for (const item of result.harnessReplay) {
      if (item.kind === 'harness_message') {
        const content = typeof item.payload.content === 'string' ? item.payload.content : '';
        const stage = typeof item.payload.stage === 'string' ? item.payload.stage : undefined;
        if (content) {
          harnessStore.addHarnessMessage(sid, content, stage);
          if (stage) {
            const existingStage = harnessRuntime?.stageResults.find((s) => s.stage === stage);
            if (existingStage?.status !== 'running') {
              harnessStore.updateStageResult(sid, {
                stage,
                stageLabel: content,
                status: 'running',
                messages: [],
                metrics: {},
              });
            }
          }
        }
      } else if (item.kind === 'harness_stage_result') {
        const stage = typeof item.payload.stage === 'string' ? item.payload.stage : '';
        const status = typeof item.payload.status === 'string' ? item.payload.status : 'success';
        const error = typeof item.payload.error === 'string' ? item.payload.error : undefined;
        const messages = Array.isArray(item.payload.messages) ? item.payload.messages : [];
        const metrics = item.payload.metrics || {};
        if (stage) {
          harnessStore.updateStageResult(sid, {
            stage,
            status: status as 'success' | 'failed' | 'timeout',
            error,
            messages,
            metrics,
          });
        }
      }
    }

    if (result.reasoningReplay.length > 0) {
      const store = useChatStore.getState();
      const current = store.runtimes[sid]?.reasoningSegments ?? [];
      const currentItems = current.map((segment) => ({
        at: new Date(segment.startedAt + 1).toISOString(),
        text: segment.text,
        // live 内存里的真实末帧时刻并入 replay，刷新重建后耗时终点不丢。
        updatedAt: segment.updatedAt,
      }));
      store.restoreReasoningSegments(sid, [...result.reasoningReplay, ...currentItems]);
    }
  }, [addToolCall, addToolResult, applyRecoveredSubagentToolHistory, applySubagentHistoryReplay, prependMessages, settleHistoricalToolExecutions]);

  const fetchHistoryPageResult = useCallback(async (
    sid: string,
    pageIdx: number,
    fallbackTotalPages: number
  ): Promise<LoadedHistoryPage | null> => {
    const pageKey = `${sid}:${pageIdx}`;
    const existingPromise = historyPagePromisesRef.current.get(pageKey);
    if (existingPromise) return existingPromise;

    const promise = new Promise<LoadedHistoryPage | null>((resolve) => {
      let settled = false;
      const settleCanceled = () => settle(null);
      const settle = (page: LoadedHistoryPage | null) => {
        if (settled) return;
        settled = true;
        if (historyPageCancelRef.current.get(pageKey) === settleCanceled) {
          historyPageCancelRef.current.delete(pageKey);
        }
        historyPageHandlesRef.current.delete(pageKey);
        historyPagePromisesRef.current.delete(pageKey);
        resolve(page);
      };
      historyPageCancelRef.current.set(pageKey, settleCanceled);

      const pageHandle = fetchHistoryPage({
        sessionId: sid,
        pageIdx,
        onReady: (result) => {
          const totalPages = result.totalPages ?? fallbackTotalPages;
          settle({ pageIdx, totalPages, result });
        },
        onEmpty: (emptyTotalPages) => {
          if (pageIdx > 1) {
            settle(null);
            return;
          }
          const totalPages = emptyTotalPages ?? fallbackTotalPages;
          settle({ pageIdx, totalPages, result: null });
        },
        onTimeout: () => settle(null),
        onError: (message) => {
          console.warn('[history.page]', message);
        },
      });
      historyPageHandlesRef.current.set(pageKey, pageHandle);

      void request(HISTORY_GET_METHOD, {
        session_id: sid,
        page_idx: pageIdx,
      }).catch((error) => {
        pageHandle.dispose();
        if (historyPageHandlesRef.current.get(pageKey) === pageHandle) {
          historyPageHandlesRef.current.delete(pageKey);
        }
        console.error('Failed to load older history:', error);
        settle(null);
      });
    });
    historyPagePromisesRef.current.set(pageKey, promise);
    return promise;
  }, [request]);

  const applyLoadedHistoryPage = useCallback((sid: string, page: LoadedHistoryPage) => {
    if (page.result) {
      applyHistoryPageResult(sid, page.result);
    }
    setHistoryPagerMeta(sid, {
      loadedPages: page.pageIdx,
      totalPages: page.totalPages,
    });
  }, [applyHistoryPageResult, setHistoryPagerMeta]);

  const startBackgroundHistoryPrefetch = useCallback((sid: string, initialLoadedPages: number, initialTotalPages: number) => {
    if (initialLoadedPages >= initialTotalPages || historyLoadingSessionsRef.current.has(sid)) {
      return;
    }
    const token = (historyBackgroundPrefetchTokensRef.current.get(sid) ?? 0) + 1;
    historyBackgroundPrefetchTokensRef.current.set(sid, token);
    historyLoadingSessionsRef.current.add(sid);
    setHistoryRetryAvailable(sid, false);
    if (sessionIdRef.current === sid) {
      setHistoryPrepending(true);
    }

    void (async () => {
      try {
        const outcome = await prefetchHistoryPages({
          initialLoadedPages,
          initialTotalPages,
          isCurrent: () => token === historyBackgroundPrefetchTokensRef.current.get(sid),
          fetchPage: (pageIdx, totalPages) =>
            fetchHistoryPageResult(sid, pageIdx, totalPages),
          applyPage: (page) => {
            applyLoadedHistoryPage(sid, page);
          },
          waitForNextPaint,
        });
        if (
          outcome === 'failed' &&
          token === historyBackgroundPrefetchTokensRef.current.get(sid)
        ) {
          setHistoryRetryAvailable(sid, true);
        }
      } finally {
        historyLoadingSessionsRef.current.delete(sid);
        if (sessionIdRef.current === sid) {
          setHistoryPrepending(false);
        }
      }
    })();
  }, [
    applyLoadedHistoryPage,
    fetchHistoryPageResult,
    setHistoryRetryAvailable,
  ]);

  const upsertSessionMetadata = useCallback((session: Session, options: { setCurrent?: boolean } = {}) => {
    const sessionStore = useSessionStore.getState();
    const exists = sessionStore.sessions.some((item) => item.session_id === session.session_id);
    if (exists) {
      sessionStore.updateSession(session.session_id, session);
    } else {
      sessionStore.addSession(session);
    }
    if (options.setCurrent) {
      sessionStore.setCurrentSession(session);
    }
  }, []);

  const loadSessionMetadata = useCallback(async (targetSessionId: string): Promise<Session | null> => {
    try {
      const session = await request<Session>('session.get_metadata', {
        session_id: targetSessionId,
      });
      upsertSessionMetadata(session, { setCurrent: sessionIdRef.current === targetSessionId });
      useWorkspaceStore.getState().upsertSession(session);
      if (sessionIdRef.current === targetSessionId) {
        setMissingSessionId((current) => (current === targetSessionId ? null : current));
        // 同 handleRestoreSession：拿到后端 metadata 里的 model 后还原 selectedModelName，
        // 覆盖"targetSession 为空、走 loadSessionMetadata"这条恢复路径（如从 cron 触发
        // 会话列表点进来的占位 session 之后补全元数据的场景，bug002）。
        if (session?.model) {
          useSessionStore.getState().setSelectedModelName(targetSessionId, session.model);
        }
      }
      return session;
    } catch (error) {
      console.warn('Failed to fetch session metadata:', error);
      if (sessionIdRef.current === targetSessionId) {
        setMissingSessionId(targetSessionId);
      }
      return null;
    }
  }, [request, upsertSessionMetadata]);

  // 获取服务端配置（通过 WS 方法）
  const fetchConfig = useCallback(async () => {
    try {
      const config = await request<Record<string, unknown>>('config.get');
      setA2UIFeatureEnabled(normalizeA2UIEnabled(config.a2ui_enabled));
      setServerConfig(config);
      setConfigError(null);
      if (!modelSetupGuideEvaluatedRef.current) {
        modelSetupGuideEvaluatedRef.current = true;
        if (!oauthNavRestoredRef.current && (shouldPreviewModelSetupGuide() || isSetupGuideEnabled(config.setup_guide_enabled))) {
          setActiveNav('chat');
          setModelSetupGuideManual(false);
          setModelSetupGuideStep(0);
        }
      }
    } catch (error) {
      console.error('Failed to fetch config:', error);
      setServerConfig(null);
      setConfigError(t('app.configError'));
    }
    // 同步获取多模型列表
    try {
      const resp = await request<{ models: ModelEntry[]; active_model: string }>('models.list');
      if (resp?.models) {
        setAvailableModels(resp.models, resp.active_model);
      }
    } catch (error) {
      console.warn('Failed to fetch models list:', error);
    }
  }, [request, t, setAvailableModels]);

  useEffect(() => {
    if (!FEATURE_APP_UPDATER_UI || !isConnected || startupUpdateCheckRef.current) {
      return;
    }
    startupUpdateCheckRef.current = true;
    const timeoutId = window.setTimeout(() => {
      void request('updater.check', { manual: false })
        .then((payload) => {
          window.dispatchEvent(new CustomEvent('jiuwenswarm:updater-status', { detail: payload }));
        })
        .catch((updateError) => {
          console.warn('Startup updater check failed:', updateError);
        });
    }, 5000);
    return () => {
      window.clearTimeout(timeoutId);
    };
  }, [isConnected, request]);

  const clearRestartAutoCloseTimer = useCallback(() => {
    if (restartAutoCloseTimerRef.current != null) {
      window.clearTimeout(restartAutoCloseTimerRef.current);
      restartAutoCloseTimerRef.current = null;
    }
  }, []);

  const closeRestartModal = useCallback(() => {
    clearRestartAutoCloseTimer();
    setRestartModalOpen(false);
    setRestartSuccess(false);
    setRestartSeenDisconnect(false);
    setAppliedWithoutRestart(false);
  }, [clearRestartAutoCloseTimer]);

  const clearSaveToastTimer = useCallback(() => {
    if (saveToastTimerRef.current != null) {
      window.clearTimeout(saveToastTimerRef.current);
      saveToastTimerRef.current = null;
    }
  }, []);

  const clearProactiveToastTimer = useCallback(() => {
    if (proactiveToastTimerRef.current != null) {
      window.clearTimeout(proactiveToastTimerRef.current);
      proactiveToastTimerRef.current = null;
    }
  }, []);

  const showSaveToast = useCallback(() => {
    setSaveToastVisible(true);
    clearSaveToastTimer();
    saveToastTimerRef.current = window.setTimeout(() => {
      setSaveToastVisible(false);
      saveToastTimerRef.current = null;
    }, 3000);
  }, [clearSaveToastTimer]);

  const securityAlertTimerRef = useRef<number | null>(null);

  useEffect(() => {
    const handleSecurityAlert = (e: CustomEvent) => {
      setSecurityAlertContent(e.detail.message);
      setSecurityAlertVisible(true);
      if (securityAlertTimerRef.current) {
        clearTimeout(securityAlertTimerRef.current);
      }
      securityAlertTimerRef.current = window.setTimeout(() => {
        setSecurityAlertVisible(false);
        securityAlertTimerRef.current = null;
      }, 5000);
    };
    window.addEventListener('security-alert', handleSecurityAlert as EventListener);
    return () => {
      window.removeEventListener('security-alert', handleSecurityAlert as EventListener);
      if (securityAlertTimerRef.current) clearTimeout(securityAlertTimerRef.current);
    };
  }, []);

  const handleConfigChanged = useCallback(() => {
    void fetchConfig();
  }, [fetchConfig]);

  const handleSettingsHasChangesChange = useCallback((hasChanges: boolean) => {
    settingsHasChangesRef.current = hasChanges;
  }, []);

  const handleModelsRefresh = useCallback(async () => {
    try {
      const resp = await request<{ models: ModelEntry[]; active_model: string }>('models.list');
      if (resp?.models) {
        setAvailableModels(resp.models, resp.active_model);
      }
    } catch (error) {
      console.warn('Failed to refresh models list:', error);
    }
  }, [request, setAvailableModels]);

  const detectExternalCli = useCallback(async (cliAgent: ExternalCliAgentKind, cliPath?: string) => {
    return request<{
      cli_agent: ExternalCliAgentKind;
      status: "ok" | "warning" | "missing" | "unsupported" | "unavailable";
      path?: string;
      version?: string;
      reference_version?: string;
      message?: string;
    }>("external_cli.detect", {
      cli_agent: cliAgent,
      cli_path: cliPath || "",
    });
  }, [request]);

  const selectExternalCliPath = useCallback(async (cliAgent: ExternalCliAgentKind, initialPath?: string) => {
    const desktopPicker = window.pywebview?.api?.select_local_file_path;
    const title = t("config.externalCli.selectFileTitle", { agent: cliAgent });
    if (typeof desktopPicker === "function") {
      const selectedPath = await desktopPicker(initialPath || "", title);
      return selectedPath || null;
    }
    const payload = await request<{ path?: string | null; cancelled?: boolean }>(
      "path.select_file",
      {
        cli_agent: cliAgent,
        initial_path: initialPath || "",
        title,
      },
      { timeoutMs: 10 * 60 * 1000 },
    );
    if (payload?.cancelled || !payload?.path) {
      return null;
    }
    return payload.path;
  }, [request, t]);

  const getExternalCliDependencyInstallStatus = useCallback(
    async (cliAgent: ExternalCliAgentKind): Promise<ExternalCliDependencyInstallStatus> => {
      return request<ExternalCliDependencyInstallStatus>(
        "external_cli.install_status",
        { cli_agent: cliAgent },
        { timeoutMs: 10 * 1000 },
      );
    },
    [request],
  );

  const trackExternalCliDependencyInstalls = useCallback(
    (statuses: ExternalCliInstallStatuses) => {
      setExternalCliInstallStatuses(statuses);
      setExternalCliInstallDialogOpen(true);
    },
    [],
  );

  const updateExternalCliInstallStatus = useCallback(
    (cliAgent: ExternalCliAgentKind, status: ExternalCliDependencyInstallStatus) => {
      setExternalCliInstallStatuses((current) => ({ ...current, [cliAgent]: status }));
    },
    [],
  );

  const savePermissionSilent = useCallback(async (updates: Record<string, string>) => {
    try {
      await request<{ updated?: string[]; applied_without_restart?: boolean }>('config.set', updates);
      setServerConfig((prev) => {
        if (!prev) return updates;
        return { ...prev, ...updates };
      });
    } catch (error) {
      console.error('Failed to save permission:', error);
      setRestartModalOpen(true);
      setRestartSuccess(false);
      setRestartSeenDisconnect(false);
      setAppliedWithoutRestart(false);
    }
  }, [request]);

  const applyConfigSaveUiState = useCallback((appliedWithoutRestart: boolean) => {
    setConfigError(null);
    setRestartModalOpen(true);
    setRestartSuccess(false);
    setRestartSeenDisconnect(false);
    setAppliedWithoutRestart(appliedWithoutRestart);
    clearRestartAutoCloseTimer();
    if (appliedWithoutRestart) {
      setRestartSuccess(true);
      restartAutoCloseTimerRef.current = window.setTimeout(() => {
        closeRestartModal();
      }, 5000);
    }
  }, [clearRestartAutoCloseTimer, closeRestartModal]);

  const saveSymphonyEnabled = useCallback(async (enabled: boolean) => {
    const updates = { symphony_enabled: enabled ? 'true' : 'false' };
    const result = await request<{ updated?: string[]; applied_without_restart?: boolean }>(
      'config.set',
      updates,
    );
    setServerConfig((prev) => ({ ...(prev ?? {}), ...updates }));
    setConfigError(null);
    const appliedWithoutRestart = result?.applied_without_restart === true;
    if (!appliedWithoutRestart) {
      applyConfigSaveUiState(false);
    }
    return appliedWithoutRestart;
  }, [applyConfigSaveUiState, request]);

  useEffect(() => {
    if (!restartModalOpen || restartSuccess) {
      return;
    }
    if (!isConnected) {
      setRestartSeenDisconnect(true);
      return;
    }
    if (restartSeenDisconnect && isConnected) {
      setRestartSuccess(true);
      clearRestartAutoCloseTimer();
      restartAutoCloseTimerRef.current = window.setTimeout(() => {
        closeRestartModal();
      }, 5000);
    }
  }, [
    clearRestartAutoCloseTimer,
    closeRestartModal,
    isConnected,
    restartModalOpen,
    restartSeenDisconnect,
    restartSuccess,
  ]);

  useEffect(() => {
    return () => {
      clearRestartAutoCloseTimer();
      clearSaveToastTimer();
      clearProactiveToastTimer();
    };
  }, [clearProactiveToastTimer, clearRestartAutoCloseTimer, clearSaveToastTimer]);

  useEffect(() => {
    const message = proactiveNotificationMessage?.trim();
    if (!message) return;
    setProactiveToastMessage(message);
    setProactiveToastVisible(true);
    clearProactiveToastTimer();
    proactiveToastTimerRef.current = window.setTimeout(() => {
      setProactiveToastVisible(false);
      setProactiveNotification(null);
      proactiveToastTimerRef.current = null;
    }, 8000);
  }, [clearProactiveToastTimer, proactiveNotificationMessage, setProactiveNotification]);

  useEffect(() => {
    if (!isConnected || initialDataLoaded) {
      return;
    }
    void (async () => {
      await fetchConfig();
      setInitialDataLoaded(true);
    })();
  }, [fetchConfig, initialDataLoaded, isConnected]);

  useEffect(() => {
    if (!isConnected || !routeSessionId) {
      setMissingSessionId(null);
      return;
    }
    void loadSessionMetadata(routeSessionId);
  }, [isConnected, loadSessionMetadata, routeSessionId]);

  // 聊天处理完成后更新本地会话元数据，以便拾取自动生成的标题等更新。
  const prevProcessingBySessionRef = useRef(new Map<string, boolean>());
  useEffect(() => {
    if (!sessionId || sessionId === NEW_CONVERSATION_ID) {
      return;
    }

    const prevProcessing = prevProcessingBySessionRef.current.get(sessionId) ?? false;
    if (prevProcessing && !isProcessing) {
      if (hasPendingQuestion) {
        return;
      }
      void (async () => {
        const session = await loadSessionMetadata(sessionId);
        if (session) {
          useWorkspaceStore.getState().upsertSession(session);
        }
      })();
    }
    prevProcessingBySessionRef.current.set(sessionId, isProcessing);
  }, [sessionId, isProcessing, hasPendingQuestion, loadSessionMetadata]);

  // 连接成功后从 config.yaml 同步 preferred_language 到前端显示
  useEffect(() => {
    if (!isConnected) return;
    void webRequest<{ preferred_language?: string }>('locale.get_conf')
      .then((payload) => {
        const lang = payload?.preferred_language;
        if (lang === 'zh' || lang === 'en') {
          i18n.changeLanguage(lang);
        }
      })
      .catch(() => {});
  }, [isConnected]);

  // 当会话 ID 变化或页面加载时，自动加载历史会话
  useEffect(() => {
    if (!isConnected || !sessionId || sessionId === NEW_CONVERSATION_ID) return;
    
    if (sessionIdsCreatedInThisPageRef.current.has(sessionId)) {
      setHistoryPagerMeta(sessionId, null);
      setHistoryLoadingMore(false);
      setLoadingHistory(sessionId, false);
      return;
    }

    // 新建会话时跳过历史加载
    const isNew = useChatStore.getState().runtimes[sessionId]?.isNewSession ?? false;
    if (isNew) {
      useChatStore.getState().setNewSession(sessionId, false);
      setHistoryPagerMeta(sessionId, null);  // 新会话无历史，不显示分页栏
      setLoadingHistory(sessionId, false);
      return;
    }

    // 当前页面新建的会话已在上方复用实时内存数据；对于其他会话，
    // historyPagerMeta 表示已完成 history 首屏恢复，可直接复用并继续补齐剩余分页。
    const existingRuntime = useChatStore.getState().getRuntime(sessionId);
    const subagentRuntime = useSubagentStore.getState().getRuntime(sessionId);
    const hasStorageOnlySubagentCache = Object.keys(subagentRuntime?.cacheOnlySubagentIds ?? {}).length > 0;
    if (existingRuntime && existingRuntime.historyPagerMeta) {
      if (hasStorageOnlySubagentCache) {
        useSubagentStore.getState().removeRuntime(sessionId);
      } else {
        setLoadingHistory(sessionId, false);
        startBackgroundHistoryPrefetch(
          sessionId,
          existingRuntime.historyPagerMeta.loadedPages,
          existingRuntime.historyPagerMeta.totalPages
        );
        return;
      }
    }

    // 清理之前的历史加载句柄
    disposeInFlightHistoryHandles(sessionId);
    setHistoryPagerMeta(sessionId, null);
    setHistoryLoadingMore(false);
    
    setLoadingHistory(sessionId, true);
    // 开始历史会话加载
    const restoreHandle = beginHistoryRestore({
      sessionId: sessionId,
      onReady: (messages, totalPages) => {
        historyRestoreFromPanelHintRef.current = false;
        // "目标完成"回显消息纯前端合成，从未写进后端 session 历史，history.get 拉回来的
        // messages 里不会有它——按时间戳把本地持久化的记录补回去，见
        // hooks/useWebSocket.ts 的 applyIncomingGoal/mergePersistedGoalCompletionMessages。
        // 同时给命中"曾经设置过目标"的 user 消息回填 isGoalObjectiveMessage 徽章标记，
        // 见 stampGoalObjectiveMessages。
        replaceHistoryMessages(
          sessionId,
          stampGoalObjectiveMessages(sessionId, mergePersistedGoalCompletionMessages(sessionId, messages))
        );
        const restoredTotalPages = totalPages ?? 1;
        setHistoryPagerMeta(sessionId, {
          loadedPages: 1,
          totalPages: restoredTotalPages,
        });
        setLoadingHistory(sessionId, false);
        startBackgroundHistoryPrefetch(sessionId, 1, restoredTotalPages);
        queueMicrotask(() => {
          if (historyRestoreHandlesRef.current.get(sessionId) === restoreHandle) {
            historyRestoreHandlesRef.current.delete(sessionId);
          }
        });
      },
      onEmpty: (emptyTotalPages) => {
        replaceHistoryMessages(sessionId, mergePersistedGoalCompletionMessages(sessionId, []));
        const restoredTotalPages = emptyTotalPages ?? 1;
        setHistoryPagerMeta(sessionId, {
          loadedPages: 1,
          totalPages: restoredTotalPages,
        });
        if (historyRestoreFromPanelHintRef.current) {
          historyRestoreFromPanelHintRef.current = false;
          addMessage(sessionId, {
            id: `history-restore-empty-${Date.now()}`,
            role: 'system',
            content: tRef.current('sessions.restoreEmpty'),
            timestamp: new Date().toISOString(),
          });
        }
        setLoadingHistory(sessionId, false);
        startBackgroundHistoryPrefetch(sessionId, 1, restoredTotalPages);
        if (historyRestoreHandlesRef.current.get(sessionId) === restoreHandle) {
          historyRestoreHandlesRef.current.delete(sessionId);
        }
      },
      onToolReplay: (items) => {
        for (const item of items) {
          for (const result of normalizeSubagentWaitResults(item.payload)) {
            useSubagentStore.getState().applyResult(sessionId, result);
          }
          if (item.kind === 'tool_call') {
            const n = normalizeToolCallPayload(item.payload);
            addToolCall(
              sessionId,
              {
                id: n.id,
                name: n.name,
                arguments: n.arguments,
                description: n.description,
                formatted_args: n.formatted_args,
                display_name: n.display_name,
                memberName: n.memberName,
              },
              { startedAt: item.at }
            );
          } else {
            const n = normalizeToolResultPayload(item.payload);
            addToolResult(
              sessionId,
              {
                toolName: n.toolName,
                result: n.result,
                success: n.success,
                ...(n.pending ? { pending: true } : {}),
                toolCallId: n.toolCallId,
                summary: n.summary,
                skillTree: n.skillTree,
                ...(n.timedOut ? { timedOut: true } : {}),
                ...(n.beamSearch ? { beamSearch: n.beamSearch } : {}),
              },
              { updatedAt: item.at }
            );
          }
        }
        applyRecoveredSubagentToolHistory(sessionId, items);
        settleHistoricalToolExecutions(sessionId);
      },
      onHarnessReplay: (items: HistoryHarnessReplayItem[]) => {
        const harnessStore = useHarnessStore.getState();
        const harnessRuntime = harnessStore.getRuntime(sessionId);
        for (const item of items) {
          if (item.kind === 'harness_message') {
            const content = typeof item.payload.content === 'string' ? item.payload.content : '';
            const stage = typeof item.payload.stage === 'string' ? item.payload.stage : undefined;
            if (content) {
              harnessStore.addHarnessMessage(sessionId, content, stage);
              // Update stage result with running status and label from message
              if (stage) {
                const existingStage = harnessRuntime?.stageResults.find((s) => s.stage === stage);
                if (existingStage?.status !== 'running') {
                  harnessStore.updateStageResult(sessionId, {
                    stage,
                    stageLabel: content,
                    status: 'running',
                    messages: [],
                    metrics: {},
                  });
                }
              }
            }
          } else if (item.kind === 'harness_stage_result') {
            const stage = typeof item.payload.stage === 'string' ? item.payload.stage : '';
            const status = typeof item.payload.status === 'string' ? item.payload.status : 'success';
            const error = typeof item.payload.error === 'string' ? item.payload.error : undefined;
            const messages = Array.isArray(item.payload.messages) ? item.payload.messages : [];
            const metrics = item.payload.metrics || {};
            if (stage) {
              harnessStore.updateStageResult(sessionId, {
                stage,
                status: status as 'success' | 'failed' | 'timeout',
                error,
                messages,
                metrics,
              });
            }
          }
        }
      },
      onSubagentReplay: (items) => {
        applySubagentHistoryReplay(sessionId, items);
      },
      onReasoningReplay: (items) => {
        restoreReasoningSegments(sessionId, items);
      },
      onCompactionReplay: (info) => {
        // 回显「本轮完成上下文压缩 N 次」：恢复进 chatStore，渲染与实时事件同一处
        const chatStore = useChatStore.getState();
        chatStore.ensureRuntime(sessionId);
        chatStore.setContextCompressionStatus(sessionId, undefined, {
          count: info.count,
          summaries: info.summaries,
        });
      },
      onError: (message) => {
        console.warn('[history.restore]', message);
        setLoadingHistory(sessionId, false);
      },
    });
    historyRestoreHandlesRef.current.set(sessionId, restoreHandle);

    // 调用历史会话接口
    void (async () => {
      try {
        await request(HISTORY_GET_METHOD, {
          session_id: sessionId,
          page_idx: 1,
        });
      } catch (error) {
        historyRestoreFromPanelHintRef.current = false;
        restoreHandle.dispose();
        if (historyRestoreHandlesRef.current.get(sessionId) === restoreHandle) {
          historyRestoreHandlesRef.current.delete(sessionId);
        }
        // 发生错误时，设置 historyPagerMeta 为 null，显示欢迎信息
        setHistoryPagerMeta(sessionId, null);
        console.error('Failed to load history:', error);
        setLoadingHistory(sessionId, false);
        // 忽略 "invalid page_idx or session history not found" 错误，因为这是新会话的正常情况
        const errorMessage = error instanceof Error ? error.message : String(error);
        if (sessionIdRef.current === sessionId && !errorMessage.includes('invalid page_idx or session history not found')) {
          clearMessages(sessionId);
          addMessage(sessionId, {
            id: `history-load-failed-${Date.now()}`,
            role: 'system',
            content: tRef.current('sessions.errors.restoreFailed', { sessionId }),
            timestamp: new Date().toISOString(),
          });
        }
      }
    })();
  }, [
    isConnected,
    sessionId,
    historyBootstrapKey,
    request,
    addMessage,
    addToolCall,
    addToolResult,
    applyRecoveredSubagentToolHistory,
    applySubagentHistoryReplay,
    settleHistoricalToolExecutions,
    clearMessages,
    disposeInFlightHistoryHandles,
    setLoadingHistory,
    setHistoryPagerMeta,
    replaceHistoryMessages,
    restoreReasoningSegments,
    startBackgroundHistoryPrefetch,
  ]);

  useEffect(() => {
    if (!isConnected || !sessionId || sessionId === NEW_CONVERSATION_ID) return;
    restoreSubagentHistory(sessionId);
  }, [historyBootstrapKey, isConnected, restoreSubagentHistory, sessionId]);

  useEffect(() => {
    if (!isConnected || !sessionId || sessionId === NEW_CONVERSATION_ID || !subagentStatusSignature) return;
    const runtime = useSubagentStore.getState().getRuntime(sessionId);
    if (!Object.values(runtime?.subagentsById ?? {}).some((subagent) => subagent.status !== 'running')) return;
    restoreSubagentHistory(sessionId);
  }, [isConnected, restoreSubagentHistory, sessionId, subagentStatusSignature]);

  // 会话切换/页面加载时主动拉一次当前 Goal 状态（协议文档 v2 §11 推荐流程）——不然刷新页面
  // 后 GoalBar 要等下一次 goal.updated 推送才会重新出现，目标 paused/静默期时甚至会一直缺失
  // （2026-07-21 真机联调发现，见 backend-requests.md #1 末尾）。新会话（promoted from 'new'）
  // 同样可能已经有 Goal（欢迎页 armed 流程可以直接创建），不跳过。
  // get 完如果 status 是 active，按 §11 第4步再补发一次流式 resume——不是"目标被暂停了要恢复"，
  // 是"重新抢一次输出听筒"：切会话/刷新导致之前监听后端输出的那条连接断了，目标可能还在后台跑，
  // 这时候没人在听它的实时输出（chat.delta/chat.reasoning 等）。resume 对一个本来就 active 的
  // 目标发是幂等的（状态不会变），抢到听筒就能继续收到实时输出，抢不到收 runtime.accepted，
  // 都不算错误。
  useEffect(() => {
    if (!isConnected || !sessionId || sessionId === NEW_CONVERSATION_ID) return;
    void (async () => {
      await refreshGoal(sessionId);
      // 等 get 落地这段时间里用户可能已经切到别的会话，避免对着旧会话发 resume。
      if (sessionIdRef.current !== sessionId) return;
      const goal = useGoalStore.getState().runtimes[sessionId]?.goal;
      if (goal?.status === 'active') {
        void resumeGoal(sessionId);
      }
    })();
  }, [isConnected, sessionId, refreshGoal, resumeGoal]);

  const requestComposerFocus = useCallback(() => {
    setComposerFocusNonce((nonce) => nonce + 1);
  }, []);

  const enterNewConversation = useCallback((
    targetMode: AgentMode = mode,
    options: NewConversationOptions = {},
    lifecycle: { clearPreviousSession?: boolean } = {},
  ) => {
    const currentSessionId = sessionIdRef.current;
    const currentRuntime = useSessionStore.getState().getRuntime(currentSessionId);
    const pendingNewRuntime = useSessionStore.getState().getRuntime(NEW_CONVERSATION_ID);
    const shouldRestorePendingNewConversation =
      currentSessionId !== NEW_CONVERSATION_ID
      && pendingNewConversationRef.current
      && Boolean(pendingNewRuntime);
    newConversationPreviousSessionRef.current = resolvePendingPreviousSession({
      currentSessionId,
      currentMode: currentRuntime?.mode ?? mode,
      pending: newConversationPreviousSessionRef.current,
      newConversationId: NEW_CONVERSATION_ID,
      clear: lifecycle.clearPreviousSession,
    });
    // 返回尚未发送的新建任务时，恢复该临时会话自己的模式和模型；真正开始一个新任务时，
    // 仍固定使用配置的默认模型，不继承当前正式会话手动切换过的模型。
    // 默认模型列表尚未加载完成时兜底沿用当前会话的模型，避免新会话没有模型可用。
    const { mode: nextMode, selectedModelName } = resolveNewConversationEntrySettings(
      targetMode,
      useSessionStore.getState().defaultModelName,
      currentRuntime?.selectedModelName ?? null,
      shouldRestorePendingNewConversation ? pendingNewRuntime : null,
    );
    const selectedProject = options.project ?? useWorkspaceStore.getState().selectedProject;
    const projectDir = resolveNewConversationProjectDir(
      options.preserveProject,
      options.project?.project_dir,
      selectedProject?.project_dir,
    );
    disposeInFlightHistoryHandles(
      currentSessionId !== NEW_CONVERSATION_ID ? currentSessionId : undefined,
    );
    setHistoryLoadingMore(false);
    const pendingAgentSelection = shouldRestorePendingNewConversation
      && pendingNewRuntime?.agentSelectionIntent.kind === 'select'
      ? pendingNewRuntime.agentSelectionIntent
      : null;
    resetNewConversationRuntime({ mode: nextMode, selectedModelName, projectDir });
    if (pendingAgentSelection) {
      useSessionStore.getState().setAgentSelectionIntent(NEW_CONVERSATION_ID, pendingAgentSelection);
    }
    if (options.initialInputValue) {
      useChatStore.getState().setInputValue(NEW_CONVERSATION_ID, options.initialInputValue);
    }
    options.initialSelectedSkills?.forEach((skill) => useSessionStore.getState().addSelectedSkill(NEW_CONVERSATION_ID, skill));
    // 扩展详情页"使用"按钮跳转——除了带上 demo 示例文案，还要顺带把这个扩展的会话内启用
    // 开关打开，跟 initialInputValue 走的是同一条通道。
    options.initialEnabledPlugins?.forEach((id) => useSessionStore.getState().addEnabledPlugin(NEW_CONVERSATION_ID, id));
    options.initialEnabledMcps?.forEach((name) => useSessionStore.getState().addEnabledMcp(NEW_CONVERSATION_ID, name));
    if (options.preserveProject) {
      preserveSelectedProjectOnChatNewRef.current = true;
      newConversationProjectRef.current = selectedProject
        ? {
          project_id: selectedProject.project_id,
          project_dir: selectedProject.project_dir,
        }
        : null;
    } else {
      newConversationProjectRef.current = null;
      setSelectedProject(null);
    }
    sessionIdRef.current = NEW_CONVERSATION_ID;
    setSessionId(NEW_CONVERSATION_ID);
    setCurrentSession(null);
    setTeamAreaExpanded(false);
    setSingleAgentPanelExpanded(false);
    navigate({ kind: 'chat-new' });
    setActiveNav('chat');
    requestComposerFocus();
  }, [disposeInFlightHistoryHandles, mode, navigate, requestComposerFocus, setCurrentSession, setSelectedProject, setSingleAgentPanelExpanded, setTeamAreaExpanded]);

  // 监听从 SkillPanel 发来的"新建会话并插入技能"事件
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as { skillName: string; prefixText?: string; suffixText?: string; secondSkillName?: string; metadata?: Record<string, unknown> };
      enterNewConversation();
      // 存储 metadata，sendMessage 时随 chat.send 发送后清除（skill-creator 统一入口等场景）
      if (detail.metadata) {
        useSessionStore.getState().ensureRuntime(NEW_CONVERSATION_ID);
        useSessionStore.getState().setSessionMetadata(NEW_CONVERSATION_ID, detail.metadata);
      }
      // 延迟派发，确保 ChatPanel/InputArea 已挂载并注册了事件监听器
      setTimeout(() => {
        window.dispatchEvent(new CustomEvent('chat-input-insert-skill', {
          detail: { skillName: detail.skillName, prefixText: detail.prefixText, suffixText: detail.suffixText, secondSkillName: detail.secondSkillName }
        }));
      }, 0);
    };
    window.addEventListener('jiuwen:new-conversation', handler);
    return () => window.removeEventListener('jiuwen:new-conversation', handler);
  }, [enterNewConversation]);

  const handleNewSession = useCallback(async (options?: NewConversationOptions) => {
    enterNewConversation(mode, options);
  }, [enterNewConversation, mode]);

  // 切换模式
  const handleSwitchMode = useCallback((targetMode: AgentMode) => {
    const currentId = sessionIdRef.current;
    if (useChatStore.getState().getRuntime(currentId)?.isProcessing) return;
    if (currentId === NEW_CONVERSATION_ID) {
      setMode(NEW_CONVERSATION_ID, targetMode);
      return;
    }
    enterNewConversation(targetMode);
  }, [enterNewConversation, setMode]);

  const handleKVCInputIntent = useCallback((targetSessionId: string) => {
    // OFF must remain the ordinary JiuwenSwarm path: do not emit even the
    // best-effort prepare control request. AgentServer keeps its own gate as
    // a fail-closed boundary for stale or non-Web clients.
    if (!kvCacheAffinityEnabled) return;
    if (!targetSessionId || targetSessionId === NEW_CONVERSATION_ID) return;
    if (kvcPreparedInputSessionRef.current === targetSessionId) return;

    // Leading-edge intent: start prefetch on the first real insertion instead
    // of waiting until the user stops typing. InputArea reports beforeinput,
    // paste and input as browser-compatible fallbacks; this latch collapses
    // them into one control request for the current foreground visit.
    kvcPreparedInputSessionRef.current = targetSessionId;
    const runtime = useSessionStore.getState().getRuntime(targetSessionId);
    void request<{ scheduled?: boolean; outcome?: string }>('session.kvc.prepare', {
      session_id: targetSessionId,
      intent_id: generateUuidV4(),
      view_id: kvcViewIdRef.current,
      mode: runtime?.mode ?? mode,
    }).then((response) => {
      if (response?.outcome === 'failed'
          && kvcPreparedInputSessionRef.current === targetSessionId) {
        kvcPreparedInputSessionRef.current = null;
      }
    }).catch((error) => {
      // Allow the next editor event to retry when the control request itself
      // could not reach AgentServer. KVC remains an optional optimization.
      if (kvcPreparedInputSessionRef.current === targetSessionId) {
        kvcPreparedInputSessionRef.current = null;
      }
      console.debug('session.kvc.prepare skipped:', error);
    });
  }, [kvCacheAffinityEnabled, mode, request]);

  const handleUseAgent = useCallback((agentId: string) => {
    const currentSessionId = sessionIdRef.current || NEW_CONVERSATION_ID;
    const sessionStore = useSessionStore.getState();
    sessionStore.setAgentSelectionIntent(currentSessionId, { kind: 'select', id: agentId });
    sessionStore.setMode(currentSessionId, 'agent');
    setActiveNav('chat');
    requestComposerFocus();
  }, [requestComposerFocus]);

  const handleUseAgentPrompt = useCallback((agentId: string, prompt: string) => {
    enterNewConversation('agent', { initialInputValue: prompt });
    useSessionStore.getState().setAgentSelectionIntent(NEW_CONVERSATION_ID, { kind: 'select', id: agentId });
  }, [enterNewConversation]);

  const handleSendMessage = useCallback(async (content: string, mediaItems?: MediaItem[]) => {
    const currentSessionId = sessionIdRef.current;
    if (!currentSessionId) return;
    if (currentSessionId === NEW_CONVERSATION_ID) {
      const persistCommand = parsePersistSessionCommand(content);
      if (persistCommand.persistSession && !persistCommand.content) {
        window.alert(t('persistSession.textRequired'));
        return;
      }
      const messageContent = persistCommand.content;
      if (creatingSessionRef.current) return;
      creatingSessionRef.current = true;
      useChatStore.getState().setProcessing(NEW_CONVERSATION_ID, true);
      const newRuntime = useSessionStore.getState().getRuntime(NEW_CONVERSATION_ID);
      const runtimeSettings = {
        mode: newRuntime?.mode ?? mode,
        selectedModelName: useSessionStore.getState().getEffectiveModelName(NEW_CONVERSATION_ID),
        projectDir: newRuntime?.projectDirectory ?? null,
        persistSession: persistCommand.persistSession,
      };
      const baseWorkContext = getWorkContextForSession(NEW_CONVERSATION_ID);
      const preservedProject = newConversationProjectRef.current;
      const workContext = {
        project_id: baseWorkContext.project_id || preservedProject?.project_id,
        project_dir: baseWorkContext.project_dir || preservedProject?.project_dir,
        work_mode: useWorkspaceStore.getState().workMode,
      };
      try {
        const createParams: Record<string, unknown> = {
          create_token: generateUuidV4(),
          mode: runtimeSettings.mode,
          is_swarm: runtimeSettings.mode === 'team',
          title: createConversationTitle(messageContent).slice(0, 100),
          work_mode: workContext.work_mode,
          view_id: kvcViewIdRef.current,
          persist_session: runtimeSettings.persistSession,
        };
        const previousSession = newConversationPreviousSessionRef.current;
        if (previousSession) {
          createParams.previous_session_id = previousSession.sessionId;
          createParams.previous_mode = previousSession.mode;
        }
        if (runtimeSettings.selectedModelName) {
          createParams.model_name = runtimeSettings.selectedModelName;
        }
        if (workContext.project_id) {
          createParams.project_id = workContext.project_id;
        }
        if (workContext.project_dir) {
          createParams.project_dir = workContext.project_dir;
        }
        const created = await createConversationSession(request, createParams);
        const newSid = created.session_id;
        const createdSession = registerCreatedConversation(
          created.session_id,
          { ...runtimeSettings, persistSession: created.persist_session },
          Date.now(),
          messageContent,
          {
            project_id: created.project_id || workContext.project_id,
            project_dir: created.project_dir || workContext.project_dir,
            work_mode: created.work_mode || workContext.work_mode,
            persist_session: created.persist_session,
          },
        );
        // 迁移 'new' 会话的已选技能到新会话
        const pendingSkills = useSessionStore.getState().getRuntime(NEW_CONVERSATION_ID)?.selectedSkills ?? [];
        pendingSkills.forEach((skill) => useSessionStore.getState().addSelectedSkill(newSid, skill));
        useSessionStore.getState().clearSelectedSkills(NEW_CONVERSATION_ID);
        // 迁移 'new' 会话的 metadata 到新会话（skill-creator 统一入口等场景）
        // 必须在 removeRuntime 之前完成，否则 NEW 会话 runtime 会被清掉
        const pendingMetadata = useSessionStore.getState().getRuntime(NEW_CONVERSATION_ID)?.metadata;
        if (pendingMetadata) {
          useSessionStore.getState().setSessionMetadata(newSid, pendingMetadata);
          useSessionStore.getState().setSessionMetadata(NEW_CONVERSATION_ID, null);
        }
        // 同样搬家：欢迎页（'new'）上如果已经通过"+"菜单"扩展"面板开了某些插件/MCP 的会话内
        // 开关（或者是"使用"按钮带过来的 initialEnabledPlugins/initialEnabledMcps），真实
        // session_id 创建后要跟着过去，否则下面 removeRuntime('new') 会把这些选择直接冲掉。
        const pendingEnabledPlugins = useSessionStore.getState().getRuntime(NEW_CONVERSATION_ID)?.enabledPlugins ?? [];
        pendingEnabledPlugins.forEach((id) => useSessionStore.getState().addEnabledPlugin(newSid, id));
        useSessionStore.getState().clearEnabledPlugins(NEW_CONVERSATION_ID);
        const pendingEnabledMcps = useSessionStore.getState().getRuntime(NEW_CONVERSATION_ID)?.enabledMcps ?? [];
        pendingEnabledMcps.forEach((name) => useSessionStore.getState().addEnabledMcp(newSid, name));
        useSessionStore.getState().clearEnabledMcps(NEW_CONVERSATION_ID);
        const pendingAgentSelection = useSessionStore.getState().getRuntime(NEW_CONVERSATION_ID)?.agentSelectionIntent ?? { kind: 'keep' as const };
        useSessionStore.getState().setAgentSelectionIntent(newSid, pendingAgentSelection);
        useSessionStore.getState().clearAgentSelectionIntent(NEW_CONVERSATION_ID);
        pendingNewConversationRef.current = false;
        useSessionStore.getState().removeRuntime(NEW_CONVERSATION_ID);
        // Plan 开关是按 session 存的。欢迎页上开关记在 'new' 名下，这里必须搬到真实
        // 会话，否则 sendMessage 取到的是新会话的默认值 false，这条消息就不会带
        // `.plan`，整个 Plan 流程（只读约束、计划审批弹窗）全都不会触发。
        if (usePlanStore.getState().isActive(NEW_CONVERSATION_ID)) {
          // 连"用户手动打开开关"这个一次性标记一起搬过去：欢迎页那次点击就是显式
          // 进入 Plan，标记决定这条消息是否带 plan_entry_source。
          usePlanStore.getState().setActive(newSid, true, {
            explicitEntry: usePlanStore
              .getState()
              .hasPendingExplicitEntry(NEW_CONVERSATION_ID),
            entrySource:
              usePlanStore.getState().getPendingEntrySource(NEW_CONVERSATION_ID) ?? undefined,
          });
        }
        usePlanStore.getState().removeRuntime(NEW_CONVERSATION_ID);
        useWorkspaceStore.getState().upsertSession(createdSession, { isNew: true });
        sessionIdsCreatedInThisPageRef.current.add(newSid);
        useChatStore.getState().setProcessing(NEW_CONVERSATION_ID, false);
        sessionIdRef.current = newSid;
        setSessionId(newSid);
        navigate({ kind: 'chat-session', sessionId: newSid }, { replace: true });
        const goalArmedOnNew = useGoalStore.getState().runtimes[NEW_CONVERSATION_ID]?.armed ?? false;
        useGoalStore.getState().setArmed(NEW_CONVERSATION_ID, false);
        if (goalArmedOnNew) {
          // 欢迎页 "+" 选了「目标」：这条内容不走普通 chat.send，
          // 本地落一条 user 消息（供徽章匹配）后改调 command.goal（见 InputArea.tsx 的同款分流逻辑）
          queueOrAddGoalObjectiveMessage(newSid, messageContent);
          setGoalObjective(newSid, messageContent);
        } else {
          const sent = await sendMessage(messageContent, newSid, mediaItems);
          if (!sent) {
            useChatStore.getState().setInputValue(newSid, messageContent);
          }
        }
        newConversationProjectRef.current = null;
        newConversationPreviousSessionRef.current = null;
      } catch (error) {
        useChatStore.getState().setProcessing(NEW_CONVERSATION_ID, false);
        useChatStore.getState().setThinking(NEW_CONVERSATION_ID, false);
        useChatStore.getState().setInputValue(NEW_CONVERSATION_ID, content);
        console.error('Failed to create conversation:', error);
        window.alert(t('multiSession.errors.create'));
      } finally {
        creatingSessionRef.current = false;
      }
      return;
    }
    disposeInFlightHistoryHandles(currentSessionId);
    const sent = await sendMessage(content, currentSessionId, mediaItems);
    if (sent) {
      const sessionState = useSessionStore.getState();
      const session =
        sessionState.currentSession?.session_id === currentSessionId
          ? sessionState.currentSession
          : sessionState.sessions.find((item) => item.session_id === currentSessionId);
      await useWorkspaceStore.getState().refreshSessionWorkspace(session);
    } else {
      useChatStore.getState().setInputValue(currentSessionId, content);
    }
  }, [disposeInFlightHistoryHandles, mode, navigate, request, sendMessage, setGoalObjective, t]);

  const handlePersistMedia = useCallback((content: string, mediaItems: MediaItem[]) => {
    const currentSessionId = sessionIdRef.current;
    if (!currentSessionId || currentSessionId === NEW_CONVERSATION_ID) {
      return Promise.reject(new Error('会话未就绪，请稍后重试'));
    }
    return persistMedia(content, currentSessionId, mediaItems);
  }, [persistMedia]);

  const handlePersistDocuments = useCallback((content: string, mediaItems: MediaItem[]) => {
    const currentSessionId = sessionIdRef.current;
    if (!currentSessionId || currentSessionId === NEW_CONVERSATION_ID) {
      return Promise.reject(new Error('会话未就绪，请稍后重试'));
    }
    return persistDocuments(content, currentSessionId, mediaItems);
  }, [persistDocuments]);

  useEffect(() => {
    return setA2UIActionHandler((message) => {
      const currentSessionId = sessionIdRef.current;
      if (!currentSessionId || currentSessionId === NEW_CONVERSATION_ID) return;
      return sendStructuredChatContent(
        buildA2UIClientEventContent(message),
        currentSessionId,
      );
    });
  }, [sendStructuredChatContent]);

  const handleInterrupt = useCallback((newInput?: string) => {
    const currentSessionId = sessionIdRef.current;
    if (!currentSessionId || currentSessionId === NEW_CONVERSATION_ID) return;
    const trimmed = newInput?.trim();
    if (!trimmed) return;
    void supplement(currentSessionId, trimmed);
  }, [supplement]);

  const handleCancel = useCallback(() => {
    const currentSessionId = sessionIdRef.current;
    if (!currentSessionId || currentSessionId === NEW_CONVERSATION_ID) return;
    // 目标是否 active 决定停止按钮要不要顺带把目标转为 paused——约定行为：其它状态
    // （paused/blocked/completed/无目标）下，停止只结束会话，不碰目标本身。
    const isGoalActive = useGoalStore.getState().runtimes[currentSessionId]?.goal?.status === 'active';
    if (mode === 'team') {
      void pause(currentSessionId);
      if (isGoalActive) void pauseGoal(currentSessionId);
      return;
    }
    // agent 模式下有队列任务时，暂停队列自动发送
    if (mode === 'agent') {
      const runtime = useChatStore.getState().getRuntime(currentSessionId);
      if (runtime && runtime.taskQueue.length > 0) {
        useChatStore.getState().setQueuePaused(currentSessionId, true);
      }
    }
    void cancel(currentSessionId);
    if (isGoalActive) void pauseGoal(currentSessionId);
  }, [cancel, mode, pause, pauseGoal]);

  /**
   * 删除目标：active 时除了清目标，还要顺带结束当前会话输出——复用停止按钮同一套中断调用
   * （team 走 pause、其余走 cancel）。先清目标再补发中断，避免目标还没清掉那个空档被
   * "ACTIVE 目标保持交互打开"的后端逻辑又续上一轮。非 active 状态下只清目标，不打断当前
   * 会话（如果还有一轮在自然跑完，让它继续）。
   */
  const handleClearGoal = useCallback(
    (sessionId: string) => {
      const isGoalActive = useGoalStore.getState().runtimes[sessionId]?.goal?.status === 'active';
      void clearGoal(sessionId);
      if (!isGoalActive) return;
      if (mode === 'team') {
        void pause(sessionId);
      } else {
        void cancel(sessionId);
      }
    },
    [cancel, clearGoal, mode, pause]
  );

  const handleUserAnswer = useCallback((requestId: string, answers: UserAnswer[], source?: string) => {
    const currentSessionId = sessionIdRef.current;
    if (!currentSessionId || currentSessionId === NEW_CONVERSATION_ID) return;
    void sendUserAnswer(currentSessionId, requestId, answers, source);
  }, [sendUserAnswer]);

  const handleLoadMoreHistory = useCallback(async () => {
    if (!historyPagerMeta) return;
    if (historyLoadingSessionsRef.current.has(sessionId) || historyPagerMeta.loadedPages >= historyPagerMeta.totalPages) return;

    const sid = sessionId;
    const nextPage = historyPagerMeta.loadedPages + 1;
    const fallbackTotal = historyPagerMeta.totalPages;
    const prevToken = historyBackgroundPrefetchTokensRef.current.get(sid) ?? 0;
    const token = prevToken + 1;
    historyBackgroundPrefetchTokensRef.current.set(sid, token);
    historyLoadingSessionsRef.current.add(sid);
    setHistoryRetryAvailable(sid, false);
    setHistoryLoadingMore(true);
    setLoadingHistory(sid, true);
    let page: LoadedHistoryPage | null = null;
    try {
      page = await fetchHistoryPageResult(sid, nextPage, fallbackTotal);
      if (
        page &&
        token === historyBackgroundPrefetchTokensRef.current.get(sid)
      ) {
        applyLoadedHistoryPage(sid, page);
      }
    } finally {
      historyLoadingSessionsRef.current.delete(sid);
      setHistoryLoadingMore(false);
      setLoadingHistory(sid, false);
    }
    if (token !== historyBackgroundPrefetchTokensRef.current.get(sid)) {
      return;
    }
    if (!page) {
      setHistoryRetryAvailable(sid, true);
      return;
    }
    startBackgroundHistoryPrefetch(sid, page.pageIdx, page.totalPages);
  }, [
    applyLoadedHistoryPage,
    fetchHistoryPageResult,
    historyPagerMeta,
    sessionId,
    setHistoryRetryAvailable,
    setLoadingHistory,
    startBackgroundHistoryPrefetch,
  ]);

  const chatHistoryPager = useMemo(() => {
    if (!historyPagerMeta) return null;
    return {
      loadedPages: historyPagerMeta.loadedPages,
      totalPages: historyPagerMeta.totalPages,
      loadingMore: historyLoadingMore,
      prepending: historyPrepending,
      retryAvailable: historyRetrySessions.has(sessionId),
      onLoadMore: handleLoadMoreHistory,
    };
  }, [
    handleLoadMoreHistory,
    historyLoadingMore,
    historyPagerMeta,
    historyPrepending,
    historyRetrySessions,
    sessionId,
  ]);

  const performSessionRestore = useCallback(
    async (targetSessionId: string, targetMode?: string, targetSession?: Session, options?: { skipHistoryLoad?: boolean }) => {
      const previousSessionId = sessionIdRef.current;
      const previousMode =
        useSessionStore.getState().getRuntime(previousSessionId)?.mode ?? mode;
      const resolvedMode = targetMode ?? targetSession?.mode ?? previousMode;
      disposeInFlightHistoryHandles(targetSessionId);
      if (previousSessionId && previousSessionId !== targetSessionId) {
        try {
          await request('session.switch', {
            session_id: targetSessionId,
            previous_session_id: previousSessionId,
            previous_mode: previousMode,
            mode: resolvedMode,
            view_id: kvcViewIdRef.current,
          });
        } catch (error) {
          if (isTeamAgentMode(resolvedMode)) {
            console.error('Failed to switch team session:', error);
            window.alert(t('sessions.errors.switchSession'));
            return;
          }
          console.warn('Session switch lifecycle hook failed; continuing restore:', error);
        }
      }

      setHistoryLoadingMore(false);
      const existingRuntime = useChatStore.getState().getRuntime(targetSessionId);
      if (!existingRuntime) {
        useChatStore.getState().ensureRuntime(targetSessionId);
        setProcessing(targetSessionId, false);
        setThinking(targetSessionId, false);
        setPaused(targetSessionId, false);
        clearTeamRuntimeState(targetSessionId);
        clearMessages(targetSessionId);
        clearTodos(targetSessionId);
        resetHarnessStore(targetSessionId);
        historyRestoreFromPanelHintRef.current = true;
      }
      // 确保 session runtime 存在；否则 useSessionStore.setMode 会因找不到 runtime 而直接跳过，
      // 导致从会话页签恢复后前端 mode 不会切换到目标会话对应的 mode。
      ensureSessionRuntimes(targetSessionId);
      sessionIdRef.current = targetSessionId;
      setSessionId(targetSessionId);
      if (targetSession) {
        upsertSessionMetadata(targetSession, { setCurrent: true });
        // 会话打开时若后端 metadata 带 model（首条 chat.send 显式携带 model_name 时
        // 由后端落盘），写进 runtime.selectedModelName——单 Agent 与集群（team）会话
        // 同样恢复，保证刷新页面后模型选择不回退到默认模型。
        if (targetSession.model) {
          useSessionStore.getState().setSelectedModelName(targetSessionId, targetSession.model);
        }
      } else {
        setCurrentSession(null);
      }
      if (resolvedMode) {
        setMode(targetSessionId, resolvedMode as AgentMode);
        // 恢复会话时同步 Plan 开关：后端回传的 mode 可能是三段命名
        // `agent.{work|code}.plan` / `team.{work|code}.plan`，而 setMode 会把
        // 它归一成基础模式（normalizeAgentMode 折叠成 `agent` / `team`）。若不
        // 补一次 setActive，planStore 仍是空 runtime（active:false），后续
        // sendMessage 走 resolveOutgoingMode 时 isActive 为 false，出站 mode
        // 退回基础模式，Plan 流程静默丢失。按 isPlanWireMode 判定，非 plan
        // 会话不受影响。
        if (isPlanWireMode(resolvedMode)) {
          usePlanStore.getState().setActive(targetSessionId, true);
        }
      }
      setActiveNav('chat');
      navigate({ kind: 'chat-session', sessionId: targetSessionId });
      if (!options?.skipHistoryLoad) {
        setHistoryBootstrapKey((k) => k + 1);
      }
      requestComposerFocus();
      if (!targetSession) {
        void loadSessionMetadata(targetSessionId);
      }
    },
    [
      clearMessages,
      clearTodos,
      disposeInFlightHistoryHandles,
      mode,
      navigate,
      loadSessionMetadata,
      request,
      requestComposerFocus,
      resetHarnessStore,
      setActiveNav,
      setCurrentSession,
      setHistoryLoadingMore,
      setMode,
      setPaused,
      setProcessing,
      setSessionId,
      setThinking,
      t,
      upsertSessionMetadata,
    ]
  );

  const handleRestoreSession = useCallback(
    (
      targetSessionId: string,
      targetMode?: string,
      targetSession?: Session,
      options?: { skipHistoryLoad?: boolean },
    ): Promise<void> => {
      // WebSocket requests are processed concurrently by AgentServer. Queue
      // navigation here so rapid A -> B -> C clicks cannot race and let an
      // older response overwrite the latest selected session.
      const queuedRestore = sessionRestoreQueueRef.current
        .catch(() => undefined)
        .then(() => performSessionRestore(
          targetSessionId,
          targetMode,
          targetSession,
          options,
        ));
      sessionRestoreQueueRef.current = queuedRestore.catch(() => undefined);
      return queuedRestore;
    },
    [performSessionRestore],
  );

  const requestSessionNavigation = useCallback((target: Session | 'new', options?: NewConversationOptions) => {
    if (target === 'new') { enterNewConversation(mode, options); return; }
    if (isMobile) {
      setTeamAreaExpanded(false);
      setToolPanelHidden(true);
    }
    void handleRestoreSession(target.session_id, target.mode, target);
  }, [enterNewConversation, handleRestoreSession, isMobile, mode, setTeamAreaExpanded, setToolPanelHidden]);

  const handleTeamSessionsDeleted = useCallback(async (sessionIds: string[]) => {
    const deletedSessionIds = new Set(sessionIds);
    const sessionState = useSessionStore.getState();

    for (const deletedSessionId of deletedSessionIds) {
      forgetCreatedConversation(deletedSessionId);
      sessionState.removeSession(deletedSessionId);
      sessionState.removeRuntime(deletedSessionId);
      useChatStore.getState().removeRuntime(deletedSessionId);
      useSubagentStore.getState().removeRuntime(deletedSessionId);
      useTodoStore.getState().removeRuntime(deletedSessionId);
      useHarnessStore.getState().removeRuntime(deletedSessionId);
      useGoalStore.getState().removeRuntime(deletedSessionId);
    }

    if (routeSessionId && deletedSessionIds.has(routeSessionId)) {
      setMissingSessionId(routeSessionId);
    }

    const workspaceState = useWorkspaceStore.getState();
    const loadedProjectIds = Object.keys(workspaceState.projectSessions);
    await workspaceState.loadProjects();
    await Promise.all(loadedProjectIds.map((projectId) => workspaceState.loadProjectSessions(projectId)));

    const cronStore = useCronStore.getState();
    for (const [jobId, sessions] of Object.entries(cronStore.cronSessions)) {
      if (sessions.some((session) => deletedSessionIds.has(session.session_id))) {
        const job = cronStore.jobs.find((item) => item.id === jobId);
        void cronStore.loadCronSessions(job?.project_id || 'default', jobId);
      }
    }
  }, [routeSessionId]);

  const handleDeleteConversation = useCallback(async () => {
    if (!deleteTarget) return;
    const runtime = useChatStore.getState().getRuntime(deleteTarget.session_id);
    if (runtime?.isProcessing || runtime?.pendingQuestion) {
      setDialogError(t('multiSession.deleteRunningDisabled'));
      return;
    }
    setDialogBusy(true); setDialogError(null);
    try {
      const deletedSession = deleteTarget;
      await request('session.delete', { session_id: deleteTarget.session_id });
      forgetCreatedConversation(deleteTarget.session_id);
      useSessionStore.getState().removeSession(deleteTarget.session_id);
      useSessionStore.getState().removeRuntime(deleteTarget.session_id);
      useChatStore.getState().removeRuntime(deleteTarget.session_id);
      useSubagentStore.getState().removeRuntime(deleteTarget.session_id);
      useTodoStore.getState().removeRuntime(deleteTarget.session_id);
      useHarnessStore.getState().removeRuntime(deleteTarget.session_id);
      useGoalStore.getState().removeRuntime(deleteTarget.session_id);
      const deletingCurrent = sessionIdRef.current === deleteTarget.session_id;
      setDeleteTarget(null);
      await useWorkspaceStore.getState().refreshSessionWorkspace(deletedSession);
      // 删除 session 后刷新所属定时任务的触发会话列表
      const cronStore = useCronStore.getState();
      for (const [jobId, sessions] of Object.entries(cronStore.cronSessions)) {
        if (sessions.some((s) => s.session_id === deletedSession.session_id)) {
          const job = cronStore.jobs.find((j) => j.id === jobId);
          void cronStore.loadCronSessions(job?.project_id || 'default', jobId);
        }
      }
      if (deletingCurrent) {
        // session.delete already owns B's KVC eviction. Do not carry the
        // deleted Session into C's session.create as previous_session_id.
        enterNewConversation(mode, {}, { clearPreviousSession: true });
      }
    } catch { setDialogError(t('multiSession.errors.delete')); }
    finally { setDialogBusy(false); }
  }, [deleteTarget, enterNewConversation, mode, request, t]);

  const handleNavigate = useCallback(
    (nav: MainNavKey) => {
      if (
        activeNav === 'settings' &&
        nav !== 'settings' &&
        settingsHasChangesRef.current &&
        !window.confirm(t('settingsPanel.dialog.discardConfirm'))
      ) {
        return;
      }
      if (nav !== 'settings') setRequestedSettingsModuleId(null);
      setActiveNav(nav);
      if (nav === 'chat') {
        setConversationSidebarCollapsed(false);
        if (isMobile) {
          setTeamAreaExpanded(false);
          setToolPanelHidden(true);
        }
      }
      if (modelSetupGuideStep === 1 && nav === 'settings') {
        setRequestedSettingsModuleId('models');
        setModelSetupGuideStep(2);
      }
      if (nav === 'skills') setHasVisitedSkills(true);
      if (nav === 'personalContext') setHasVisitedPersonalContext(true);
    },
    [activeNav, isMobile, modelSetupGuideStep, setHasVisitedPersonalContext, setRequestedSettingsModuleId, setTeamAreaExpanded, setToolPanelHidden, t],
  );

  const skipModelSetupGuide = useCallback(() => {
    setModelSetupGuideStep(null);
    setModelSetupGuideManual(false);

    void request('config.set', { setup_guide_enabled: 'false' })
      .then(() => {
        setServerConfig((current) => ({
          ...(current ?? {}),
          setup_guide_enabled: 'false',
        }));
      })
      .catch((error) => {
        console.error('Failed to disable setup guide:', error);
      });
  }, [request]);

  const quickSetupModelSetupGuide = useCallback(() => {
    setModelSetupGuideStep(null);
    setModelSetupGuideManual(false);
    // 显式指定使用 huawei-cloud-maas-setup skill，避免 agent 自行上网搜索
    void handleSendMessage(
      '请使用 huawei-cloud-maas-setup 技能帮我配置华为云 MaaS 服务。'
      + '严格按照其中的步骤引导我完成购买、获取 API Key 和配置写入。'
    );
  }, [handleSendMessage]);

  const manualSetupModelSetupGuide = useCallback(() => {
    setModelSetupGuideStep(1);
  }, []);

  const acknowledgeModelSetupGuide = useCallback(() => {
    setModelSetupGuideStep(null);
    setModelSetupGuideManual(false);

    void request('config.set', { setup_guide_enabled: 'false' })
      .then(() => {
        setServerConfig((current) => ({
          ...(current ?? {}),
          setup_guide_enabled: 'false',
        }));
      })
      .catch((error) => {
        console.error('Failed to disable setup guide:', error);
      });
  }, [request]);

  const handleExportShare = useCallback(async () => {
    const currentSessionId = sessionIdRef.current;
    if (!currentSessionId || currentSessionId === NEW_CONVERSATION_ID || (isProcessing && !isPaused) || isExportingShare) {
      return;
    }
    setIsExportingShare(true);
    try {
      const params = new URLSearchParams({
        session_id: currentSessionId,
      });
      const response = await fetch(`/share-api/snapshot?${params.toString()}`, {
        cache: 'no-store',
      });
      const contentType = response.headers.get('content-type') || '';
      if (!response.ok) {
        let detail = '';
        try {
          const payload = await response.json();
          detail = typeof payload?.error === 'string' ? payload.error : '';
        } catch {
          detail = await response.text().catch(() => '');
        }
        throw new Error(detail || `HTTP ${response.status}`);
      }
      if (!contentType.includes('application/json')) {
        throw new Error('share_snapshot_not_json');
      }
      const payload = await response.json() as {
        filename?: string;
        snapshot?: ShareImageSnapshot;
      };
      if (!payload.snapshot) {
        throw new Error('missing_snapshot');
      }
      shareExportFilenameRef.current = payload.filename || payload.snapshot.metadata?.filename || 'jiuwenswarm-share.png';
      setShareExportSnapshot(payload.snapshot);
    } catch (error) {
      console.error('Failed to export share image:', error);
      window.alert(t('share.exportFailed'));
      setIsExportingShare(false);
      setShareExportSnapshot(null);
    }
  }, [isExportingShare, isPaused, isProcessing, t]);

  useEffect(() => {
    if (!shareExportSnapshot) {
      return;
    }
    const token = shareExportTokenRef.current + 1;
    shareExportTokenRef.current = token;

    void (async () => {
      try {
        const node = shareExportRef.current;
        if (!node) {
          throw new Error('share_image_node_missing');
        }
        const imageBlob = await exportShareImageNode(node);
        if (shareExportTokenRef.current !== token) {
          return;
        }
        const saved = await saveShareImage(imageBlob, shareExportFilenameRef.current);
        if (saved) {
          showSaveToast();
        }
      } catch (error) {
        console.error('Failed to render share image:', error);
        window.alert(t('share.exportFailed'));
      } finally {
        if (shareExportTokenRef.current === token) {
          setIsExportingShare(false);
          setShareExportSnapshot(null);
        }
      }
    })();
  }, [shareExportSnapshot, showSaveToast, t]);

  const routeSessionMissing = routeSessionId !== null
    && initialDataLoaded
    && missingSessionId === routeSessionId
    && isConversationMissing(routeSessionId, true, sessions);
  const showConversationNotFound = route.kind === 'not-found' || routeSessionMissing;
  const showWorkspaceDivider = isTeamAreaExpanded && !showConversationNotFound && !shouldFullscreen;
  const isNewSessionPromotion = Boolean(sessionId && sessionIdsCreatedInThisPageRef.current.has(sessionId));
  const composerFocusKey = showConversationNotFound ? null : `${sessionId}:${composerFocusNonce}`;

  useEffect(() => {
    if (!showWorkspaceDivider) clearChatPanelResize();
    return () => {
      clearChatPanelResize();
    };
  }, [clearChatPanelResize, showWorkspaceDivider]);

  return (
    <div
      className={`shell shell--icon-rail ${activeNav === 'agents' ? 'shell--agent-management' : ''}`}
      data-testid="app-shell"
      data-session-id={sessionId}
    >
      {/* Navigation Sidebar */}
      <SessionSidebar
        activeNav={activeNav}
        onNavigate={handleNavigate}
        onNewSession={handleNewSession}
        showNewSession={false}
        hiddenNavItems={hiddenNavItems}
      />

      {modelSetupGuideStep !== null ? (
        <ModelSetupGuide
          step={modelSetupGuideStep}
          manual={modelSetupGuideManual}
          onAcknowledge={acknowledgeModelSetupGuide}
          onSkip={skipModelSetupGuide}
          onQuickSetup={quickSetupModelSetupGuide}
          onManualSetup={manualSetupModelSetupGuide}
        />
      ) : null}

      {/* Main Content */}
      <main className={`content ${activeNav === 'chat' ? 'content--chat' : ''} ${isTeamAreaExpanded ? 'content--team-expanded' : ''}`}>
        {configError && (
          <div className="card mb-4" data-testid="app-config-error">
            <div className="text-sm text-text-muted">
              {configError}. {t('app.configErrorHint')}
              <span className="mono"> python -m tests.web_gateway_jiuwenclaw_integration </span>
              {t('app.configErrorDefault')}
              <span className="mono"> jiuwenswarm/channels/web/frontend/.env.local </span>
              {t('app.configErrorEnv')} <span className="mono">VITE_API_BASE</span> {t('common.and')} <span className="mono">VITE_WS_BASE</span>.
            </div>
          </div>
        )}

        {activeNav === 'chat' && (
          <>
            <div className="chat-layout flex-1 flex min-h-0 overflow-hidden">
              <ConversationSidebar
                activeSessionId={sessionId === NEW_CONVERSATION_ID ? null : sessionId}
                onNew={(options) => requestSessionNavigation('new', options)}
                onSelect={requestSessionNavigation}
                onDelete={(session) => { setDialogError(null); setDeleteTarget(session); }}
                onOpenCron={() => handleNavigate('cron')}
                isCronActive={false}
                collapsed={conversationSidebarCollapsed}
                floating={conversationSidebarFloating}
                onToggleCollapse={() => setConversationSidebarCollapsed((v) => !v)}
              />
              <div className="chat-workspace flex-1 flex min-h-0 overflow-hidden">
                {showConversationNotFound && (
                  <div className="flex-1 flex flex-col items-center justify-center gap-4" data-testid="app-conversation-not-found">
                    <h1 className="text-lg font-semibold text-text" data-testid="app-conversation-not-found-title">{t('multiSession.notFound.title')}</h1>
                    <div className="flex gap-2">
                      <button className="btn primary" onClick={() => enterNewConversation()} data-testid="app-conversation-not-found-new-button">
                        {t('multiSession.notFound.newConversation')}
                      </button>
                    </div>
                  </div>
                )}
                {/* Chat Panel - 在展开时可拖拽调整宽度 */}
                <div
                  className={`${showConversationNotFound || shouldFullscreen ? 'hidden' : 'flex'} chat-layout__surface  pt-0 flex-col ${isTeamAreaExpanded ? '' : 'min-w-0'} min-h-0 ${isTeamAreaExpanded ? '' : 'flex-1'}`}
                  style={isTeamAreaExpanded ? { width: `${chatPanelWidthPct}%` } : undefined}
                  data-testid="app-chat-surface"
                >
                  <div className={`flex-1 min-h-0`}>
                    <ChatPanel
                      onSendMessage={handleSendMessage}
                      onInputIntent={kvCacheAffinityEnabled ? handleKVCInputIntent : undefined}
                      onPersistMedia={handlePersistMedia}
                      onPersistDocuments={handlePersistDocuments}
                      onInterrupt={handleInterrupt}
                      onCancel={handleCancel}
                      onSwitchMode={handleSwitchMode}
                      isProcessing={isProcessing}
                      onUserAnswer={handleUserAnswer}
                      onExportShare={handleExportShare}
                      isExportingShare={isExportingShare}
                      canExportShare={Boolean(sessionId && sessionId !== NEW_CONVERSATION_ID && (!isProcessing || isPaused))}
                      sessionTitle={sessionTitle}
                      sessionProjectName={sessionProjectName}
                      sessionProject={sessionProject}
                      teamAreaExpanded={toolPanelHidden ? null : isTeamAreaExpanded}
                      autoFocusKey={composerFocusKey}
                      onNavigateToSkills={() => handleNavigate('skills')}
                      onNavigateToAgents={() => handleNavigate('agents')}
                      onToggleTeamArea={handleToggleDetailPanel}
                      onOpenCodeReview={handleOpenCodeReview}
                      permissionsEnabled={serverConfig?.permissions_enabled !== 'false'}
                      heartbeatPanelOpen={heartbeatPanelOpen}
                      onToggleHeartbeatPanel={handleToggleHeartbeatPanel}
                      onSavePermission={savePermissionSilent}
                      historyPager={chatHistoryPager}
                      isHistoryRestoring={isRestoringHistorySession}
                      onSetGoal={setGoalObjective}
                      onPauseGoal={pauseGoal}
                      onResumeGoal={resumeGoal}
                      onClearGoal={handleClearGoal}
                      onDrainTaskQueueIfIdle={drainTaskQueueIfIdle}
                    />
                  </div>
                </div>

                {/* 可拖拽分割线 */}
                {showWorkspaceDivider && (
                  <div
                    className="resize-divider resize-divider--workspace touch-none select-none"
                    role="separator"
                    aria-orientation="vertical"
                    onPointerDown={handleDividerPointerDown}
                    data-testid="app-workspace-divider"
                    onPointerMove={handleDividerPointerMove}
                    onPointerUp={finishDividerResize}
                    onPointerCancel={finishDividerResize}
                    onLostPointerCapture={(event) => {
                      clearChatPanelResize(event.pointerId);
                    }}
                  />
                )}

                {/* Tool Panel / Expanded Team Panel */}
                {!toolPanelHidden && (toolPanelHasContent || isRestoringTeamHistory) && !showConversationNotFound && !heartbeatPanelOpen && (
                  <ToolPanel
                    sessionId={sessionId}
                    project={sessionProject}
                    isNewSessionPromotion={isNewSessionPromotion}
                    teamAreaExpanded={teamAreaExpanded}
                    teamAreaActiveTab={teamAreaActiveTab}
                    teamAreaActiveDetailTab={teamAreaActiveDetailTab}
                    teamAreaSelectedMemberId={teamAreaSelectedMemberId}
                    codeReviewTarget={codeReviewTarget}
                    teamAreaSelectedArtifactId={teamAreaSelectedArtifactId}
                    singleAgentPanelExpanded={singleAgentPanelExpanded}
                    singleAgentPanelActiveTab={singleAgentPanelActiveTab}
                    singleAgentPanelSelectedArtifactId={singleAgentPanelSelectedArtifactId}
                    singleAgentPanelSelectedSubagentId={singleAgentPanelSelectedSubagentId}
                    setTeamAreaExpanded={setTeamAreaExpanded}
                    setTeamAreaActiveTab={setTeamAreaActiveTab}
                    setTeamAreaActiveDetailTab={setTeamAreaActiveDetailTab}
                    setTeamAreaSelectedMemberId={setTeamAreaSelectedMemberId}
                    setCodeReviewTarget={setCodeReviewTarget}
                    setTeamAreaSelectedArtifactId={setTeamAreaSelectedArtifactId}
                    setSingleAgentPanelExpanded={setSingleAgentPanelExpanded}
                    setSingleAgentPanelActiveTab={setSingleAgentPanelActiveTab}
                    setSingleAgentPanelSelectedArtifactId={setSingleAgentPanelSelectedArtifactId}
                    setSingleAgentPanelSelectedSubagentId={setSingleAgentPanelSelectedSubagentId}
                    shouldFullscreen={shouldFullscreen}
                    onCloseFloating={() => setToolPanelHidden(true)}
                  />
                )}

                {/* 心跳面板：跟 ToolPanel 一样占用右侧工作区一栏，而不是浮在页面上方的浮层 */}
                {heartbeatPanelOpen && sessionId && sessionId !== NEW_CONVERSATION_ID && !showConversationNotFound && (
                  <HeartbeatPanel sessionId={sessionId} onClose={() => setHeartbeatPanelOpen(false)} />
                )}
              </div>
            </div>
          </>
        )}
        {activeNav === 'agents' && (
          <div className="app-section">
            <AgentManagementPanel
              onUseAgent={handleUseAgent}
              onUsePrompt={handleUseAgentPrompt}
              onCreateViaChat={() => requestSessionNavigation('new', {
                initialInputValue: t('agentManagement.actions.createViaChatPrompt'),
                initialSelectedSkills: ['agent-creator'],
              })}
            />
          </div>
        )}
        {activeNav === 'teams' && (
          <div className="app-section">
            <TeamPanel onSessionsDeleted={handleTeamSessionsDeleted} />
          </div>
        )}
        {activeNav === 'sessions' && (
          <div className="app-section">
            <SessionsPanel
              currentSessionId={sessionId}
              isConnected={isConnected}
              isProcessing={isProcessing}
              onRestoreSession={handleRestoreSession}
            />
          </div>
        )}
        {activeNav === 'cron' && (
          <div className="chat-layout flex-1 flex min-h-0 overflow-hidden">
            <ConversationSidebar
              // 停留在定时任务时，项目/会话列表不应该还显示"选中"效果——定时任务和它们是同一级的
              // 互斥选中关系，传 null 让列表里的选中态清空（沿用"新建会话时传 null"的既有语义）
              activeSessionId={null}
              onNew={(options) => requestSessionNavigation('new', options)}
              onSelect={requestSessionNavigation}
              onDelete={(session) => { setDialogError(null); setDeleteTarget(session); }}
              onOpenCron={() => handleNavigate('cron')}
              isCronActive
              collapsed={conversationSidebarCollapsed}
              floating={conversationSidebarFloating}
              onToggleCollapse={() => setConversationSidebarCollapsed((v) => !v)}
            />
            <div className="chat-workspace flex-1 flex min-h-0 overflow-hidden">
              <CronPanel
                sessionId={sessionId}
                onCreateViaChat={(initialInputValue) => requestSessionNavigation('new', { initialInputValue })}
                onSelectSession={(session) => {
                  if (typeof session === 'string') {
                    // 立即执行返回的 session_id 可能还未在后端创建（agent 刚开始执行），
                    // 构造最小 Session 占位对象，让 upsertSessionMetadata 直接加入会话列表，
                    // 避免 loadSessionMetadata 立即失败导致"对话不存在或已删除"。
                    // 后续 cron 广播到达时会刷新会话列表补全完整元数据。
                    // 跳过初始历史加载：session 是全新的，空响应的 replaceHistoryMessages
                    // 会覆盖后续到达的广播消息。
                    void handleRestoreSession(session, undefined, {
                      session_id: session,
                      title: '',
                      project_id: '',
                      project_dir: '',
                      mode: 'agent',
                      status: 'active',
                      message_count: 0,
                      created_at: new Date().toISOString(),
                      updated_at: new Date().toISOString(),
                    }, { skipHistoryLoad: true });
                    return;
                  }
                  requestSessionNavigation(session);
                }}
              />
            </div>
          </div>
        )}
        {activeNav === 'settings' && (
          <div className="app-section">
            <SettingsPage
              definition={settingsPageDefinition}
              isConnected={isConnected}
              connectionState={connectionState}
              request={settingsRequest}
              onHasChangesChange={handleSettingsHasChangesChange}
              onDetectExternalCli={detectExternalCli}
              onSelectExternalCliPath={selectExternalCliPath}
              onTrackExternalCliDependencyInstalls={trackExternalCliDependencyInstalls}
              externalCliInstallStatuses={externalCliInstallStatuses}
              externalCliInstallBusy={Object.values(externalCliInstallStatuses).some(
                (status) => status?.status === 'running',
              )}
              onOpenExternalCliInstallDialog={() => setExternalCliInstallDialogOpen(true)}
              initialModuleId={requestedSettingsModuleId ?? undefined}
            />
          </div>
        )}
        {FEATURE_APP_UPDATER_UI && activeNav === 'updatepanel' && (
          <div className="app-section">
            <UpdatePanel isConnected={isConnected} request={request} />
          </div>
        )}

        {FEATURE_PERSONAL_CONTEXT_UI && hasVisitedPersonalContext && (
          <div className={`app-section ${activeNav === 'personalContext' ? '' : 'is-hidden'}`}>
            <PersonalContextPanel isConnected={isConnected} isActive={activeNav === 'personalContext'} />
          </div>
        )}

        {hasVisitedSkills && (
          <div className={`app-section ${activeNav === 'skills' ? '' : 'is-hidden'}`}>
            <SkillPanel
              sessionId={sessionId}
              isConnected={isConnected}
              isActive={activeNav === 'skills'}
              symphonyEnabled={normalizeConfigBoolean(serverConfig?.symphony_enabled)}
              onSymphonyEnabledChange={saveSymphonyEnabled}
              onNavigateToSettings={() => requestSettingsModule('agent')}
            />
          </div>
        )}
        {activeNav === 'connectorMarket' && (
          <div className="app-section">
            <ConnectorMarketPanel
              onCreateViaChat={() => window.dispatchEvent(new CustomEvent('jiuwen:new-conversation', {
                detail: {
                  skillName: 'plugin-creator',
                  suffixText: t('connectorMarket.chatPrompts.createPlugin'),
                  metadata: { scene: 'create_plugin' },
                },
              }))}
              onUseExample={(initialInputValue, mcpName) =>
                requestSessionNavigation('new', { initialInputValue, initialEnabledMcps: [mcpName] })
              }
              onUsePluginExample={(initialInputValue, pluginId) =>
                requestSessionNavigation('new', { initialInputValue, initialEnabledPlugins: [pluginId] })
              }
              onUseExtension={({ kind, id }) =>
                requestSessionNavigation(
                  'new',
                  kind === 'plugin' ? { initialEnabledPlugins: [id] } : { initialEnabledMcps: [id] },
                )
              }
            />
          </div>
        )}
      </main>

      {deleteTarget && (
        <DeleteDialog
          title={deleteTarget.title || t('multiSession.untitled')}
          deleting={dialogBusy}
          error={dialogError}
          onCancel={() => setDeleteTarget(null)}
          onDelete={() => { void handleDeleteConversation(); }}
        />
      )}

      {/* 连接状态提示 */}
      {!isConnected && (
        <div className="app-toast-wrapper app-toast-wrapper--top" data-testid="app-connection-toast">
          <div className="app-connection-toast animate-rise" data-testid="app-connection-toast-message" data-variant={serverConfig ? 'connecting' : 'loadingConfig'}>
            {serverConfig ? t('connection.connecting') : t('connection.loadingConfig')}
          </div>
        </div>
      )}

      {saveToastVisible && (
        <div className="app-toast-wrapper app-toast-wrapper--top-center" data-testid="app-save-toast">
          <div className="app-session-toast animate-rise" data-testid="app-save-toast-message">
            {t('common.saveSuccess')}
          </div>
        </div>
      )}

      {proactiveToastVisible && proactiveToastMessage && (
        <div className="app-toast-wrapper app-toast-wrapper--top-center" data-testid="app-proactive-notification-toast">
          <div className="bg-warn-subtle text-warn px-4 py-2 rounded-lg shadow-lg animate-rise text-sm" data-testid="app-proactive-notification-toast-message">
            {proactiveToastMessage}
          </div>
        </div>
      )}

      {/* 安全警告提示 */}
      {securityAlertVisible && (
        <div className="app-toast-wrapper app-toast-wrapper--top" data-testid="app-security-alert">
          <div className="app-security-alert animate-rise" data-testid="app-security-alert-panel">
            <div className="app-security-alert__header" data-testid="app-security-alert-header">
              <div className="app-security-alert__title" data-testid="app-security-alert-title">
                <span>⚠️</span>
                <span className="text-xs font-medium text-text" data-testid="app-security-alert-title-text">{t('app.securityAlertTitle')}</span>
              </div>
              <button
                type="button"
                onClick={() => {
                  setSecurityAlertVisible(false);
                  if (securityAlertTimerRef.current) {
                    clearTimeout(securityAlertTimerRef.current);
                    securityAlertTimerRef.current = null;
                  }
                }}
                className="app-security-alert__close"
                data-testid="app-security-alert-close"
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
            <div className="app-security-alert__content text-sm" data-testid="app-security-alert-content">
              {securityAlertContent}
            </div>
          </div>
        </div>
      )}

      {/* 配置保存后重启状态弹窗 */}
      {restartModalOpen && (
        <div className="app-restart-modal" data-testid="app-restart-modal">
          <div className="app-restart-modal__backdrop" data-testid="app-restart-modal-backdrop" />
          <div className="app-restart-modal__panel" data-testid="app-restart-modal-panel">
            <div className="flex flex-col items-center text-center" data-testid="app-restart-modal-body">
              {!restartSuccess ? (
                <div className="w-12 h-12 rounded-full border-4 border-border border-t-accent animate-spin mb-4" data-testid="app-restart-modal-status-icon" data-variant="loading" />
              ) : (
                <div className="w-12 h-12 rounded-full bg-ok/15 text-ok flex items-center justify-center mb-4" data-testid="app-restart-modal-status-icon" data-variant="success">
                  <svg className="w-7 h-7" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                  </svg>
                </div>
              )}
              <h3 className="text-base font-semibold text-text mb-1" data-testid="app-restart-modal-title">
                {!restartSuccess
                  ? t('app.restarting')
                  : appliedWithoutRestart
                    ? t('app.configApplied')
                    : t('app.restartSuccess')}
              </h3>
              <p className="text-sm text-text-muted mb-5" data-testid="app-restart-modal-description">
                {!restartSuccess
                  ? t('app.restartWaiting')
                  : appliedWithoutRestart
                    ? t('app.configAppliedDesc')
                    : t('app.restartSuccessDesc')}
              </p>
              {restartSuccess && (
                <button
                  type="button"
                  onClick={closeRestartModal}
                  className="btn primary !px-4 !py-2"
                  data-testid="app-restart-modal-ok"
                >
                  {t('common.ok')}
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      <ExternalCliInstallDialog
        open={externalCliInstallDialogOpen}
        statuses={externalCliInstallStatuses}
        onClose={() => setExternalCliInstallDialogOpen(false)}
        onGetStatus={getExternalCliDependencyInstallStatus}
        onStatusChange={updateExternalCliInstallStatus}
      />

      <div className="share-image-stage" aria-hidden="true" data-testid="app-share-image-stage">
        <ShareImageDocument ref={shareExportRef} snapshot={shareExportSnapshot} />
      </div>
    </div>
  );
}

function App({
  settingsPageDefinition,
  resolveSettingsRequest,
}: {
  settingsPageDefinition: SettingsPageDefinition;
  resolveSettingsRequest: (openSourceRequest: SettingsRequest) => SettingsRequest;
}) {
  return (
    <ErrorBoundary>
      <AppContent
        settingsPageDefinition={settingsPageDefinition}
        resolveSettingsRequest={resolveSettingsRequest}
      />
    </ErrorBoundary>
  );
}

/**
 * 鉴权外壳: 进入前探测 cookie 是否携带有效 access_token + 是否一体机模式。
 * - GET /api/web-config: 本地端点, 拿 {remote: bool, iam_enabled: bool}
 *   - iam_enabled=false: 未配置 IAM, 无鉴权, 直接渲染主 App
 *   - iam_enabled=true: 配置了 IAM, 继续探测登录态
 * - GET /auth-api/v1/auth/permissions (同源, 浏览器自动带 HttpOnly cookie)
 *   - 200 -> 已登录, 渲染主 App (+ 一体机模式时浮 LogoutButton)
 *   - 401/其他 -> 未登录, 渲染 LoginPage
 * control-panel 只认 Authorization: Bearer, app_web.py 的 _proxy_auth_http
 * 会从 jw_token cookie 取 token 注入头, 故前端只需同源请求。
 */
function AppWithAuth({
  settingsPageDefinition,
  resolveSettingsRequest,
}: {
  settingsPageDefinition: SettingsPageDefinition;
  resolveSettingsRequest: (openSourceRequest: SettingsRequest) => SettingsRequest;
}) {
  const [authStatus, setAuthStatus] = useState<'checking' | 'loggedOut' | 'loggedIn' | 'noIam'>('checking');
  const [remote, setRemote] = useState(false);

  useEffect(() => {
    let cancelled = false;
    // 先拿 web-config: 如果 iam_enabled=false, 直接跳过鉴权探测
    fetch('/api/web-config', { credentials: 'same-origin' })
      .then((r) => r.json())
      .then((cfg) => {
        if (cancelled) return;
        if (cfg && typeof cfg.remote === 'boolean') setRemote(cfg.remote);
        if (cfg && cfg.iam_enabled === false) {
          setAuthStatus('noIam');
          return;
        }
        // IAM 已配置, 探测登录态
        fetch('/auth-api/v1/auth/permissions', { credentials: 'same-origin' })
          .then((resp) => {
            if (cancelled) return;
            if (resp.ok) {
              setAuthStatus('loggedIn');
            } else {
              setAuthStatus('loggedOut');
            }
          })
          .catch(() => {
            if (!cancelled) setAuthStatus('loggedOut');
          });
      })
      .catch(() => {
        // web-config 获取失败, 回退到探测登录态
        fetch('/auth-api/v1/auth/permissions', { credentials: 'same-origin' })
          .then((resp) => {
            if (cancelled) return;
            if (resp.ok) {
              setAuthStatus('loggedIn');
            } else {
              setAuthStatus('loggedOut');
            }
          })
          .catch(() => {
            if (!cancelled) setAuthStatus('loggedOut');
          });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (authStatus === 'checking') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-900">
        <div className="text-slate-400 text-sm">Loading…</div>
      </div>
    );
  }
  if (authStatus === 'loggedOut') {
    return <LoginPage />;
  }
  return (
    <>
      {remote && <LogoutButton />}
      <App settingsPageDefinition={settingsPageDefinition} resolveSettingsRequest={resolveSettingsRequest} />
    </>
  );
}

export default AppWithAuth;
