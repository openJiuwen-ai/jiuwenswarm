import { addError, addInfo } from "./core/commands/helpers.js";
import { PrWatchController } from "./core/commands/builtins/autofix-pr.watch.js";
import type { CommandContext, PreferredLanguage } from "./core/commands/types.js";
import type {
  HandoffPort,
  TaskLifecyclePort,
  UiLifecyclePort,
} from "./core/supervision/protocol.js";
import type { ReauthenticationPort } from "./core/supervision/protocol.js";
import {
  computeTimeoutAt,
  isIgnorableHistoryRestoreError,
  rebuildToolExecutionStateFromEntries,
  upsertToolGroupDisplay,
} from "./core/app-state-helpers.js";
import {
  applyToolResult,
  coalesceAssistantHistoryEntries,
  coalesceToolGroupEntries,
  createToolCallDisplay,
  mergeHistoryMessagesForRestore,
  parseHistoryFrame,
} from "./core/history-parser.js";
import { getToolGroupIds } from "./core/transcript-timeline.js";
import {
  handleIncomingFrame,
  type AppEventDelegate,
  type PendingQuestion,
  type PendingQuestionItem,
  type UserAnswer,
  type HarnessExtensionReady,
  type HarnessActivateInteraction,
} from "./core/event-handlers.js";
import { isTeamMode, normalizeToClientMode, type ClientMode } from "./core/modes.js";
import { isEventFrame, type EventFrame, type FileAttachment } from "./core/protocol.js";
import {
  PLAN_ENTRY_SOURCE_SLASH_COMMAND,
  type PlanEntrySource,
} from "./core/plan-entry-source.js";
import {
  StreamingState,
  type ContextCompressionStats,
  type HistoryItem,
  type SubtaskState,
  type TeamMemberEvent,
  type TeamMessageEvent,
  type TeamTaskEvent,
  type TodoItem,
  type ToolCallDisplay,
  type ToolExecution,
} from "./core/types.js";
import { isTeamWorking } from "./ui/components/team-shared.js";
import {
  getCurrentAccentColor,
  getCurrentThemeName,
  setCurrentAccentColor,
  setCurrentThemeName,
  type AccentColorName,
  type ThemeName,
} from "./ui/theme.js";
import { type ConnectionStatus, WsClient } from "./core/ws-client.js";
import {
  getTrustedDirs,
  validateDirPath,
  addTrustedDir,
  setTrustedDir,
  removeTrustedDir,
  clearTrustedDirs,
  setCurrentProjectDir,
  getCurrentProjectDir,
  setCurrentCwd,
  getCurrentCwd,
} from "./core/tui-trusted-dirs-store.js";
import { loadTuiConfig } from "./core/tui-config-store.js";
import { runStatusLineCommand } from "./core/statusline-runner.js";
import { generateCreateToken } from "./core/session-state.js";
import {
  applyWorkflowUpdate,
  collectWaitingForHuman,
  countWaitingForHuman,
  findWorkflowAgent,
  mergeHumanPromptText,
  mergeWorkflowRun,
  normalizeWorkflowRun,
  reassembleAgentFieldParts,
  isSessionNode,
  phaseLocalTurnNumber,
  sessionTurnLabelNumber,
  shouldShowTurnInDetailOrReply,
  type WorkflowRun,
  type WorkflowPhase,
  type WorkflowAgent,
} from "./core/workflows.js";
import type { PendingHumanPrompt } from "./core/event-handlers.js";
import { spawnSync } from "node:child_process";

// A just-exited TUI can remain bound briefly while its WebSocket close is
// observed by the gateway. Retry only this startup-specific ownership race.
const BOOT_SESSION_IN_USE_RETRY_DELAYS_MS = [100, 200, 400, 800, 800, 800, 800];

function isTransientSessionInUseError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return message.toLowerCase().includes("already active in another window");
}

export interface ModelUsageEntry {
  model: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

export interface SessionUsageSummary {
  total_input_tokens: number;
  total_output_tokens: number;
  total_tokens: number;
  byModel: ModelUsageEntry[];
}

export interface CurrentQueryUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

interface VisibleUserRequest {
  requestId: string;
  content: string;
  sessionId: string;
}

export interface AppSnapshot {
  connectionStatus: ConnectionStatus;
  sessionId: string;
  mode: ClientMode;
  themeName: ThemeName;
  accentColor: AccentColorName;
  transcriptMode: "compact" | "detailed";
  transcriptFoldMode: "none" | "tools" | "thinking" | "all";
  collapsedToolGroupIds: Set<string>;
  entries: HistoryItem[];
  toolExecutions: ToolExecution[];
  streamingState: StreamingState;
  pendingQuestion: PendingQuestion | null;
  lastError: string | null;
  isProcessing: boolean;
  /**
   * 当前 UI 观测到是否存在运行中的工作。
   * 用于渲染与本地交互（如 Esc）；Ctrl+C 的中断请求仍以服务端为准，不依赖此值放行。
   */
  cancellableWork: boolean;
  isPaused: boolean;
  isInterrupted: boolean;
  activeSubtasks: SubtaskState[];
  todos: TodoItem[];
  teamMemberEvents: TeamMemberEvent[];
  teamTaskEvents: TeamTaskEvent[];
  teamMessageEvents: TeamMessageEvent[];
  workflowRuns: WorkflowRun[];
  pendingHumanPrompts: Map<string, PendingHumanPrompt>;
  evolutionStatus: "idle" | "running";
  /** 后端 config.get 成功读取到的技能自演进展示开关。 */
  skillEvolutionEnabled: boolean;
  contextCompression: ContextCompressionStats | null;
  contextWindowLimit: number | null;
  contextUsedPercentage: number | null;
  modelInfo: { provider: string; model: string; version: string };
  /** 全局选中的 agentos 备份模型名（请求级注入）；null 表示使用启动默认 */
  selectedAgentosModel: string | null;
  /** 全局选中的 agentos 备份模型 provider（仅展示用，头部 Provider 行）；空表示未取到 */
  selectedAgentosProvider: string;
  /** 注入用 key（model_name#global_idx）；后端 model_key 缺省时回退到 selectedAgentosModel */
  selectedAgentosModelKey: string | null;
  preferredLanguage: PreferredLanguage;
  sessionTitle: string;
  statusLineText: string | null;
  memoryWarnings: {
    path: string;
    kind: string;
    char_count: number;
    threshold: number;
    message: string;
  }[];
  /** 当前正在执行的命令名称，用于追踪不可中断命令。 */
  runningCommand: string | null;
  streamStalled: boolean;
  streamIdleMs: number | null;
  currentQueryUsage: CurrentQueryUsage;
  /** /btw 侧问题覆盖层：独立于 transcript 渲染，不受滚动影响 */
  btwOverlay: { question: string; answer: string } | null;
  /** 当前 btw overlay 在历史中的下标（-1 表示无选中） */
  btwOverlayIndex: number;
  /** btw 历史总数（用于提示 i/n） */
  btwOverlayTotal: number;
  /** BTW 是否处于活动状态（加载中或 overlay 可见），Esc 优先消费 */
  btwActive: boolean;
  /** /btw 正在回答的问题；非空时显示加载动画。 */
  btwPendingQuestion: string | null;
}

function formatElapsed(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) {
    return "0s";
  }
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

function normalizePreferredLanguage(value: unknown): PreferredLanguage {
  return typeof value === "string" && value.trim().toLowerCase() === "en" ? "en" : "zh";
}

/** config.get 将技能自演进开关展平为 skill_evolution；未知/缺失值必须保持关闭。 */
function parseSkillEvolutionEnabled(value: unknown): boolean {
  if (value === true) return true;
  if (typeof value !== "string") return false;
  return ["true", "1", "yes", "on", "enabled"].includes(value.trim().toLowerCase());
}

const LOCAL_FILE_SEARCH_TOOL_NAMES = new Set([
  "grep",
  "rg",
  "ripgrep",
  "search",
]);

const DEFERRED_TRANSCRIPT_EVENTS = new Set([
  "chat.delta",
  "chat.final",
  "chat.reasoning",
  "chat.error",
  "chat.tool_call",
  "chat.tool_result",
  "chat.symphony_status",
  "chat.interrupt_result",
  "chat.ask_user_question",
  "chat.media",
  "chat.file",
  "chat.subtask_update",
  "chat.session_result",
  "session_result",
  "history.message",
  "context.compression_state",
  "todo.updated",
  "team.member",
  "team.task",
  "team.message",
  "harness.extension_ready",
  "harness.activate_interaction",
]);

function isPlanClientMode(mode: ClientMode): boolean {
  return mode.endsWith(".plan");
}

// ── Auto-recap (自动回顾) 常量 ──
/** 用户空闲多久后自动触发回顾（5分钟）。 */
const AUTO_RECAP_IDLE_THRESHOLD_MS = 5 * 60_000;
/** 周期性检查空闲状态的时间间隔（30秒）。 */
const AUTO_RECAP_CHECK_INTERVAL_MS = 30_000;
const ACTIVE_TURN_RECONNECT_TIMEOUT_MS = 60_000;
const ACTIVE_NETWORK_CHECK_INTERVAL_MS = 8_000;

function isLocalFileSearchTool(name: string): boolean {
  return LOCAL_FILE_SEARCH_TOOL_NAMES.has(name.trim().toLowerCase());
}

function isRejectOption(label: string): boolean {
  const normalized = label.trim();
  return (
    normalized.includes("拒绝") || /^reject\b/i.test(normalized) || /^deny\b/i.test(normalized)
  );
}

function isPlanApprovalRejectWithoutFeedback(
  pendingQuestion: PendingQuestion,
  answers: UserAnswer[],
): boolean {
  if (
    pendingQuestion.source !== "confirm_interrupt" ||
    pendingQuestion.planApprovalKind !== "plan_approval"
  ) {
    return false;
  }

  const rejected = answers.some((answer) =>
    answer.selected_options.some((option) => isRejectOption(option)),
  );
  if (!rejected) {
    return false;
  }

  return answers.every((answer) => !answer.custom_input?.trim());
}

function withPlanApprovalCancelFeedback(answers: UserAnswer[]): UserAnswer[] {
  return answers.map((answer) => ({
    ...answer,
    custom_input:
      answer.custom_input?.trim() ||
      "用户取消了本次计划审批。请保持 plan 模式并等待用户下一条指令，不要继续调用 exit_plan_mode。",
  }));
}

function detectRipgrep(): boolean {
  try {
    const result = spawnSync("rg", ["--version"], { stdio: "ignore" });
    return result.status === 0;
  } catch {
    return false;
  }
}

export class CliPiAppState {
  private listeners = new Set<() => void>();
  private entries: HistoryItem[] = [];
  /** AppScreen 注入的 setInput 回调，用于自动恢复后填充输入框。 */
  private _setInputRef: ((text: string) => void) | null = null;
  /** AppScreen 注入的 getInputValue 回调，用于自动恢复判断输入框是否为空。 */
  private _getInputValueRef: (() => string) | null = null;
  private connectionStatus: ConnectionStatus = "idle";
  private sessionId: string;
  private sessionTitle: string = "";
  /**
   * `--session <id>` 启动时传入的目标 session id；为 null 表示用户未显式指定，
   * 此时由 AgentServer 分配新 ID 并尝试领取预热实例。显式 ID 则通过同一个
   * `session.create` 原子恢复或登记，并明确绕过预热。
   */
  private bootSessionId: string | null = null;
  private readonly bootPersistSession: boolean = false;
  /** 幂等守卫：boot session creation 只在首次 connection.ack 触发一次（重连/重发不重试）。 */
  private bootSessionHandled = false;
  /** connection.ack 到来时放行启动会话创建；构造期即存在，避免 connected 回调抢跑。 */
  private bootSessionStart: (() => void) | null = null;
  /** 启动会话创建完成前，后续命令与 chat 帧统一排队到该 Promise。 */
  private bootSessionCreation: Promise<void> | null = null;
  /** 普通启动的幂等创建 token；重连期间保持稳定。 */
  private readonly bootCreateToken = generateCreateToken();
  private mode: ClientMode = "agent.code.normal";
  private themeName: ThemeName = getCurrentThemeName();
  private accentColor: AccentColorName = getCurrentAccentColor();
  private transcriptMode: "compact" | "detailed" = "compact";
  private transcriptFoldMode: "none" | "tools" | "thinking" | "all" = "none";
  private collapsedToolGroupIds = new Set<string>();
  private streamingState: StreamingState = StreamingState.Idle;
  private pendingQuestion: PendingQuestion | null = null;
  private localPendingQuestion: {
    requestId: string;
    resolve: (answers: UserAnswer[]) => void;
    reject: (error: Error) => void;
  } | null = null;
  private lastError: string | null = null;
  private activeSubtasks = new Map<string, SubtaskState>();
  private todos: TodoItem[] = [];
  private teamMemberEvents: TeamMemberEvent[] = [];
  private teamTaskEvents: TeamTaskEvent[] = [];
  private teamMessageEvents: TeamMessageEvent[] = [];
  private workflowRuns: WorkflowRun[] = [];
  private pendingHumanPrompts: Map<string, PendingHumanPrompt> = new Map();
  private evolutionStatus: "idle" | "running" = "idle";
  /** 配置读取失败时保持 false，避免前端暴露不可用的演进命令。 */
  private skillEvolutionEnabled = false;
  private contextCompression: ContextCompressionStats | null = null;
  private contextWindowLimit: number | null = null;
  private contextUsedPercentage: number | null = null;
  private toolExecutions = new Map<string, ToolExecution>();
  private toolExecutionOrder: string[] = [];
  private orphanToolResults = new Map<
    string,
    { tool: ToolCallDisplay; requestId?: string; updatedAt: string }
  >();
  private historyEntries: HistoryItem[] = [];
  private historyFlushTimer: ReturnType<typeof setTimeout> | null = null;
  private toolTimeoutTimer: ReturnType<typeof setTimeout> | null = null;
  private historyRequestToken = 0;
  /** history.get 流返回的分页总数；由 `history.message` 事件帧的 `total_pages` 持续刷新。 */
  private historyTotalPages: number | null = null;
  /** 各页 done 事件的 resolver；restoreHistory 循环拉取时按 page_idx 等待。 */
  private historyPageDoneResolvers = new Map<number, () => void>();
  private unlistenStatus: (() => void) | null = null;
  private unlistenFrames: (() => void) | null = null;
  private statusLineText: string | null = null;
  private statusLineTimer: ReturnType<typeof setInterval> | null = null;
  private usageByModel = new Map<string, ModelUsageEntry>();
  private currentQueryUsage: CurrentQueryUsage = {
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
  };
  private modelInfo: { provider: string; model: string; version: string } = {
    provider: "",
    model: "",
    version: "",
  };
  /**
   * 全局选中的 agentos 备份模型名（请求级注入）。
   * 非空时 sendMessage 在 params 注入 model_name，由 AgentServer
   * _resolve_model_for_request 命中 agentos 缓存条目。切回 defaults 模型
   * 时由 setModel 清空（恢复启动默认模型路由）。
   */
  private selectedAgentosModel: string | null = null;
  private selectedAgentosProvider: string = "";
  // 注入用 key（model_name#global_idx），仅用于 chat.send 的 model_name 参数，
  // 后端 _resolve_model_by_name 据此精确命中同名 agentos 条目，避免纯名误路由到
  // defaults。展示一律用 selectedAgentosModel（纯名）。无后端 model_key 时回退到纯名。
  private selectedAgentosModelKey: string | null = null;
  private preferredLanguage: PreferredLanguage = "zh";
  private memoryWarnings: {
    path: string;
    kind: string;
    char_count: number;
    threshold: number;
    message: string;
  }[] = [];
  private memoryRefreshTimer: ReturnType<typeof setInterval> | null = null;
  private activeTurnReconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private activeTurnReconnectNoticeShown = false;
  private lastStreamActivityAt: number | null = null;
  private streamStallNoticeTimer: ReturnType<typeof setTimeout> | null = null;
  private streamStalled = false;
  /** 静默中断当前任务时置 true，抑制 chat.interrupt_result 的 UI 通知。 */
  private suppressInterruptResult = false;
  /** /btw 侧问题覆盖层：独立于 transcript 渲染 */
  private btwOverlay: { question: string; answer: string } | null = null;
  /** /btw 历史记录（按提问先后顺序），overlay 是其中"当前选中"条目的视图 */
  private btwHistory: { question: string; answer: string }[] = [];
  /** 当前 overlay 在 btwHistory 中的下标，-1 表示无选中 */
  private btwOverlayIndex = -1;
  /** BTW 是否处于活动状态（加载中或 overlay 可见），用于 Esc 优先级判断 */
  private _btwActive = false;
  /** /btw 正在回答的问题；与回答 overlay 的可见状态分离。 */
  private btwPendingQuestion: string | null = null;
  /** 本地中断请求标志，cancel() 调用时立即置 true，用于 long-running 命令的中断检测。 */
  private interruptRequested = false;
  /** 是否有一个 cancel interrupt 在途（已发送但未收到 interrupt_result 确认）。
   *  用于 esc 去抖：为 true 时重复 esc 不再发送新 interrupt。仅由 cancel() 实际发送 interrupt 时置 true、
   *  由 clearInterruptRequested（收到 result）清 false——不与 requestLocalInterrupt 的 interruptRequested 混用。 */
  private cancelInterruptInFlight = false;
  /** 最近一次向服务端发送 chat.interrupt(cancel) 的时间戳；用于 esc 去抖兜底超时，避免 result 丢失时窗口永久锁死。 */
  private lastInterruptSentAt = 0;
  /** esc 去抖兜底超时（毫秒）：发送 interrupt 后若 result 迟迟不回来，超过该时长允许下一次 esc 重新发送，
   *  防止 interrupt_result 丢失导致窗口永久锁死。设得比 AgentServer 最慢的取消响应（含首次初始化阻塞）略长。 */
  private static readonly INTERRUPT_DEBOUNCE_FALLBACK_MS = 30000;
  /** 当前正在执行的斜杠命令 WS 请求 ID，用于 Ctrl+C 时立即取消。 */
  private activeCommandRequestId: string | null = null;
  /** 当前正在执行的命令名称，用于追踪不可中断命令。 */
  private runningCommand: string | null = null;
  private pendingPlanEntrySource: PlanEntrySource | null = null;
  private lastVisibleUserRequest: VisibleUserRequest | null = null;
  /** 保存 askQuestions 之前的 streamingState，用于在对话框关闭后恢复。 */
  private streamingStateBeforeQuestion: StreamingState | null = null;
  /** 当前回合的起始时间戳，用于在回合结束时计算执行耗时。 */
  private turnStartedAt: number | null = null;
  /** ── Auto-recap 字段 ── */
  /** 最后一次用户交互时间戳，用于判断空闲时长。 */
  private lastActivityAt: number = Date.now();
  /** 自动回顾状态机：idle → pending → generated → idle（用户发言后重置）。 */
  private autoRecapState: "idle" | "pending" | "generated" = "idle";
  /** 周期检查空闲状态的定时器。 */
  private autoRecapTimer: ReturnType<typeof setInterval> | null = null;
  /** Active TUI-side PR watch, if any (see startPrWatch). */
  private prWatchController: PrWatchController | null = null;
  /** Run-scoped grant: auto-approve tool-permission prompts during a /autofix-pr
   *  run. Set when the user grants it at the start; cleared when the run ends
   *  (single-round turn end, watch stop) or is interrupted (Ctrl+C, session reset). */
  private autofixAutoApprove = false;
  /** 是否启用自动回顾（从 config.yaml 读取，默认 true）。 */
  private autoRecapEnabled: boolean = true;
  private ripgrepAvailable: boolean | null = null;
  private ripgrepSearchTipShown = false;
  /** Harness extension ready info (for file tree display) */
  private harnessExtensionReady: HarnessExtensionReady | null = null;
  /** Harness activate interaction state (for user confirmation) */
  private harnessActivateInteraction: HarnessActivateInteraction | null = null;
  private deferTranscriptFrames = false;
  private deferredTranscriptFrames: EventFrame[] = [];

