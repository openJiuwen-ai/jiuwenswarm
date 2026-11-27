/**
 * App 主组件
 *
 * 应用主布局，整合所有组件
 */

import { useState, useCallback, useEffect, useRef, Component, ReactNode } from 'react';
import { ChatPanel } from './components/ChatPanel';
import { SessionSidebar } from './components/SessionSidebar';
import { ToolPanel } from './components/ToolPanel';
import { StatusBar } from './components/StatusBar';
import { HeartbeatMessageModal } from './features/HeartbeatMessageModal';
import {
  beginHistoryRestore,
  fetchHistoryPage,
  HISTORY_GET_METHOD,
  type HistoryRestoreHandle,
} from './features/historyRestore';
import {
  normalizeToolCallPayload,
  normalizeToolResultPayload,
  tryDeepResearchStandaloneAssistantTurn,
} from './features/tool-events/toolEventNormalizer';
import { useWebSocket } from './hooks';
import { webRequest } from './services/webClient';
import { AgentMode, UserAnswer } from './types';
import { useSessionStore, useChatStore, useTodoStore } from './stores';
import { useTranslation } from 'react-i18next';
import i18n from './i18n';
import './App.css';

type MainNavKey = 'chat';

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
    <div className="flex items-center justify-center h-screen bg-bg text-text p-8">
      <div className="max-w-2xl card">
        <h1 className="text-2xl font-bold text-danger mb-4">
          {t('app.errorTitle')}
        </h1>
        <p className="text-text-muted mb-4">
          {error?.message || t('app.unknownError')}
        </p>
        <pre className="bg-secondary p-4 rounded-lg text-sm overflow-auto max-h-64 font-mono">
          {error?.stack}
        </pre>
        <button
          onClick={() => window.location.reload()}
          className="btn primary mt-4"
        >
          {t('app.reload')}
        </button>
      </div>
    </div>
  );
}

// 语言切换组件（与 config.yaml preferred_language 同步）
function LanguageSwitcher() {
  const { i18n } = useTranslation();
  const isZh = i18n.language.startsWith('zh');
  const handleChange = (lang: 'zh' | 'en') => {
    i18n.changeLanguage(lang);
    void webRequest('locale.set_conf', { preferred_language: lang }).catch(() => {
      // 写回 config 失败时静默忽略，本地切换仍生效
    });
  };
  return (
    <div className="flex items-center gap-1 rounded-lg bg-secondary/60 px-2 py-1">
      <button
        type="button"
        onClick={() => handleChange('zh')}
        className={`text-xs px-2 py-1 rounded ${isZh ? 'bg-accent text-white font-medium' : 'text-text-muted hover:text-text'}`}
      >
        中
      </button>
      <button
        type="button"
        onClick={() => handleChange('en')}
        className={`text-xs px-2 py-1 rounded ${!isZh ? 'bg-accent text-white font-medium' : 'text-text-muted hover:text-text'}`}
      >
        En
      </button>
    </div>
  );
}

