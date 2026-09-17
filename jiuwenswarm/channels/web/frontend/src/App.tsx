/**
 * App 主组件
 *
 * 应用主布局，整合所有组件
 */

import { useState, useCallback, useEffect, useRef, Component, ReactNode, useMemo, type PointerEvent as ReactPointerEvent } from 'react';
import { ChatPanel } from './components/ChatPanel';
import { SessionSidebar } from './components/SessionSidebar';
import { SkillPanel } from './components/SkillPanel';
import { AgentPanel } from './components/AgentPanel/index';
import { TeamPanel } from './components/TeamPanel';
import { SessionsPanel } from './components/SessionsPanel';
import CronPanel from './components/CronPanel';
import { ToolPanel } from './components/ToolPanel';
import { ConfigPanel } from './components/ConfigPanel';
import { ChannelsPanel } from './components/ChannelsPanel';
import { BrowserPanel } from './components/BrowserPanel';
import { UpdatePanel } from './components/UpdatePanel';
import { ExtensionsHubPanel } from './components/ExtensionsHubPanel';
import {
  ShareImageDocument,
  exportShareImageNode,
  type ShareImageSnapshot,
} from './features/shareImageExport';
import type { CodeReviewTarget } from './features/code-mode/types';

import { FEATURE_APP_UPDATER_UI } from './featureFlags';
import {
  beginHistoryRestore,
  fetchHistoryPage,
  HISTORY_GET_METHOD,
  type HistoryRestoreHandle,
  type HistoryHarnessReplayItem,
  type FetchHistoryPageResult,
} from './features/historyRestore';
import { prefetchHistoryPages } from './features/historyPagination';
import { queueOrAddGoalObjectiveMessage } from './features/goalPendingObjectiveBubble';
import {
  normalizeToolCallPayload,
  normalizeToolResultPayload,
} from './features/tool-events/toolEventNormalizer';
import { useWebSocket, mergePersistedGoalCompletionMessages, stampGoalObjectiveMessages } from './hooks';
import { webRequest } from './services/webClient';
import { useTeamPanelState } from './features/teamPanelState';
import { AgentMode, MediaItem, UserAnswer, ModelEntry, type Session } from './types';
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
  resetNewConversationRuntime,
} from './multi-session/state/newConversationLifecycle';
import { resolveNewConversationProjectDir } from './multi-session/state/newConversationProject';
import { toDisplaySessionTitle } from './utils/documentMessage';
import { createConversationSession } from './multi-session/state/createConversationSession';
import { useTranslation } from 'react-i18next';
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
import './App.css';

const TEAM_SESSION_MODES = new Set([
  'team',
  'team.plan',
  'team.plan.normal',
  'team.plan.code',
  'code.team',
]);
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
const EXTERNAL_CLI_AGENT_CONFIG_KEYS = new Set([
  "external_cli_agent_claude_enabled",
  "external_cli_agent_claude_use_builtin",
  "external_cli_agent_claude_cli_path",
  "external_cli_agent_codex_enabled",
  "external_cli_agent_codex_use_builtin",
  "external_cli_agent_codex_cli_path",
]);

function isTeamMode(mode: string): boolean {
  return TEAM_SESSION_MODES.has(mode);
}

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

type MainNavKey = 'chat' | 'skills' | 'agents' | 'teams' | 'sessions' | 'cron' | 'channels' | 'extensions' | 'configpanel' | 'browserpanel' | 'updatepanel';

type LoadedHistoryPage = {
  pageIdx: number;
  totalPages: number;
  result: FetchHistoryPageResult | null;
};

type AgentsTeamsSavePayload = {
  agents: Record<string, {
    model: { provider: string; api_base: string; api_key: string; model: string };
    skills: string[];
  }>;
  team: Array<{
    team_name: string;
    lifecycle: string;
    teammate_mode: string;
    spawn_mode: string;
    enable_permissions: boolean;
    external_cli_agents?: Array<{ cli_agent: "claude" | "codex"; cli_path?: string }>;
    external_cli_publish_url?: string;
    leader: { member_name: string; display_name: string; persona: string; agent_key: string };
    teammate: { agent_key: string };
    predefined_members: Array<{ member_name: string; display_name: string; persona: string; prompt_hint: string; agent_key: string }>;
  }>;
};

type ConfigSaveAllPayload = {
  config?: Record<string, string>;
  models?: ModelEntry[];
  agents?: AgentsTeamsSavePayload["agents"];
  team?: AgentsTeamsSavePayload["team"];
};