  // ── /switch 公共契约端口（可选；由 index.ts 注入） ──
  /** HandoffPort；未注入时 /switch 通过 CommandContext 回退到 NOT_SUPERVISED。 */
  private handoffPort: HandoffPort | null = null;
  /** TaskLifecyclePort；未注入时回退到 hasServerTask()/cancel()。 */
  private taskLifecycle: TaskLifecyclePort | null = null;
  /** ReauthenticationPort；由 ws-client 在权威认证过期时调用。 */
  private reauthPort: ReauthenticationPort | null = null;
  /** UiLifecyclePort；统一顶层关闭路径。 */
  private uiLifecycle: UiLifecyclePort | null = null;
  /**
   * Remote 模式：--url 指向远端服务器时为 true。
   * 本地 PC 路径不可被远端沙箱访问，故请求时不发送本地 project_dir/cwd/trusted_dirs，
   * 改用 {@link remoteProjectDir}（服务器侧 /home/<user-id>）作为 workspace。
   */
  private isRemote = false;
  /** Remote 模式下的服务器侧 project_dir：/home/<user-id>；非 remote 时为空串。 */
  private remoteProjectDir = "";
  /** interrupt_result 事件订阅器；等待型取消按 requestId 关联。 */
  private interruptResultListeners = new Set<
    (requestId: string, sessionId: string, success: boolean, message?: string) => void
  >();
  /** WebSocket 断开订阅器；等待型取消按 CONNECTION_LOST reject。 */
  private connectionLostListeners = new Set<() => void>();
  /** AppState.stop 订阅器；等待型取消按 STATE_STOPPED reject。 */
  private stopListeners = new Set<() => void>();
  private readonly eventDelegate: AppEventDelegate = {
    getConnectionStatus: () => this.connectionStatus,
    getSessionId: () => this.sessionId,
    setSessionId: (sessionId) => {
      this.sessionId = sessionId;
      this.lastVisibleUserRequest = null;
    },
    setMode: (mode) => {
      this.mode = mode;
    },
    getMode: () => this.mode,
    getPreferredLanguage: () => this.preferredLanguage,
    getEntries: () => this.entries,
    setEntries: (entries) => {
      this.entries = entries;
    },
    setStreamingState: (state) => {
      this.setStreamingStateInternal(state);
      this.emitChange();
    },
    setPendingQuestion: (question) => {
      // Run-scoped auto-approve: during a /autofix-pr run the user pre-authorized,
      // silently approve tool-permission prompts instead of surfacing the card.
      if (question && this.autofixAutoApprove && this.tryAutoApprovePermission(question)) {
        return;
      }
      this.pendingQuestion = question;
    },
    setLastError: (error) => {
      this.lastError = error;
    },
    getActiveSubtasks: () => this.activeSubtasks,
    setTodos: (todos) => {
      this.todos = todos;
    },
    appendTeamMemberEvent: (event) => {
      this.teamMemberEvents = [...this.teamMemberEvents.slice(-99), event];
    },
    appendTeamTaskEvent: (event) => {
      this.teamTaskEvents = [...this.teamTaskEvents.slice(-99), event];
    },
    appendTeamMessageEvent: (event) => {
      this.teamMessageEvents = [...this.teamMessageEvents.slice(-99), event];
    },
    applyWorkflowUpdate: (workflow) => {
      this.applyWorkflowUpdate(workflow);
    },
    setPendingHumanPrompts: (prompts) => {
      this.setPendingHumanPrompts(prompts);
    },
    setEvolutionStatus: (status) => {
      this.evolutionStatus = status;
    },
    setContextCompression: (stats) => {
      this.contextCompression = stats;
    },
    setContextWindowLimit: (n) => {
      this.contextWindowLimit = n;
    },
    setContextUsedPercentage: (n) => {
      this.contextUsedPercentage = n;
    },
    addToolCallPayload: (payload, sessionId, requestId, startedAt) => {
      this.addToolCallPayload(payload, sessionId, requestId, startedAt);
    },
    addToolResultPayload: (payload, sessionId, requestId, updatedAt) => {
      this.addToolResultPayload(payload, sessionId, requestId, updatedAt);
    },
    addSyntheticToolExecution: (tool, sessionId, requestId, at) => {
      this.addSyntheticToolExecution(tool, sessionId, requestId, at);
    },
    setCurrentWorkspaceFromTool: (path) => {
      this.setCurrentWorkspaceFromTool(path);
    },
    clearToolExecutionState: () => {
      this.clearToolExecutionState();
    },
    markRunningToolsInterrupted: () => {
      this.markRunningToolsInterrupted();
    },
    pushHistoryEntry: (entry) => {
      this.historyEntries.push(entry);
    },
    scheduleHistoryFlush: () => {
      this.scheduleHistoryFlush();
    },
    safeRestoreHistory: (sessionId) => {
      this.safeRestoreHistory(sessionId);
    },
    setSessionTitle: (title) => {
      this.setSessionTitle(title);
    },
    safeFetchSessionTitle: (sessionId) => {
      this.safeFetchSessionTitle(sessionId);
    },
    initializeBootSession: () => {
      this.initializeBootSession();
    },
    getSuppressInterruptResult: () => this.suppressInterruptResult,
    clearSuppressInterruptResult: () => {
      this.suppressInterruptResult = false;
    },
    clearInterruptRequested: () => this.clearInterruptRequested(),
    reportHistoryPageMeta: ({ totalPages }) => {
      if (typeof totalPages === "number" && Number.isFinite(totalPages) && totalPages > 0) {
        this.historyTotalPages = totalPages;
      }
    },
    notifyHistoryPageDone: (pageIdx) => {
      const resolver = this.historyPageDoneResolvers.get(pageIdx);
      if (resolver) {
        this.historyPageDoneResolvers.delete(pageIdx);
        resolver();
      }
    },
    tryAutoRestoreAfterCancel: () => this.tryAutoRestoreAfterCancel(),
    appendUsageSummary: (usage, model) => {
      this.appendUsageDelta(usage, model);
    },
    appendUsageMetadata: (usage) => {
      this.updateCurrentUsageTokens(usage);
    },
    addWorkedForEntry: () => {
      if (this.turnStartedAt === null) return;
      const elapsed = Date.now() - this.turnStartedAt;
      this.turnStartedAt = null;
      this.addItem(
        addInfo(this.sessionId, `Worked for ${formatElapsed(elapsed)}`, undefined, { view: "dim" }),
      );
    },
    setHarnessExtensionReady: (info) => {
      this.harnessExtensionReady = info;
    },
    setHarnessActivateInteraction: (state) => {
      this.harnessActivateInteraction = state;
    },
    getHarnessActivateInteraction: () => this.harnessActivateInteraction,
    autoActivateExtension: (interactionId: string) => {
      // TUI auto-activates extensions without user confirmation
      this.sendEventOnly(
        "chat.send",
        {
          query: "",
          content: "",
          mode: "auto_harness",
          activate_response: {
            interaction_id: interactionId,
            action: "accept",
            feedback: "",
          },
        },
        true,
      );
    },
    notifyInterruptResult: (requestId, sessionId, success, message) => {
      this.notifyInterruptResult(requestId, sessionId, success, message);
    },
  };

  constructor(
    private readonly wsClient: WsClient,
    cliSession?: string,
    cliPersistSession?: boolean,
    supervision?: {
      handoffPort?: HandoffPort | null;
      taskLifecycle?: TaskLifecyclePort | null;
      reauthPort?: ReauthenticationPort | null;
      uiLifecycle?: UiLifecyclePort | null;
      isRemote?: boolean;
      remoteProjectDir?: string;
    },
  ) {
    this.sessionId = cliSession || generateCreateToken();
    this.bootPersistSession = Boolean(cliPersistSession);
    this.bootSessionId = cliSession ? cliSession : null;
    const startGate = new Promise<void>((resolve) => {
      this.bootSessionStart = resolve;
    });
    const bootCreation = (async () => {
      await startGate;
      await this.createBootSession();
    })();
    this.bootSessionCreation = bootCreation;
    void bootCreation
      .catch(() => undefined)
      .finally(() => {
        if (this.bootSessionCreation === bootCreation) {
          this.bootSessionCreation = null;
        }
      });
    const config = loadTuiConfig();
    if (config.theme) {
      setCurrentThemeName(config.theme);
      this.themeName = config.theme;
    }
    // 注入 /switch 公共契约端口（可选；未注入时回退到非托管实现）。
    this.handoffPort = supervision?.handoffPort ?? null;
    this.taskLifecycle = supervision?.taskLifecycle ?? null;
    this.reauthPort = supervision?.reauthPort ?? null;
    this.uiLifecycle = supervision?.uiLifecycle ?? null;
    this.isRemote = supervision?.isRemote ?? false;
    this.remoteProjectDir = supervision?.remoteProjectDir ?? "";
  }

  start(): void {
    this.unlistenStatus = this.wsClient.onStatusChange(async (status) => {
      this.connectionStatus = status;
      this.handleConnectionStatusChanged(status);
      this.emitChange();
      if (status === "connected") {
        await this.fetchModelInfo();
      }
      if (status === "message_too_big") {
        this.addItem(addError(this.sessionId, "消息过大，服务器拒绝了连接。请缩短输入内容后重新发送。"));
      }
    });

    this.unlistenFrames = this.wsClient.onFrame((frame) => {
      this.handleFrame(frame);
    });

    this.wsClient.connect();
    this.startStatusLinePoll();
    // auto-recap timer 由 fetchModelInfo() 在拿到配置后启动，
    // 避免在配置为 disabled 时仍提前启动 timer。
  }

  stop(): void {
    // 通知所有 stop 订阅器（等待型取消按 STATE_STOPPED reject）。
    for (const listener of this.stopListeners) {
      try {
        listener();
      } catch {
        // ignore — 进程即将退出
      }
    }
    this.stopListeners.clear();

    if (this.localPendingQuestion) {
      this.localPendingQuestion.reject(new Error("app stopped while awaiting input"));
      this.localPendingQuestion = null;
    }
    if (this.historyFlushTimer) {
      clearTimeout(this.historyFlushTimer);
      this.historyFlushTimer = null;
    }
    if (this.toolTimeoutTimer) {
      clearTimeout(this.toolTimeoutTimer);
      this.toolTimeoutTimer = null;
    }
    this.clearActiveTurnReconnectTimer();
    this.clearStreamStallWatchdog();
    this.unlistenStatus?.();
    this.unlistenStatus = null;
    this.unlistenFrames?.();
    this.unlistenFrames = null;
    this.stopStatusLinePoll();
    this.stopMemoryRefresh();
    this.stopAutoRecapTimer();
    this.wsClient.disconnect();
  }

  private setSkillEvolutionEnabled(enabled: boolean): void {
    if (this.skillEvolutionEnabled === enabled) {
      return;
    }
    this.skillEvolutionEnabled = enabled;
    this.emitChange();
  }

  private async fetchModelInfo(): Promise<void> {
    try {
      const [configPayload, modelsPayload, memoryPayload] = await Promise.allSettled([
        this.request("config.get", {}),
        this.request("models.list", {}),
        this.request<Record<string, unknown>>("memory.status", { detailed: true }),
      ]);
      const config =
        configPayload.status === "fulfilled" &&
        configPayload.value &&
        typeof configPayload.value === "object"
          ? (configPayload.value as Record<string, unknown>)
          : {};
      const modelsResult =
        modelsPayload.status === "fulfilled" &&
        modelsPayload.value &&
        typeof modelsPayload.value === "object"
          ? (modelsPayload.value as Record<string, unknown>)
          : {};
      const activeModelName = String(modelsResult.active_model ?? "").trim();
      const models = Array.isArray(modelsResult.models)
        ? (modelsResult.models as Record<string, unknown>[])
        : [];
      const activeModel = activeModelName
        ? models.find((m) => m.model_name === activeModelName)
        : models[0];
      this.preferredLanguage = normalizePreferredLanguage(config.preferred_language);
      this.setSkillEvolutionEnabled(parseSkillEvolutionEnabled(config.skill_evolution));
      this.autoRecapEnabled = config.auto_recap_enabled !== "false";
      // 同步 auto-recap timer：WS 连接后才拿到配置，需根据实际值启停 timer
      if (this.autoRecapEnabled) {
        if (!this.autoRecapTimer) {
          this.startAutoRecapTimer();
        }
      } else {
        this.stopAutoRecapTimer();
      }
      this.modelInfo = {
        provider: String(activeModel?.model_provider ?? config.model_provider ?? ""),
        model: activeModelName || String(config.model ?? ""),
        version: String(config.app_version ?? ""),
      };

      // 从 models.list 的模型数据中提取上下文窗口大小（不需要agent初始化）
      if (activeModel && typeof activeModel.context_window_tokens === "number") {
        this.contextWindowLimit = activeModel.context_window_tokens;
      }

      const memoryResult =
        memoryPayload.status === "fulfilled" &&
        memoryPayload.value &&
        typeof memoryPayload.value === "object"
          ? (memoryPayload.value as Record<string, unknown>)
          : {};
      const largeFiles = Array.isArray(memoryResult.large_files)
        ? (memoryResult.large_files as {
            path: string;
            kind: string;
            char_count: number;
            threshold: number;
            message: string;
          }[])
        : [];
      this.memoryWarnings = largeFiles;

      this.startMemoryRefresh();
      this.emitChange();
    } catch {
      // ignore error, use defaults
      // config.get 失败时，按默认值 true 启动 auto-recap timer
      this.startAutoRecapTimer();
    }
  }