// 主题切换组件
function ThemeToggle() {
  const { t } = useTranslation();
  const [theme, setTheme] = useState(() => {
    return localStorage.getItem('theme') || 'light';
  });

  const toggleTheme = (newTheme: string) => {
    setTheme(newTheme);
    localStorage.setItem('theme', newTheme);
    if (newTheme === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
  };

  const themeIndex = theme === 'system' ? 0 : theme === 'dark' ? 1 : 2;

  return (
    <div className="theme-toggle">
      <div className="theme-toggle__track" style={{ '--theme-index': themeIndex } as React.CSSProperties}>
        <div className="theme-toggle__indicator" />
        <button
          className={`theme-toggle__button ${theme === 'system' ? 'active' : ''}`}
          onClick={() => toggleTheme('system')}
          title={t('app.themeSystem')}
        >
          <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
            <line x1="8" y1="21" x2="16" y2="21" />
            <line x1="12" y1="17" x2="12" y2="21" />
          </svg>
        </button>
        <button
          className={`theme-toggle__button ${theme === 'dark' ? 'active' : ''}`}
          onClick={() => toggleTheme('dark')}
          title={t('app.themeDark')}
        >
          <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
          </svg>
        </button>
        <button
          className={`theme-toggle__button ${theme === 'light' ? 'active' : ''}`}
          onClick={() => toggleTheme('light')}
          title={t('app.themeLight')}
        >
          <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="5" />
            <line x1="12" y1="1" x2="12" y2="3" />
            <line x1="12" y1="21" x2="12" y2="23" />
            <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
            <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
            <line x1="1" y1="12" x2="3" y2="12" />
            <line x1="21" y1="12" x2="23" y2="12" />
            <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
            <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
          </svg>
        </button>
      </div>
    </div>
  );
}

// 会话 ID 持久化（使用 sessionStorage：同标签页刷新保留，多标签页隔离）
const SESSION_STORAGE_KEY = 'openjiuwen_current_session';

function generateSessionId(): string {
  const ts = Date.now().toString(16);
  const rand = Math.random().toString(16).slice(2, 8);
  return `sess_${ts}_${rand}`;
}

function getStoredSessionId(): string | null {
  try {
    return sessionStorage.getItem(SESSION_STORAGE_KEY);
  } catch {
    return null;
  }
}

function storeSessionId(sessionId: string | null) {
  try {
    if (sessionId && sessionId !== 'new') {
      sessionStorage.setItem(SESSION_STORAGE_KEY, sessionId);
    } else {
      sessionStorage.removeItem(SESSION_STORAGE_KEY);
    }
  } catch {
    // ignore
  }
}

function AppContent() {
  const { t } = useTranslation();
  // 优先使用存储的会话 ID，避免每次刷新创建新会话
  const [sessionId, setSessionId] = useState<string>(() => {
    const stored = getStoredSessionId();
    return stored || 'new';
  });
  const [activeNav, setActiveNav] = useState<MainNavKey>('chat');
  const [serverConfig, setServerConfig] = useState<Record<string, unknown> | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [initialDataLoaded, setInitialDataLoaded] = useState(false);
  const [restartModalOpen, setRestartModalOpen] = useState(false);
  const [restartSuccess, setRestartSuccess] = useState(false);
  const [restartSeenDisconnect, setRestartSeenDisconnect] = useState(false);
  const [appliedWithoutRestart, setAppliedWithoutRestart] = useState(false);
  const [newSessionToastVisible, setNewSessionToastVisible] = useState(false);
  const [heartbeatToastVisible, setHeartbeatToastVisible] = useState(false);
  const [heartbeatToastMessage, setHeartbeatToastMessage] = useState('');
  const [heartbeatModalOpen, setHeartbeatModalOpen] = useState(false);
  useEffect(() => {
    if (activeNav === 'chat') {
      const { availableModels, setSelectedModelName } = useSessionStore.getState();
      const defaultModel = availableModels[0]?.model_name;
      if (defaultModel) {
        setSelectedModelName(defaultModel);
      }
    }
  }, [activeNav]);

  const restartAutoCloseTimerRef = useRef<number | null>(null);
  const newSessionToastTimerRef = useRef<number | null>(null);
  const heartbeatToastTimerRef = useRef<number | null>(null);
  const lastHeartbeatToastKeyRef = useRef<string | null>(null);
  /** 自「恢复会话」加载 history 后的分页元数据；用于聊天区顶部加载更早消息 */
  const [historyPagerMeta, setHistoryPagerMeta] = useState<{
    loadedPages: number;
    totalPages: number;
  } | null>(null);
  const [historyLoadingMore, setHistoryLoadingMore] = useState(false);
  const sessionIdRef = useRef(sessionId);
  const historyRestoreHandleRef = useRef<HistoryRestoreHandle | null>(null);
  const historyPageHandleRef = useRef<HistoryRestoreHandle | null>(null);
  /** 为 true 表示刚从「会话列表」恢复；history 为空时在 useEffect 的 onEmpty 中提示一次 */
  const historyRestoreFromPanelHintRef = useRef(false);

  const disposeInFlightHistoryHandles = useCallback(() => {
    historyRestoreHandleRef.current?.dispose();
    historyRestoreHandleRef.current = null;
    historyPageHandleRef.current?.dispose();
    historyPageHandleRef.current = null;
  }, []);

  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);

  useEffect(() => () => disposeInFlightHistoryHandles(), [disposeInFlightHistoryHandles]);

  const { setCurrentSession, setSessions, setAvailableModels, mode, heartbeatMessage, heartbeatUpdatedAt } = useSessionStore();
  const {
    clearMessages,
    clearSubtasks,
    addMessage,
    addToolCall,
    addToolResult,
    prependMessages,
    isProcessing,
    setProcessing,
    setThinking,
    setPaused,
  } = useChatStore();
  const { clearTodos } = useTodoStore();

  // WebSocket 连接 - provider 由后端配置决定 - provider 由后端配置决定，前端默认不在 URL query 传递
  const {
    isConnected,
    request,
    sendMessage,
    pause,
    cancel,
    supplement,
    resume,
    switchMode,
    sendUserAnswer,
  } = useWebSocket({
    activeSessionId: sessionId,
    onConnect: (payload) => {
      const currentStored = getStoredSessionId();
      if (payload.session_id) {
        // 仅在尚无有效 session 时采纳后端分配的 session_id；
        // 重连时保持已有会话，防止被覆盖
        if (!currentStored) {
          console.log('Adopting backend session:', payload.session_id);
          setSessionId(payload.session_id);
          storeSessionId(payload.session_id);
        } else {
          console.log('Keeping existing session:', currentStored);
        }
      } else if (!currentStored) {
        // 后端未提供 session_id 且本地也无有效 session：兜底生成
        const fallbackSid = generateSessionId();
        console.log('Generated fallback session:', fallbackSid);
        setSessionId(fallbackSid);
        storeSessionId(fallbackSid);
      }
    },
    onDisconnect: () => {
      console.log('Disconnected');
    },
    onError: (error) => {
      console.error('WebSocket error:', error);
    },
  });

  // 获取会话列表
  const fetchSessions = useCallback(async () => {
    try {
      const payload = await request<{ sessions?: unknown[] }>('session.list', {
        limit: 20,
      });
      if (payload?.sessions && Array.isArray(payload.sessions)) {
        // 兼容新格式(对象数组)和旧格式(字符串数组)
        const normalized = payload.sessions.map((item) => {
          if (typeof item === 'string') {
            return { session_id: item } as Parameters<typeof setSessions>[0][number];
          }
          if (item && typeof item === 'object') {
            return item as Parameters<typeof setSessions>[0][number];
          }
          return null;
        }).filter(Boolean) as Parameters<typeof setSessions>[0];
        setSessions(normalized);
      }
    } catch (error) {
      console.error('Failed to fetch sessions:', error);
    }
  }, [request, setSessions]);

  // 获取服务端配置（通过 WS 方法）
  const fetchConfig = useCallback(async () => {
    try {
      const config = await request<Record<string, unknown>>('config.get');
      setServerConfig(config);
      setConfigError(null);
    } catch (error) {
      console.error('Failed to fetch config:', error);
      setServerConfig(null);
      setConfigError(t('app.configError'));
    }
    // 同步获取多模型列表
    try {
      const resp = await request<{ models: Array<{ model_name: string; api_base: string; api_key: string; model_provider: string; temperature?: number }>; active_model: string }>('models.list');
      if (resp?.models) {
        setAvailableModels(resp.models, resp.active_model);
      }
    } catch (error) {
      console.warn('Failed to fetch models list:', error);
    }
  }, [request, t, setAvailableModels]);

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

  const clearNewSessionToastTimer = useCallback(() => {
    if (newSessionToastTimerRef.current != null) {
      window.clearTimeout(newSessionToastTimerRef.current);
      newSessionToastTimerRef.current = null;
    }
  }, []);

  const clearHeartbeatToastTimer = useCallback(() => {
    if (heartbeatToastTimerRef.current != null) {
      window.clearTimeout(heartbeatToastTimerRef.current);
      heartbeatToastTimerRef.current = null;
    }
  }, []);

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
      clearNewSessionToastTimer();
      clearHeartbeatToastTimer();
    };
  }, [clearHeartbeatToastTimer, clearNewSessionToastTimer, clearRestartAutoCloseTimer]);

  useEffect(() => {
    const normalized = heartbeatMessage?.trim();
    if (!normalized) {
      return;
    }
    if (normalized.toUpperCase() === 'HEARTBEAT_OK') {
      return;
    }
    const toastKey = `${heartbeatUpdatedAt ?? ''}::${normalized}`;
    if (lastHeartbeatToastKeyRef.current === toastKey) {
      return;
    }
    lastHeartbeatToastKeyRef.current = toastKey;
    setHeartbeatToastMessage(normalized);
    setHeartbeatToastVisible(true);
    clearHeartbeatToastTimer();
    heartbeatToastTimerRef.current = window.setTimeout(() => {
      setHeartbeatToastVisible(false);
      heartbeatToastTimerRef.current = null;
    }, 15000);
  }, [clearHeartbeatToastTimer, heartbeatMessage, heartbeatUpdatedAt]);

  useEffect(() => {
    if (!isConnected || initialDataLoaded) {
      return;
    }
    void (async () => {
      await fetchConfig();
      await fetchSessions();
      setInitialDataLoaded(true);
    })();
  }, [fetchConfig, fetchSessions, initialDataLoaded, isConnected]);

  // 聊天处理完成后刷新会话列表，以便拾取自动生成的标题等元数据更新
  const prevProcessingRef = useRef(false);
  useEffect(() => {
    if (prevProcessingRef.current && !isProcessing) {
      void fetchSessions();
    }
    prevProcessingRef.current = isProcessing;
  }, [isProcessing, fetchSessions]);

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
    if (!isConnected || !sessionId || sessionId === 'new') return;
    
    // 仅处理以 sess_ 开头的会话 ID
    if (!sessionId.startsWith('sess_')) return;
    
    // 清理之前的历史加载句柄
    disposeInFlightHistoryHandles();
    setHistoryPagerMeta(null);
    setHistoryLoadingMore(false);
    
    // 开始历史会话加载
    const restoreHandle = beginHistoryRestore({
      sessionId: sessionId,
      onReady: (messages, totalPages) => {
        if (sessionIdRef.current !== sessionId) {
          return;
        }
        historyRestoreFromPanelHintRef.current = false;
        clearMessages();
        messages.forEach((message) => addMessage(message));
        setHistoryPagerMeta({
          loadedPages: 1,
          totalPages: totalPages ?? 1,
        });
        queueMicrotask(() => {
          historyRestoreHandleRef.current = null;
        });
      },
      onEmpty: (emptyTotalPages) => {
        if (sessionIdRef.current !== sessionId) {
          return;
        }
        clearMessages();
        setHistoryPagerMeta({
          loadedPages: 1,
          totalPages: emptyTotalPages ?? 1,
        });
        if (historyRestoreFromPanelHintRef.current) {
          historyRestoreFromPanelHintRef.current = false;
          addMessage({
            id: `history-restore-empty-${Date.now()}`,
            role: 'system',
            content: t('sessions.restoreEmpty'),
            timestamp: new Date().toISOString(),
          });
        }
        historyRestoreHandleRef.current = null;
      },
      onToolReplay: (items) => {
        if (sessionIdRef.current !== sessionId) {
          return;
        }
        clearSubtasks();
        for (const item of items) {
          if (item.kind === 'tool_call') {
            const n = normalizeToolCallPayload(item.payload);
            addToolCall(
              {
                id: n.id,
                name: n.name,
                arguments: n.arguments,
                description: n.description,
                formatted_args: n.formatted_args,
                memberId: n.memberId,
                memberName: n.memberName,
              },
              { startedAt: item.at }
            );
          } else {
            const standalone = tryDeepResearchStandaloneAssistantTurn(item.payload);
            if (standalone) {
              addMessage({
                id: standalone.messageId,
                role: 'assistant',
                content: standalone.content,
                timestamp: item.at,
              });
            } else {
              const n = normalizeToolResultPayload(item.payload);
              addToolResult(
                {
                  toolName: n.toolName,
                  result: n.result,
                  success: n.success,
                  toolCallId: n.toolCallId,
                  summary: n.summary,
                },
                { updatedAt: item.at }
              );
            }
          }
        }
      },
      onError: (message) => {
        console.warn('[history.restore]', message);
      },
    });
    historyRestoreHandleRef.current = restoreHandle;

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
        historyRestoreHandleRef.current = null;
        // 发生错误时，设置 historyPagerMeta 为 null，显示欢迎信息
        setHistoryPagerMeta(null);
        console.error('Failed to load history:', error);
        // 忽略 "invalid page_idx or session history not found" 错误，因为这是新会话的正常情况
        const errorMessage = error instanceof Error ? error.message : String(error);
        if (sessionIdRef.current === sessionId && !errorMessage.includes('invalid page_idx or session history not found')) {
          clearMessages();
          addMessage({
            id: `history-load-failed-${Date.now()}`,
            role: 'system',
            content: t('sessions.errors.restoreFailed', { sessionId }),
            timestamp: new Date().toISOString(),
          });
        }
      }
    })();
  }, [
    isConnected,
    sessionId,
    request,
    t,
    addMessage,
    addToolCall,
    addToolResult,
    clearMessages,
    clearSubtasks,
    disposeInFlightHistoryHandles,
  ]);

  // 新建会话：立即生成可用的 session_id，避免停留在 'new' 导致无法发送消息
  const handleNewSession = useCallback(async () => {
    disposeInFlightHistoryHandles();
    setHistoryPagerMeta(null);
    setHistoryLoadingMore(false);
    setProcessing(false);
    setThinking(false);
    setPaused(false);
    clearMessages();
    clearTodos();
    const newSid = generateSessionId();
    try {
      const payload = await request<{ session_id?: string }>('session.create', {
        session_id: newSid,
      });
      const createdSid =
        typeof payload?.session_id === 'string' && payload.session_id
          ? payload.session_id
          : newSid;
      setSessionId(createdSid);
      setCurrentSession(null);
      storeSessionId(createdSid);
      // 保持当前模式
      if (switchMode) {
        try {
          await switchMode(createdSid, mode);
        } catch (error) {
          console.error('Failed to set mode for new session:', error);
        }
      }
      await fetchSessions();
    } catch (error) {
      console.error('Failed to create session:', error);
      return;
    }
    setNewSessionToastVisible(true);
    clearNewSessionToastTimer();
    newSessionToastTimerRef.current = window.setTimeout(() => {
      setNewSessionToastVisible(false);
      newSessionToastTimerRef.current = null;
    }, 2000);
  }, [
    clearMessages,
    clearNewSessionToastTimer,
    clearTodos,
    disposeInFlightHistoryHandles,
    fetchSessions,
    mode,
    request,
    setCurrentSession,
    setPaused,
    setProcessing,
    setThinking,
    switchMode,
  ]);

  // 切换模式
  const handleSwitchMode = useCallback((mode: AgentMode) => {
    if (!sessionId || sessionId === 'new') return;
    void switchMode(sessionId, mode);
  }, [sessionId, switchMode]);

  const handleSendMessage = useCallback((content: string) => {
    if (!sessionId || sessionId === 'new') return;
    void sendMessage(content, sessionId);
  }, [sendMessage, sessionId]);

  const handleInterrupt = useCallback((newInput?: string) => {
    if (!sessionId || sessionId === 'new') return;
    const trimmed = newInput?.trim();
    if (!trimmed) return;
    void supplement(sessionId, trimmed);
  }, [sessionId, supplement]);

  const handlePause = useCallback(() => {
    if (!sessionId || sessionId === 'new') return;
    void pause(sessionId);
  }, [pause, sessionId]);

  const handleCancel = useCallback(() => {
    if (!sessionId || sessionId === 'new') return;
    void cancel(sessionId);
  }, [cancel, sessionId]);

  const handleResume = useCallback(() => {
    if (!sessionId || sessionId === 'new') return;
    void resume(sessionId);
  }, [resume, sessionId]);

  const handleUserAnswer = useCallback((requestId: string, answers: UserAnswer[], source?: string) => {
    if (!sessionId || sessionId === 'new') return;
    void sendUserAnswer(sessionId, requestId, answers, source);
  }, [sendUserAnswer, sessionId]);

  const handleLoadMoreHistory = useCallback(async () => {
    if (!sessionId.startsWith('sess_') || !historyPagerMeta) return;
    if (historyLoadingMore || historyPagerMeta.loadedPages >= historyPagerMeta.totalPages) return;

    const sid = sessionId;
    const nextPage = historyPagerMeta.loadedPages + 1;
    const fallbackTotal = historyPagerMeta.totalPages;

    setHistoryLoadingMore(true);
    const pageHandle = fetchHistoryPage({
      sessionId: sid,
      onReady: ({ messages, toolReplay, totalPages }) => {
        if (sessionIdRef.current !== sid) {
          setHistoryLoadingMore(false);
          historyPageHandleRef.current = null;
          return;
        }
        prependMessages(messages);
        for (const item of toolReplay) {
          if (item.kind === 'tool_call') {
            const n = normalizeToolCallPayload(item.payload);
            addToolCall(
              {
                id: n.id,
                name: n.name,
                arguments: n.arguments,
                description: n.description,
                formatted_args: n.formatted_args,
                memberId: n.memberId,
                memberName: n.memberName,
              },
              { startedAt: item.at }
            );
          } else {
            const standalone = tryDeepResearchStandaloneAssistantTurn(item.payload);
            if (standalone) {
              addMessage({
                id: standalone.messageId,
                role: 'assistant',
                content: standalone.content,
                timestamp: item.at,
              });
            } else {
              const n = normalizeToolResultPayload(item.payload);
              addToolResult(
                {
                  toolName: n.toolName,
                  result: n.result,
                  success: n.success,
                  toolCallId: n.toolCallId,
                  summary: n.summary,
                },
                { updatedAt: item.at }
              );
            }
          }
        }
        setHistoryPagerMeta({
          loadedPages: nextPage,
          totalPages: totalPages ?? fallbackTotal,
        });
        setHistoryLoadingMore(false);
        historyPageHandleRef.current = null;
      },
      onEmpty: (emptyTotalPages) => {
        if (sessionIdRef.current !== sid) {
          setHistoryLoadingMore(false);
          historyPageHandleRef.current = null;
          return;
        }
        setHistoryPagerMeta({
          loadedPages: nextPage,
          totalPages: emptyTotalPages ?? fallbackTotal,
        });
        setHistoryLoadingMore(false);
        historyPageHandleRef.current = null;
      },
      onError: (message) => {
        console.warn('[history.page]', message);
      },
    });
    historyPageHandleRef.current = pageHandle;

    try {
      await request(HISTORY_GET_METHOD, {
        session_id: sid,
        page_idx: nextPage,
      });
    } catch (error) {
      pageHandle.dispose();
      historyPageHandleRef.current = null;
      console.error('Failed to load older history:', error);
      setHistoryLoadingMore(false);
    }
  }, [
    addMessage,
    addToolCall,
    addToolResult,
    historyLoadingMore,
    historyPagerMeta,
    prependMessages,
    request,
    sessionId,
  ]);

  const handleNavigate = useCallback((nav: MainNavKey) => {
    setActiveNav(nav);
  }, []);

  const heartbeatToastPreviewRaw = heartbeatToastMessage.replace(/\s+/g, ' ').trim();
  const heartbeatToastPreview = heartbeatToastPreviewRaw.length > 120
    ? `${heartbeatToastPreviewRaw.slice(0, 120)}...`
    : heartbeatToastPreviewRaw;

  return (
    <div className="shell" data-testid="app-shell" data-session-id={sessionId}>
      {/* Topbar */}
      <header className="topbar">
        <div className="flex items-center gap-4">
          <div className="brand">
            <img src="/logo.png" alt="OpenJiuwen" className="brand-logo-img" />
            <div className="brand-text">
              <span className="brand-title">JiuwenClaw</span>
              <span className="brand-sub">AI Assistant</span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3">
          {/* 连接状态 */}
          <div className="pill">
            <span className={`statusDot ${isConnected ? 'ok' : ''}`} />
            <span className="mono text-sm">
              {isConnected ? t('connection.connected') : t('connection.disconnected')}
            </span>
          </div>

          {/* 语言切换 */}
          <LanguageSwitcher />

          {/* 主题切换 */}
          <ThemeToggle />
        </div>
      </header>

      {/* Navigation Sidebar */}
      <SessionSidebar
        activeNav={activeNav}
        onNavigate={handleNavigate}
        appVersion={typeof serverConfig?.app_version === 'string' ? serverConfig.app_version : '0.1.7'}
      />

      {/* Main Content */}
      <main className="content">
        {configError && (
          <div className="card mb-4">
            <div className="text-sm text-text-muted">
              {configError}. {t('app.configErrorHint')}
              <span className="mono"> python -m tests.web_gateway_jiuwenclaw_integration </span>
              {t('app.configErrorDefault')}
              <span className="mono"> jiuwenclaw/web/.env.local </span>
              {t('app.configErrorEnv')} <span className="mono">VITE_API_BASE</span> {t('common.and')} <span className="mono">VITE_WS_BASE</span>.
            </div>
          </div>
        )}

        {activeNav === 'chat' && (
          <>
            <div className="flex-1 flex min-h-0 overflow-hidden">
              {/* Chat Panel */}
              <div className="flex-1 flex flex-col min-w-0 min-h-0">
                <div className="flex-1 min-h-0">
                  <ChatPanel
                    onSendMessage={handleSendMessage}
                    onInterrupt={handleInterrupt}
                    onSwitchMode={handleSwitchMode}
                    isProcessing={isProcessing}
                    onNewSession={handleNewSession}
                    onUserAnswer={handleUserAnswer}
                    historyPager={
                      historyPagerMeta
                        ? {
                            loadedPages: historyPagerMeta.loadedPages,
                            totalPages: historyPagerMeta.totalPages,
                            loadingMore: historyLoadingMore,
                            onLoadMore: handleLoadMoreHistory,
                          }
                        : null
                    }
                  />
                </div>

                {/* Status Bar - 只在非集群模式下显示 */}
                {mode !== 'team' && (
                  <StatusBar
                    onPause={handlePause}
                    onCancel={handleCancel}
                    onResume={handleResume}
                  />
                )}
              </div>

              {/* Tool Panel */}
              <ToolPanel />
            </div>
          </>
        )}
      </main>

      {/* 连接状态提示 */}
      {!isConnected && (
        <div className="app-toast-wrapper app-toast-wrapper--top">
          <div className="app-connection-toast animate-rise">
            {serverConfig ? t('connection.connecting') : t('connection.loadingConfig')}
          </div>
        </div>
      )}

      {/* 新建会话提示 */}
      {newSessionToastVisible && (
        <div className="app-toast-wrapper app-toast-wrapper--top-center">
          <div className="app-session-toast animate-rise">
            {t('chat.sessionCreated')}
          </div>
        </div>
      )}

      {/* 全局心跳消息提示 */}
      {heartbeatToastVisible && (
        <div className="app-toast-wrapper app-toast-wrapper--top">
          <div className="app-heartbeat-toast animate-rise">
            <div className="app-heartbeat-toast__header">
              <div className="app-heartbeat-toast__title">
                <span className="app-heartbeat-toast__dot animate-pulse" />
                <span className="text-xs font-medium text-text">{t('app.heartbeatTitle')}</span>
              </div>
              <button
                type="button"
                onClick={() => {
                  setHeartbeatToastVisible(false);
                  clearHeartbeatToastTimer();
                }}
                className="app-heartbeat-toast__close"
                aria-label={t('app.heartbeatClose')}
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
            <button
              type="button"
              onClick={() => {
                setHeartbeatModalOpen(true);
                setHeartbeatToastVisible(false);
                clearHeartbeatToastTimer();
              }}
              className="app-heartbeat-toast__content text-sm"
              title={t('app.heartbeatViewFull')}
            >
              <span className="app-heartbeat-toast__preview">
                {heartbeatToastPreview}
              </span>
            </button>
          </div>
        </div>
      )}

      {/* 配置保存后重启状态弹窗 */}
      {restartModalOpen && (
        <div className="app-restart-modal">
          <div className="app-restart-modal__backdrop" />
          <div className="app-restart-modal__panel">
            <div className="flex flex-col items-center text-center">
              {!restartSuccess ? (
                <div className="w-12 h-12 rounded-full border-4 border-border border-t-accent animate-spin mb-4" />
              ) : (
                <div className="w-12 h-12 rounded-full bg-ok/15 text-ok flex items-center justify-center mb-4">
                  <svg className="w-7 h-7" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                  </svg>
                </div>
              )}
              <h3 className="text-base font-semibold text-text mb-1">
                {!restartSuccess ? t('app.restarting') : appliedWithoutRestart ? t('app.configApplied') : t('app.restartSuccess')}
              </h3>
              <p className="text-sm text-text-muted mb-5">
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
                >
                  {t('common.ok')}
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      <HeartbeatMessageModal
        open={heartbeatModalOpen}
        message={heartbeatToastMessage}
        onClose={() => setHeartbeatModalOpen(false)}
      />
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