type ConfigSaveResult = {
  updated?: string[];
  applied_without_restart?: boolean;
  models_count?: number | null;
  external_cli_dependency_installs?: Partial<Record<ExternalCliAgentKind, ExternalCliDependencyInstallStatus>>;
};

type ExternalCliAgentKind = "claude" | "codex";

type ExternalCliDependencyInstallStatus = {
  cli_agent?: ExternalCliAgentKind;
  status?: string;
  phase?: string;
  error?: string;
  last_log?: string;
  log_tail?: string[];
  started_at?: number;
  finished_at?: number;
  updated_at?: number;
};

function externalCliDependencyInstallAgents(result: ConfigSaveResult | void): Set<ExternalCliAgentKind> {
  const installs = result?.external_cli_dependency_installs ?? {};
  return new Set(
    Object.entries(installs)
      .filter((entry): entry is [ExternalCliAgentKind, ExternalCliDependencyInstallStatus] => {
        const [cliAgent, status] = entry;
        return (cliAgent === "claude" || cliAgent === "codex") && !!status?.status;
      })
      .map(([cliAgent]) => cliAgent),
  );
}

function hasExternalCliDependencyInstallResult(result: ConfigSaveResult | void): boolean {
  return externalCliDependencyInstallAgents(result).size > 0;
}

function removeExternalCliAgentsFromTeamPayload(
  team: AgentsTeamsSavePayload["team"],
  cliAgents: Set<ExternalCliAgentKind>,
): AgentsTeamsSavePayload["team"] {
  if (cliAgents.size === 0) {
    return team;
  }
  return team.map((item) => ({
    ...item,
    external_cli_agents: item.external_cli_agents?.filter((agent) => !cliAgents.has(agent.cli_agent)),
  }));
}

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