  readonly refreshModelInfo = async (): Promise<void> => {
    await this.fetchModelInfo();
  };

  private startMemoryRefresh(): void {
    this.stopMemoryRefresh();
    this.memoryRefreshTimer = setInterval(async () => {
      try {
        const memoryResult = await this.request<Record<string, unknown>>(
          "memory.status",
          { detailed: true },
          10_000,
        );
        const largeFiles = Array.isArray(memoryResult.large_files)
          ? (memoryResult.large_files as {
              path: string;
              kind: string;
              char_count: number;
              threshold: number;
              message: string;
            }[])
          : [];
        this.memoryWarnings = largeFiles;
        this.emitChange();
      } catch {
        // ignore — next interval will retry
      }
    }, 30_000);
  }

  private stopMemoryRefresh(): void {
    if (this.memoryRefreshTimer) {
      clearInterval(this.memoryRefreshTimer);
      this.memoryRefreshTimer = null;
    }
  }

  private hasActiveResponseStream(): boolean {
    return this.connectionStatus === "connected" && this.streamingState === StreamingState.Responding;
  }

  private handleStreamingStateChanged(wasActiveResponseStream: boolean): void {
    const isActiveResponseStream = this.hasActiveResponseStream();
    if (isActiveResponseStream && !wasActiveResponseStream) {
      this.noteStreamActivity();
      return;
    }
    if (!isActiveResponseStream) {
      this.clearStreamStallWatchdog();
    }
  }

  private setStreamingStateInternal(state: StreamingState): void {
    const wasActiveResponseStream = this.hasActiveResponseStream();
    this.streamingState = state;
    this.handleStreamingStateChanged(wasActiveResponseStream);
    // Single-round /autofix-pr: the run ends when the turn goes idle, so drop the
    // auto-approve grant. A watch owns the grant across its rounds' idle gaps, so
    // skip while a watch is active (it clears via onStopped instead).
    if (
      state === StreamingState.Idle &&
      this.autofixAutoApprove &&
      !this.prWatchController?.active
    ) {
      this.autofixAutoApprove = false;
    }
  }

  private noteStreamActivity(): void {
    if (!this.hasActiveResponseStream()) {
      return;
    }
    this.lastStreamActivityAt = Date.now();
    this.streamStalled = false;
    this.scheduleStreamStallWatchdog();
  }

  private scheduleStreamStallWatchdog(): void {
    this.clearStreamStallTimers();
    if (!this.hasActiveResponseStream() || this.lastStreamActivityAt === null) {
      return;
    }
    const idleMs = Date.now() - this.lastStreamActivityAt;
    this.streamStallNoticeTimer = setTimeout(() => {
      this.streamStallNoticeTimer = null;
      void this.handleStreamStallNotice();
    }, Math.max(0, ACTIVE_NETWORK_CHECK_INTERVAL_MS - idleMs));
  }

  private clearStreamStallTimers(): void {
    if (this.streamStallNoticeTimer) {
      clearTimeout(this.streamStallNoticeTimer);
      this.streamStallNoticeTimer = null;
    }
  }

  private clearStreamStallWatchdog(): void {
    this.clearStreamStallTimers();
    this.lastStreamActivityAt = null;
    this.streamStalled = false;
  }

  private async handleStreamStallNotice(): Promise<void> {
    if (!this.hasActiveResponseStream()) {
      return;
    }
    // 到达此处时 connectionStatus 必为 "connected"（hasActiveResponseStream 已隐含，
    // 且本函数 fire 于定时器回调、其间无 await 改写该字段）。ws 仍 connected 即认为
    // 本地服务（Gateway/AgentServer）还活着、任务在跑只是暂未吐帧，故续期不报错。
    // air-gapped 内网里 ws 恒 connected，故永远续期，消除公网探针必然失败导致的误报。
    // 公网用户真断网时 ws 会先变 reconnecting（见 handleConnectionStatusChanged，走 60s
    // 重连看门狗），本函数在 reconnecting 下不会被调到，故公网保护语义不变。
    this.lastStreamActivityAt = Date.now();
    this.scheduleStreamStallWatchdog();
  }

  private frameBelongsToActiveSession(frame: EventFrame): boolean {
    const eventSessionId = typeof frame.payload.session_id === "string" ? frame.payload.session_id : "";
    return !eventSessionId || eventSessionId === this.sessionId;
  }

  private isStreamProgressFrame(frame: EventFrame): boolean {
    const event = typeof frame.payload.event_type === "string" ? frame.payload.event_type : frame.event;
    return event !== "chat.processing_status" &&
      event !== "connection.ack" &&
      event !== "history.message" &&
      event !== "context.usage" &&
      event !== "context.compression_state";
  }

  private handleConnectionStatusChanged(status: ConnectionStatus): void {
    if (status === "reconnecting") {
      this.clearStreamStallWatchdog();
      this.startActiveTurnReconnectWatchdog();
      return;
    }
    if (status === "connected") {
      const hadReconnectNotice = this.activeTurnReconnectNoticeShown;
      this.clearActiveTurnReconnectTimer();
      this.activeTurnReconnectNoticeShown = false;
      if (hadReconnectNotice) {
        this.addItem(
          addInfo(
            this.sessionId,
            "Connection restored. Syncing session updates from the backend.",
            "i",
            { view: "dim" },
          ),
        );
      }
      if (this.streamingState === StreamingState.Responding) {
        this.noteStreamActivity();
      }
      return;
    }
    if (status === "auth_failed" || status === "message_too_big") {
      this.failActiveTurnAfterConnectionLoss(
        status === "auth_failed"
          ? "Backend connection failed authentication. Stopped waiting for the current response."
          : "Backend closed the connection because the message was too large. Stopped waiting for the current response.",
      );
      // 通知连接丢失订阅器（等待型取消按 CONNECTION_LOST reject）。
      for (const listener of this.connectionLostListeners) {
        try {
          listener();
        } catch {
          // ignore
        }
      }
      this.connectionLostListeners.clear();
    }
  }

  private startActiveTurnReconnectWatchdog(): void {
    const snapshot = this.getSnapshot();
    if (!snapshot.cancellableWork && !snapshot.pendingQuestion) {
      return;
    }
    if (!this.activeTurnReconnectNoticeShown) {
      this.activeTurnReconnectNoticeShown = true;
      this.addItem(
        addInfo(this.sessionId, "Connection lost. Retrying backend connection...", "!", {
          view: "dim",
        }),
      );
    }
    if (this.activeTurnReconnectTimer) {
      return;
    }
    this.activeTurnReconnectTimer = setTimeout(() => {
      this.activeTurnReconnectTimer = null;
      if (this.connectionStatus === "connected") {
        return;
      }
      this.failActiveTurnAfterConnectionLoss(
        "Connection lost for over 60 seconds. Stopped waiting for the current response; the backend may still finish it after reconnect.",
      );
    }, ACTIVE_TURN_RECONNECT_TIMEOUT_MS);
  }

  private clearActiveTurnReconnectTimer(): void {
    if (this.activeTurnReconnectTimer) {
      clearTimeout(this.activeTurnReconnectTimer);
      this.activeTurnReconnectTimer = null;
    }
  }

  private failActiveTurnAfterConnectionLoss(message: string): void {
    this.clearActiveTurnReconnectTimer();
    this.clearStreamStallWatchdog();
    const snapshot = this.getSnapshot();
    if (!snapshot.cancellableWork && !snapshot.pendingQuestion) {
      return;
    }
    if (this.localPendingQuestion) {
      this.localPendingQuestion.reject(new Error(message));
      this.localPendingQuestion = null;
    }
    if (this.activeCommandRequestId) {
      this.wsClient.cancelRequest(this.activeCommandRequestId, message);
      this.activeCommandRequestId = null;
    }
    this.pendingQuestion = null;
    this.setStreamingStateInternal(StreamingState.Idle);
    this.streamingStateBeforeQuestion = null;
    this.activeSubtasks.clear();
    this.todos = [];
    this.evolutionStatus = "idle";
    this.clearInterruptRequested();
    this.markRunningToolsConnectionLost();
    this.addItem(addError(this.sessionId, message));
  }

