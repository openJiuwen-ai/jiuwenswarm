import type { HistoryItem, TeamMessageEvent } from "../types.js";
import type { AccentColorName, ThemeName } from "../../ui/theme.js";
import type { PendingQuestionItem, UserAnswer } from "../event-handlers.js";
import type { FileAttachment } from "../protocol.js";
import type { ConfigItemSchema } from "./builtins/config.js";
import type { ClientMode } from "../modes.js";
import type { SessionUsageSummary } from "../../app-state.js";
import type {
  CancelAndWaitOptions,
  HandoffCheckResult,
  HandoffTarget,
} from "../supervision/protocol.js";

export type ConnectionStatus = "idle" | "connecting" | "connected" | "reconnecting" | "auth_failed" | "message_too_big";

export type PreferredLanguage = "zh" | "en";

export type StatusViewTab = "status" | "usage" | "config";

export enum CommandKind {
  BUILT_IN = "built-in",
}

export interface CommandSuggestion {
  value: string;
  description?: string;
  usage?: string;
  example?: string;
}

export interface CommandContext {
  /** 版本信息 */
  version: string;
  /**
   * options.logAsUser=false 可用于发送内部控制消息（例如 /init 生成的 orchestration prompt），
   * 避免在 CLI/TUI 历史中渲染为普通用户输入。
   */
  sendEventOnly: (method: string, params: Record<string, unknown>) => string;
  request: <T = Record<string, unknown>>(
    method: string,
    params: Record<string, unknown>,
    timeoutMs?: number,
  ) => Promise<T>;
  askQuestions: (questions: PendingQuestionItem[], source?: string) => Promise<UserAnswer[]>;
  sendMessage: (
    content: string,
    attachments?: FileAttachment[],
    mode?: ClientMode,
    options?: { logAsUser?: boolean },
    skills?: string[],
  ) => string | null;
  sessionId: string;
  preferredLanguage: PreferredLanguage;
  entries: HistoryItem[];
  /** Sidechain / team messages (not part of main conversation entries) */
  teamMessageEvents: TeamMessageEvent[];
  themeName: ThemeName;
  accentColor: AccentColorName;
  updateSession: (id: string) => void;
  addItem: (item: HistoryItem) => void;
  /** 设置 /btw 侧问题覆盖层（独立于 transcript 渲染，不受滚动影响） */
  setBtwOverlay?: (question: string, answer: string) => void;
  /** 清除 /btw 侧问题覆盖层 */
  clearBtwOverlay?: () => void;
  /** 设置 BTW 活动状态（加载中或 overlay 可见），用于 Esc 优先级判断 */
  setBtwActive?: (active: boolean) => void;
  /** 设置 /btw 正在回答的问题；null 表示加载已结束。 */
  setBtwPendingQuestion?: (question: string | null) => void;
  clearEntries: () => void;
  restoreHistory: (sessionId: string) => Promise<void>;
  exitApp: () => void;
  isProcessing: boolean;
  /** Check if interrupt was requested locally (immediate detection for long-running commands) */
  isInterruptRequested: () => boolean;
  /** Clear local interrupt flag (for long-running commands to reset after handling interrupt) */
  clearInterruptRequested: () => void;
  /** Set the currently running command name (for tracking uninterruptible commands) */
  setRunningCommand?: (name: string | null) => void;
  connectionStatus: ConnectionStatus;
  mode: ClientMode;
  setMode: (mode: ClientMode) => void;
  markPlanEntryFromSlashCommand?: () => void;
  setModel: (name: string) => void;
  setPreferredLanguage: (language: PreferredLanguage) => void;
  setThemeName: (theme: ThemeName) => void;
  setAccentColor: (color: AccentColorName) => void;
  transcriptMode: "compact" | "detailed";
  setTranscriptMode: (mode: "compact" | "detailed") => void;
  transcriptFoldMode: "none" | "tools" | "thinking" | "all";
  setTranscriptFoldMode: (mode: "none" | "tools" | "thinking" | "all") => void;
  collapsedToolGroupCount: number;
  collapseToolGroups: (scope: "last" | "all") => void;
  expandToolGroups: (scope: "last" | "all") => void;
  sessionTitle: string;
  setSessionTitle: (title: string) => void;
  // Trusted directories management (project-scoped)
  getTrustedDirs: () => string[];
  validateDirPath: (path: string) => "valid" | "not_found" | "invalid" | "no_access";
  addTrustedDir: (path: string) => "added" | "exists" | "not_found" | "invalid" | "no_access";
  setTrustedDir: (path: string) => "set" | "not_found" | "invalid" | "no_access";
  removeTrustedDir: (path: string) => boolean;
  clearTrustedDirs: () => void;
  setCurrentProjectDir: (dir: string) => void;
  getCurrentProjectDir: () => string;
  // Workspace directory (current working directory)
  getWorkspaceDir: () => string | undefined;
  setInput?: (text: string) => void;
  getUsageSummary: () => SessionUsageSummary;
  enterConfigEditor?: (
    focusKey?: string,
    configPayload?: Record<string, unknown> & { schema?: ConfigItemSchema[] },
    mode?: "edit" | "reset",
  ) => void;
  enterStatusView?: (tab?: StatusViewTab) => void;
  /**
   * Open a file in the user's external editor. The promise resolves after the
   * editor closes. While it is open the TUI is frozen (non-operable),
   * mirroring Claude Code's editFileInEditor. When the editor exits, onDone is
   * called so the caller can report completion exactly once. A false result
   * means neither the configured nor fallback editor launched.
   */
  openInEditor?: (filePath: string, onDone?: (success?: boolean) => void) => Promise<void>;
  /** Open a folder in system file explorer (Windows: explorer, macOS: open -R, Linux: xdg-open).
   * Returns true if an explorer was launched; false if no GUI explorer is
   * available (e.g. headless Linux server), so the caller can fall back to
   * a path hint instead of claiming the folder was opened. */
  openFolder?: (folderPath: string) => boolean;
  /** Enter FileViewer mode to view large content (e.g., formatted logs) */
  enterFileViewer?: (content: string, title: string, source: string) => void;
  /** Enter DiffViewer mode to browse git/turn diffs interactively */
  enterDiffViewer?: (payload: Record<string, unknown>) => void;
  restartStatusLine?: () => void;
  /** Get the current JSON data that would be piped to the statusline command */
  getStatusLineJsonInput?: () => Record<string, unknown>;
  /** Check if there are running team-related tasks that would be interrupted by mode switch */
  hasRunningTeamTasks?: () => boolean;