function AppContent() {
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
  const [configError, setConfigError] = useState<string | null>(null);
  const [initialDataLoaded, setInitialDataLoaded] = useState(false);
  const [restartModalOpen, setRestartModalOpen] = useState(false);
  const [restartSuccess, setRestartSuccess] = useState(false);
  const [isExportingShare, setIsExportingShare] = useState(false);
  const [shareExportSnapshot, setShareExportSnapshot] = useState<ShareImageSnapshot | null>(null);
  const [restartSeenDisconnect, setRestartSeenDisconnect] = useState(false);
  const [appliedWithoutRestart, setAppliedWithoutRestart] = useState(false);
  const [a2uiRefreshPending, setA2uiRefreshPending] = useState(false);
  const [saveToastVisible, setSaveToastVisible] = useState(false);
  const [configChangedConfirmOpen, setConfigChangedConfirmOpen] = useState(false);
  const [proactiveToastVisible, setProactiveToastVisible] = useState(false);
  const [proactiveToastMessage, setProactiveToastMessage] = useState('');
  const [securityAlertVisible, setSecurityAlertVisible] = useState(false);
  const [securityAlertContent, setSecurityAlertContent] = useState('');
  const [hasVisitedSkills, setHasVisitedSkills] = useState(false);
  const [hasVisitedChannels, setHasVisitedChannels] = useState(false);
  const [sidebarMorePanelOpen, setSidebarMorePanelOpen] = useState(false);
  const [modelSetupGuideStep, setModelSetupGuideStep] = useState<ModelSetupGuideStep | null>(null);
  const [modelSetupGuideManual, setModelSetupGuideManual] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Session | null>(null);
  const [dialogBusy, setDialogBusy] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [composerFocusNonce, setComposerFocusNonce] = useState(0);
  const [missingSessionId, setMissingSessionId] = useState<string | null>(null);
  const startupUpdateCheckRef = useRef(false);
  const modelSetupGuideEvaluatedRef = useRef(false);
  /** 从 SkillNet 等入口跳转配置页时，首次展开对应配置分组（如第三方服务） */
  const [configInitialExpandGroup, setConfigInitialExpandGroup] = useState<string | null>(null);

  useEffect(() => {
    tRef.current = t;
  }, [t]);

  useEffect(() => {
    if (activeNav !== 'configpanel') {
      setConfigInitialExpandGroup(null);
    }
    if (activeNav === 'chat') {
      const { availableModels, setSelectedModelName } = useSessionStore.getState();
      const defaultModel = availableModels[0];
      const runtime = useSessionStore.getState().getRuntime(sessionId);
      if (defaultModel && !runtime?.selectedModelName) {
        useSessionStore.getState().ensureRuntime(sessionId);
        setSelectedModelName(sessionId, defaultModel.alias || defaultModel.model_name);
      }
    }
  }, [activeNav, sessionId]);

  useEffect(() => {
    if (!FEATURE_APP_UPDATER_UI && activeNav === 'updatepanel') {
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

  const restartAutoCloseTimerRef = useRef<number | null>(null);
  const saveToastTimerRef = useRef<number | null>(null);
  const proactiveToastTimerRef = useRef<number | null>(null);
  const hasChangesRef = useRef(false);
  const [historyLoadingMore, setHistoryLoadingMore] = useState(false);
  const [historyPrepending, setHistoryPrepending] = useState(false);
  const [historyRetrySessions, setHistoryRetrySessions] = useState<ReadonlySet<string>>(
    () => new Set()
  );
  /** 仅用于强制重跑「首屏 history」effect：从会话列表恢复时若 sessionId 未变，也要重新拉 history 并恢复 historyPagerMeta */
  const [historyBootstrapKey, setHistoryBootstrapKey] = useState(0);
  const sessionIdRef = useRef(sessionId);
  const sessionRestoreQueueRef = useRef<Promise<void>>(Promise.resolve());
  const historyLoadingSessionsRef = useRef(new Set<string>());
  const historyRestoreHandlesRef = useRef(new Map<string, HistoryRestoreHandle>());
  const historyPageHandlesRef = useRef(new Map<string, HistoryRestoreHandle>());
  const historyPagePromisesRef = useRef(new Map<string, Promise<LoadedHistoryPage | null>>());
  const historyPageCancelRef = useRef(new Map<string, () => void>());
  const historyBackgroundPrefetchTokensRef = useRef(new Map<string, number>());
  const creatingSessionRef = useRef(false);
  const sessionIdsCreatedInThisPageRef = useRef(new Set<string>());
  const shareExportRef = useRef<HTMLDivElement>(null);
  const shareExportFilenameRef = useRef('jiuwenswarm-share.png');
  const shareExportTokenRef = useRef(0);
  const preserveSelectedProjectOnChatNewRef = useRef(false);
  const newConversationProjectRef = useRef<Pick<Session, 'project_id' | 'project_dir'> | null>(null);
  const newConversationPreviousSessionRef = useRef<{
    sessionId: string;
    mode: AgentMode;
  } | null>(null);
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
    setHistoryLoadingMore(false);
    setHistoryPrepending(historyLoadingSessionsRef.current.has(sessionId));
  }, [sessionId]);

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

  useEffect(() => {
    if (route.kind === 'chat-session') {
      sessionIdRef.current = route.sessionId;
      setSessionId(route.sessionId);
      setActiveNav('chat');
    } else if (route.kind === 'chat-new') {
      if (window.location.pathname !== '/chat/new') navigate({ kind: 'chat-new' }, { replace: true });
      if (preserveSelectedProjectOnChatNewRef.current) {
        preserveSelectedProjectOnChatNewRef.current = false;
      } else {
        useWorkspaceStore.getState().setSelectedProject(null);
      }
      sessionIdRef.current = 'new';
      setSessionId('new');
      setActiveNav('chat');
      setTeamAreaExpanded(false);
    }
  }, [navigate, route, setTeamAreaExpanded]);

  useEffect(() => {
    ensureSessionRuntimes(sessionId);
    useChatStore.getState().setActiveSessionId(sessionId);
  }, [sessionId]);

  useEffect(() => {
    if (!initialDataLoaded) {
      return;
    }
    void loadProjects();
  }, [initialDataLoaded, loadProjects]);

  const { setCurrentSession, setAvailableModels, setMode, setTeamLeaderMemberIds } = useSessionStore();
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

  useEffect(() => {
    setCodeReviewTarget(null);
  }, [sessionId]);

  const handleToggleDetailPanel = useCallback((expanded: boolean) => {
    if (expanded && mode !== 'team' && teamAreaActiveTab === 'team') {
      setTeamAreaActiveTab('planning');
    }
    setTeamAreaExpanded(expanded);
  }, [mode, setTeamAreaActiveTab, setTeamAreaExpanded, teamAreaActiveTab]);

  const handleOpenCodeReview = useCallback((target: CodeReviewTarget) => {
    setCodeReviewTarget(target);
    setTeamAreaActiveTab('review');
    setTeamAreaExpanded(true);
  }, [setTeamAreaActiveTab, setTeamAreaExpanded]);

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
  const clearSubtasks = useChatStore((s) => s.clearSubtasks);
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
      ...Array.from(historyPageHandlesRef.current.keys(), (key) => key.split(':', 1)[0]),
      ...historyLoadingSessionsRef.current,
    ])) {
      cancelSession(targetSid);
    }
  }, [setHistoryRetryAvailable, setLoadingHistory]);

  useEffect(() => () => disposeInFlightHistoryHandles(), [disposeInFlightHistoryHandles]);
  const todos = useTodoStore((s) => s.runtimes[sessionId]?.todos ?? []);
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
        return todos.length > 0 || hasMessages || hasCodeEnvironment;
    }
  }, [mode, todos.length, teamTaskEvents.length, teamTasks.length, teamMembers.length, extensionReady?.runtimePath, messages.length, isRestoringTeamHistory, sessionId, sessionProject?.work_mode]);
  // 单 agent 模式同样复用集群模式的展开布局（百分比宽度 + 可拖拽分割线），
  // 避免右侧面板与聊天面板平分空间导致宽度与集群模式不一致；auto_harness 走收起态分支。
  const isTeamAreaExpanded = mode !== 'auto_harness' && teamAreaExpanded && toolPanelHasContent;

  // WebSocket 连接 - provider 由后端配置决定 - provider 由后端配置决定，前端默认不在 URL query 传递
  const {
    isConnected,
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
  }, [addToolCall, addToolResult, prependMessages, settleHistoricalToolExecutions]);

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
          const totalPages = emptyTotalPages ?? fallbackTotalPages;
          settle({ pageIdx, totalPages, result: null });
        },
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
        if (shouldPreviewModelSetupGuide() || isSetupGuideEnabled(config.setup_guide_enabled)) {
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
    setA2uiRefreshPending(false);
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

  const validateModelConfig = useCallback(
    async (fields: {
      api_base: string;
      api_key: string;
      model: string;
      model_provider: string;
      reasoning_level?: string;
    }) => {
      await request('config.validate_model', fields, { timeoutMs: 60000 });
    },
    [request],
  );

  const handleModelsReplaceAll = useCallback(async (models: ModelEntry[]) => {
    await request('models.replace_all', { models });
  }, [request]);

  const handleConfigChanged = useCallback(() => {
    if (hasChangesRef.current) {
      setConfigChangedConfirmOpen(true);
      return;
    }
    void fetchConfig();
  }, [fetchConfig]);

  const handleHasChangesChange = useCallback((hasChanges: boolean) => {
    hasChangesRef.current = hasChanges;
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

  const saveConfigAndRestart = useCallback(async (updates: Record<string, string>): Promise<ConfigSaveResult> => {
    const payload = await request<ConfigSaveResult>(
      'config.set',
      updates
    );
    const hasExternalCliDependencyInstall = hasExternalCliDependencyInstallResult(payload);
    const effectiveUpdates = hasExternalCliDependencyInstall
      ? Object.fromEntries(
          Object.entries(updates).filter(([key]) => !EXTERNAL_CLI_AGENT_CONFIG_KEYS.has(key)),
        )
      : updates;
    setServerConfig((prev) => {
      if (!prev) return effectiveUpdates;
      const next: Record<string, unknown> = { ...prev, ...effectiveUpdates };
      // Keep the bilingual memory_forbidden_description dictionary structure.
      if (typeof prev?.memory_forbidden_description === 'object' && prev.memory_forbidden_description !== null
          && !Array.isArray(prev.memory_forbidden_description) && effectiveUpdates.memory_forbidden_description !== undefined) {
        const prevDict = prev.memory_forbidden_description as Record<string, string>;
        const lang = i18n.language || 'zh';
        next.memory_forbidden_description = { ...prevDict, [lang]: effectiveUpdates.memory_forbidden_description };
      }
      return next;
    });
    if (hasExternalCliDependencyInstall) {
      return payload;
    }
    setConfigError(null);
    setRestartModalOpen(true);
    setRestartSuccess(false);
    setRestartSeenDisconnect(false);
    if ('a2ui_enabled' in updates) {
      setAppliedWithoutRestart(false);
      setA2uiRefreshPending(true);
      setRestartSuccess(true);
      clearRestartAutoCloseTimer();
      restartAutoCloseTimerRef.current = window.setTimeout(() => {
        closeRestartModal();
        window.location.reload();
      }, 5000);
    } else {
      setAppliedWithoutRestart(payload?.applied_without_restart === true);
      clearRestartAutoCloseTimer();
      if (payload?.applied_without_restart === true) {
        setRestartSuccess(true);
        restartAutoCloseTimerRef.current = window.setTimeout(() => {
          closeRestartModal();
        }, 5000);
      }
    }
    return payload;
  }, [clearRestartAutoCloseTimer, closeRestartModal, request]);

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

  const buildAgentsTeamsFlatConfig = useCallback((payload: AgentsTeamsSavePayload) => {
    const updates: Record<string, string> = {};
    const agentCount = Object.keys(payload.agents).length;
    Object.entries(payload.agents).forEach(([name, agent], idx) => {
      updates[`agent_name_${idx}`] = name;
      updates[`agent_model_${idx}`] = agent.model.model;
      updates[`agent_skills_${idx}`] = agent.skills.join(',');
    });
    for (let i = agentCount; i < 10; i++) {
      updates[`agent_name_${i}`] = "";
      updates[`agent_model_${i}`] = "";
      updates[`agent_skills_${i}`] = "";
    }
    payload.team.forEach((team, idx) => {
      // 使用与后端一致的键名格式：team_${idx}_name
      updates[`team_${idx}_name`] = team.team_name;
      updates[`team_${idx}_lifecycle`] = team.lifecycle;
      updates[`team_${idx}_teammate_mode`] = team.teammate_mode;
      updates[`team_${idx}_spawn_mode`] = team.spawn_mode;
      updates[`team_${idx}_enable_permissions`] = String(team.enable_permissions);
      updates[`team_${idx}_leader_member_name`] = team.leader.member_name;
      updates[`team_${idx}_leader_display_name`] = team.leader.display_name;
      updates[`team_${idx}_leader_persona`] = team.leader.persona;
      updates[`team_${idx}_leader_agent_key`] = team.leader.agent_key;
      updates[`team_${idx}_teammate_agent_key`] = team.teammate.agent_key;
      updates[`team_${idx}_predefined_members`] = team.predefined_members?.length
        ? JSON.stringify(team.predefined_members)
        : "";
      updates[`team_${idx}_external_cli_agents`] = team.external_cli_agents?.length
        ? JSON.stringify(team.external_cli_agents)
        : "";
    });
    for (let i = payload.team.length; i < 10; i++) {
      // 使用与后端一致的键名格式：team_${i}_name
      updates[`team_${i}_name`] = "";
      updates[`team_${i}_lifecycle`] = "";
      updates[`team_${i}_teammate_mode`] = "";
      updates[`team_${i}_spawn_mode`] = "";
      updates[`team_${i}_enable_permissions`] = "";
      updates[`team_${i}_leader_member_name`] = "";
      updates[`team_${i}_leader_display_name`] = "";
      updates[`team_${i}_leader_persona`] = "";
      updates[`team_${i}_leader_agent_key`] = "";
      updates[`team_${i}_teammate_agent_key`] = "";
      updates[`team_${i}_predefined_members`] = "";
      updates[`team_${i}_external_cli_agents`] = "";
    }
    return updates;
  }, []);

  const handleAgentsTeamsSave = useCallback(async (payload: AgentsTeamsSavePayload) => {
    const result = await request<{ updated?: string[]; applied_without_restart?: boolean }>(
      'config.set',
      payload as unknown as Record<string, string>
    );
    // 更新前端配置缓存
    const updates = buildAgentsTeamsFlatConfig(payload);
    setServerConfig((prev: Record<string, unknown> | null) => ({ ...prev, ...updates }));
    applyConfigSaveUiState(result?.applied_without_restart === true);
  }, [applyConfigSaveUiState, buildAgentsTeamsFlatConfig, request]);

  const saveAllConfigAndRestart = useCallback(async (payload: ConfigSaveAllPayload): Promise<ConfigSaveResult> => {
    const isA2UIChange = payload.config && 'a2ui_enabled' in payload.config;
    const result = await request<ConfigSaveResult>(
      'config.save_all',
      payload as unknown as Record<string, unknown>
    );
    const pendingExternalCliAgents = externalCliDependencyInstallAgents(result);
    const hasExternalCliDependencyInstall = pendingExternalCliAgents.size > 0;
    setServerConfig((prev) => {
      const next: Record<string, unknown> = { ...(prev ?? {}) };
      if (payload.config) {
        const effectiveConfig = hasExternalCliDependencyInstall
          ? Object.fromEntries(
              Object.entries(payload.config).filter(([key]) => !EXTERNAL_CLI_AGENT_CONFIG_KEYS.has(key)),
            )
          : payload.config;
        Object.assign(next, effectiveConfig);
        if (typeof prev?.memory_forbidden_description === 'object' && prev.memory_forbidden_description !== null
            && !Array.isArray(prev.memory_forbidden_description)
            && effectiveConfig.memory_forbidden_description !== undefined) {
          const prevDict = prev.memory_forbidden_description as Record<string, string>;
          const lang = i18n.language || 'zh';
          next.memory_forbidden_description = {
            ...prevDict,
            [lang]: effectiveConfig.memory_forbidden_description,
          };
        }
      }
      if (payload.agents !== undefined || payload.team !== undefined) {
        const agents = payload.agents || {};
        const team = removeExternalCliAgentsFromTeamPayload(payload.team || [], pendingExternalCliAgents);
        Object.assign(next, buildAgentsTeamsFlatConfig({
          agents,
          team,
        }));
      }
      return next;
    });
    if (hasExternalCliDependencyInstall) {
      return result;
    }
    if (isA2UIChange) {
      // Show modal then refresh page after 5 seconds
      setConfigError(null);
      setRestartModalOpen(true);
      setRestartSuccess(true);
      setRestartSeenDisconnect(false);
      setAppliedWithoutRestart(false);
      setA2uiRefreshPending(true);
      clearRestartAutoCloseTimer();
      restartAutoCloseTimerRef.current = window.setTimeout(() => {
        closeRestartModal();
        window.location.reload();
      }, 5000);
    } else {
      applyConfigSaveUiState(result?.applied_without_restart === true);
    }
    return result;
  }, [applyConfigSaveUiState, buildAgentsTeamsFlatConfig, i18n.language, request]);

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
    if (existingRuntime && existingRuntime.historyPagerMeta) {
      setLoadingHistory(sessionId, false);
      startBackgroundHistoryPrefetch(
        sessionId,
        existingRuntime.historyPagerMeta.loadedPages,
        existingRuntime.historyPagerMeta.totalPages
      );
      return;
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
        clearSubtasks(sessionId);
        for (const item of items) {
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
    settleHistoricalToolExecutions,
    clearMessages,
    clearSubtasks,
    disposeInFlightHistoryHandles,
    setLoadingHistory,
    setHistoryPagerMeta,
    replaceHistoryMessages,
    restoreReasoningSegments,
    startBackgroundHistoryPrefetch,
  ]);

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

  const enterNewConversation = useCallback((targetMode: AgentMode = mode, options: NewConversationOptions = {}) => {
    const currentSessionId = sessionIdRef.current;
    const currentRuntime = useSessionStore.getState().getRuntime(currentSessionId);
    newConversationPreviousSessionRef.current =
      currentSessionId && currentSessionId !== NEW_CONVERSATION_ID
        ? {
          sessionId: currentSessionId,
          mode: currentRuntime?.mode ?? mode,
        }
        : null;
    // 新建会话固定使用配置的默认模型，不继承当前会话手动切换过的模型；
    // 默认模型列表尚未加载完成时兜底沿用当前会话的模型，避免新会话没有模型可用。
    const selectedModelName = useSessionStore.getState().defaultModelName ?? currentRuntime?.selectedModelName ?? null;
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
    resetNewConversationRuntime({ mode: targetMode, selectedModelName, projectDir });
    if (options.initialInputValue) {
      useChatStore.getState().setInputValue(NEW_CONVERSATION_ID, options.initialInputValue);
    }
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
    navigate({ kind: 'chat-new' });
    setActiveNav('chat');
    requestComposerFocus();
  }, [disposeInFlightHistoryHandles, mode, navigate, requestComposerFocus, setCurrentSession, setSelectedProject, setTeamAreaExpanded]);

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

  const handleSendMessage = useCallback(async (content: string, mediaItems?: MediaItem[]) => {
    const currentSessionId = sessionIdRef.current;
    if (!currentSessionId) return;
    if (currentSessionId === NEW_CONVERSATION_ID) {
      if (creatingSessionRef.current) return;
      creatingSessionRef.current = true;
      useChatStore.getState().setProcessing(NEW_CONVERSATION_ID, true);
      const newRuntime = useSessionStore.getState().getRuntime(NEW_CONVERSATION_ID);
      const runtimeSettings = {
        mode: newRuntime?.mode ?? mode,
        selectedModelName: useSessionStore.getState().getEffectiveModelName(NEW_CONVERSATION_ID),
        projectDir: newRuntime?.projectDirectory ?? null,
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
          title: createConversationTitle(content).slice(0, 100),
          work_mode: workContext.work_mode,
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
          runtimeSettings,
          Date.now(),
          content,
          {
            project_id: created.project_id || workContext.project_id,
            project_dir: created.project_dir || workContext.project_dir,
            work_mode: created.work_mode || workContext.work_mode,
          },
        );
        // 迁移 'new' 会话的已选技能到新会话
        const pendingSkills = useSessionStore.getState().getRuntime(NEW_CONVERSATION_ID)?.selectedSkills ?? [];
        pendingSkills.forEach((skill) => useSessionStore.getState().addSelectedSkill(newSid, skill));
        useSessionStore.getState().clearSelectedSkills(NEW_CONVERSATION_ID);
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
          queueOrAddGoalObjectiveMessage(newSid, content);
          setGoalObjective(newSid, content);
        } else {
          const sent = await sendMessage(content, newSid, mediaItems);
          if (!sent) {
            useChatStore.getState().setInputValue(newSid, content);
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
          });
        } catch (error) {
          if (isTeamMode(resolvedMode)) {
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
        clearSubtasks(targetSessionId);
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
        // 还原后端记录的会话模型：打开会话时若后端 metadata 带 model，写进
        // runtime.selectedModelName，避免 selectedModelName 为空被全局默认兜底，
        // 导致界面显示成默认模型（如定时任务选了非默认模型的会话，bug002）。
        if (targetSession.model) {
          useSessionStore.getState().setSelectedModelName(targetSessionId, targetSession.model);
        }
      } else {
        setCurrentSession(null);
      }
      if (resolvedMode) {
        setMode(targetSessionId, resolvedMode as AgentMode);
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
      clearSubtasks,
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
    void handleRestoreSession(target.session_id, target.mode, target);
  }, [enterNewConversation, handleRestoreSession, mode]);

  const handleTeamSessionsDeleted = useCallback(async (sessionIds: string[]) => {
    const deletedSessionIds = new Set(sessionIds);
    const sessionState = useSessionStore.getState();

    for (const deletedSessionId of deletedSessionIds) {
      forgetCreatedConversation(deletedSessionId);
      sessionState.removeSession(deletedSessionId);
      sessionState.removeRuntime(deletedSessionId);
      useChatStore.getState().removeRuntime(deletedSessionId);
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
        enterNewConversation();
      }
    } catch { setDialogError(t('multiSession.errors.delete')); }
    finally { setDialogBusy(false); }
  }, [deleteTarget, enterNewConversation, request, t]);

  const handleNavigate = useCallback((nav: MainNavKey) => {
    setActiveNav(nav);
    if (modelSetupGuideStep === 1 && nav === 'configpanel') {
      setModelSetupGuideStep(2);
    }
    if (nav === 'skills') setHasVisitedSkills(true);
    if (nav === 'channels') setHasVisitedChannels(true);
  }, [modelSetupGuideStep]);

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

  const openModelSetupGuide = useCallback(() => {
    setActiveNav('chat');
    setModelSetupGuideManual(true);
    setModelSetupGuideStep(0);
  }, []);

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
  const showWorkspaceDivider = isTeamAreaExpanded && !showConversationNotFound;
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
      className={`shell shell--icon-rail ${sidebarMorePanelOpen ? 'shell--more-panel-open' : ''}`}
      data-testid="app-shell"
      data-session-id={sessionId}
    >
      {/* Navigation Sidebar */}
      <SessionSidebar
        activeNav={activeNav}
        onNavigate={handleNavigate}
        appVersion={typeof serverConfig?.app_version === 'string' ? serverConfig.app_version : ''}
        isConnected={isConnected}
        onNewSession={handleNewSession}
        showNewSession={false}
        hiddenNavItems={['sessions']}
        onMorePanelOpenChange={setSidebarMorePanelOpen}
        onSetupGuideRequest={openModelSetupGuide}
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
                  className={`${showConversationNotFound ? 'hidden' : 'flex'} chat-layout__surface p-3 pt-0 flex-col min-w-0 min-h-0 ${isTeamAreaExpanded ? '' : 'flex-1'}`}
                  style={isTeamAreaExpanded ? { width: `${chatPanelWidthPct}%` } : undefined}
                  data-testid="app-chat-surface"
                >
                  <div className={`flex-1 min-h-0`}>
                    <ChatPanel
                      onSendMessage={handleSendMessage}
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
                      teamAreaExpanded={isTeamAreaExpanded}
                      autoFocusKey={composerFocusKey}
                      onNavigateToSkills={() => handleNavigate('skills')}
                      onToggleTeamArea={handleToggleDetailPanel}
                      onOpenCodeReview={handleOpenCodeReview}
                      permissionsEnabled={serverConfig?.permissions_enabled !== 'false'}
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
                {(toolPanelHasContent || isRestoringTeamHistory) && !showConversationNotFound && (
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
                    setTeamAreaExpanded={setTeamAreaExpanded}
                    setTeamAreaActiveTab={setTeamAreaActiveTab}
                    setTeamAreaActiveDetailTab={setTeamAreaActiveDetailTab}
                    setTeamAreaSelectedMemberId={setTeamAreaSelectedMemberId}
                    setCodeReviewTarget={setCodeReviewTarget}
                    setTeamAreaSelectedArtifactId={setTeamAreaSelectedArtifactId}
                  />
                )}
              </div>
            </div>
          </>
        )}
        {activeNav === 'agents' && (
          <div className="app-section">
            <AgentPanel sessionId={sessionId} />
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
        {activeNav === 'configpanel' && (
          <div className="app-section">
            <ConfigPanel
              config={serverConfig}
              isConnected={isConnected}
              sessionId={sessionId}
              onSaveConfig={saveConfigAndRestart}
              onSaveAllConfig={saveAllConfigAndRestart}
              onValidateModel={validateModelConfig}
              initialExpandGroupTag={configInitialExpandGroup}
              onModelsReplaceAll={handleModelsReplaceAll}
              onModelValidate={validateModelConfig}
              onModelsRefresh={handleModelsRefresh}
              onAgentsTeamsSave={handleAgentsTeamsSave}
              onHasChangesChange={handleHasChangesChange}
              onDetectExternalCli={detectExternalCli}
              onSelectExternalCliPath={selectExternalCliPath}
              onGetExternalCliDependencyInstallStatus={getExternalCliDependencyInstallStatus}
            />
          </div>
        )}
        {activeNav === 'browserpanel' && (
          <div className="app-section">
            <BrowserPanel isConnected={isConnected} request={request} />
          </div>
        )}
        {FEATURE_APP_UPDATER_UI && activeNav === 'updatepanel' && (
          <div className="app-section">
            <UpdatePanel isConnected={isConnected} request={request} />
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
              onNavigateToConfig={() => {
                setConfigInitialExpandGroup('third_party_api');
                setActiveNav('configpanel');
              }}
            />
          </div>
        )}
        {hasVisitedChannels && (
          <div className={`app-section ${activeNav === 'channels' ? '' : 'is-hidden'}`}>
            <ChannelsPanel isConnected={isConnected} />
          </div>
        )}
        {activeNav === 'extensions' && (
          <div className="app-section">
            <ExtensionsHubPanel sessionId={sessionId} isConnected={isConnected} />
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
                  : a2uiRefreshPending
                    ? t('app.a2uiRefresh')
                    : appliedWithoutRestart
                      ? t('app.configApplied')
                      : t('app.restartSuccess')}
              </h3>
              <p className="text-sm text-text-muted mb-5" data-testid="app-restart-modal-description">
                {!restartSuccess
                  ? t('app.restartWaiting')
                  : a2uiRefreshPending
                    ? t('app.a2uiRefreshDesc')
                    : appliedWithoutRestart
                      ? t('app.configAppliedDesc')
                      : t('app.restartSuccessDesc')}
              </p>
              {restartSuccess && (
                <button
                  type="button"
                  onClick={() => {
                    if (a2uiRefreshPending) {
                      window.location.reload();
                    } else {
                      closeRestartModal();
                    }
                  }}
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

      {configChangedConfirmOpen && (
        <div className="app-restart-modal" data-testid="app-config-changed-modal">
          <div className="app-restart-modal__backdrop" data-testid="app-config-changed-modal-backdrop" />
          <div className="app-restart-modal__panel" data-testid="app-config-changed-modal-panel">
            <div className="flex flex-col items-center text-center" data-testid="app-config-changed-modal-body">
              <div className="w-12 h-12 rounded-full bg-warn-subtle text-warn flex items-center justify-center mb-4" data-testid="app-config-changed-modal-icon">
                <svg className="w-7 h-7" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
                </svg>
              </div>
              <h3 className="text-base font-semibold text-text mb-1" data-testid="app-config-changed-modal-title">{t('config.errors.configChangedTitle')}</h3>
              <p className="text-sm text-text-muted mb-5" data-testid="app-config-changed-modal-description">{t('config.errors.configChangedDesc')}</p>
              <div className="flex gap-3" data-testid="app-config-changed-modal-actions">
                <button type="button" onClick={() => { setConfigChangedConfirmOpen(false); void fetchConfig(); }} className="btn primary !px-4 !py-2" data-testid="app-config-changed-modal-confirm">
                  {t('config.errors.configChangedConfirm')}
                </button>
                <button type="button" onClick={() => setConfigChangedConfirmOpen(false)} className="btn !px-4 !py-2" data-testid="app-config-changed-modal-cancel">
                  {t('config.errors.configChangedCancel')}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="share-image-stage" aria-hidden="true" data-testid="app-share-image-stage">
        <ShareImageDocument ref={shareExportRef} snapshot={shareExportSnapshot} />
      </div>
    </div>
  );
}

function App() {
  return (
    <ErrorBoundary>
      <AppContent />
    </ErrorBoundary>
  );
}

export default App;