  onChange(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  getSnapshot(): AppSnapshot {
    const isProcessing =
      this.streamingState === StreamingState.Responding ||
      this.streamingState === StreamingState.WaitingForConfirmation;
    const hasRunningTools = this.toolExecutionOrder.some((id) => {
      const ex = this.toolExecutions.get(id);
      return ex?.tool.status === "running";
    });
    const hasActiveSubtasks = [...this.activeSubtasks.values()].some(
      (s) => s.status !== "completed" && s.status !== "error",
    );
    // 与「Ctrl+C 强制结束当前任务」对齐：有任一进行中工作则为 true。
    // 包含 activeCommandRequestId 以确保 /btw 等命令请求在等待响应期间
    // 也能被 Esc 取消（WS 请求会被立即中止，避免等待超时）。
    const cancellableWork =
      isProcessing ||
      this.streamingState === StreamingState.Paused ||
      hasRunningTools ||
      hasActiveSubtasks ||
      this.evolutionStatus === "running" ||
      this.activeCommandRequestId !== null ||
      (isTeamMode(this.mode) && isTeamWorking(this.teamMemberEvents, this.teamMessageEvents));
    return {
      connectionStatus: this.connectionStatus,
      sessionId: this.sessionId,
      mode: this.mode,
      themeName: this.themeName,
      accentColor: this.accentColor,
      transcriptMode: this.transcriptMode,
      transcriptFoldMode: this.transcriptFoldMode,
      collapsedToolGroupIds: new Set(this.collapsedToolGroupIds),
      entries: [...this.entries],
      toolExecutions: this.toolExecutionOrder
        .map((toolCallId) => this.toolExecutions.get(toolCallId))
        .filter((item): item is ToolExecution => Boolean(item)),
      streamingState: this.streamingState,
      pendingQuestion: this.pendingQuestion
        ? {
            ...this.pendingQuestion,
            questions: this.pendingQuestion.questions.map((question) => ({
              ...question,
              options: [...question.options],
            })),
          }
        : null,
      lastError: this.lastError,
      isProcessing,
      cancellableWork,
      isPaused: this.streamingState === StreamingState.Paused,
      isInterrupted: this.streamingState === StreamingState.Interrupted,
      activeSubtasks: [...this.activeSubtasks.values()].sort((a, b) => a.index - b.index),
      todos: [...this.todos],
      teamMemberEvents: [...this.teamMemberEvents],
      teamTaskEvents: [...this.teamTaskEvents],
      teamMessageEvents: [...this.teamMessageEvents],
      workflowRuns: this.workflowRuns.map((workflow) => ({
        ...workflow,
        logs: workflow.logs ? [...workflow.logs] : undefined,
        phases: (workflow.phases ?? []).map((phase) => ({
          ...phase,
          agents: (phase.agents ?? []).map((agent) => ({
            ...agent,
            activity: agent.activity ? [...agent.activity] : undefined,
          })),
        })),
      })),
      pendingHumanPrompts: new Map(this.pendingHumanPrompts),
      evolutionStatus: this.evolutionStatus,
      skillEvolutionEnabled: this.skillEvolutionEnabled,
      contextCompression: this.contextCompression ? { ...this.contextCompression } : null,
      contextWindowLimit: this.contextWindowLimit,
      contextUsedPercentage: this.contextUsedPercentage,
      modelInfo: this.modelInfo,
      selectedAgentosModel: this.selectedAgentosModel,
      selectedAgentosProvider: this.selectedAgentosProvider,
      selectedAgentosModelKey: this.selectedAgentosModelKey,
      preferredLanguage: this.preferredLanguage,
      sessionTitle: this.sessionTitle,
      statusLineText: this.statusLineText,
      memoryWarnings: [...this.memoryWarnings],
      runningCommand: this.runningCommand,
      streamStalled: this.streamStalled,
      streamIdleMs:
        this.lastStreamActivityAt === null ? null : Date.now() - this.lastStreamActivityAt,
      currentQueryUsage: { ...this.currentQueryUsage },
      btwOverlay: this.btwOverlay,
      btwOverlayIndex: this.btwOverlayIndex,
      btwOverlayTotal: this.btwHistory.length,
      btwActive: this._btwActive,
      btwPendingQuestion: this.btwPendingQuestion,
    };
  }

  /** Check if interrupt was requested locally (for long-running command detection) */
  isInterruptRequested(): boolean {
    return this.interruptRequested;
  }

  /**
   * Set local interrupt flag (for long-running local commands like log streaming).
   * Returns true if an active command WS request was cancelled — this signals
   * to the Ctrl+C handler that the interrupt consumed the keystroke and the
   * "double-press-to-exit" timer should be reset.
   */
  requestLocalInterrupt(): boolean {
    this.interruptRequested = true;
    // 立即取消正在执行的命令 WS 请求（如 /recap），避免等待 60 秒超时
    if (this.activeCommandRequestId) {
      this.wsClient.cancelRequest(this.activeCommandRequestId, "cancelled");
      this.activeCommandRequestId = null;
      return true; // 命令请求被取消 → Ctrl+C 被消费，不应触发"连按两次退出"
    }
    return false;
  }

  /** Clear local interrupt flag (called after handling interrupt) */
  clearInterruptRequested(): void {
    this.interruptRequested = false;
    // 收到 interrupt_result（成功或失败）即表示在途的 cancel 已确认，解除去抖拦截，允许下一次 esc 重新发送。
    // 不清 lastInterruptSentAt：兜底超时用它判断"result 丢失"窗口，若清了则兜底失效。
    this.cancelInterruptInFlight = false;
  }

  /** Set the currently running command name (for tracking uninterruptible commands) */
  setRunningCommand(name: string | null): void {
    this.runningCommand = name;
    this.emitChange();
  }

  /** Check if there's a server task running (for deciding whether to send chat.interrupt) */
  hasServerTask(): boolean {
    const snapshot = this.getSnapshot();
    // Include pendingQuestion: user may want to cancel while waiting for answer
    return snapshot.isProcessing || snapshot.cancellableWork || Boolean(snapshot.pendingQuestion);
  }

  getCommandContext(): CommandContext {
    const snapshot = this.getSnapshot();
    const toolGroupIds = getToolGroupIds(snapshot.entries, snapshot.toolExecutions);
    return {
      version: snapshot.modelInfo.version || "",
      sendEventOnly: this.sendEventOnly,
      request: this.request,
      askQuestions: this.askQuestions,
      sendMessage: this.sendMessage,
      sessionId: snapshot.sessionId,
      preferredLanguage: snapshot.preferredLanguage,
      entries: snapshot.entries,
      teamMessageEvents: snapshot.teamMessageEvents,
      themeName: snapshot.themeName,
      accentColor: snapshot.accentColor,
      updateSession: this.updateSession,
      addItem: this.addItem,
      setBtwOverlay: this.setBtwOverlay,
      clearBtwOverlay: this.clearBtwOverlay,
      setBtwActive: this.setBtwActive,
      setBtwPendingQuestion: this.setBtwPendingQuestion,
      clearEntries: this.clearEntries,
      restoreHistory: this.restoreHistory,
      exitApp: () => {
        // AppScreen injects the real exit handler when executing slash commands.
      },
      isProcessing: snapshot.isProcessing,
      /** Check if interrupt was requested locally (for long-running command detection) */
      isInterruptRequested: () => this.interruptRequested,
      /** Clear local interrupt flag (for long-running commands to reset after handling interrupt) */
      clearInterruptRequested: () => this.clearInterruptRequested(),
      connectionStatus: snapshot.connectionStatus,
      mode: snapshot.mode,
      setMode: this.setMode,
      markPlanEntryFromSlashCommand: this.markPlanEntryFromSlashCommand,
      setModel: this.setModel,
      setSelectedAgentosModel: this.setSelectedAgentosModel,
      setPreferredLanguage: this.setPreferredLanguage,
      setThemeName: this.setThemeName,
      setAccentColor: this.setAccentColor,
      transcriptMode: snapshot.transcriptMode,
      setTranscriptMode: this.setTranscriptMode,
      transcriptFoldMode: snapshot.transcriptFoldMode,
      setTranscriptFoldMode: this.setTranscriptFoldMode,
      collapsedToolGroupCount: toolGroupIds.filter((id) => snapshot.collapsedToolGroupIds.has(id))
        .length,
      collapseToolGroups: this.collapseToolGroups,
      expandToolGroups: this.expandToolGroups,
      sessionTitle: snapshot.sessionTitle,
      setSessionTitle: this.setSessionTitle,
      getTrustedDirs: getTrustedDirs,
      validateDirPath: validateDirPath,
      addTrustedDir: addTrustedDir,
      setTrustedDir: setTrustedDir,
      removeTrustedDir: removeTrustedDir,
      clearTrustedDirs: clearTrustedDirs,
      setCurrentProjectDir: (dir: string) => {
        setCurrentProjectDir(dir);
        setCurrentCwd(dir);
      },
      getCurrentProjectDir: getCurrentProjectDir,
      getWorkspaceDir: () => getCurrentCwd() || process.cwd(),
      enterConfigEditor: undefined, // AppScreen injects the real handler when executing slash commands.
      enterFileViewer: undefined, // AppScreen injects the real handler when executing slash commands.
      enterDiffViewer: undefined, // AppScreen injects the real handler when executing slash commands.
      setInput: this._setInputRef ?? undefined,
      enterStatusView: undefined,
      getUsageSummary: () => this.getUsageSummary(),
      restartStatusLine: () => this.restartStatusLinePoll(),
      getStatusLineJsonInput: () => this.buildStatusLineJsonInput(),
      hasRunningTeamTasks: () => {
        const snapshot = this.getSnapshot();
        // Use cancellableWork which covers all stages: processing, running tools, subtasks, team working
        return snapshot.cancellableWork;
      },
      setRunningCommand: (name: string | null) => this.setRunningCommand(name),
      // ── /switch 公共契约端口 ──
      checkHandoff: (target) => this.handoffPort?.checkHandoff(target)
        ?? {
          ok: false,
          code: "NOT_SUPERVISED",
          message: "Running outside agentos-tui launcher; start via `agentos-tui` to use /switch",
        },
      requestHandoff: (target, switchContent) => this.handoffPort
        ? this.handoffPort.requestHandoff(target, switchContent)
        : Promise.reject(new Error("Handoff port not available (not supervised)")),
      hasServerTask: () => this.taskLifecycle?.hasServerTask() ?? this.hasServerTask(),
      cancelAndWaitForIdle: (opts) => this.taskLifecycle
        ? this.taskLifecycle.cancelAndWaitForIdle(opts)
        : Promise.reject(new Error("Task lifecycle port not available")),
      startPrWatch: this.startPrWatch,
      stopPrWatch: this.stopPrWatch,
      isPrWatchActive: this.isPrWatchActive,
      setAutofixAutoApprove: this.setAutofixAutoApprove,
    };
  }

  /** Start a TUI-side PR watch (replacing any existing one). */
  readonly startPrWatch = (config: {
    repo: string;
    prNumber: string;
    platform: string;
    intervalMs?: number;
    autoApprove?: boolean;
    preferredLanguage?: PreferredLanguage;
  }): void => {
    const { autoApprove, preferredLanguage, ...watchConfig } = config;
    this.prWatchController?.stop("被新的 watch 取代", { silent: true });
    // Apply the run-scoped grant here — after the prior watch's onStopped has
    // cleared it, but before start() synchronously sends the first round — so
    // round 1 is auto-approved too. (Setting it in the command before this call
    // would be wiped by the replaced watch's onStopped.)
    if (autoApprove) this.autofixAutoApprove = true;
    this.prWatchController = new PrWatchController(
      {
        sendMessage: (prompt) => this.sendMessage(prompt, undefined, this.mode, { logAsUser: false }),
        isBusy: () => {
          const s = this.getSnapshot();
          return s.isProcessing || s.cancellableWork || Boolean(s.pendingQuestion);
        },
        isConnected: () => this.connectionStatus === "connected",
        notify: (message, isError) =>
          this.addItem((isError ? addError : addInfo)(this.sessionId, message, isError ? undefined : "i")),
        preferredLanguage: preferredLanguage ?? this.preferredLanguage,
        onStopped: () => {
          // Watch ended (green / merged / closed / fuse / replaced) → drop the
          // run-scoped auto-approve grant so the next run asks again.
          this.autofixAutoApprove = false;
        },
      },
      watchConfig,
    );
    this.prWatchController.start();
  };

  /** Stop the active PR watch; returns true if one was running. */
  readonly stopPrWatch = (): boolean => {
    if (!this.prWatchController?.active) return false;
    this.prWatchController.stop(this.preferredLanguage === "zh" ? "用户手动停止" : "stopped by user");
    this.prWatchController = null;
    return true;
  };

  readonly isPrWatchActive = (): boolean => Boolean(this.prWatchController?.active);

  /** Grant/revoke run-scoped auto-approval of tool-permission prompts. */
  readonly setAutofixAutoApprove = (on: boolean): void => {
    this.autofixAutoApprove = on;
  };

  /**
   * If this is a tool-permission prompt and we can confidently identify its
   * "allow once" option, approve it automatically (the user pre-authorized) and
   * return true. Scoped to permission_interrupt only — ask_user / confirm / plan
   * prompts are genuine decisions and must still reach the user. If the option
   * shape is not what we expect, return false so the card renders normally
   * (fail toward asking, never toward a blind wrong answer).
   */
  private tryAutoApprovePermission(question: PendingQuestion): boolean {
    if (question.source !== "permission_interrupt") return false;
    const options = question.questions?.[0]?.options;
    if (!Array.isArray(options) || options.length === 0) return false;
    // "Allow once" = an allow option that is NOT "always allow" (which would
    // persist beyond the run). zh: 本次允许 / 总是允许; en: Allow once / Always allow.
    const isAllow = (l: string) => l.includes("允许") || /^allow\b/i.test(l.trim());
    const isAlwaysAllow = (l: string) =>
      l.includes("总是允许") || /^always allow\b/i.test(l.trim());
    const allowOnce = options.find((o) => isAllow(o.label) && !isAlwaysAllow(o.label));
    if (!allowOnce) return false;
    this.pendingQuestion = question; // submitQuestionAnswers reads this
    this.addItem(
      addInfo(this.sessionId, `※ /autofix-pr 自动批准命令：${allowOnce.label}`, "※"),
    );
    this.submitQuestionAnswers([
      { selected_options: [allowOnce.label], custom_input: allowOnce.label },
    ]);
    return true;
  }

  getUsageSummary(): SessionUsageSummary {
    const entries = Array.from(this.usageByModel.values());
    return {
      total_input_tokens: entries.reduce((s, e) => s + e.input_tokens, 0),
      total_output_tokens: entries.reduce((s, e) => s + e.output_tokens, 0),
      total_tokens: entries.reduce((s, e) => s + e.total_tokens, 0),
      byModel: entries,
    };
  }

  private appendUsageDelta(usage: Record<string, unknown>, model?: string): void {
    const key = model || "unknown";
    const existing = this.usageByModel.get(key);
    const inputDelta = this.safeTokenCount(usage.input_tokens);
    const outputDelta = this.safeTokenCount(usage.output_tokens);
    const totalDelta =
      typeof usage.total_tokens === "number" && Number.isFinite(usage.total_tokens)
        ? Math.max(0, usage.total_tokens)
        : inputDelta + outputDelta;
    const entry: ModelUsageEntry = {
      model: key,
      input_tokens: (existing?.input_tokens ?? 0) + inputDelta,
      output_tokens: (existing?.output_tokens ?? 0) + outputDelta,
      total_tokens: (existing?.total_tokens ?? 0) + totalDelta,
    };
    this.usageByModel.set(key, entry);
  }

  private updateCurrentUsageTokens(usage: Record<string, unknown>): void {
    const inputDelta = this.safeTokenCount(usage.input_tokens);
    const outputDelta = this.safeTokenCount(usage.output_tokens);
    const totalDelta =
      typeof usage.total_tokens === "number" && Number.isFinite(usage.total_tokens)
        ? Math.max(0, usage.total_tokens)
        : inputDelta + outputDelta;
    if (inputDelta === 0 && outputDelta === 0 && totalDelta === 0) {
      return;
    }
    this.currentQueryUsage = {
      input_tokens: this.currentQueryUsage.input_tokens + inputDelta,
      output_tokens: this.currentQueryUsage.output_tokens + outputDelta,
      total_tokens: this.currentQueryUsage.total_tokens + totalDelta,
    };
  }

  private safeTokenCount(value: unknown): number {
    return typeof value === "number" && Number.isFinite(value) ? Math.max(0, value) : 0;
  }

  private resetCurrentUsageTokens(): void {
    this.currentQueryUsage = {
      input_tokens: 0,
      output_tokens: 0,
      total_tokens: 0,
    };
  }

  /** AppScreen 在初始化时注入 setInput 回调，使 app-state 可以填充输入框。 */
  setInputRef(ref: (text: string) => void): void {
    this._setInputRef = ref;
  }

  /** AppScreen 在初始化时注入 getInputValue 回调，使 app-state 可以读取输入框内容。 */
  getInputValueRef(ref: () => string): void {
    this._getInputValueRef = ref;
  }

  // ── TaskLifecyclePort 订阅接口 ──

  /**
   * 订阅 interrupt_result 事件；按 requestId + sessionId 关联。
   * 返回取消订阅函数。迟到事件（waiter 已清除）由 listener 自行过滤。
   */
  onInterruptResult(
    handler: (requestId: string, sessionId: string, success: boolean, message?: string) => void,
  ): () => void {
    this.interruptResultListeners.add(handler);
    return () => {
      this.interruptResultListeners.delete(handler);
    };
  }

  /** 订阅 WebSocket 断开或认证失败。 */
  onConnectionLost(handler: () => void): () => void {
    this.connectionLostListeners.add(handler);
    return () => {
      this.connectionLostListeners.delete(handler);
    };
  }

  /** 订阅 AppState.stop（进程退出前清理）。 */
  onStop(handler: () => void): () => void {
    this.stopListeners.add(handler);
    return () => {
      this.stopListeners.delete(handler);
    };
  }

  /**
   * 由 event-handlers 在收到 chat.interrupt_result 时调用；
   * 通知所有订阅器，按 requestId + sessionId 关联。
   */
  notifyInterruptResult(
    requestId: string,
    sessionId: string,
    success: boolean,
    message?: string,
  ): void {
    for (const listener of this.interruptResultListeners) {
      try {
        listener(requestId, sessionId, success, message);
      } catch {
        // ignore — listener 错误不应影响其他订阅器或 UI 处理
      }
    }
  }

  /**
   * 由 index.ts 在 AppState 构造后回填监督端口。
   * TaskLifecyclePort、HandoffPort、ReauthenticationPort 依赖 AppState 方法，
   * 无法在构造函数中直接创建。
   */
  setSupervisionPorts(ports: {
    handoffPort: HandoffPort | null;
    taskLifecycle: TaskLifecyclePort | null;
    reauthPort: ReauthenticationPort | null;
    uiLifecycle: UiLifecyclePort | null;
  }): void {
    this.handoffPort = ports.handoffPort;
    this.taskLifecycle = ports.taskLifecycle;
    this.reauthPort = ports.reauthPort;
    this.uiLifecycle = ports.uiLifecycle;
  }

  /** 测试/外部用：获取 ReauthenticationPort（由 ws-client 在权威认证过期时调用）。 */
  getReauthPort(): ReauthenticationPort | null {
    return this.reauthPort;
  }

  /**
   * 计算随请求发送的本地上下文路径（project_dir/cwd/trusted_dirs）。
   * Remote 模式下本地 PC 路径不可被远端沙箱访问，故只发送服务器侧 /home/<user-id>
   * 作为 project_dir/cwd，不发送 trusted_dirs；非 remote 时沿用本地 trusted_dirs/cwd。
   */
  private resolveRequestPaths(): {
    projectDir: string;
    cwd: string;
    trustedDirs: string[];
  } {
    if (this.isRemote && this.remoteProjectDir) {
      return { projectDir: this.remoteProjectDir, cwd: this.remoteProjectDir, trustedDirs: [] };
    }
    const trustedDirs = getTrustedDirs();
    const projectDir = getCurrentProjectDir() || process.cwd();
    const cwd = getCurrentCwd() || projectDir;
    return { projectDir, cwd, trustedDirs };
  }

  readonly sendEventOnly = (
    method: string,
    params: Record<string, unknown>,
    isStream = false,
  ): string => {
    const id = `tui_${Date.now().toString(16)}_${Math.random().toString(36).slice(2, 6)}`;
    const send = () => {
      const { projectDir, cwd, trustedDirs } = this.resolveRequestPaths();
      this.wsClient.send({
        type: "req",
        id,
        method,
        ...(isStream ? { is_stream: true } : {}),
        params: {
          ...params,
          ...(method === "session.create" && params.session_id === undefined
            ? {}
            : { session_id: (params.session_id as string | undefined) ?? this.sessionId }),
          ...(trustedDirs.length > 0 ? { trusted_dirs: trustedDirs } : {}),
          ...(projectDir ? { project_dir: projectDir } : {}),
          ...(cwd ? { cwd } : {}),
        },
      });
    };
    const bootCreation = this.bootSessionCreation;
    if (bootCreation) {
      void bootCreation.then(send).catch(() => undefined);
    } else {
      send();
    }
    return id;
  };

  private requestAgentServer = async <T = Record<string, unknown>>(
    method: string,
    params: Record<string, unknown>,
    timeoutMs: number | undefined,
    waitForBootSession: boolean,
  ): Promise<T> => {
    if (waitForBootSession && this.bootSessionCreation) {
      await this.bootSessionCreation;
    }
    const id = `tui_${Date.now().toString(16)}_${Math.random().toString(36).slice(2, 6)}`;
    // 记录当前命令请求 ID，以便 Ctrl+C 时能立即取消 WS 请求
    this.activeCommandRequestId = id;
    const { projectDir, cwd, trustedDirs } = this.resolveRequestPaths();
    try {
      const response = await this.wsClient.request(
        id,
        method,
        {
          ...params,
          ...(method === "session.create" && params.session_id === undefined
            ? {}
            : { session_id: params.session_id ?? this.sessionId }),
          ...(trustedDirs.length > 0 ? { trusted_dirs: trustedDirs } : {}),
          ...(projectDir ? { project_dir: projectDir } : {}),
          ...(cwd ? { cwd } : {}),
        },
        timeoutMs ?? 30000,
      );
      if (method === "config.get") {
        const payload = response.payload;
        const enabled =
          payload && typeof payload === "object" && !Array.isArray(payload)
            ? parseSkillEvolutionEnabled((payload as Record<string, unknown>).skill_evolution)
            : false;
        this.setSkillEvolutionEnabled(enabled);
      } else if (
        method === "config.set" &&
        Object.prototype.hasOwnProperty.call(params, "skill_evolution")
      ) {
        // config.set 响应只确认写盘；重新读取权威 payload，保证 hot reload 后 UI 与后端一致。
        void this.fetchModelInfo();
      }
      return response.payload as T;
    } finally {
      // 请求完成后清理追踪（无论成功/失败/取消）
      if (this.activeCommandRequestId === id) {
        this.activeCommandRequestId = null;
      }
    }
  };

  readonly request = async <T = Record<string, unknown>>(
    method: string,
    params: Record<string, unknown>,
    timeoutMs?: number,
  ): Promise<T> => this.requestAgentServer<T>(method, params, timeoutMs, true);

  readonly notifyDisconnectBeforeExit = async (reason = "user_exit"): Promise<void> => {
    if (this.connectionStatus !== "connected") {
      return;
    }
    const id = `tui_disconnect_${Date.now().toString(16)}_${Math.random().toString(36).slice(2, 6)}`;
    try {
      await this.wsClient.request(
        id,
        "tui.disconnect",
        {
          reason,
          session_id: this.sessionId,
          mode: this.mode,
        },
        500,
      );
    } catch {
      // Best effort only; the process is exiting.
    }
  };

  readonly loadWorkflowSnapshot = async (sessionId = this.sessionId): Promise<void> => {
    const payload = await this.request<{
      type?: string;
      workflows?: unknown[];
      session_id?: string;
      total?: number;
      has_more?: boolean;
    }>(
      "command.workflows",
      {
        action: "list",
        session_id: sessionId,
      },
      30000,
    );
    this.applyWorkflowSnapshotPayload(payload);
  };

  readonly loadWorkflowDetail = async (
    workflowId: string,
    sessionId = this.sessionId,
    phaseOffset = 0,
  ): Promise<void> => {
    const payload = await this.request<{
      type?: string;
      workflow?: unknown;
      phase_total?: number;
      has_more?: boolean;
    }>(
      "command.workflows",
      {
        action: "get_workflow",
        workflow_id: workflowId,
        session_id: sessionId,
        phase_offset: phaseOffset,
      },
      30000,
    );
    if (
      payload.type === "workflow_run_detail" &&
      payload.workflow &&
      typeof payload.workflow === "object" &&
      !Array.isArray(payload.workflow) &&
      "id" in payload.workflow
    ) {
      const workflow = payload.workflow as WorkflowRun;
      if (payload.has_more === true) {
        workflow.has_more = true;
      }
      if (typeof payload.phase_total === "number") {
        workflow.phase_total = payload.phase_total;
      }
      this.applyWorkflowUpdate(workflow);
    }
  };

  readonly loadPhaseAgents = async (
    workflowId: string,
    phaseId: string,
    sessionId = this.sessionId,
    agentOffset = 0,
  ): Promise<void> => {
    const payload = await this.request<{
      type?: string;
      phase?: unknown;
      agent_total?: number;
      has_more?: boolean;
      error?: unknown;
    }>(
      "command.workflows",
      {
        action: "get_phase",
        workflow_id: workflowId,
        phase_id: phaseId,
        session_id: sessionId,
        agent_offset: agentOffset,
      },
      30000,
    );
    if (payload.error || !payload.phase || typeof payload.phase !== "object") return;
    const phase = payload.phase as WorkflowPhase & { workflow_id?: string };
    const existing = this.workflowRuns.find((item) => item.id === workflowId);
    if (!existing) return;
    // Hand the incoming phase (agent summaries) to applyWorkflowUpdate's merge
    // path — mergeWorkflowAgent preserves already-loaded full bodies (from
    // get_agent) and stamps detail_pending on summary-only agents.
    this.applyWorkflowUpdate({
      ...existing,
      phases: [...(existing.phases ?? []), phase],
    });
  };

  readonly loadAgentDetail = async (
    workflowId: string,
    phaseId: string,
    agentId: string,
    sessionId = this.sessionId,
  ): Promise<string> => {
    const payload = await this.request<{
      type?: string;
      agent?: unknown;
      error?: unknown;
    }>(
      "command.workflows",
      {
        action: "get_agent",
        workflow_id: workflowId,
        phase_id: phaseId,
        agent_id: agentId,
        session_id: sessionId,
      },
      30000,
    );
    if (payload.error || !payload.agent || typeof payload.agent !== "object") return "";
    const agent = reassembleAgentFieldParts(payload.agent as WorkflowAgent);

    const existing = this.workflowRuns.find((item) => item.id === workflowId);
    if (!existing) return agent.human_prompt ?? "";

    const updatedPhases = (existing.phases ?? []).map((phase) =>
      phase.id === phaseId
        ? {
            ...phase,
            agents: (phase.agents ?? []).map((a) =>
              a.id === agentId ? { ...a, ...agent } : a,
            ),
          }
        : phase,
    );
    this.applyWorkflowUpdate({ ...existing, phases: updatedPhases });
    return agent.human_prompt ?? "";
  };

  readonly ensureHumanPromptLoaded = async (
    workflowId: string,
    agentId: string,
  ): Promise<void> => {
    const lookup = findWorkflowAgent(this.workflowRuns, workflowId, agentId);
    if (!lookup) return;
    const current = lookup.agent.human_prompt?.trim() ?? "";
    if (current) return;
    try {
      await this.loadAgentDetail(workflowId, lookup.phase.id, agentId);
    } catch {
      // Best-effort — pending list still shows whatever partial text we have.
    }
  };

  readonly applyWorkflowSnapshotPayload = (payload: {
    type?: unknown;
    workflows?: unknown;
    total?: unknown;
    has_more?: unknown;
    [key: string]: unknown;
  }): void => {
    const workflows = Array.isArray(payload.workflows) ? payload.workflows : [];
    if (payload.type !== "workflow_run_snapshot" && workflows.length === 0) {
      return;
    }
    const incoming = workflows.filter((item): item is WorkflowRun =>
      Boolean(item && typeof item === "object" && !Array.isArray(item) && "id" in item),
    );
    const merged = incoming.map((workflow) => {
      const existing = this.workflowRuns.find((item) => item.id === workflow.id);
      return normalizeWorkflowRun(mergeWorkflowRun(existing, workflow));
    });
    const incomingIds = new Set(incoming.map((workflow) => workflow.id));
    const preserved = this.workflowRuns.filter((workflow) => !incomingIds.has(workflow.id));
    this.setWorkflowRuns([...merged, ...preserved]);
  };

  readonly setWorkflowRuns = (workflows: WorkflowRun[]): void => {
    this.workflowRuns = workflows.map((workflow) => normalizeWorkflowRun(workflow));
    this.recomputePendingHumanPrompts();
    this.emitChange();
  };

  readonly applyWorkflowUpdate = (workflow: WorkflowRun): void => {
    this.workflowRuns = applyWorkflowUpdate(this.workflowRuns, workflow);
    this.recomputePendingHumanPrompts();
    this.emitChange();
  };

  /** Recompute the pending-human-prompt map from the full workflow snapshot. */
  private recomputePendingHumanPrompts(): void {
    // Preserve reply summaries from the previous map for entries that are no
    // longer waiting (replied -> running) so the list can dim them briefly.
    const next = new Map<string, PendingHumanPrompt>();
    for (const wf of this.workflowRuns) {
      const waiting = collectWaitingForHuman(wf);
      for (const { phase, agent } of waiting) {
        const key = `${wf.id}:${agent.correlation_id ?? agent.id}`;
        const turn = phaseLocalTurnNumber(agent, phase.agents ?? []) ?? 0;
        const prev = this.pendingHumanPrompts.get(key);
        next.set(key, {
          workflow_run_id: wf.id,
          workflow_id: wf.id,
          workflow_name: wf.name,
          agent_id: agent.id,
          correlation_id: agent.correlation_id ?? "",
          prompt: mergeHumanPromptText(prev?.prompt, agent.human_prompt ?? ""),
          label: agent.name,
          is_session: isSessionNode(agent),
          turn,
          // Carry forward a prior reply summary if this entry was replied then
          // somehow re-listed (defensive — normally a replied turn won't wait again).
          replied: prev?.replied,
          answer: prev?.answer,
        });
      }
      // Dim entries that were waiting and are now replied (status moved off
      // waiting_for_human but human_reply was just set).
      for (const phase of wf.phases ?? []) {
        for (const agent of phase.agents ?? []) {
          if (agent.status !== "waiting_for_human" && agent.human_reply) {
            const key = `${wf.id}:${agent.correlation_id ?? agent.id}`;
            const prev = this.pendingHumanPrompts.get(key);
            if (prev && !prev.replied) {
              next.set(key, { ...prev, replied: true, answer: agent.human_reply });
            }
          }
        }
      }
    }
    this.pendingHumanPrompts = next;
  }

  readonly setPendingHumanPrompts = (prompts: Map<string, PendingHumanPrompt> | null): void => {
    this.pendingHumanPrompts = prompts ?? new Map();
    this.emitChange();
  };

  readonly updateSession = (newId: string): void => {
    this.sessionId = newId;
    this.lastVisibleUserRequest = null;
    this.usageByModel.clear();
    this.resetCurrentUsageTokens();
    this.workflowRuns = [];
    this.pendingHumanPrompts = new Map();
    this.btwOverlay = null;
    this.btwHistory = [];
    this.btwOverlayIndex = -1;
    this._btwActive = false;
    this.btwPendingQuestion = null;
    this.pendingPlanEntrySource = null;
    if (this.accentColor !== "default") {
      this.accentColor = "default";
      setCurrentAccentColor("default");
    }
    this.emitChange();
  };

  readonly setSessionTitle = (title: string): void => {
    this.sessionTitle = title;
    this.emitChange();
  };

  readonly safeFetchSessionTitle = (sessionId: string): void => {
    void (async () => {
      try {
        const meta = await this.request<{ session_id: string; title: string }>("session.rename", {
          session_id: sessionId,
        });
        this.setSessionTitle(meta.title || "");
      } catch {
        // 标题获取失败不影响核心功能
      }
    })();
  };

  readonly addItem = (item: HistoryItem): void => {
    this.entries = [...this.entries, item];
    if (item.kind === "error") {
      this.lastError = item.content;
    } else {
      this.lastError = null;
    }
    // 用户发言后重置自动回顾状态，允许下一次空闲时触发新的回顾
    if (item.kind === "user") {
      this.autoRecapState = "idle";
      // 用户发送新消息时自动清除 /btw overlay（含历史）
      if (
        this.btwOverlay !== null ||
        this.btwHistory.length > 0 ||
        this.btwPendingQuestion !== null
      ) {
        this.btwOverlay = null;
        this.btwHistory = [];
        this.btwOverlayIndex = -1;
        this._btwActive = false;
        this.btwPendingQuestion = null;
      }
    }
    this.emitChange();
  };

  /** 设置 /btw 侧问题覆盖层（独立于 transcript 渲染，不受滚动影响） */
  readonly setBtwOverlay = (question: string, answer: string): void => {
    this.btwPendingQuestion = null;
    this.btwOverlay = { question, answer };
    this.btwHistory.push({ question, answer });
    this.btwOverlayIndex = this.btwHistory.length - 1;
    this.emitChange();
  };

  /** 设置 BTW 活动状态（加载中或 overlay 可见），用于 Esc 优先级判断 */
  readonly setBtwActive = (active: boolean): void => {
    const pendingChanged = !active && this.btwPendingQuestion !== null;
    if (this._btwActive !== active || pendingChanged) {
      this._btwActive = active;
      if (!active) {
        this.btwPendingQuestion = null;
      }
      this.emitChange();
    }
  };

  readonly setBtwPendingQuestion = (question: string | null): void => {
    if (this.btwPendingQuestion !== question) {
      this.btwPendingQuestion = question;
      this.emitChange();
    }
  };

  /** 清除 /btw 侧问题覆盖层（同时清空历史，Esc 视为放弃这批侧问） */
  readonly clearBtwOverlay = (): void => {
    if (
      this.btwOverlay !== null ||
      this.btwHistory.length > 0 ||
      this.btwPendingQuestion !== null
    ) {
      this.btwOverlay = null;
      this.btwHistory = [];
      this.btwOverlayIndex = -1;
      this._btwActive = false;
      this.btwPendingQuestion = null;
      this.emitChange();
    }
  };

  /** 在 btw 历史中前后切换当前 overlay（仅 ≥2 条时生效） */
  readonly navigateBtw = (direction: -1 | 1): void => {
    if (this.btwHistory.length < 2 || this.btwOverlayIndex < 0) return;
    const len = this.btwHistory.length;
    const next = Math.max(0, Math.min(len - 1, this.btwOverlayIndex + direction));
    if (next === this.btwOverlayIndex) return;
    this.btwOverlayIndex = next;
    this.btwOverlay = this.btwHistory[next];
    this.emitChange();
  };

  /** 删除当前 btw 条目；剩余非空则跳到相邻条目，为空则关闭 overlay */
  readonly deleteCurrentBtwEntry = (): void => {
    if (this.btwOverlayIndex < 0 || this.btwHistory.length === 0) return;
    this.btwHistory.splice(this.btwOverlayIndex, 1);
    const len = this.btwHistory.length;
    if (len === 0) {
      this.btwOverlay = null;
      this.btwOverlayIndex = -1;
      this._btwActive = false;
      this.btwPendingQuestion = null;
    } else {
      this.btwOverlayIndex = Math.min(this.btwOverlayIndex, len - 1);
      this.btwOverlay = this.btwHistory[this.btwOverlayIndex];
    }
    this.emitChange();
  };

  readonly isHelpVisible = (): boolean => {
    if (this.entries.length === 0) return false;
    const lastEntry = this.entries[this.entries.length - 1];
    return lastEntry?.kind === "info" && lastEntry.meta?.view === "help";
  };

  readonly dismissHelp = (): boolean => {
    if (!this.isHelpVisible()) return false;
    this.entries = this.entries.slice(0, -1);
    this.emitChange();
    return true;
  };

  readonly beginDeferredTranscript = (): void => {
    this.deferTranscriptFrames = true;
  };

  readonly flushDeferredTranscript = (): void => {
    if (!this.deferTranscriptFrames && this.deferredTranscriptFrames.length === 0) {
      return;
    }
    this.deferTranscriptFrames = false;
    const frames = this.deferredTranscriptFrames;
    this.deferredTranscriptFrames = [];
    let changed = false;
    for (const frame of frames) {
      changed = handleIncomingFrame(this.eventDelegate, frame) || changed;
    }
    if (changed) {
      this.emitChange();
    }
  };

  readonly clearEntries = (): void => {
    if (this.localPendingQuestion) {
      this.localPendingQuestion.reject(new Error("input flow was interrupted"));
      this.localPendingQuestion = null;
    }
    // A watch is tied to the current PR/session; a new/cleared session drops it.
    this.prWatchController?.stop("会话已重置", { silent: true });
    this.prWatchController = null;
    this.autofixAutoApprove = false;
    this.entries = [];
    this.pendingQuestion = null;
    this.lastError = null;
    this.btwOverlay = null;
    this.btwHistory = [];
    this.btwOverlayIndex = -1;
    this._btwActive = false;
    this.btwPendingQuestion = null;
    this.setStreamingStateInternal(StreamingState.Idle);
    this.collapsedToolGroupIds.clear();
    this.activeSubtasks.clear();
    this.todos = [];
    this.teamMemberEvents = [];
    this.teamTaskEvents = [];
    this.teamMessageEvents = [];
    this.evolutionStatus = "idle";
    this.contextCompression = null;
    this.contextWindowLimit = null;
    this.contextUsedPercentage = null;
    this.clearToolExecutionState();
    this.historyEntries = [];
    this.historyTotalPages = null;
    this.historyPageDoneResolvers.clear();
    this.harnessExtensionReady = null;
    this.harnessActivateInteraction = null;
    this.deferTranscriptFrames = false;
    this.deferredTranscriptFrames = [];
    this.clearActiveTurnReconnectTimer();
    this.activeTurnReconnectNoticeShown = false;
    this.clearStreamStallWatchdog();
    this.emitChange();
  };

  readonly setMode = (mode: ClientMode): void => {
    if (!isPlanClientMode(mode)) {
      this.pendingPlanEntrySource = null;
    }
    if (this.mode !== mode) {
      this.mode = mode;
      this.emitChange();
    }
  };

  readonly markPlanEntryFromSlashCommand = (): void => {
    this.pendingPlanEntrySource = PLAN_ENTRY_SOURCE_SLASH_COMMAND;
  };

  readonly setModel = (name: string): void => {
    const trimmed = name.trim();
    if (trimmed && this.modelInfo.model !== trimmed) {
      this.modelInfo = { ...this.modelInfo, model: trimmed };
      this.emitChange();
    }
    // 切回 defaults 模型 → 放弃 agentos 请求级注入，恢复启动默认模型路由
    if (this.selectedAgentosModel !== null) {
      this.selectedAgentosModel = null;
      this.selectedAgentosProvider = "";
      this.selectedAgentosModelKey = null;
      this.emitChange();
    }
  };

  /**
   * 全局选中 agentos 备份模型（请求级注入）。与 setModel 互斥：
   * 选 agentos 不改 modelInfo（启动默认不变），仅记录注入名；
   * setModel 会清空本字段。
   *
   * name 为展示用纯名；key 为后端 model_key（model_name#global_idx），供
   * chat.send 精确注入同名 agentos 条目，缺省时回退到 name。
   */
  readonly setSelectedAgentosModel = (
    name: string | null,
    provider?: string,
    key?: string | null,
  ): void => {
    const trimmed = name ? name.trim() : "";
    const next = trimmed || null;
    const nextKey = key ? key.trim() || null : null;
    if (
      this.selectedAgentosModel !== next
      || this.selectedAgentosProvider !== (provider ?? "")
      || this.selectedAgentosModelKey !== (next ? nextKey : null)
    ) {
      this.selectedAgentosModel = next;
      this.selectedAgentosProvider = next ? (provider ?? "") : "";
      this.selectedAgentosModelKey = next ? nextKey : null;
      this.emitChange();
    }
  };

  readonly setPreferredLanguage = (language: PreferredLanguage): void => {
    if (this.preferredLanguage !== language) {
      this.preferredLanguage = language;
      this.emitChange();
    }
  };

  readonly setLastError = (error: string | null): void => {
    this.lastError = error;
    this.emitChange();
  };

  readonly setThemeName = (theme: ThemeName): void => {
    if (this.themeName !== theme) {
      this.themeName = theme;
      setCurrentThemeName(theme);
      this.emitChange();
    }
  };

  readonly setAccentColor = (color: AccentColorName): void => {
    if (this.accentColor !== color) {
      this.accentColor = color;
      setCurrentAccentColor(color);
      this.emitChange();
    }
  };

  readonly setTranscriptMode = (mode: "compact" | "detailed"): void => {
    if (this.transcriptMode !== mode) {
      this.transcriptMode = mode;
      this.emitChange();
    }
  };

  readonly setTranscriptFoldMode = (mode: "none" | "tools" | "thinking" | "all"): void => {
    if (this.transcriptFoldMode !== mode) {
      this.transcriptFoldMode = mode;
      this.emitChange();
    }
  };

  readonly collapseToolGroups = (scope: "last" | "all"): void => {
    const ids = getToolGroupIds(
      this.entries,
      this.toolExecutionOrder
        .map((toolCallId) => this.toolExecutions.get(toolCallId))
        .filter((item): item is ToolExecution => Boolean(item)),
    );
    if (scope === "all") {
      this.collapsedToolGroupIds = new Set(ids);
    } else {
      const last = ids[ids.length - 1];
      if (last) {
        this.collapsedToolGroupIds = new Set(this.collapsedToolGroupIds);
        this.collapsedToolGroupIds.add(last);
      }
    }
    this.emitChange();
  };

  readonly expandToolGroups = (scope: "last" | "all"): void => {
    if (scope === "all") {
      this.collapsedToolGroupIds.clear();
    } else {
      const ids = getToolGroupIds(
        this.entries,
        this.toolExecutionOrder
          .map((toolCallId) => this.toolExecutions.get(toolCallId))
          .filter((item): item is ToolExecution => Boolean(item)),
      );
      const last = ids[ids.length - 1];
      if (last) {
        this.collapsedToolGroupIds = new Set(this.collapsedToolGroupIds);
        this.collapsedToolGroupIds.delete(last);
      }
    }
    this.emitChange();
  };

  readonly sendMessage = (
    content: string,
    attachments?: FileAttachment[],
    modeOverride?: ClientMode,
    options?: { logAsUser?: boolean },
    skills?: string[],
  ): string | null => {
    if (this.connectionStatus !== "connected") return null;
    const mode = modeOverride ?? this.mode;
    const planEntrySource = isPlanClientMode(mode) ? this.pendingPlanEntrySource : null;
    const params = {
      content,
      query: content,
      mode,
      ...(attachments?.length ? { attachments } : {}),
      ...(planEntrySource ? { plan_entry_source: planEntrySource } : {}),
      ...(skills?.length ? { skills } : {}),
      // agentos 备份模型：请求级注入 model_name，AgentServer 据此路由到 agentos 缓存条目。
      // 为空时省略，沿用启动默认模型。优先用后端 model_key（model_name#global_idx），
      // 后端 _resolve_model_by_name 据 #global_idx 精确命中同名 agentos 条目；
      // 无 key 时回退纯名（同名时会被解析到 defaults，仅作兼容兜底）。
      ...((this.selectedAgentosModelKey || this.selectedAgentosModel)
        ? { model_name: this.selectedAgentosModelKey || this.selectedAgentosModel }
        : {}),
    };
    // Pre-check: reject messages whose serialized frame exceeds 7 MB (gateway
    // server max_size is 8 MB; leave 1 MB margin for JSON overhead).
    const estimatedSize = JSON.stringify({ type: "req", method: "chat.send", params }).length;
    if (estimatedSize > 7 * 1024 * 1024) {
      this.addItem(addError(this.sessionId, `消息过大（约 ${Math.round(estimatedSize / 1024 / 1024)} MB），请缩短输入内容。`));
      this.emitChange();
      return null;
    }
    // Team 模式允许在 stream 未结束时直接 chat.send，不先发 cancel interrupt。
    if (this.streamingState !== StreamingState.Idle && !isTeamMode(mode)) {
      this.suppressInterruptResult = true;
      this.sendEventOnly("chat.interrupt", { intent: "cancel", mode: this.mode });
    }
    const requestId = this.sendEventOnly(
      "chat.send",
      params,
      true,
    );
    if (planEntrySource) {
      this.pendingPlanEntrySource = null;
    }
    this.lastError = null;
    this.resetCurrentUsageTokens();
    if (options?.logAsUser !== false) {
      this.lastVisibleUserRequest = { requestId, content, sessionId: this.sessionId };
      // 用户发言后重置自动回顾状态，允许下一次空闲时触发新的回顾
      this.autoRecapState = "idle";
      this.entries = [
        ...this.entries,
        {
          kind: "user",
          id: `user-${requestId}`,
          sessionId: this.sessionId,
          content,
          at: new Date().toISOString(),
        },
      ];
    } else {
      this.lastVisibleUserRequest = null;
    }
    this.setStreamingStateInternal(StreamingState.Responding);
    this.turnStartedAt = Date.now();
    this.emitChange();
    return requestId;
  };

  supplement(content: string, attachments?: FileAttachment[]): string | null {
    if (this.connectionStatus !== "connected") return null;
    const trimmed = content.trim();
    if (!trimmed) return null;
    // Same pre-check as sendMessage: reject oversized frames.
    const estimatedSize = JSON.stringify({ type: "req", method: "chat.interrupt", params: { intent: "supplement", new_input: trimmed, ...(attachments?.length ? { attachments } : {}) } }).length;
    if (estimatedSize > 7 * 1024 * 1024) {
      this.addItem(addError(this.sessionId, `消息过大（约 ${Math.round(estimatedSize / 1024 / 1024)} MB），请缩短输入内容。`));
      this.emitChange();
      return null;
    }
    const requestId = this.sendEventOnly("chat.interrupt", {
      intent: "supplement",
      new_input: trimmed,
      mode: this.mode,
      ...(attachments?.length ? { attachments } : {}),
    });
    this.lastError = null;
    this.resetCurrentUsageTokens();
    // 用户发言后重置自动回顾状态（supplement 也是用户消息）
    this.autoRecapState = "idle";
    this.entries = [
      ...this.entries,
      {
        kind: "user",
        id: `user-${requestId}`,
        sessionId: this.sessionId,
        content: trimmed,
        at: new Date().toISOString(),
      },
    ];
    this.setStreamingStateInternal(StreamingState.Responding);
    this.emitChange();
    return requestId;
  }

  /** 向服务端请求中断当前 session 的任务；成功发送前不宣称"已中断"。 */
  cancel(options?: { showNotice?: boolean }): boolean {
    // User intervention (Ctrl+C / other) revokes the run-scoped auto-approve
    // grant — subsequent tool prompts must reach the user again.
    this.autofixAutoApprove = false;
    if (this.connectionStatus !== "connected") {
      if (options?.showNotice !== false) {
        this.addItem(addError(this.sessionId, "Unable to interrupt task while disconnected"));
      }
      return false;
    }
    if (options?.showNotice === false) {
      this.suppressInterruptResult = true;
    }
    // Reject local pending question immediately so local commands (e.g. /export) can terminate
    if (this.localPendingQuestion) {
      this.localPendingQuestion.reject(new Error("interrupted by Ctrl+C"));
      this.localPendingQuestion = null;
      this.pendingQuestion = null;
      this.setStreamingStateInternal(StreamingState.Idle);
    } else if (this.pendingQuestion) {
      // Server-side pendingQuestion (ask_user_interrupt, permission_interrupt, etc.)
      // chat.interrupt sent below will notify the server to cancel the agent work
      this.pendingQuestion = null;
      this.setStreamingStateInternal(StreamingState.Idle);
    }
    // Set local interrupt flag immediately for long-running command detection
    this.interruptRequested = true;
    // 同时取消正在执行的命令 WS 请求（与 requestLocalInterrupt 保持一致）
    if (this.activeCommandRequestId) {
      this.wsClient.cancelRequest(this.activeCommandRequestId, "cancelled");
      this.activeCommandRequestId = null;
    }
    const hadLocalWork = this.getSnapshot().cancellableWork;
    // esc 去抖：混合策略。
    // 主条件：上一个 cancel interrupt 在途、尚未收到 interrupt_result 确认（cancelInterruptInFlight=true）
    //   → 不再重复发送。这覆盖"用户每隔 1.5-2s 按一次 esc、每次都超过短去抖窗口"的真实连按节奏——
    //   只要后端还没确认取消，就绝不再发新 interrupt，避免 N 个 interrupt 积压、最后一次性回 N 个"任务已取消"。
    // 兜底：若 result 丢失迟迟不回（超 INTERRUPT_DEBOUNCE_FALLBACK_MS），允许重新发送，防止窗口永久锁死。
    // 收到 interrupt_result（成功或失败）时 clearInterruptRequested 会清 cancelInterruptInFlight 提前解锁。
    const now = Date.now();
    const fallbackExpired =
      this.lastInterruptSentAt > 0 &&
      now - this.lastInterruptSentAt >= CliPiAppState.INTERRUPT_DEBOUNCE_FALLBACK_MS;
    const withinDebounce = this.cancelInterruptInFlight && !fallbackExpired;
    if (!withinDebounce) {
      this.cancelInterruptInFlight = true;
      this.lastInterruptSentAt = now;
      this.sendEventOnly("chat.interrupt", { intent: "cancel", mode: this.mode });
      if (options?.showNotice !== false && hadLocalWork) {
        this.addItem(addInfo(this.sessionId, "Request Interrupted", "i"));
      }
    }
    return true;
  }

  pause(): boolean {
    if (this.connectionStatus !== "connected") {
      return false;
    }
    this.interruptRequested = true;
    const hadLocalWork = this.getSnapshot().cancellableWork;
    this.sendEventOnly("chat.interrupt", { intent: "pause", mode: this.mode });
    return hadLocalWork;
  }

  resume(): void {
    this.sendEventOnly("chat.resume", {});
  }

  submitQuestionAnswers(answers: UserAnswer[]): void {
    if (!this.pendingQuestion) return;
    if (
      this.localPendingQuestion &&
      this.pendingQuestion.requestId === this.localPendingQuestion.requestId
    ) {
      const resolver = this.localPendingQuestion;
      this.localPendingQuestion = null;
      this.pendingQuestion = null;
      // 恢复之前的 streamingState
      this.streamingState = this.streamingStateBeforeQuestion ?? StreamingState.Idle;
      this.streamingStateBeforeQuestion = null;
      resolver.resolve(answers);
      this.emitChange();
      return;
    }
    if (isPlanApprovalRejectWithoutFeedback(this.pendingQuestion, answers)) {
      const resumeMode = this.pendingQuestion.resumeMode ?? this.mode;
      this.sendEventOnly("chat.send", {
        query: "",
        request_id: this.pendingQuestion.requestId,
        answers: withPlanApprovalCancelFeedback(answers),
        source: this.pendingQuestion.source,
        mode: resumeMode,
        plan_approval_kind: this.pendingQuestion.planApprovalKind,
        plan_content: this.pendingQuestion.planContent ?? "",
        plan_language: this.pendingQuestion.planLanguage ?? "cn",
      });
      this.pendingQuestion = null;
      this.setStreamingStateInternal(StreamingState.Idle);
      this.streamingStateBeforeQuestion = null;
      this.emitChange();
      return;
    }
    const source = this.pendingQuestion.source;
    const approvalTransport =
      this.pendingQuestion.evolutionMeta &&
      typeof this.pendingQuestion.evolutionMeta.approval_transport === "string"
        ? this.pendingQuestion.evolutionMeta.approval_transport
        : undefined;
    const shouldResumeInterrupt =
      source === "permission_interrupt" ||
      source === "confirm_interrupt" ||
      source === "ask_user_interrupt" ||
      source === "evolution_interrupt" ||
      (source === "skill_evolution_approval" && approvalTransport === "interrupt");

    if (shouldResumeInterrupt) {
      const resumeMode = this.pendingQuestion.resumeMode ?? this.mode;
      const structuredPlanPayload =
        this.pendingQuestion.planApprovalKind === "plan_approval"
          ? {
              plan_approval_kind: this.pendingQuestion.planApprovalKind,
              plan_content: this.pendingQuestion.planContent ?? "",
              plan_language: this.pendingQuestion.planLanguage ?? "cn",
            }
          : {};
      this.sendEventOnly(
        "chat.send",
        {
          query: "",
          request_id: this.pendingQuestion.requestId,
          answers,
          source,
          mode: resumeMode,
          ...structuredPlanPayload,
        },
        true,
      );
      this.setStreamingStateInternal(StreamingState.Responding);
    } else {
      const params: Record<string, unknown> = {
        request_id: this.pendingQuestion.requestId,
        answers,
        mode: this.mode,
      };
      if (this.pendingQuestion.evolutionMeta) {
        params.evolution_meta = this.pendingQuestion.evolutionMeta;
      }
      this.sendEventOnly(
        "chat.user_answer",
        params,
      );
    }
    this.pendingQuestion = null;
    if (this.streamingState !== StreamingState.Responding) {
      this.setStreamingStateInternal(StreamingState.Idle);
    }
    this.streamingStateBeforeQuestion = null;
    this.emitChange();
  }

  answerQuestion(answer: string): void {
    this.submitQuestionAnswers([{ selected_options: [answer], custom_input: answer }]);
  }

  readonly askQuestions = (
    questions: PendingQuestionItem[],
    source = "local_command",
  ): Promise<UserAnswer[]> => {
    if (questions.length === 0) {
      return Promise.resolve([]);
    }
    if (this.pendingQuestion || this.localPendingQuestion) {
      return Promise.reject(new Error("another question is already active"));
    }

    const requestId = `local_${Date.now().toString(16)}_${Math.random().toString(36).slice(2, 6)}`;
    // 保存之前的 streamingState，用于在对话框关闭后恢复（如果之前是在运行状态）
    this.streamingStateBeforeQuestion = this.streamingState;
    this.pendingQuestion = {
      requestId,
      source,
      questions,
    };
    this.setStreamingStateInternal(StreamingState.Idle);
    this.emitChange();

    return new Promise<UserAnswer[]>((resolve, reject) => {
      this.localPendingQuestion = { requestId, resolve, reject };
    });
  };

  readonly restoreHistory = async (targetSessionId: string): Promise<void> => {
    this.historyRequestToken += 1;
    const requestToken = this.historyRequestToken;
    this.historyEntries = [];
    this.historyTotalPages = null;
    this.historyPageDoneResolvers.clear();
    this.clearToolExecutionState();
    if (this.historyFlushTimer) {
      clearTimeout(this.historyFlushTimer);
      this.historyFlushTimer = null;
    }

    // 本地 channel 对 `history.get` 的 ack 里只有 `accepted: true` / `session_id` / `page_idx`，
    // 真正的消息和分页元数据通过 `history.message` 事件流异步到达；这里先处理本地 ack 返回里
    // 恰好带 messages 的分支（老链路兼容），再按 `total_pages` 循环逐页拉取。
    const fetchPage = async (pageIdx: number): Promise<void> => {
      const donePromise = new Promise<void>((resolve) => {
        this.historyPageDoneResolvers.set(pageIdx, resolve);
      });
      let ackPayload: {
        messages?: unknown[];
        total_pages?: number;
        page_idx?: number;
      } = {};
      try {
        ackPayload = await this.request<{
          messages?: unknown[];
          total_pages?: number;
          page_idx?: number;
        }>("history.get", { session_id: targetSessionId, page_idx: pageIdx });
      } catch (error) {
        this.historyPageDoneResolvers.delete(pageIdx);
        throw error;
      }

      if (Array.isArray(ackPayload.messages) && ackPayload.messages.length > 0) {
        const merged = mergeHistoryMessagesForRestore(ackPayload.messages);
        for (const message of merged) {
          if (requestToken !== this.historyRequestToken) {
            this.historyPageDoneResolvers.delete(pageIdx);
            return;
          }
          const entry = parseHistoryFrame({
            type: "event",
            event: "history.message",
            payload: {
              session_id: targetSessionId,
              message,
              total_pages: ackPayload.total_pages,
              page_idx: ackPayload.page_idx,
            },
          });
          if (entry) {
            this.historyEntries.push(entry);
          }
        }
        if (typeof ackPayload.total_pages === "number" && ackPayload.total_pages > 0) {
          this.historyTotalPages = ackPayload.total_pages;
        }
        // 本地 ack 已经自带全部数据，不会再发 done 事件，这里手动解析。
        this.historyPageDoneResolvers.delete(pageIdx);
        return;
      }

      // 等待 history.message 流的 `status: done` 帧；加超时保护，避免 done 丢失时无限挂起。
      const PAGE_TIMEOUT_MS = 15_000;
      await Promise.race([
        donePromise,
        new Promise<void>((resolve) => {
          setTimeout(() => {
            if (this.historyPageDoneResolvers.has(pageIdx)) {
              this.historyPageDoneResolvers.delete(pageIdx);
            }
            resolve();
          }, PAGE_TIMEOUT_MS);
        }),
      ]);
    };

    try {
      // 先拉第 1 页（取最新 50 条）。
      await fetchPage(1);
      if (requestToken !== this.historyRequestToken) return;

      // 后端按 `list(reversed(raw))` 分页：第 2, 3, ... 页是更早的消息。
      // 没有显式上限能确定会话总大小，统一按 total_pages 循环；每页不超过 50 条事件，成本可控。
      const totalPages = this.historyTotalPages ?? 1;
      for (let page = 2; page <= totalPages; page++) {
        if (requestToken !== this.historyRequestToken) return;
        try {
          await fetchPage(page);
        } catch (error) {
          // 容忍老会话或竞态下的 `invalid page_idx` 错误：停止翻页，保留已拉到的消息。
          if (isIgnorableHistoryRestoreError(error)) {
            break;
          }
          throw error;
        }
      }
    } catch (error) {
      if (requestToken === this.historyRequestToken) {
        throw error;
      }
      return;
    }

    setTimeout(() => {
      if (requestToken !== this.historyRequestToken) return;
      this.applyHistoryEntriesToTranscript();
    }, 80);
  };

  /** cancel 成功后自动回退判断。

   条件：
   1. 输入框为空（用户取消后没有输入新内容）
   2. 最后一个 selectable user message 之后的消息只有合成/系统类，
      无实质 assistant 输出
   满足时自动调用 rewind_and_restore RPC 截断对话+恢复文件，
   并将被回退的内容填充输入框。
   */
  readonly tryAutoRestoreAfterCancel = async (): Promise<void> => {
    // 条件 1：输入框为空
    if (this._getInputValueRef && this._getInputValueRef().trim() !== "") {
      this.lastVisibleUserRequest = null;
      return;
    }

    const visibleRequest = this.lastVisibleUserRequest;
    if (!visibleRequest || visibleRequest.sessionId !== this.sessionId) return;
    const clearVisibleRequest = () => {
      if (this.lastVisibleUserRequest === visibleRequest) {
        this.lastVisibleUserRequest = null;
      }
    };

    const entries = this.getSnapshot().entries;
    if (!entries || entries.length === 0) {
      clearVisibleRequest();
      return;
    }

    // 找最后一个实质 user message
    const nonSyntheticTags = ["<local-command-stdout>", "<bash-stdout>", "<task-notification>"];
    let lastUserIdx = -1;
    for (let i = entries.length - 1; i >= 0; i--) {
      const e = entries[i];
      if (e.kind !== "user") continue;
      const content = typeof e.content === "string" ? e.content : "";
      if (nonSyntheticTags.some((tag) => content.includes(tag))) continue;
      lastUserIdx = i;
      break;
    }
    if (lastUserIdx < 0) {
      clearVisibleRequest();
      return;
    }
    const lastUser = entries[lastUserIdx];
    if (
      !lastUser ||
      lastUser.kind !== "user" ||
      lastUser.sessionId !== visibleRequest.sessionId ||
      lastUser.content.trim() !== visibleRequest.content.trim()
    ) {
      clearVisibleRequest();
      return;
    }

    // 判断 lastUserIdx 之后是否只有合成/系统类消息
    const hasSubstantialAfter = entries.slice(lastUserIdx + 1).some((e) => {
      if (e.kind === "user") {
        const content = typeof e.content === "string" ? e.content : "";
        return !nonSyntheticTags.some((tag) => content.includes(tag));
      }
      // assistant 含实质内容视为有实质输出
      if (e.kind === "assistant") return true;
      // 其他（info/error/system）视为合成消息
      return false;
    });

    if (hasSubstantialAfter) {
      clearVisibleRequest();
      return; // 有实质内容，不自动回退
    }

    // 获取 turn 列表以确定 lastUserIdx 对应的 turn_index
    try {
      const turnsPayload = await this.request<{
        turns?: { turn_index: number; content_preview: string; content?: string }[];
        total?: number;
      }>("history.list_turns", { session_id: this.sessionId });
      const turns = turnsPayload.turns ?? [];
      if (turns.length === 0) {
        clearVisibleRequest();
        return;
      }

      const restoreTurn = this.findAutoRestoreTurn(turns, visibleRequest);
      if (!restoreTurn) {
        clearVisibleRequest();
        return;
      }

      const rewindPayload = await this.request<{
        content?: string;
        content_preview?: string;
        remaining_records?: number;
        removed_records?: number;
        restored_files?: string[];
        deleted_files?: string[];
        restore_errors?: { file: string; error: string }[];
      }>("session.rewind_and_restore", {
        session_id: this.sessionId,
        turn_index: restoreTurn.turn_index,
      });

      this.entries = [];
      this.emitChange();
      await this.restoreHistory(this.sessionId);

      const restoreText = rewindPayload.content ?? restoreTurn.content_preview ?? "";
      this._setInputRef?.(restoreText);
      clearVisibleRequest();

      this.addItem(
        addInfo(
          this.sessionId,
          "Auto-restored to before the last turn (no substantial output after cancel). " +
            "The removed turn text has been placed in the input for you to edit.\n" +
            "Note: Rewinding does not affect files edited manually or via bash commands.",
          "i",
        ),
      );
    } catch {
      // 自动回退失败不影响 cancel 本身
      clearVisibleRequest();
    }
  };

  private findAutoRestoreTurn(
    turns: { turn_index: number; content_preview: string; content?: string; request_id?: string }[],
    visibleRequest: VisibleUserRequest,
  ): { turn_index: number; content_preview: string; content?: string; request_id?: string } | null {
    const byRequestId = turns.find((turn) => turn.request_id === visibleRequest.requestId);
    if (byRequestId) return byRequestId;

    const target = visibleRequest.content.trim();
    if (!target) return null;
    const targetPreview = target.slice(0, 80);
    const matches = turns.filter((turn) => {
      if (typeof turn.content === "string" && turn.content.trim()) {
        return turn.content.trim() === target;
      }
      return turn.content_preview.trim() === targetPreview;
    });
    return matches.length === 1 ? matches[0]! : null;
  }

  private applyHistoryEntriesToTranscript(): void {
    // AgentServer 为了让分页优先返回最新页，在 `_handle_history_get_stream` 中把整条历史做了
    // `list(reversed(raw))` 后再流式下发；CLI 按到达顺序 push 到 `historyEntries` 会得到倒序。
    // 这里按消息时间戳重排回时间升序，再做同 turn 合并，保证 UI 从最早到最新正常显示。
    const ordered = [...this.historyEntries]
      .map((entry, originalIndex) => ({ entry, originalIndex, ts: Date.parse(entry.at) }))
      .sort((a, b) => {
        const ta = Number.isNaN(a.ts) ? 0 : a.ts;
        const tb = Number.isNaN(b.ts) ? 0 : b.ts;
        if (ta !== tb) return ta - tb;
        return a.originalIndex - b.originalIndex;
      })
      .map((item) => item.entry);

    const restored = coalesceAssistantHistoryEntries(ordered);
    // 合并分散的 tool_group 条目（chat.tool_call + chat.tool_result）
    const restoredWithTools = coalesceToolGroupEntries(restored);

    // Merge: keep existing frontend entries that aren't in the restored set,
    // so that live entries accumulated during streaming aren't lost on reconnection.
    const restoredById = new Map<string, HistoryItem>();
    for (const entry of restoredWithTools) {
      restoredById.set(entry.id, entry);
    }

    // Build merged set: restored entries + frontend-only entries (not in restored).
    // For entries present in both, prefer the restored version (server-authoritative).
    const merged: HistoryItem[] = [];
    for (const entry of restoredWithTools) {
      merged.push(entry);
    }
    for (const entry of this.entries) {
      if (!restoredById.has(entry.id)) {
        merged.push(entry);
      }
    }

    // Sort merged entries by timestamp for consistent display order.
    const sorted = merged
      .map((entry, originalIndex) => ({ entry, originalIndex, ts: Date.parse(entry.at) }))
      .sort((a, b) => {
        const ta = Number.isNaN(a.ts) ? 0 : a.ts;
        const tb = Number.isNaN(b.ts) ? 0 : b.ts;
        if (ta !== tb) return ta - tb;
        return a.originalIndex - b.originalIndex;
      })
      .map((item) => item.entry);

    this.entries = [...sorted];
    this.rebuildToolExecutionState();
    this.collapseAllToolGroupsAfterRestore();
    this.emitChange();
  }

  private collapseAllToolGroupsAfterRestore(): void {
    const ids = getToolGroupIds(this.entries, Array.from(this.toolExecutions.values()));
    for (const id of ids) {
      this.collapsedToolGroupIds.add(id);
    }
  }

  private readonly clearToolExecutionState = (): void => {
    if (this.toolTimeoutTimer) {
      clearTimeout(this.toolTimeoutTimer);
      this.toolTimeoutTimer = null;
    }
    this.toolExecutions = new Map();
    this.toolExecutionOrder = [];
    this.orphanToolResults = new Map();
  };

  private scheduleToolTimeoutCheck(): void {
    if (this.toolTimeoutTimer) {
      clearTimeout(this.toolTimeoutTimer);
      this.toolTimeoutTimer = null;
    }
    let nextTimeoutMs = Number.POSITIVE_INFINITY;
    const now = Date.now();
    for (const execution of this.toolExecutions.values()) {
      if (execution.tool.status !== "running") {
        continue;
      }
      const timeoutMs = Date.parse(execution.timeoutAt);
      if (Number.isNaN(timeoutMs)) {
        continue;
      }
      nextTimeoutMs = Math.min(nextTimeoutMs, timeoutMs);
    }
    if (!Number.isFinite(nextTimeoutMs)) {
      return;
    }
    const delay = Math.max(0, nextTimeoutMs - now);
    this.toolTimeoutTimer = setTimeout(() => {
      this.toolTimeoutTimer = null;
      if (this.markTimedOutExecutions()) {
        this.emitChange();
      } else {
        this.scheduleToolTimeoutCheck();
      }
    }, delay + 10);
  }

  private markRunningToolsInterrupted(): void {
    const nowIso = new Date().toISOString();
    let changed = false;
    for (const [toolCallId, execution] of this.toolExecutions) {
      if (execution.tool.status !== "running") {
        continue;
      }
      const nextTool: ToolCallDisplay = {
        ...execution.tool,
        status: "completed",
        summary: execution.tool.summary?.trim() ? execution.tool.summary : "Interrupted",
      };
      this.toolExecutions.set(toolCallId, {
        ...execution,
        tool: nextTool,
        updatedAt: nowIso,
      });
      this.entries = upsertToolGroupDisplay(
        this.entries,
        execution.sessionId,
        execution.requestId,
        nextTool,
      );
      changed = true;
    }
    if (changed) {
      this.scheduleToolTimeoutCheck();
      this.emitChange();
    }
  }

  private markRunningToolsConnectionLost(): void {
    const nowIso = new Date().toISOString();
    let changed = false;
    for (const [toolCallId, execution] of this.toolExecutions) {
      if (execution.tool.status !== "running") {
        continue;
      }
      const nextTool: ToolCallDisplay = {
        ...execution.tool,
        status: "error",
        isError: true,
        summary: execution.tool.summary?.trim() ? execution.tool.summary : "Connection lost",
      };
      this.toolExecutions.set(toolCallId, {
        ...execution,
        tool: nextTool,
        updatedAt: nowIso,
      });
      this.entries = upsertToolGroupDisplay(
        this.entries,
        execution.sessionId,
        execution.requestId,
        nextTool,
      );
      changed = true;
    }
    if (changed) {
      this.scheduleToolTimeoutCheck();
      this.emitChange();
    }
  }

  private markTimedOutExecutions(): boolean {
    const nowIso = new Date().toISOString();
    const nowMs = Date.parse(nowIso);
    let changed = false;
    for (const [toolCallId, execution] of this.toolExecutions) {
      if (execution.tool.status !== "running") {
        continue;
      }
      const timeoutMs = Date.parse(execution.timeoutAt);
      if (Number.isNaN(timeoutMs) || timeoutMs > nowMs) {
        continue;
      }
      const nextTool: ToolCallDisplay = {
        ...execution.tool,
        status: "timeout",
        isError: false,
      };
      this.toolExecutions.set(toolCallId, {
        ...execution,
        tool: nextTool,
        updatedAt: nowIso,
        timedOutAt: nowIso,
      });
      this.entries = upsertToolGroupDisplay(
        this.entries,
        execution.sessionId,
        execution.requestId,
        nextTool,
      );
      changed = true;
    }
    this.scheduleToolTimeoutCheck();
    return changed;
  }

  private hasRipgrep(): boolean {
    if (this.ripgrepAvailable === null) {
      this.ripgrepAvailable = detectRipgrep();
    }
    return this.ripgrepAvailable;
  }

  private maybeAddRipgrepSearchTip(tool: ToolCallDisplay, sessionId: string): void {
    if (
      this.ripgrepSearchTipShown ||
      !isLocalFileSearchTool(tool.name) ||
      this.hasRipgrep()
    ) {
      return;
    }
    this.ripgrepSearchTipShown = true;
    this.entries = [
      ...this.entries,
      addInfo(
        sessionId,
        "Tips: 未检测到 ripgrep (rg)，本次文件搜索可能较慢。" +
          "建议安装 rg 以优化文件搜索效果。",
        "i",
        { view: "dim" },
      ),
    ];
    this.lastError = null;
  }

  private addToolCallPayload(
    payload: Record<string, unknown>,
    sessionId: string,
    requestId?: string,
    startedAt?: string,
  ): void {
    const tool = createToolCallDisplay(payload);
    if (!tool.callId) {
      return;
    }

    if (this.toolExecutions.has(tool.callId)) {
      return;
    }

    this.maybeAddRipgrepSearchTip(tool, sessionId);
    const started = startedAt ?? new Date().toISOString();
    const orphan = this.orphanToolResults.get(tool.callId);
    const nextTool = orphan
      ? {
          ...tool,
          status: orphan.tool.status,
          result: orphan.tool.result,
          summary: orphan.tool.summary,
          isError: orphan.tool.isError,
        }
      : tool;

    this.toolExecutions.set(tool.callId, {
      toolCallId: tool.callId,
      sessionId,
      requestId: requestId ?? orphan?.requestId,
      tool: nextTool,
      startedAt: started,
      updatedAt: orphan?.updatedAt ?? started,
      timeoutAt: computeTimeoutAt(started),
      resultArrivedAfterTimeout: false,
    });
    this.toolExecutionOrder.push(tool.callId);
    if (orphan) {
      this.orphanToolResults.delete(tool.callId);
    }
    this.entries = upsertToolGroupDisplay(
      this.entries,
      sessionId,
      requestId ?? orphan?.requestId,
      nextTool,
    );
    this.scheduleToolTimeoutCheck();
  }

  private addToolResultPayload(
    payload: Record<string, unknown>,
    sessionId: string,
    requestId?: string,
    updatedAt?: string,
  ): void {
    const baseTool = createToolCallDisplay(payload);
    const resultTool = applyToolResult(baseTool, payload);
    if (!resultTool.callId) {
      return;
    }

    const nowIso = updatedAt ?? new Date().toISOString();
    const existing = this.toolExecutions.get(resultTool.callId);
    if (!existing) {
      this.orphanToolResults.set(resultTool.callId, {
        tool: resultTool,
        requestId,
        updatedAt: nowIso,
      });
      this.entries = upsertToolGroupDisplay(this.entries, sessionId, requestId, resultTool);
      return;
    }

    const wasTimedOut = existing.tool.status === "timeout";
    const nextTool: ToolCallDisplay = {
      ...existing.tool,
      ...resultTool,
      arguments: existing.tool.arguments,
      description: existing.tool.description ?? resultTool.description,
      formattedArgs: existing.tool.formattedArgs ?? resultTool.formattedArgs,
      status: wasTimedOut && !resultTool.isError ? "timeout" : resultTool.status,
      summary: wasTimedOut
        ? resultTool.summary
          ? `${resultTool.summary} (after timeout)`
          : resultTool.isError
            ? "failed after timeout"
            : "completed after timeout"
        : resultTool.summary,
    };
    this.toolExecutions.set(resultTool.callId, {
      ...existing,
      requestId: existing.requestId ?? requestId,
      tool: nextTool,
      updatedAt: nowIso,
      resultArrivedAfterTimeout: wasTimedOut || existing.resultArrivedAfterTimeout,
    });
    this.entries = upsertToolGroupDisplay(
      this.entries,
      sessionId,
      existing.requestId ?? requestId,
      nextTool,
    );
    this.applyTodoToolResult(nextTool);
    this.scheduleToolTimeoutCheck();
  }

  private applyTodoToolResult(tool: ToolCallDisplay): void {
    if (tool.status !== "completed" || tool.isError) {
      return;
    }
    const args = tool.arguments;
    if (!args || typeof args !== "object" || Array.isArray(args)) {
      return;
    }
    const record = args as Record<string, unknown>;
    if (tool.name === "todo_create") {
      this.applyTodoCreateArgs(record);
      return;
    }
    if (tool.name === "todo_modify") {
      this.applyTodoModifyArgs(record);
    }
  }

  private applyTodoCreateArgs(args: Record<string, unknown>): void {
    const tasks = Array.isArray(args.tasks) ? args.tasks : [];
    const nowIso = new Date().toISOString();
    const nextTodos = tasks
      .filter((task): task is Record<string, unknown> => {
        return Boolean(task && typeof task === "object" && !Array.isArray(task));
      })
      .map((task, index): TodoItem | null => {
        const id = typeof task.id === "string" ? task.id : "";
        if (!id) {
          return null;
        }
        const status = this.normalizeTodoStatus(task.status) ?? "pending";
        return {
          id,
          content: typeof task.content === "string" ? task.content : id,
          activeForm: typeof task.activeForm === "string" ? task.activeForm : "",
          status: task.status === undefined && index === 0 ? "in_progress" : status,
          createdAt: nowIso,
          updatedAt: nowIso,
        };
      })
      .filter((todo): todo is TodoItem => todo !== null);
    if (nextTodos.length > 0) {
      this.todos = nextTodos;
    }
  }

  private applyTodoModifyArgs(args: Record<string, unknown>): void {
    const action = typeof args.action === "string" ? args.action : "update";
    if (action === "delete") {
      const ids = new Set(
        (Array.isArray(args.ids) ? args.ids : []).filter(
          (id): id is string => typeof id === "string",
        ),
      );
      if (ids.size > 0) {
        this.todos = this.todos.filter((todo) => !ids.has(todo.id));
      }
      return;
    }
    if (action !== "update") {
      return;
    }
    const updates = Array.isArray(args.todos) ? args.todos : [];
    const byId = new Map(this.todos.map((todo) => [todo.id, todo]));
    const nowIso = new Date().toISOString();
    for (const update of updates) {
      if (!update || typeof update !== "object" || Array.isArray(update)) {
        continue;
      }
      const item = update as Record<string, unknown>;
      const id = typeof item.id === "string" ? item.id : "";
      if (!id) {
        continue;
      }
      const status = this.normalizeTodoStatus(item.status);
      if (status === null) {
        byId.delete(id);
        continue;
      }
      const current = byId.get(id);
      if (!current) {
        continue;
      }
      byId.set(id, {
        ...current,
        content: typeof item.content === "string" ? item.content : current.content,
        activeForm: typeof item.activeForm === "string" ? item.activeForm : current.activeForm,
        status: status ?? current.status,
        updatedAt: nowIso,
      });
    }
    this.todos = [...byId.values()];
  }

  private normalizeTodoStatus(status: unknown): TodoItem["status"] | null | undefined {
    if (status === undefined) {
      return undefined;
    }
    if (
      status === "deleted" ||
      status === "delete" ||
      status === "cancelled" ||
      status === "canceled"
    ) {
      return null;
    }
    if (
      status === "in_progress" ||
      status === "completed" ||
      status === "pending" ||
      status === "error"
    ) {
      return status;
    }
    if (status === "failed") {
      return "error";
    }
    return undefined;
  }

  private setCurrentWorkspaceFromTool(path: string): void {
    const previousCwd = getCurrentCwd();
    const result = setTrustedDir(path);
    if (result !== "set") {
      this.entries = [
        ...this.entries,
        addError(this.sessionId, `Failed to switch workspace: ${path}`),
      ];
      return;
    }

    try {
      setCurrentCwd(path);
    } catch (error) {
      setCurrentCwd(previousCwd);
      const message = error instanceof Error ? error.message : String(error);
      this.entries = [
        ...this.entries,
        addError(this.sessionId, `Failed to switch workspace: ${path} (${message})`),
      ];
      return;
    }

    try {
      this.sendEventOnly("command.add_dir", {
        path,
        remember: true,
      });
    } catch (error) {
      console.warn("Failed to sync workspace directory to server:", error);
    }

    this.entries = [
      ...this.entries,
      addInfo(this.sessionId, `Workspace switched: ${path}`, "c", {
        view: "kv",
        title: "Workspace",
        items: [{ label: "path", value: path }],
      }),
    ];
  }

  private addSyntheticToolExecution(
    tool: ToolCallDisplay,
    sessionId: string,
    requestId?: string,
    at?: string,
  ): void {
    const timestamp = at ?? new Date().toISOString();
    this.toolExecutions.set(tool.callId, {
      toolCallId: tool.callId,
      sessionId,
      requestId,
      tool,
      startedAt: timestamp,
      updatedAt: timestamp,
      timeoutAt: computeTimeoutAt(timestamp),
    });
    if (!this.toolExecutionOrder.includes(tool.callId)) {
      this.toolExecutionOrder.push(tool.callId);
    }
    this.entries = upsertToolGroupDisplay(this.entries, sessionId, requestId, tool);
    this.scheduleToolTimeoutCheck();
  }

  private rebuildToolExecutionState(): void {
    const rebuilt = rebuildToolExecutionStateFromEntries(this.entries);
    this.toolExecutions = rebuilt.toolExecutions;
    this.toolExecutionOrder = rebuilt.toolExecutionOrder;
    this.orphanToolResults = new Map();
    this.scheduleToolTimeoutCheck();
  }

  private startStatusLinePoll(): void {
    this.executeStatusLineCommand();
    this.statusLineTimer = setInterval(() => {
      this.executeStatusLineCommand();
    }, 2_000);
  }

  private stopStatusLinePoll(): void {
    if (this.statusLineTimer) {
      clearInterval(this.statusLineTimer);
      this.statusLineTimer = null;
    }
  }

  // ── Auto-recap (自动回顾) 方法 ──

  /** 更新用户活动时间戳，表示用户刚刚与 TUI 交互。 */
  recordActivity(): void {
    this.lastActivityAt = Date.now();
  }

  private startAutoRecapTimer(): void {
    if (this.autoRecapTimer) {
      return; // 防止重复启动导致 timer 泄漏
    }
    this.autoRecapTimer = setInterval(() => {
      this.checkAutoRecap();
    }, AUTO_RECAP_CHECK_INTERVAL_MS);
  }

  private stopAutoRecapTimer(): void {
    if (this.autoRecapTimer) {
      clearInterval(this.autoRecapTimer);
      this.autoRecapTimer = null;
    }
  }

  /** 周期性检查是否满足自动回顾条件。 */
  private checkAutoRecap(): void {
    // 条件1：空闲时间 >= 5分钟
    if (Date.now() - this.lastActivityAt < AUTO_RECAP_IDLE_THRESHOLD_MS) {
      return;
    }
    // 条件2：本次空闲期间还没有生成过回顾
    if (this.autoRecapState !== "idle") {
      return;
    }
    // 条件3：当前没有正在执行的任务
    const snapshot = this.getSnapshot();
    if (snapshot.isProcessing || snapshot.cancellableWork) {
      return;
    }
    // 条件4：WebSocket 已连接
    if (this.connectionStatus !== "connected") {
      return;
    }
    // 条件5：对话中至少要有用户或助手的消息可回顾（排除 command_echo / info 等系统条目）
    if (!this.entries.some((e) => e.kind === "user" || e.kind === "assistant")) {
      return;
    }

    this.triggerAutoRecap();
  }

  /** 自动触发回顾，调用后端生成摘要并显示。 */
  private async triggerAutoRecap(): Promise<void> {
    this.autoRecapState = "pending";
    this.addItem(addInfo(this.sessionId, "※ Auto-recaping...", "※", { source: "auto_recap" }));

    try {
      const payload = await this.request<Record<string, unknown>>(
        "command.recap",
        { mode: this.mode },
        60_000,
      );

      const status = payload.status as string;
      if (status === "ok") {
        const summary = payload.summary as string;
        this.addItem(addInfo(this.sessionId, `※ ${summary}`, "※", { source: "auto_recap" }));
        this.autoRecapState = "generated";
      } else if (status === "no_turn") {
        // 当前会话没有可回顾的内容，设为 generated 防止反复触发
        this.autoRecapState = "generated";
      } else {
        // failed：后端生成失败（如模型调用出错），设为 generated 防止反复触发
        // 用户发言后会重置为 idle，届时再重新尝试
        this.autoRecapState = "generated";
      }
    } catch {
      // 请求失败或被取消（如 Ctrl+C、WS 断连），设为 generated 防止反复触发
      this.autoRecapState = "generated";
    }

    // 无论成功/失败/取消，更新活动时间以避免反复触发
    this.lastActivityAt = Date.now();
  }

  restartStatusLinePoll(): void {
    this.stopStatusLinePoll();
    this.statusLineText = null;
    this.emitChange();
    this.startStatusLinePoll();
  }

  private buildStatusLineJsonInput(): Record<string, unknown> {
    const snapshot = this.getSnapshot();
    const usage = this.getUsageSummary();
    const { cwd, trustedDirs } = this.resolveRequestPaths();
    return {
      session_id: snapshot.sessionId,
      session_name: snapshot.sessionTitle,
      cwd,
      mode: snapshot.mode,
      model: snapshot.modelInfo.model,
      provider: snapshot.modelInfo.provider,
      version: snapshot.modelInfo.version,
      preferred_language: snapshot.preferredLanguage,
      connection: snapshot.connectionStatus,
      theme: snapshot.themeName,
      accent_color: snapshot.accentColor,
      transcript_mode: snapshot.transcriptMode,
      transcript_fold_mode: snapshot.transcriptFoldMode,
      is_processing: snapshot.isProcessing,
      is_paused: snapshot.isPaused,
      is_interrupted: snapshot.isInterrupted,
      cancellable_work: snapshot.cancellableWork,
      streaming_state: snapshot.streamingState,
      last_error: snapshot.lastError,
      evolution_status: snapshot.evolutionStatus,
      active_subtask_count: snapshot.activeSubtasks.length,
      todo_count: snapshot.todos.length,
      trusted_dirs: trustedDirs,
      usage: {
        total_input_tokens: usage.total_input_tokens,
        total_output_tokens: usage.total_output_tokens,
        total_tokens: usage.total_tokens,
      },
      context_window: {
        context_window_size: snapshot.contextWindowLimit ?? 0,
        used_percentage: snapshot.contextUsedPercentage ?? 0,
        remaining_percentage: snapshot.contextUsedPercentage != null
          ? Math.max(0, 100 - (snapshot.contextUsedPercentage ?? 0))
          : 0,
      },
    };
  }

  private executeStatusLineCommand(): void {
    const config = loadTuiConfig();
    const sl = config.statusLine;
    if (!sl || sl.type !== "command" || !sl.command) {
      if (this.statusLineText !== null) {
        this.statusLineText = null;
        this.emitChange();
      }
      return;
    }
    const jsonInput = JSON.stringify(this.buildStatusLineJsonInput());
    const cmd = sl.command;
    try {
      runStatusLineCommand(
        cmd,
        jsonInput,
        getCurrentCwd() || process.cwd(),
        (err, stdout) => {
          if (err) return;
          const text = stdout.trim().replace(/\r\n/g, "\n");
          if (text !== this.statusLineText) {
            this.statusLineText = text || null;
            this.emitChange();
          }
        },
      );
    } catch {
      // Ignore launch errors; invalid commands should not disrupt the TUI.
    }
  }

  private emitChange(): void {
    for (const listener of this.listeners) {
      listener();
    }
  }

  private handleFrame(frame: unknown): void {
    if (!isEventFrame(frame as EventFrame | any)) {
      return;
    }
    const typedFrame = frame as EventFrame;
    if (this.frameBelongsToActiveSession(typedFrame) && this.isStreamProgressFrame(typedFrame)) {
      this.noteStreamActivity();
    }
    if (this.shouldDeferTranscriptFrame(typedFrame)) {
      this.deferredTranscriptFrames.push(typedFrame);
      return;
    }
    if (handleIncomingFrame(this.eventDelegate, typedFrame)) {
      this.emitChange();
    }
  }

  private shouldDeferTranscriptFrame(frame: EventFrame): boolean {
    if (!this.deferTranscriptFrames) {
      return false;
    }
    const payload = frame.payload;
    const effectiveEvent = typeof payload.event_type === "string" ? payload.event_type : frame.event;
    if (!DEFERRED_TRANSCRIPT_EVENTS.has(effectiveEvent)) {
      return false;
    }
    const eventSessionId = typeof payload.session_id === "string" ? payload.session_id : "";
    return !eventSessionId || eventSessionId === this.sessionId;
  }

  private scheduleHistoryFlush(): void {
    if (this.historyFlushTimer) {
      clearTimeout(this.historyFlushTimer);
    }
    this.historyFlushTimer = setTimeout(() => {
      this.historyFlushTimer = null;
      this.applyHistoryEntriesToTranscript();
    }, 50);
  }

  private safeRestoreHistory(sessionId: string): void {
    void (async () => {
      try {
        await this.restoreHistory(sessionId);
      } catch (error) {
        if (isIgnorableHistoryRestoreError(error)) {
          return;
        }
        this.lastError = error instanceof Error ? error.message : String(error);
        this.emitChange();
      }
    })();
  }

  /** connection.ack 放行一次构造期启动屏障；实际创建统一在 createBootSession 中完成。 */
  private initializeBootSession(): void {
    if (this.bootSessionHandled) return;
    if (this.connectionStatus !== "connected") return;
    this.bootSessionHandled = true;
    const start = this.bootSessionStart;
    this.bootSessionStart = null;
    start?.();
  }

  /**
   * 普通启动不传 session_id，由 AgentServer 分配并领取预热实例；`--session <id>`
   * 传入显式 ID，走 TUI 兼容的非预热恢复/登记路径。两者共享同一个启动 Promise。
   */
  private async createBootSession(): Promise<void> {
    const target = this.bootSessionId;
    const previousMode = this.mode;
    try {
      type BootSessionResult = {
        session_id?: string;
        mode?: string;
        created?: boolean;
      };
      let res: BootSessionResult;
      let retryIndex = 0;
      while (true) {
        try {
          res = await this.requestAgentServer<BootSessionResult>(
            "session.create",
            {
              ...(target ? { session_id: target } : { create_token: this.bootCreateToken }),
              ...(!target && this.bootPersistSession ? { persist_session: true } : {}),
              previous_session_id: "",
              previous_mode: previousMode,
              mode: previousMode,
            },
            undefined,
            false,
          );
          break;
        } catch (error) {
          if (
            target === null
            || !isTransientSessionInUseError(error)
            || retryIndex >= BOOT_SESSION_IN_USE_RETRY_DELAYS_MS.length
          ) {
            throw error;
          }
          const delayMs = BOOT_SESSION_IN_USE_RETRY_DELAYS_MS[retryIndex];
          retryIndex += 1;
          await new Promise<void>((resolve) => setTimeout(resolve, delayMs));
        }
      }
      const createdId = res?.session_id;
      if (typeof createdId !== "string" || !createdId) {
        throw new Error("session.create did not return a session id");
      }
      const placeholderId = this.sessionId;
      const pendingVisibleRequest = this.lastVisibleUserRequest;
      this.updateSession(createdId);
      if (target === null && placeholderId !== createdId) {
        this.entries = this.entries.map((entry) =>
          entry.sessionId === placeholderId ? { ...entry, sessionId: createdId } : entry,
        );
        if (pendingVisibleRequest?.sessionId === placeholderId) {
          this.lastVisibleUserRequest = { ...pendingVisibleRequest, sessionId: createdId };
        }
        this.emitChange();
      }
      const resolvedMode = res?.mode;
      if (typeof resolvedMode === "string" && resolvedMode) {
        // 后端可能回旧 canonical 串（历史 session 重连），走 normalizeToClientMode
        // 归一到新串；未知串丢弃，避免 ``as`` 强转把旧串写入 this.mode 让
        // isTeamMode / isPlanClientMode / formatModeForDisplay 误判。
        const normalized = normalizeToClientMode(resolvedMode);
        if (normalized) {
          this.setMode(normalized);
        }
      }
      if (target !== null) {
        this.safeRestoreHistory(createdId);
        this.safeFetchSessionTitle(createdId);
      }
    } catch (createErr) {
      const message = createErr instanceof Error ? createErr.message : String(createErr);
      this.lastError = `session.create failed: ${message}`;
      this.emitChange();
      throw createErr;
    }
  }
}