  // ── /switch 公共契约端口（可选；JiuwenSwarm 独立运行时注入非托管实现） ──

  /** HandoffPort 预检：校验托管标记、动作退出码和目标能力。 */
  checkHandoff?: (target: HandoffTarget) => HandoffCheckResult;
  /** HandoffPort 请求：二次校验后调用统一顶层关闭路径，输出 handoff JSON 到 stdout。 */
  requestHandoff?: (target: HandoffTarget, switchContent: string) => Promise<void>;
  /** TaskLifecyclePort：统一任务快照；/switch 用于判断是否需要询问中断。 */
  hasServerTask?: () => boolean;
  /** TaskLifecyclePort：等待型取消；只供 /switch 等生命周期动作使用。 */
  cancelAndWaitForIdle?: (options?: CancelAndWaitOptions) => Promise<void>;
}

export interface SlashCommand {
  name: string;
  altNames?: string[];
  description: string;
  usage?: string;
  example?: string;
  /**
   * Inline hint shown after the cursor when the user has typed this command
   * (or sub-command) with no further arguments.  For commands that accept
   * key=value fields, this should list the available keys with brief labels,
   * e.g.  "name=任务名 cron_expr=\"时间\" description=\"让Agent做什么\""
   */
  argGuide?: string;
  /** 在/help中隐藏，但仍可执行 */
  hidden?: boolean;
  /** 仅在后端开启技能自演进时显示在 help/补全中；直接输入仍可执行。 */
  requiresSkillEvolution?: boolean;
  isSafeConcurrent?: boolean;
  kind: CommandKind;
  action: (ctx: CommandContext, args: string) => void | Promise<void>;
  completion?: (ctx: CommandContext, partial: string) => string[] | Promise<string[]>;
  completionSuffix?: string;
  takesArgs?: boolean;
  subCommands?: SlashCommand[];
}

export type SlashCommandListProvider = () => readonly SlashCommand[];
