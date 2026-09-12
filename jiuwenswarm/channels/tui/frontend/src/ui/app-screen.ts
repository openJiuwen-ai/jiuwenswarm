import {
  CombinedAutocompleteProvider,
  Editor,
  SelectList,
  type SelectItem,
  type SelectListTruncatePrimaryContext,
  type AutocompleteItem,
  type AutocompleteProvider,
  type Component,
  type Focusable,
  type SlashCommand as TuiSlashCommand,
  TUI,
  matchesKey,
  isKeyRelease,
  isKeyRepeat,
  decodeKittyPrintable,
  truncateToWidth,
  visibleWidth,
  wrapTextWithAnsi,
} from "@mariozechner/pi-tui";
import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import type { CliPiAppState } from "../app-state.js";
import { openFileInEditor as openInExternalEditor, openFolderInExplorer } from "../core/utils/editor.js";
import {
  extractAttachmentsFromText,
  extractFilePathsFromPaste,
  findAttachmentTokenAtCursor,
  formatAttachmentMention,
  isImageAttachment,
  isPurePathPaste,
  isSupportedAttachment,
  syncComposerImageTokens,
} from "../core/attachments.js";
import {
  CommandService,
  parseSlashCommand,
  type InstalledSkillEntry,
} from "../core/commands/CommandService.js";
import type { SlashCommand } from "../core/commands/types.js";
import { addCommandEcho, addError, addInfo } from "../core/commands/helpers.js";
import { copyToClipboard } from "../core/commands/clipboard.js";
import { CheckboxList, CheckboxGroup as CheckboxGroupType } from "./components/checkbox-list.js";
import type { FileAttachment } from "../core/protocol.js";
import {
  type ModelMeta,
  type ModelListPayload,
  isReservedMultimodalModelKey,
} from "../core/commands/builtins/model.js";
import {
  sanitizeSessionList,
  type SessionListPayload,
  type SessionMeta,
} from "../core/commands/builtins/resume.js";
import type { ConfigItemSchema } from "../core/commands/builtins/config.js";
import type { McpListItem, McpListPayload } from "../core/commands/builtins/mcp.js";
import { buildModeAutocompleteItems } from "../core/commands/builtins/mode.js";
import { MemoryViewController, type MemoryViewTab } from "./memory-view.js";
import { PIPELINE_VALUES, PIPELINE_OPTIONS, INTERVAL_VALUES, INTERVAL_OPTIONS, FLAG_OPTIONS } from "../core/commands/builtins/auto-harness.js";
import { formatModeForDisplay, isTeamMode, normalizeToClientMode } from "../core/modes.js";
import {
  countWaitingForHuman,
  sessionTurnLabelNumber,
  canOpenSessionHistory,
  isSessionNode,
  findWorkflowAgent,
  formatTokenCount,
  formatWorkflowBudgetDetail,
  formatWorkflowBudgetInline,
  formatWorkflowRunBudgetDetail,
  formatWorkflowRunBudgetInline,
  formatWorkflowAgentKindLabel,
  formatWorkflowTimingText,
  groupWorkflowAgentsByName,
  HUMAN_TURN_CACHED_ANSWER,
  HUMAN_TURN_CACHED_QUESTION,
  isHumanTurnCached,
  isWorkflowBudgetLow,
  workflowBudgetExhaustedScope,
  pendingHumanViewHint,
  pendingInputsBannerText,
  pausedWorkflowsBannerText,
  runningWorkflowsBannerText,
  sessionMembersInPhase,
  shouldShowTurnInDetailOrReply,
  workflowStatusBannerText,
  workflowStatusIcon,
  workflowBudgetUsedPercent,
  workflowPhaseSelectEntries,
  type WorkflowAgent,
  type WorkflowNodeType,
  type WorkflowPhase,
  type WorkflowRun,
  type WorkflowStatus,
} from "../core/workflows.js";
import {
  addTrustedDir,
  getCurrentCwd,
  getTrustedDirs,
  isTrustedDir,
  setCurrentCwd,
  setCurrentProjectDir,
} from "../core/tui-trusted-dirs-store.js";
import { consumeParseError } from "../core/tui-config-store.js";
import {
  expandPastedTextMarkers,
  formatPastedTextMarker,
  normalizePastedText,
  shouldCollapsePastedText,
  stripBracketedPasteMarkers,
} from "../core/pasted-text.js";
import { handleAppScreenKeyInput } from "./keymap.js";
import { getContextBindings, resolveAction } from "../core/keybindings/resolver.js";
import { buildAppScreenLines } from "./screen-layout.js";
import { buildTranscriptLines } from "./transcript-renderer.js";
import {
  isTeamWorking,
  orderedMemberIds,
  teamWorkingStartedAtMs,
} from "./components/team-shared.js";
import { padToWidth, prefixedLines, renderMarkdownLines, renderStyledMarkdownLines, renderWrappedText } from "./rendering/text.js";
import { chalk, editorTheme, palette, selectListTheme, setCurrentThemeName } from "./theme.js";
import type { Hunk, GitDiffData, GitDiffStats, TurnDiff } from "../core/types.js";

const END_CURSOR = "\x1b[7m \x1b[0m";
const ENABLE_MOUSE_TRACKING = "\x1b[?1000h\x1b[?1006h";
const DISABLE_MOUSE_TRACKING = "\x1b[?1000l\x1b[?1006l";
const TRANSCRIPT_WHEEL_SCROLL_LINES = 3;
// 不可中断的命令列表（ESC 按下时显示提示）
const UNINTERRUPTIBLE_COMMANDS = ["compact"];
const SWARM_WORKFLOW_LOG_PREVIEW_ROWS = 8;
const SWARM_WORKFLOW_AGENT_PREVIEW_LIMIT = 8;
const SWARM_WORKFLOW_AGENT_TEXT_PREVIEW_ROWS = 6;
const PERMISSION_TOOL_RE = /工具\s+`([^`]+)`\s+需要授权/;
const CONFIRM_TOOL_RE = /(?:Tool|工具)\s*:\s*`([^`]+)`/i;

/**
 * Terminal mouse reporting takes ownership of drag events, which prevents the
 * terminal's native text selection and copy behaviour. Scope it to UI states
 * that actually need mouse events (pending questions / interactive overlays)
 * AND to scrollable transcripts: a transcript taller than the viewport needs
 * mouse tracking so the wheel can page history, which costs native selection
 * only while content overflows. Short transcripts stay selectable.
 */
export function shouldCaptureTerminalMouse(
  pendingQuestionActive: boolean,
  interactiveOverlayActive: boolean,
  transcriptMayScroll: boolean,
): boolean {
  return pendingQuestionActive || interactiveOverlayActive || transcriptMayScroll;
}
const CONFIRM_ACTION_RE = /\*\*(?:Agent wants to|Tool `[^`]+` requires your approval)([^*]*)\*\*/i;
const PLAN_REJECT_INPUT_RE = /(\s+\[ .+ \])$/;
const PERMISSION_RISK_RE = /安全风险评估：\**\s*([^\s*]+)?\s*\**([^*\n]+?风险)\**/m;
const PERMISSION_QUOTE_RE = /^>\s*(.+)$/gm;
const PERMISSION_JSON_BLOCK_RE = /```json\s*([\s\S]*?)\s*```/i;
const SGR_MOUSE_RE = /^\x1b\[<(\d+);(\d+);(\d+)([mM])$/;

interface QuestionOptionRowHit {
  row: number;
  value: string;
}

function stripAnsi(text: string): string {
  return text.replace(/\x1b\[[0-9;]*m/g, "");
}

function parseSgrMouseRelease(
  data: string,
): { button: number; col: number; row: number } | null {
  const match = data.match(SGR_MOUSE_RE);
  if (!match || match[4] !== "m") {
    return null;
  }
  const button = Number.parseInt(match[1], 10) & 3;
  const col = Number.parseInt(match[2], 10);
  const row = Number.parseInt(match[3], 10);
  if (!Number.isFinite(col) || !Number.isFinite(row)) {
    return null;
  }
  return { button, col, row };
}

function wrapText(text: string, maxWidth: number): string[] {
  if (maxWidth < 1) return [text];
  const words = text.split(/\s+/);
  const lines: string[] = [];
  let current = "";
  for (const word of words) {
    if (!word) continue;
    const test = current ? `${current} ${word}` : word;
    if (test.length > maxWidth && current) {
      lines.push(current);
      current = word;
    } else {
      current = test;
    }
  }
  if (current) lines.push(current);
  return lines.length > 0 ? lines : [""];
}
const RUNNING_TIMER_RESET_GRACE_MS = 15_000;

function getSgrMouseWheelOffset(data: string, currentOffset: number): number | null {
  if (!data.startsWith("\x1b[<") || !data.endsWith("M")) return null;
  const [buttonCode] = data.slice(3, -1).split(";");

  if (buttonCode === "64") return currentOffset + TRANSCRIPT_WHEEL_SCROLL_LINES;
  if (buttonCode === "65") return Math.max(0, currentOffset - TRANSCRIPT_WHEEL_SCROLL_LINES);
  return null;
}

type PermissionSummary = {
  tool?: string;
  risk?: string;
  reason?: string;
  command?: string;
  description?: string;
};

type ResumeSessionListState = {
  list: SelectList;
  sessions: SessionMeta[];
  total: number;
  searchQuery: string;
  showAllProjects: boolean;
  branchFilterEnabled: boolean;
  currentBranch: string;
  /** 非空时进入只读预览态（Space 触发，对齐 Claude Code preview）：展示选中会话的信息卡 */
  preview: SessionMeta | null;
  /** 预览时的最新对话摘要列表 */
  previewMessages: PreviewMessage[];
  /** 预览消息是否仍在请求中（用于区分"加载中"与"已加载但为空"） */
  previewLoading: boolean;
  /** 预览消息滚动偏移（行数），正数表示向上滚动查看更早内容 */
  previewScrollOffset: number;
  /** 非空时进入重命名态（Ctrl+R 触发，对齐 Claude Code Ctrl+R）：编辑选中会话标题 */
  rename: { sessionId: string; value: string } | null;
};

/** 预览消息（完整对话内容，对齐 Claude Code session preview） */
type PreviewMessage = {
  role: string;
  content: string;
  event_type: string;
};

type ModelFormField = "model_name" | "alias" | "api_base" | "api_key" | "model_provider" | "reasoning_level";

type ModelFormState = {
  fields: Record<ModelFormField, string>;
  selectedField: number;
  original: Record<ModelFormField, string>;
};

type ModelListState = {
  phase: "list" | "input" | "delete_confirm";
  list: SelectList | null;
  models: string[];
  current: string;
  modelsMeta: ModelMeta[];
  emptyMessage?: string;
  inputMode?: "add" | "edit";
  target?: { name: string; index: number };
  form?: ModelFormState;
  inputError?: string;
};

const MODEL_VALUE_SEPARATOR = "\x00";
const MODEL_FORM_FIELDS: ModelFormField[] = ["model_name", "alias", "api_base", "api_key", "model_provider", "reasoning_level"];
const MODEL_REQUIRED_FIELDS: ModelFormField[] = ["model_name", "api_base", "api_key", "model_provider"];
const DEFAULT_MODEL_PROVIDER = "OpenAI";
const MODEL_PROVIDER_OPTIONS = ["OpenAI", "OpenRouter", "DashScope", "SiliconFlow", "AscendAffinity", "DeepSeek"];
const REASONING_LEVEL_OPTIONS = ["", "off", "low", "medium", "high"];
const MAX_MODEL_NAME_LENGTH = 100;
const MAX_ALIAS_LENGTH = 100;
const MAX_API_BASE_LENGTH = 512;
const MAX_API_KEY_LENGTH = 2048;

type ToolSelectorState = {
  list: CheckboxList;
  name: string;
  description: string;
  when_to_use: string;
  defaultPrompt: string;
  location: string;
  generate: boolean;
};

type ThemeListState = {
  list: SelectList;
  current: string;
};

type McpListState = {
  list: SelectList;
  items: McpListItem[];
};

type McpDetailState = {
  serverName: string;
  info: Record<string, unknown>;
  enabled: boolean;
  actions: SelectList;
};

type McpToolItem = {
  id: string;
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  server_name: string;
};

type McpToolsState = {
  serverName: string;
  tools: McpToolItem[];
  list: SelectList;
};

type McpToolDetailState = {
  serverName: string;
  tool: McpToolItem;
};

type ConfigEditorPhase = "search_list" | "select_value" | "input_value";

type ConfigEditorMode = "edit" | "reset";

type ConfigEditorState = {
  phase: ConfigEditorPhase;
  mode: ConfigEditorMode;  // edit=修改值, reset=重置到默认值
  schemaList: ConfigItemSchema[];
  currentValues: Record<string, string>;
  selectedKey: string | null;
  searchQuery: string;
  searchMode: boolean;      // true=搜索模式(输入字符过滤), false=浏览模式(导航操作)
  list: SelectList;
  previousPhase: ConfigEditorPhase | null;  // select_value/input_value 返回时用
  savedList: SelectList | null;             // 进入子面板前保存的扁平列表，返回时恢复
};

type StatusViewTab = "status" | "usage" | "config";

type StatusViewPhase = "tab_view" | "config_editor";

type StatusViewState = {
  phase: StatusViewPhase;
  tab: StatusViewTab;
  list: SelectList;
  statusPayload: import("../core/commands/builtins/status.js").StatusPayload | null;
  configPayload: (Record<string, unknown> & { schema?: ConfigItemSchema[] }) | null;
  searchMode: boolean;
  searchQuery: string;
};

type PendingListPreviousPhase = "chat" | "list" | "workflow" | "agent" | "session-detail" | null;

type PendingListEntry =
  | { kind: "workflow-header"; workflowId: string; workflowName: string }
  | {
      kind: "session-parent";
      sessionLabel: string;
      done: number;
      total: number;
    }
  | {
      kind: "waiting";
      workflowId: string;
      phaseId: string;
      agentId: string;
      sessionLabel: string;
      turn: number;
      isSessionChild: boolean;
      /** Single-turn session node rendered flat (no tree); distinguishes from human()/agent(). */
      isSession: boolean;
    };

type SwarmWorkflowsViewState =
  | {
      phase: "list";
      list: SelectList;
      loading: boolean;
    }
  | {
      phase: "workflow";
      workflowId: string;
      selectedPhaseId: string;
      focus: "phases" | "agents";
      phaseList: SelectList;
      agentList: SelectList;
      loadingDetail: boolean;
    }
  | {
      phase: "agent";
      workflowId: string;
      agentId: string;
      returnTo?: SessionDetailReturnTo;
    }
  | {
      phase: "pending-list";
      entries: PendingListEntry[];
      selectedIndex: number;
      previous_phase: PendingListPreviousPhase;
    }
  | {
      phase: "session-detail";
      workflowId: string;
      sessionLabel: string;
      phaseId: string;
      /** Isolates human_session vs agent_session when labels collide in a phase. */
      nodeType: WorkflowNodeType;
      returnTo: SessionDetailReturnTo;
      scrollOffset: number;
    };

type SessionDetailReturnTo =
  | { kind: "pending-list"; previous_phase: PendingListPreviousPhase }
  | { kind: "agent"; workflowId: string; agentId: string }
  | { kind: "workflow"; workflowId: string; selectedPhaseId: string; selectedAgentId?: string };

/** Prefer exact session primitive; fall back from kind for legacy payloads. */
function sessionNodeTypeOf(
  agent: Pick<WorkflowAgent, "node_type" | "kind">,
): WorkflowNodeType {
  if (agent.node_type === "agent_session" || agent.node_type === "human_session") {
    return agent.node_type;
  }
  return agent.kind === "human" ? "human_session" : "agent_session";
}

function sessionSelectValue(
  sessionLabel: string,
  phaseId: string,
  nodeType: WorkflowNodeType,
): string {
  return `session:${nodeType}:${sessionLabel}:${phaseId}`;
}

function parseSessionSelectValue(
  value: string,
): { sessionLabel: string; phaseId: string; nodeType: WorkflowNodeType } | null {
  if (!value.startsWith("session:")) return null;
  const rest = value.slice("session:".length);
  const firstSep = rest.indexOf(":");
  const lastSep = rest.lastIndexOf(":");
  if (firstSep <= 0 || lastSep <= firstSep) return null;
  const nodeType = rest.slice(0, firstSep);
  if (
    nodeType !== "agent_session" &&
    nodeType !== "human_session" &&
    nodeType !== "agent" &&
    nodeType !== "human"
  ) {
    return null;
  }
  return {
    nodeType,
    sessionLabel: rest.slice(firstSep + 1, lastSep),
    phaseId: rest.slice(lastSep + 1),
  };
}

function formatSwarmWorkflowsSummary(workflows: WorkflowRun[]): string {
  if (workflows.length === 0) return "No workflows";
  const statusOrder: WorkflowStatus[] = [
    "running",
    "pending",
    "planned",
    "completed",
    "failed",
    "stopped",
  ];
  const parts = statusOrder
    .map((status) => {
      const count = workflows.filter((workflow) => workflow.status === status).length;
      return count > 0 ? `${count} ${status}` : null;
    })
    .filter((part): part is string => Boolean(part));
  return parts.join(", ");
}

// FileViewer state for viewing large content (e.g., formatted logs)
type FileViewerState = {
  content: string;       // Full content text
  title: string;         // Title for header
  source: string;        // Source info
  scrollOffset: number;  // Current scroll position
  searchMode: boolean;   // Whether in search mode
  searchTerm: string;    // Search term
};

interface DiffFileEntry {
  filePath: string;
  linesAdded: number;
  linesRemoved: number;
  isNewFile: boolean;
  isUntracked: boolean;
  isBinary: boolean;
  isLargeFile: boolean;
  isTruncated: boolean;
  hunks: Hunk[];
  source: "working" | string;
}

export interface DiffSourceEntry {
  label: string;
  title: string;
  subtitle: string;
  stats: GitDiffStats | null;
  files: DiffFileEntry[];
  emptyMessage: string;
}

type DiffViewerState = {
  viewMode: "list" | "detail";
  selectedIndex: number;
  sourceIndex: number;
  sources: DiffSourceEntry[];
  scrollOffset: number;
};

type DiffFilePayload = {
  filePath: string;
  linesAdded: number;
  linesRemoved: number;
  isNewFile: boolean;
  isUntracked?: boolean;
  isBinary?: boolean;
  isLargeFile?: boolean;
  isTruncated?: boolean;
  hunks?: Hunk[];
};

function toDiffFileEntry(file: DiffFilePayload, source: "working" | string): DiffFileEntry {
  return {
    filePath: file.filePath,
    linesAdded: file.linesAdded,
    linesRemoved: file.linesRemoved,
    isNewFile: file.isNewFile,
    isUntracked: file.isUntracked ?? (source === "working" ? file.isNewFile : false),
    isBinary: file.isBinary ?? false,
    isLargeFile: file.isLargeFile ?? false,
    isTruncated: file.isTruncated ?? false,
    hunks: file.hunks || [],
    source,
  };
}

function sortDiffFiles(files: DiffFileEntry[]): DiffFileEntry[] {
  return files.sort((a, b) => a.filePath.localeCompare(b.filePath));
}

export function truncateDiffPathStart(pathValue: string, maxWidth: number): string {
  if (pathValue.length <= maxWidth) {
    return pathValue;
  }
  if (maxWidth <= 1) {
    return "…";
  }
  return `…${pathValue.slice(-(maxWidth - 1))}`;
}

function formatDiffStats(stats: GitDiffStats | null): string {
  const filesChanged = stats?.filesChanged ?? 0;
  const linesAdded = stats?.linesAdded ?? 0;
  const linesRemoved = stats?.linesRemoved ?? 0;
  const noun = filesChanged === 1 ? "file" : "files";
  const parts = [`${filesChanged} ${noun} changed`];
  if (linesAdded > 0) {
    parts.push(`+${linesAdded}`);
  }
  if (linesRemoved > 0) {
    parts.push(`-${linesRemoved}`);
  }
  return parts.join(" ");
}

export function buildDiffViewerSources(payload: Record<string, unknown>): DiffSourceEntry[] {
  const turns = (payload.turns || []) as TurnDiff[];
  const gitDiff = (payload.gitDiff || null) as GitDiffData | null;
  const sources: DiffSourceEntry[] = [];
  const currentStats = gitDiff?.stats ?? {
    filesChanged: 0,
    linesAdded: 0,
    linesRemoved: 0,
  };
  const currentFiles = gitDiff
    ? sortDiffFiles(Object.values(gitDiff.files).map((file) => toDiffFileEntry(file, "working")))
    : [];

  sources.push({
    label: "Current",
    title: "Uncommitted changes (git diff HEAD)",
    subtitle: formatDiffStats(currentStats),
    stats: currentStats,
    files: currentFiles,
    emptyMessage: currentStats.filesChanged > 0 && currentFiles.length === 0
      ? "Too many files to display details"
      : "No changes yet",
  });

  for (const turn of turns) {
    const files = sortDiffFiles(
      Object.values(turn.files).map((file) => toDiffFileEntry(file, `Turn ${turn.turnIndex}`)),
    );
    const prompt = turn.userPromptPreview ? `  ·  ${turn.userPromptPreview}` : "";
    sources.push({
      label: `T${turn.turnIndex}`,
      title: `Diff (Turn ${turn.turnIndex})`,
      subtitle: `${formatDiffStats(turn.stats)}${prompt}`,
      stats: turn.stats,
      files,
      emptyMessage: "No file changes in this turn",
    });
  }

  return sources;
}

const PATH_DELIMITERS = new Set([" ", "\t", '"', "'", "="]);

function findLastPathDelimiter(text: string): number {
  for (let i = text.length - 1; i >= 0; i--) {
    if (PATH_DELIMITERS.has(text[i] ?? "")) return i;
  }
  return -1;
}

function isPathTokenStart(text: string, index: number): boolean {
  return index === 0 || PATH_DELIMITERS.has(text[index - 1] ?? "");
}

function extractQuotedAtPrefix(text: string): string | null {
  let inQuotes = false;
  let quoteStart = -1;
  for (let i = 0; i < text.length; i++) {
    if (text[i] === '"') {
      inQuotes = !inQuotes;
      if (inQuotes) quoteStart = i;
    }
  }
  if (!inQuotes) return null;
  if (quoteStart > 0 && text[quoteStart - 1] === "@") {
    if (!isPathTokenStart(text, quoteStart - 1)) return null;
    return text.slice(quoteStart - 1);
  }
  if (!isPathTokenStart(text, quoteStart)) return null;
  return text.slice(quoteStart);
}

function extractAtPrefix(textBeforeCursor: string): string | null {
  const quotedPrefix = extractQuotedAtPrefix(textBeforeCursor);
  if (quotedPrefix?.startsWith('@"')) return quotedPrefix;
  const lastDelim = findLastPathDelimiter(textBeforeCursor);
  const tokenStart = lastDelim === -1 ? 0 : lastDelim + 1;
  if (textBeforeCursor[tokenStart] === "@") {
    return textBeforeCursor.slice(tokenStart);
  }
  return null;
}

interface ParsedPathPrefix {
  rawPrefix: string;
  isAtPrefix: boolean;
  isQuotedPrefix: boolean;
}

function parseAtPathPrefix(prefix: string): ParsedPathPrefix {
  if (prefix.startsWith('@"')) return { rawPrefix: prefix.slice(2), isAtPrefix: true, isQuotedPrefix: true };
  if (prefix.startsWith('"')) return { rawPrefix: prefix.slice(1), isAtPrefix: false, isQuotedPrefix: true };
  if (prefix.startsWith("@")) return { rawPrefix: prefix.slice(1), isAtPrefix: true, isQuotedPrefix: false };
  return { rawPrefix: prefix, isAtPrefix: false, isQuotedPrefix: false };
}

function expandHomePrefix(filePath: string): string {
  if (filePath.startsWith("~/")) {
    const expanded = path.join(os.homedir(), filePath.slice(2));
    return filePath.endsWith("/") && !expanded.endsWith(path.sep) ? `${expanded}/` : expanded;
  }
  if (filePath === "~") return os.homedir();
  return filePath;
}

function buildAtCompletionValue(filePath: string, opts: { isAtPrefix: boolean; isQuotedPrefix: boolean }): string {
  const needsQuotes = opts.isQuotedPrefix || filePath.includes(" ");
  const at = opts.isAtPrefix ? "@" : "";
  if (!needsQuotes) return `${at}${filePath}`;
  return `${at}"${filePath}"`;
}

function fallbackAtFileSuggestions(
  textBeforeCursor: string,
  cwd: string,
): { items: AutocompleteItem[]; prefix: string } | null {
  const atPrefix = extractAtPrefix(textBeforeCursor);
  if (!atPrefix) return null;

  const { rawPrefix, isAtPrefix, isQuotedPrefix } = parseAtPathPrefix(atPrefix);
  let expandedPrefix = rawPrefix;
  if (expandedPrefix.startsWith("~")) {
    expandedPrefix = expandHomePrefix(expandedPrefix);
  }

  const isRootPrefix =
    rawPrefix === "" ||
    rawPrefix === "./" ||
    rawPrefix === "../" ||
    rawPrefix === "~" ||
    rawPrefix === "~/" ||
    rawPrefix === "/" ||
    (isAtPrefix && rawPrefix === "");

  let searchDir: string;
  let searchPrefix: string;

  if (isRootPrefix) {
    searchDir = rawPrefix.startsWith("~") || expandedPrefix.startsWith("/") ? expandedPrefix : path.join(cwd, expandedPrefix);
    searchPrefix = "";
  } else if (rawPrefix.endsWith("/")) {
    searchDir = rawPrefix.startsWith("~") || expandedPrefix.startsWith("/") ? expandedPrefix : path.join(cwd, expandedPrefix);
    searchPrefix = "";
  } else {
    const dir = path.dirname(expandedPrefix);
    const file = path.basename(expandedPrefix);
    searchDir = rawPrefix.startsWith("~") || expandedPrefix.startsWith("/") ? dir : path.join(cwd, dir);
    searchPrefix = file;
  }

  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(searchDir, { withFileTypes: true });
  } catch {
    return null;
  }

  const suggestions: AutocompleteItem[] = [];
  for (const entry of entries) {
    if (!entry.name.toLowerCase().startsWith(searchPrefix.toLowerCase())) continue;

    let isDirectory = entry.isDirectory();
    if (!isDirectory && entry.isSymbolicLink()) {
      try {
        isDirectory = fs.statSync(path.join(searchDir, entry.name)).isDirectory();
      } catch {
        continue;
      }
    }

    const name = entry.name;
    const displayPrefix = rawPrefix;
    let relativePath: string;

    if (displayPrefix.endsWith("/")) {
      relativePath = displayPrefix + name;
    } else if (displayPrefix.includes("/") || displayPrefix.includes("\\")) {
      if (displayPrefix.startsWith("~/")) {
        const homeRelativeDir = displayPrefix.slice(2);
        const parentDir = path.dirname(homeRelativeDir);
        relativePath = `~/${parentDir === "." ? name : path.join(parentDir, name)}`;
      } else if (displayPrefix.startsWith("/")) {
        const parentDir = path.dirname(displayPrefix);
        relativePath = parentDir === "/" ? `/${name}` : `${parentDir}/${name}`;
      } else {
        relativePath = path.join(path.dirname(displayPrefix), name);
        if (displayPrefix.startsWith("./") && !relativePath.startsWith("./")) {
          relativePath = `./${relativePath}`;
        }
      }
    } else {
      relativePath = displayPrefix.startsWith("~") ? `~/${name}` : name;
    }

    relativePath = relativePath.replace(/\\/g, "/");
    const pathValue = isDirectory ? `${relativePath}/` : relativePath;
    const value = buildAtCompletionValue(pathValue, { isAtPrefix, isQuotedPrefix });
    const displayLabel = relativePath + (isDirectory ? "/" : "");
    suggestions.push({
      value,
      label: displayLabel,
      description: isDirectory ? "directory" : undefined,
    });
  }

  suggestions.sort((a, b) => {
    const aIsDir = a.value.endsWith("/");
    const bIsDir = b.value.endsWith("/");
    if (aIsDir && !bIsDir) return -1;
    if (!aIsDir && bIsDir) return 1;
    return a.label.localeCompare(b.label);
  });

  return suggestions.length > 0 ? { items: suggestions, prefix: atPrefix } : null;
}

/**
 * 放宽 pi-tui Editor 的行内 slash 补全触发。
 *
 * pi-tui 的 `isInSlashCommandContext`（打字母时是否触发 slash 补全）是 private 方法，
 * 硬编码 `isSlashMenuAllowed()(=cursorLine===0) && trimStart().startsWith("/")`（行首限制）。
 * private 无法用子类 public 覆盖（TS2415），故用运行时 monkey-patch 直接替换实例方法：
 * 仍要求光标在第一行，但触发条件放宽为"最后一个 token 以 / 开头"，使行内 `/skill` 也能触发。
 */
function patchEditorInlineSlash(editor: Editor): void {
  const target = editor as unknown as {
    state: { cursorLine: number; lines: string[]; cursorCol: number };
    isInSlashCommandContext: (textBeforeCursor: string) => boolean;
    isAtStartOfMessage: () => boolean;
  };

  // Patch 1: 打字母时的触发判断
  // 触发条件：(a) 行首以 / 开头（行首 slash 命令，含其参数区，如 /auto-harness run --pipeline），
  // 或 (b) 光标前最后一个 token 以 / 开头（行内 /skill，如 文字 /sk）。
  // 原 pi-tui 只判 (a)；若只判 (b) 会丢失命令参数区的自动触发（参数 token 不以 / 开头）。
  target.isInSlashCommandContext = function (textBeforeCursor: string): boolean {
    if (this.state.cursorLine !== 0) return false;
    if (textBeforeCursor.trimStart().startsWith("/")) return true;
    const tokens = textBeforeCursor.split(/\s+/);
    const lastToken = tokens[tokens.length - 1] ?? "";
    return lastToken.startsWith("/");
  };

  // Patch 2: 打 `/` 首字符时的触发判断
  // 放宽为：仍要求第一行，但允许 `/` 前有内容，只要 `/` 是当前 token 的开头。
  target.isAtStartOfMessage = function (): boolean {
    if (this.state.cursorLine !== 0) return false;
    const currentLine = this.state.lines[this.state.cursorLine] || "";
    const beforeCursor = currentLine.slice(0, this.state.cursorCol);
    const tokens = beforeCursor.split(/\s+/);
    const lastToken = tokens[tokens.length - 1] ?? "";
    return lastToken === "/";
  };
}

class ComposerAutocompleteProvider implements AutocompleteProvider {
  constructor(
    private readonly inner: AutocompleteProvider,
    private readonly cwd: string,
    private readonly memoryArgCompletion?: (sub: string) => Promise<{ label: string; description: string }[]>,
    // 行内 skill 补全候选源。
    // 内层 CombinedAutocompleteProvider 硬编码"行首 /"，
    // 行内 /skill 不会进它的命令补全分支，故外层自备 skill 列表在行内自行补全。
    private readonly skillCommands: readonly InstalledSkillEntry[] = [],
    /** 补全把整行变成 /<名字> 时回调，供上层区分「补全带来的提交」与「用户回车」。 */
    private readonly onSlashNameCompleted?: (name: string) => void,
  ) {}

  /** 光标前最后一个 token（以空白切分）。 */
  private static lastToken(textBeforeCursor: string): string {
    const parts = textBeforeCursor.split(/\s+/);
    return parts[parts.length - 1] ?? "";
  }

  /** 是否为"行内 skill 补全"场景：最后 token 形如 /xxx 且它不是整行第一个 token。 */
  private isInlineSkillContext(textBeforeCursor: string): boolean {
    const last = ComposerAutocompleteProvider.lastToken(textBeforeCursor);
    if (!last.startsWith("/")) return false;
    // 行首（整行只有这一个 token）交给内层库处理；行内才由外层接管。
    const trimmed = textBeforeCursor.replace(/\s+$/, "");
    return trimmed.length > last.length;
  }

  /** 用最后 token 的 / 后缀去 fuzzy 匹配已装 skill，生成候选。 */
  private inlineSkillSuggestions(textBeforeCursor: string): {
    items: AutocompleteItem[];
    prefix: string;
  } | null {
    const last = ComposerAutocompleteProvider.lastToken(textBeforeCursor);
    const term = last.slice(1).toLowerCase(); // 去掉开头 /
    const matched = this.skillCommands.filter((s) =>
      s.name.toLowerCase().includes(term),
    );
    if (matched.length === 0) return null;
    return {
      items: matched.map((s) => ({
        value: s.name,
        label: s.name,
        ...(s.description ? { description: s.description } : {}),
      })),
      prefix: last,
    };
  }

  async getSuggestions(
    lines: string[],
    cursorLine: number,
    cursorCol: number,
    options: { signal: AbortSignal; force?: boolean },
  ) {
    const currentLine = lines[cursorLine] ?? "";
    const textBeforeCursor = currentLine.slice(0, cursorCol);
    // 命令名补全触发：
    // 光标前最后一个 token 以 / 开头 → 补全命令+skill 全集（不区分行首/行内）。
    const tokens = textBeforeCursor.split(/\s+/);
    const lastToken = tokens[tokens.length - 1] ?? "";
    const isCommandNameCompletion = lastToken.startsWith("/");

    if (isCommandNameCompletion && cursorCol !== currentLine.length) {
      return null;
    }

    // 行内 skill 补全：内层库 CombinedAutocompleteProvider 硬编码"行首 /"（只认整行
    // 第一个字符是 /），行内 `/xxx` 不会进它的命令补全。外层在此接管行内场景。
    if (this.isInlineSkillContext(textBeforeCursor)) {
      const result = this.inlineSkillSuggestions(textBeforeCursor);
      if (result) {
        return result;
      }
    }

    // /memory edit|toggle + 空格：直接调用 completion 获取文件/key 列表，绕过 CombinedAutocompleteProvider
    const memArgMatch = textBeforeCursor.match(/^\/memory\s+(edit|toggle)\s+(\S*)$/);
    if (memArgMatch && this.memoryArgCompletion) {
      try {
        const sub = memArgMatch[1];
        const argPrefix = memArgMatch[2].toLowerCase();
        const items = await this.memoryArgCompletion(sub);
        const filtered = argPrefix
          ? items.filter((item) => item.label.toLowerCase().startsWith(argPrefix))
          : items;
        if (filtered.length > 0) {
          return {
            items: filtered.map((item) => ({
              label: item.label,
              description: item.description,
              value: item.label,
              usage: "",
              example: "",
            })),
            prefix: argPrefix,
          };
        }
      } catch { /* ignore */ }
      return null;
    }

    // /memory edit|toggle（无尾随空格）：抑制 inner provider 的文件补全。
    // inner provider 会以 "edit" 为 prefix 返回文件列表，applyCompletion 时会把
    // "edit" 替换成文件名 → /memory JIUWENSWARM.local.md（丢失子命令）。
    // 用户需要先输入空格，才走上面的 memArgMatch 路径正确展示文件列表。
    if (/^\/memory\s+(edit|toggle)$/.test(textBeforeCursor)) {
      return null;
    }

    const innerResult = await this.inner.getSuggestions(lines, cursorLine, cursorCol, options);
    if (innerResult) {
      // 对命令参数候选项做前缀过滤：取最后一个空格后的文本作为参数前缀
      const lastSpaceIdx = textBeforeCursor.lastIndexOf(" ");
      if (lastSpaceIdx >= 0) {
        const argPrefix = textBeforeCursor.slice(lastSpaceIdx + 1).toLowerCase();
        if (argPrefix) {
          const filtered = innerResult.items.filter((item) =>
            item.label.toLowerCase().startsWith(argPrefix)
          );
          if (filtered.length > 0 && filtered.length < innerResult.items.length) {
            return { ...innerResult, items: filtered };
          }
        }
      }
      return innerResult;
    }

    if (options.signal.aborted) return null;

    const atPrefix = extractAtPrefix(textBeforeCursor);
    if (atPrefix) {
      return fallbackAtFileSuggestions(textBeforeCursor, this.cwd);
    }

    return null;
  }

  applyCompletion(
    lines: string[],
    cursorLine: number,
    cursorCol: number,
    item: AutocompleteItem,
    prefix: string,
  ) {
    const currentLine = lines[cursorLine] ?? "";
    const textBeforeCursor = currentLine.slice(0, cursorCol);
    // 行首/行内统一：命令或 skill 名补全的 prefix 形如 /xxx（无第二个 /）。
    // 原逻辑要求整行等于 prefix（强制行首），现改为比较光标前最后一个 token，
    // 使行内 /skill 也能应用补全。
    const lastToken = textBeforeCursor.split(/\s+/).pop() ?? "";
    const isCommandNameCompletion = prefix.startsWith("/") && !prefix.slice(1).includes("/");

    if (isCommandNameCompletion && lastToken !== prefix) {
      return { lines, cursorLine, cursorCol };
    }

    // 行内 skill 补全应用：内层库 applyCompletion 的 isSlashCommand 要求 /
    // 前面为空（行首），行内会误走 path 分支。外层在此自行替换最后一个 /token。
    if (isCommandNameCompletion && this.isInlineSkillContext(textBeforeCursor)) {
      const before = textBeforeCursor.slice(0, textBeforeCursor.length - lastToken.length);
      const afterCursor = currentLine.slice(cursorCol);
      const newLine = `${before}/${item.value} ${afterCursor}`;
      const newLines = [...lines];
      newLines[cursorLine] = newLine;
      const newCol = before.length + item.value.length + 2; // "/" + name + 空格
      return { lines: newLines, cursorLine, cursorCol: newCol };
    }

    const result = this.inner.applyCompletion(lines, cursorLine, cursorCol, item, prefix);

    // @ 文件补全后自动加空格
    if (prefix.startsWith("@") && !result.lines[result.cursorLine]?.endsWith(" ")) {
      const line = result.lines[result.cursorLine] ?? "";
      const newLines = [...result.lines];
      newLines[result.cursorLine] = line + " ";
      return { lines: newLines, cursorLine: result.cursorLine, cursorCol: result.cursorCol + 1 };
    }

    if (isCommandNameCompletion && this.onSlashNameCompleted) {
      const completed = (result.lines[result.cursorLine] ?? "").trim().match(/^\/(\S+)$/);
      if (completed?.[1]) this.onSlashNameCompleted(completed[1]);
    }

    return result;
  }
}

const IMAGE_MIME_TYPES: Record<string, string> = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
};

function resolveFdBinary(): string | null {
  for (const candidate of ["fd", "fdfind"]) {
    const result = spawnSync(candidate, ["--version"], {
      stdio: "ignore",
      timeout: 400,
    });
    if (result.status === 0) {
      return candidate;
    }
  }
  return null;
}

export function isPlanApprovalRequest(
  source: string | undefined,
  planApprovalKind?: string,
): boolean {
  if (planApprovalKind) {
    return source === "confirm_interrupt" && planApprovalKind === "plan_approval";
  }
  return false;
}

function isPermissionRequest(source: string | undefined, questionText: string): boolean {
  return (
    source === "permission_interrupt" ||
    source === "confirm_interrupt" ||
    PERMISSION_TOOL_RE.test(questionText) ||
    CONFIRM_TOOL_RE.test(questionText) ||
    CONFIRM_ACTION_RE.test(questionText) ||
    /\*\*Tool `/i.test(questionText)
  );
}

export function getPendingQuestionTitle(
  source: string | undefined,
  progress: string,
  activeQuestionIndex: number,
  total: number,
  planApprovalKind?: string,
): string {
  if (isPlanApprovalRequest(source, planApprovalKind)) {
    return progress
      ? `Exit Plan and Execute: ${activeQuestionIndex + 1}/${total}`
      : "Exit Plan and Execute:";
  }
  if (source === "confirm_interrupt") {
    return progress ? `Confirm ${activeQuestionIndex + 1}/${total}` : "Confirm action";
  }
  return progress ? `Permission ${activeQuestionIndex + 1}/${total}` : "Permission";
}

function parsePermissionSummary(questionText: string): PermissionSummary {
  const tool =
    PERMISSION_TOOL_RE.exec(questionText)?.[1]?.trim() ||
    CONFIRM_TOOL_RE.exec(questionText)?.[1]?.trim();
  const confirmAction = CONFIRM_ACTION_RE.exec(questionText)?.[1]?.trim();
  const riskMatch = PERMISSION_RISK_RE.exec(questionText);
  const risk = riskMatch
    ? `${(riskMatch[1] ?? "").trim()} ${riskMatch[2].trim()}`.trim()
    : undefined;
  const reason = [...questionText.matchAll(PERMISSION_QUOTE_RE)]
    .map((match) => match[1]?.trim() ?? "")
    .find(Boolean);

  let command: string | undefined;
  let description: string | undefined;
  const jsonBlock = PERMISSION_JSON_BLOCK_RE.exec(questionText)?.[1]?.trim();
  if (jsonBlock) {
    try {
      const parsed = JSON.parse(jsonBlock) as Record<string, unknown>;
      command =
        typeof parsed.command === "string"
          ? parsed.command.trim()
          : typeof parsed.cmd === "string"
            ? parsed.cmd.trim()
            : undefined;
      description = typeof parsed.description === "string" ? parsed.description.trim() : undefined;
    } catch {
      // Ignore malformed JSON blocks in permission prompts.
    }
  }

  return {
    tool,
    risk,
    reason,
    command,
    description: description ?? confirmAction,
  };
}

function compressRiskLabel(risk: string | undefined): string | undefined {
  if (!risk) return undefined;
  const normalized = risk.replace(/\s+/g, " ").trim();
  return normalized
    .replace(/^高\s*/u, "High ")
    .replace(/^中\s*/u, "Medium ")
    .replace(/^低\s*/u, "Low ")
    .replace(/风险$/u, "risk");
}

function permissionToolKind(tool: string | undefined): "bash" | "filesystem" | "generic" {
  const normalized = tool?.trim().toLowerCase() ?? "";
  if (
    normalized === "bash" ||
    normalized === "shell" ||
    normalized === "sh" ||
    normalized === "powershell" ||
    normalized === "command" ||
    normalized === "exec" ||
    normalized === "run" ||
    normalized === "mcp_exec_command" ||
    normalized === "create_terminal"
  ) {
    return "bash";
  }
  if (
    normalized.includes("read") ||
    normalized.includes("write") ||
    normalized.includes("edit") ||
    normalized.includes("search") ||
    normalized.includes("grep") ||
    normalized.includes("glob") ||
    normalized.includes("fetch") ||
    normalized.includes("file") ||
    normalized.includes("memory")
  ) {
    return "filesystem";
  }
  return "generic";
}

function extractFilesystemTarget(summary: PermissionSummary): string | undefined {
  const raw = summary.command ?? summary.description ?? "";
  const quoted = /(["'`])([^"'`]+)\1/.exec(raw)?.[2]?.trim();
  if (quoted) return quoted;
  const pathish = /((?:\/|\.\/|\.\.\/)[^\s,)]+)/.exec(raw)?.[1]?.trim();
  if (pathish) return pathish;
  return undefined;
}

function renderPermissionBlock(
  width: number,
  summary: PermissionSummary,
  progressLabel: string,
): string[] {
  const lines: string[] = [];
  const risk = compressRiskLabel(summary.risk);
  const kind = permissionToolKind(summary.tool);
  const primaryDetail = summary.command ?? summary.description ?? summary.reason;

  lines.push(padToWidth(palette.status.warning(progressLabel), width));

  if (kind === "bash") {
    lines.push(
      padToWidth(palette.text.assistant(`${summary.tool ?? "command"} wants to run`), width),
    );
    if (summary.command) {
      lines.push(
        ...wrapPlainText(summary.command, width)
          .slice(0, 2)
          .map((line) => padToWidth(palette.text.tool(line), width)),
      );
    } else if (primaryDetail) {
      lines.push(
        ...wrapPlainText(primaryDetail, width)
          .slice(0, 2)
          .map((line) => padToWidth(palette.text.dim(line), width)),
      );
    }
  } else if (kind === "filesystem") {
    lines.push(
      padToWidth(palette.text.assistant(`${summary.tool ?? "tool"} wants to access files`), width),
    );
    const target = extractFilesystemTarget(summary);
    if (target) {
      lines.push(padToWidth(palette.text.tool(target), width));
    }
    if (primaryDetail && primaryDetail !== target) {
      lines.push(
        ...wrapPlainText(primaryDetail, width)
          .slice(0, 1)
          .map((line) => padToWidth(palette.text.dim(line), width)),
      );
    }
  } else {
    if (summary.tool) {
      lines.push(padToWidth(palette.text.assistant(`${summary.tool} requires permission`), width));
    }
    if (primaryDetail) {
      lines.push(
        ...wrapPlainText(primaryDetail, width)
          .slice(0, 2)
          .map((line) =>
            padToWidth(summary.command ? palette.text.tool(line) : palette.text.dim(line), width),
          ),
      );
    }
  }

  if (risk) {
    lines.push(
      padToWidth(
        /high/i.test(risk) ? palette.status.error(risk) : palette.status.warning(risk),
        width,
      ),
    );
  }

  return lines;
}

function normalizePermissionOptionLabel(label: string): string {
  const trimmed = label.trim();
  if (trimmed === "本次允许") return "Allow once";
  if (trimmed === "总是允许") return "Always allow";
  if (trimmed === "拒绝") return "Reject";
  return trimmed;
}

export function formatQuestionOptionLabelForDisplay(label: string, planApproval: boolean): string {
  if (!planApproval) {
    return normalizePermissionOptionLabel(label);
  }
  return isRejectOption(label) ? "Reject" : "Approve";
}

function isAlwaysAllowOption(label: string): boolean {
  const normalized = label.trim();
  return normalized.includes("总是允许") || /^always allow\b/i.test(normalized);
}

function isAllowOption(label: string): boolean {
  const normalized = label.trim();
  return normalized.includes("允许") || /^allow\b/i.test(normalized);
}

function isRejectOption(label: string): boolean {
  const normalized = label.trim();
  return (
    normalized.includes("拒绝") || /^reject\b/i.test(normalized) || /^deny\b/i.test(normalized)
  );
}

export function shouldCollectPlanRejectFeedback(
  source: string | undefined,
  label: string,
  planApprovalKind?: string,
): boolean {
  return isPlanApprovalRequest(source, planApprovalKind) && isRejectOption(label);
}

export function shouldAppendPlanRejectFeedback(
  source: string | undefined,
  label: string,
  planApprovalKind?: string,
): boolean {
  return shouldCollectPlanRejectFeedback(source, label, planApprovalKind);
}

export function getPlanRejectFeedbackHint(
  feedback: string,
  showCursor = false,
  cursorIndex?: number,
): string {
  const trimmed = feedback.trim();
  if (!showCursor) {
    return `[ ${trimmed || "tell jiuwenswarm what to change"} ]`;
  }
  const cursor = Math.max(0, Math.min(cursorIndex ?? feedback.length, feedback.length));
  return trimmed
    ? `[ ${feedback.slice(0, cursor)}${END_CURSOR}${feedback.slice(cursor)} ]`
    : `[ ${END_CURSOR}tell jiuwenswarm what to change ]`;
}

export function buildPlanApprovalQuestionItems(
  options: Array<{ label: string; description?: string }>,
  feedback: string,
  showRejectCursor = false,
  cursorIndex?: number,
): SelectItem[] {
  return options
    .filter((option) => !isAlwaysAllowOption(option.label))
    .map((option) => ({
      value: option.label,
      label: formatQuestionOptionLabelForDisplay(option.label, true),
      description: isRejectOption(option.label)
        ? getPlanRejectFeedbackHint(feedback, showRejectCursor, cursorIndex)
        : undefined,
    }));
}

export function getPlanApprovalListLayout(): {
  minPrimaryColumnWidth: number;
  maxPrimaryColumnWidth: number;
} {
  return { minPrimaryColumnWidth: 10, maxPrimaryColumnWidth: 10 };
}

const planApprovalSelectListTheme = {
  ...selectListTheme,
  selectedText: (value: string) => {
    const match = PLAN_REJECT_INPUT_RE.exec(value);
    if (!match?.index) {
      return selectListTheme.selectedText(value);
    }
    return (
      selectListTheme.selectedText(value.slice(0, match.index)) +
      chalk.dim(match[1])
    );
  },
  description: (value: string) => chalk.dim(value),
};

const swarmWorkflowSelectListTheme = {
  ...selectListTheme,
  description: (value: string) => palette.text.secondary(value),
};

export function wrapPlainText(text: string, width: number): string[] {
  const maxWidth = Math.max(1, width - 1);
  const source = text.replace(/\r/g, "").split("\n");
  const lines: string[] = [];
  for (const rawLine of source) {
    const wrapped = wrapTextWithAnsi(rawLine, maxWidth);
    lines.push(...(wrapped.length > 0 ? wrapped : [""]));
  }
  return lines.length > 0 ? lines : [""];
}

export function renderWrappedQuestionOptions(
  items: SelectItem[],
  selectedIndex: number,
  maxVisible: number,
  width: number,
): { lines: string[]; selectedEndIndex: number } {
  const safeWidth = Math.max(1, width);
  if (items.length === 0) {
    return {
      lines: [padToWidth(selectListTheme.noMatch("  No matching commands"), safeWidth)],
      selectedEndIndex: 1,
    };
  }

  const visibleCount = Math.max(1, maxVisible);
  const startIndex = Math.max(
    0,
    Math.min(selectedIndex - Math.floor(visibleCount / 2), items.length - visibleCount),
  );
  const endIndex = Math.min(startIndex + visibleCount, items.length);
  const lines: string[] = [];
  let selectedEndIndex = 0;
  const primaryColumnWidth = Math.max(
    34,
    Math.min(
      42,
      items.reduce(
        (widest, item) => Math.max(widest, visibleWidth(item.label || item.value) + 2),
        0,
      ),
    ),
  );

  for (let index = startIndex; index < endIndex; index++) {
    const item = items[index];
    if (!item) continue;
    const selected = index === selectedIndex;
    const marker = selected ? "→ " : "  ";
    const continuationMarker = " ".repeat(visibleWidth(marker));
    const bodyWidth = Math.max(1, safeWidth - visibleWidth(marker));
    const label = item.label || item.value;
    const description = item.description?.replace(/[\r\n]+/g, " ").trim() ?? "";
    const labelWidth = visibleWidth(label);
    const remainingDescriptionWidth =
      safeWidth - visibleWidth(marker) - primaryColumnWidth - 2;

    if (
      description &&
      safeWidth > 40 &&
      labelWidth <= primaryColumnWidth - 2 &&
      remainingDescriptionWidth > 10 &&
      visibleWidth(description) <= remainingDescriptionWidth
    ) {
      const spacing = " ".repeat(primaryColumnWidth - labelWidth);
      const content = `${label}${spacing}${description}`;
      const styled = selected
        ? selectListTheme.selectedText(`${marker}${content}`)
        : `${marker}${label}${selectListTheme.description(`${spacing}${description}`)}`;
      lines.push(padToWidth(styled, safeWidth));
    } else {
      const labelLines = wrapTextWithAnsi(label, bodyWidth);
      for (let lineIndex = 0; lineIndex < labelLines.length; lineIndex++) {
        const prefix = lineIndex === 0 ? marker : continuationMarker;
        const content = `${prefix}${labelLines[lineIndex]}`;
        lines.push(
          padToWidth(selected ? selectListTheme.selectedText(content) : content, safeWidth),
        );
      }

      if (description) {
        const descriptionPrefix = "    ";
        const descriptionWidth = Math.max(1, safeWidth - visibleWidth(descriptionPrefix));
        for (const descriptionLine of wrapTextWithAnsi(description, descriptionWidth)) {
          lines.push(
            padToWidth(
              `${descriptionPrefix}${selectListTheme.description(descriptionLine)}`,
              safeWidth,
            ),
          );
        }
      }
    }

    if (selected) {
      selectedEndIndex = lines.length;
    }
  }

  if (startIndex > 0 || endIndex < items.length) {
    lines.push(
      padToWidth(
        selectListTheme.scrollInfo(`  (${selectedIndex + 1}/${items.length})`),
        safeWidth,
      ),
    );
  }
  return { lines, selectedEndIndex };
}

/** Skip separator/blank lines when showing a compact human-question preview in lists. */
function isDecorativeQuestionLine(line: string): boolean {
  const trimmed = line.trim();
  if (!trimmed) return true;
  if (/^[=\-_─━*#~.·•]{4,}$/.test(trimmed)) return true;
  const compact = trimmed.replace(/\s/g, "");
  return compact.length >= 4 && /^[=\-_─━*#~.+]{4,}$/.test(compact);
}

function prepareHumanQuestionPreviewLines(
  text: string,
  width: number,
  maxLines = 3,
): { lines: string[]; truncated: boolean } {
  const contentLines = text
    .replace(/\r/g, "")
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => !isDecorativeQuestionLine(line));
  const source = contentLines.length > 0 ? contentLines.join("\n") : text.trim();
  const wrapped = wrapPlainText(source, width);
  return {
    lines: wrapped.slice(0, maxLines),
    truncated: wrapped.length > maxLines,
  };
}

function workflowStatusTone(status: WorkflowStatus): (value: string) => string {
  switch (status) {
    case "planned":
    case "pending":
    case "paused":
      return palette.status.warning;
    case "running":
      return palette.status.info;
    case "completed":
      return palette.status.success;
    case "failed":
      return palette.status.error;
    case "stopped":
      return palette.text.dim;
    case "waiting_for_human":
      return palette.text.humanInput;
    default:
      return palette.text.secondary;
  }
}

function formatWorkflowStatus(status: WorkflowStatus): string {
  return workflowStatusTone(status)(`${workflowStatusIcon(status)} ${status}`);
}

function formatSwarmWorkflowListStatus(status: WorkflowStatus): string {
  const icon = status === "running" ? "●" : workflowStatusIcon(status);
  return workflowStatusTone(status)(`${icon} ${status}`);
}

function formatWorkflowStatusWord(status: WorkflowStatus): string {
  return status === "waiting_for_human" ? "waiting" : status;
}

function formatWorkflowDuration(durationMs?: number | null): string | null {
  if (typeof durationMs !== "number" || !Number.isFinite(durationMs) || durationMs < 0) {
    return null;
  }
  if (durationMs < 1000) {
    return `${Math.round(durationMs)}ms`;
  }
  const totalSeconds = Math.floor(durationMs / 1000);
  if (totalSeconds >= 60) {
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes}m ${seconds}s`;
  }
  return `${(durationMs / 1000).toFixed(1)}s`;
}

function formatRelativeTime(timestamp: number | undefined): string {
  if (!timestamp) return "-";
  const diff = Date.now() / 1000 - timestamp;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`;
  return new Date(timestamp * 1000).toLocaleDateString();
}

function getDisplayLabel(s: SessionMeta): string {
  return s.title?.trim() || s.session_id;
}

const RESUME_SESSION_ID_HINT_SUFFIX_LENGTH = 4;

function getResumeSessionIdHint(sessionId: string, occurrence: number): string {
  return `#${occurrence}:${sessionId.slice(-RESUME_SESSION_ID_HINT_SUFFIX_LENGTH)}`;
}

function truncateResumeSessionPrimary({
  text,
  maxWidth,
}: SelectListTruncatePrimaryContext, idHint?: string): string {
  if (!idHint) {
    return truncateToWidth(text, maxWidth, "");
  }

  const suffix = `  ${idHint}`;
  const suffixWidth = visibleWidth(suffix);
  if (maxWidth <= suffixWidth) {
    return truncateToWidth(idHint, maxWidth, "");
  }
  return `${truncateToWidth(text, maxWidth - suffixWidth, "")}${suffix}`;
}

function sessionToSelectItem(s: SessionMeta, showProject = false): SelectItem {
  const title = s.title?.trim();
  const parts: string[] = [];
  if (s.active_in_window) parts.push("in another window");
  if (title) parts.push(s.session_id);
  if (showProject && s.project_dir?.trim()) parts.push(s.project_dir.trim());
  parts.push(formatRelativeTime(s.last_message_at));
  const msgCount = s.message_count ?? 0;
  if (msgCount > 0) parts.push(`${msgCount} msgs`);
  return {
    value: s.session_id,
    label: getDisplayLabel(s),
    description: parts.join(" · "),
  };
}

type ResumeItemOptions = {
  query: string;
  showProject: boolean;
  branchFilter: boolean;
  currentBranch: string;
};

function computeResumeItems(sessions: SessionMeta[], opts: ResumeItemOptions): SelectItem[] {
  let list = sanitizeSessionList(sessions);
  // 按 git 分支过滤（对齐 Claude Code Ctrl+B）：严格匹配当前分支。
  // 无分支记录的存量会话、以及非 git/HEAD 会话都会被过滤掉；关掉 Ctrl+B 即可看到全部。
  if (opts.branchFilter && opts.currentBranch) {
    list = list.filter((s) => (s.git_branch ?? "").trim() === opts.currentBranch);
  }
  const normalizedQuery = opts.query.toLowerCase();
  if (normalizedQuery) {
    list = list.filter((s) => {
      const label = getDisplayLabel(s).toLowerCase();
      const sid = s.session_id.toLowerCase(); // session_id 已由 sanitizeSessionList 保证非空
      const proj = (s.project_dir ?? "").toLowerCase();
      return (
        label.includes(normalizedQuery) ||
        sid.includes(normalizedQuery) ||
        (opts.showProject && proj.includes(normalizedQuery))
      );
    });
  }
  return list.map((s) => sessionToSelectItem(s, opts.showProject));
}

function formatConfigValue(schema: ConfigItemSchema, val: string): string {
  if (schema.type === "toggle") {
    return val === "true" ? "已启用" : "已禁用";
  }
  if (schema.sensitive) {
    if (!val) return "(空)";
    return "******";
  }
  return val || "(空)";
}

function filterConfigItems(
  schemas: ConfigItemSchema[],
  currentValues: Record<string, string>,
  query: string,
): SelectItem[] {
  const normalizedQuery = query.toLowerCase();

  // 有搜索词时：纯扁平过滤列表
  if (normalizedQuery) {
    return schemas
      .filter((schema) =>
        schema.key.toLowerCase().includes(normalizedQuery) ||
        schema.label.toLowerCase().includes(normalizedQuery) ||
        (schema.description ?? "").toLowerCase().includes(normalizedQuery) ||
        schema.group.toLowerCase().includes(normalizedQuery)
      )
      .map((schema) => {
        const val = currentValues[schema.key] ?? "";
        const displayVal = formatConfigValue(schema, val);
        return {
          value: schema.key,
          label: `${schema.label}: ${displayVal}`,
          description: schema.description ?? schema.label,
        };
      });
  }

  // 无搜索词时：按分组排列，分组间用分隔符
  const groups: Record<string, ConfigItemSchema[]> = {};
  for (const schema of schemas) {
    const group = schema.group || "Other";
    if (!groups[group]) groups[group] = [];
    groups[group].push(schema);
  }
  const items: SelectItem[] = [];
  for (const [groupName, groupSchemas] of Object.entries(groups)) {
    items.push({
      value: `__group_${groupName}__`,
      label: `── ${groupName} ──`,
      description: `${groupSchemas.length} 项`,
    });
    for (const schema of groupSchemas) {
      const val = currentValues[schema.key] ?? "";
      const displayVal = formatConfigValue(schema, val);
      items.push({
        value: schema.key,
        label: `  ${schema.label}: ${displayVal}`,
        description: schema.description ?? schema.label,
      });
    }
  }
  return items;
}

/** Last selected memory file path — restores cursor position within the same session. */
let lastMemorySelection: string | null = null;

export class AppScreen implements Component, Focusable {
  private readonly editor: Editor;
  private readonly unsubscribe: () => void;
  private composerAutocompleteProvider: AutocompleteProvider;
  private _focused = false;
  private activeQuestionId: string | null = null;
  private activeQuestionIndex = 0;
  private draftBeforeQuestion = "";
  private syncingComposerInput = false;
  private pendingQuestionAnswers = new Map<number, string>();
  private pendingMultiSelectAnswers = new Map<number, string[]>();
  private pendingQuestionCustomInputs = new Map<number, string>();
  private questionList: SelectList | null = null;
  private questionCheckboxList: CheckboxList | null = null;
  private questionDetailsMap: Map<string, string[]> | null = null;
  private questionPreviewMap: Map<string, string> | null = null;
  private questionOptionRows: QuestionOptionRowHit[] = [];
  private otherInputMode = false;
  private resumeSessionList: ResumeSessionListState | null = null;
  private modelList: ModelListState | null = null;
  private toolSelector: ToolSelectorState | null = null;
  private mcpList: McpListState | null = null;
  private mcpDetail: McpDetailState | null = null;
  private mcpTools: McpToolsState | null = null;
  private mcpToolDetail: McpToolDetailState | null = null;
  private themeList: ThemeListState | null = null;
  private configEditorState: ConfigEditorState | null = null;
  private statusViewState: StatusViewState | null = null;
  private mvController: MemoryViewController | null = null;
  private swarmWorkflowsViewState: SwarmWorkflowsViewState | null = null;
  private shownBudgetExhaustedWorkflowKeys = new Set<string>();
  private workflowUiSessionId = "";
  /** Currently-active swarmflow human reply input (null = not replying). */
  private replyingToHumanPrompt: {
    workflowRunId: string;
    correlationId: string;
    label: string;
    turn: number;
    isSession: boolean;
  } | null = null;
  private startupPromptList: SelectList | null = null;
  private todosCollapsed = false;
  private showTeamPanel = false;
  private selectedTeamMemberId: string | null = null;
  private viewedTeamMemberId: string | null = null;
  private transientNotice: string | null = null;
  private transientNoticeTimer: ReturnType<typeof setTimeout> | null = null;
  // ESC 双击清空输入框的待触发状态:第一次 Esc 置 true 并显示提示,
  // 3 秒内第二次 Esc 清空输入框;超时后由 transientNoticeTimer 一并复位。
  private escClearPending = false;
  private animationTimer: ReturnType<typeof setInterval> | null = null;
  private animationPhase = 0;
  private runningStartedAtMs: number | null = null;
  private runningStoppedAtMs: number | null = null;
  /** Whether the eager skill-cache fetch on first WebSocket connection has already been fired. */
  private didEagerFetchSkills = false;
  private didEagerFetchSandboxMeta = false;
  /** The exact human turn most recently submitted from a swarmflow reply editor. */
  private lastRepliedHumanPrompt: {
    workflowRunId: string;
    correlationId: string;
  } | null = null;
  private pendingSubmittedInput: string | null = null;
  private pendingSubmittedBaseline = 0;
  private pendingSubmittedSessionId: string | null = null;
  private transcriptScrollOffset = 0;
  private btwOverlayScrollOffset = 0;
  private lastBtwOverlayKey: string | null = null;
  private lastTranscriptLineCount = 0;
  private lastTranscriptLineWidth = 0;
  /** Image attachments keyed by composer `@path` tokens (e.g. cached base64 for terminal preview). */
  private composerAttachments: FileAttachment[] = [];
  private pastedTextById = new Map<number, string>();
  private pastedTextIdByContent = new Map<string, number>();
  private nextPastedTextId = 1;
  private pastedTextClearTimer: ReturnType<typeof setTimeout> | null = null;
  /** FileViewer state for viewing large content (e.g., formatted logs) */
  private fileViewerState: FileViewerState | null = null;
  /** DiffViewer state for interactive diff browsing */
  private diffViewerState: DiffViewerState | null = null;
  private mouseTrackingEnabled = false;
  /** Previous session title for terminal window title sync. */
  private previousSessionTitle: string = "";
  /** 刚由补全填入的 /<名字>，用于识别补全回车连带的那次提交。 */
  private slashNameCompletion: { name: string; at: number } | null = null;

  constructor(
    private readonly tui: TUI,
    private readonly state: CliPiAppState,
    private readonly commands: CommandService,
    private readonly exit: () => void,
  ) {
    this.editor = new Editor(tui, editorTheme, { paddingX: 1, autocompleteMaxVisible: 6 });
    patchEditorInlineSlash(this.editor);  // 方案 E：行内 slash 补全 monkey-patch
    this.composerAutocompleteProvider = this.rebuildAutocompleteProvider();
    this.editor.setAutocompleteProvider(this.composerAutocompleteProvider);
    // Whenever CommandService refreshes its installed-skills cache (on first
    // WebSocket connection and after every execute() call), rebuild the
    // CombinedAutocompleteProvider so that the /<skillName> shorthands appear
    // in the command-name dropdown.
    this.commands.onInstalledSkillsChange = (skills: readonly InstalledSkillEntry[]) => {
      this.composerAutocompleteProvider = this.rebuildAutocompleteProvider(skills);
      this.editor.setAutocompleteProvider(this.composerAutocompleteProvider);
    };
    this.editor.onChange = () => {
      if (!this.editor.getText()) {
        this.schedulePastedTextStateClear();
      } else {
        this.cancelPastedTextStateClear();
      }
      // 输入框内容变化（用户继续打字、Ctrl+C/interruptTask 清空、提交清空等）
      // 都让 ESC 双击待触发状态失效——用户已改变意图，下次 Esc 视为第一次。
      // 同时清掉“Press Esc again to clear input”提示，避免残留。
      if (this.escClearPending) {
        this.clearEscClearPending();
        this.transientNotice = null;
      }
      this.tui.requestRender();

      // pi-tui 的 Editor 只对字母数字字符自动触发 autocomplete，不处理空格。
      // 这导致 /memory edit<空格> 后不会自动展示文件列表——用户必须手按 Tab。
      // 在 slash 命令上下文中输入空格时手动触发，补全 provider 会自行决定是否展示。
      const ed = this.editor as unknown as {
        autocompleteState?: unknown;
        tryTriggerAutocomplete?: () => void;
        state?: { cursorLine: number; cursorCol: number; lines: string[] };
      };
      if (!ed.autocompleteState && ed.tryTriggerAutocomplete && ed.state) {
        const line = ed.state.lines[ed.state.cursorLine] ?? "";
        const before = line.slice(0, ed.state.cursorCol);
        if (before.trimStart().startsWith("/") && before.endsWith(" ")) {
          ed.tryTriggerAutocomplete();
        }
      }
    };
    this.editor.onSubmit = async (value) => {
      void await this.handleSubmit(value);
      void await this.commands.refreshSkills(this.state.getCommandContext());
    };
    this.unsubscribe = this.state.onChange(() => {
      this.handleStateChange();
    });
    // Ensure VSCode user settings have terminal.integrated.tabs.title
    // set to ${sequence} so OSC 0 sequences update the tab title.
    this.ensureVscodeSettings();
    // Set initial terminal window title
    this.tui.terminal.setTitle("jiuwenswarm");
    // Inject editor refs into app-state so tryAutoRestoreAfterCancel can
    // check input emptiness and populate the input field after auto-restore.
    this.state.setInputRef((text: string) => {
      this.editor.setText(text);
      if (!text) {
        this.schedulePastedTextStateClear();
      } else {
        this.cancelPastedTextStateClear();
      }
    });
    this.state.getInputValueRef(() => this.editor.getText());
    // Initialize project scope from the user's actual cwd
    setCurrentProjectDir(process.cwd());
    setCurrentCwd(process.cwd());
    // Initialize startup prompt for workspace trust
    this.initStartupPrompt();
    // 检查一次 config.json 解析错误，用 setTimeout(0) 延迟到 TUI 初始化完成
    setTimeout(() => {
      const parseError = consumeParseError();
      if (parseError) {
        this.state.setLastError("Invalid config.json. Please fix and restart.");
        this.tui.requestRender();
      }
    }, 0);
  }

  private ensureVscodeSettings(): void {
    if (process.env.TERM_PROGRAM !== "vscode") return;
    try {
      const userSettingsPath = this.getVscodeUserSettingsPath();
      if (!userSettingsPath) return;

      let settings: Record<string, unknown> = {};
      try {
        const raw = fs.readFileSync(userSettingsPath, "utf-8");
        settings = JSON.parse(raw);
      } catch {
        // File doesn't exist or is invalid — start fresh.
      }

      if (settings["terminal.integrated.tabs.title"] === "${sequence}") return;
      settings["terminal.integrated.tabs.title"] = "${sequence}";
      fs.mkdirSync(path.dirname(userSettingsPath), { recursive: true });
      fs.writeFileSync(userSettingsPath, JSON.stringify(settings, null, 2), "utf-8");
    } catch {
      // Best-effort only — silent failure.
    }
  }

  private getVscodeUserSettingsPath(): string | null {
    switch (process.platform) {
      case "win32":
        return process.env.APPDATA
          ? path.join(process.env.APPDATA, "Code", "User", "settings.json")
          : null;
      case "darwin":
        return process.env.HOME
          ? path.join(process.env.HOME, "Library", "Application Support", "Code", "User", "settings.json")
          : null;
      case "linux":
        return process.env.HOME
          ? path.join(process.env.HOME, ".config", "Code", "User", "settings.json")
          : null;
      default:
        return null;
    }
  }

  private initStartupPrompt(): void {
    const cwd = process.cwd();
    if (isTrustedDir(cwd)) {
      return;
    }
    const items: SelectItem[] = [
      {
        label: "Yes, I trust this folder",
        value: "yes",
        description: "JiuwenSwarm will be able to read, edit, and execute files here",
      },
      {
        label: "No, use default workspace",
        value: "no",
        description: "Only ~/.jiuwenswarm/agent/workspace will be accessible",
      },
    ];
    this.startupPromptList = new SelectList(items, 2, selectListTheme, {
      minPrimaryColumnWidth: 40,
      maxPrimaryColumnWidth: 60,
    });
    this.startupPromptList.onSelect = (item) => {
      if (item.value === "yes") {
        addTrustedDir(cwd);
        // Sync to server so the dir lands in permissions.file_guard.paths
        // (read/write allow, exec ask; Legacy readers may still see external_directory)
        // allow-list (persist_cli_trusted_directory), otherwise external_dir
        // checks would still intercept paths under this trusted directory.
        // Mirrors /workspace add (workspace-dir.ts).
        try {
          this.state.sendEventOnly("command.add_dir", {
            path: cwd,
            remember: true,
          });
        } catch (error) {
          console.warn("Failed to sync trusted startup directory to server:", error);
        }
      }
      this.startupPromptList = null;
      this.tui.requestRender();
    };
    this.startupPromptList.onCancel = () => {
      // Same as "No" - use default workspace
      this.startupPromptList = null;
      this.tui.requestRender();
    };
  }

  get focused(): boolean {
    return this._focused;
  }

  set focused(value: boolean) {
    this._focused = value;
    this.editor.focused = value;
  }

  private setMouseTrackingEnabled(enabled: boolean): void {
    if (this.mouseTrackingEnabled === enabled) return;
    this.mouseTrackingEnabled = enabled;
    if (enabled) {
      this.tui.terminal.write(ENABLE_MOUSE_TRACKING);
    } else {
      this.tui.terminal.write(DISABLE_MOUSE_TRACKING);
    }
  }

  dispose(): void {
    if (this.transientNoticeTimer) {
      clearTimeout(this.transientNoticeTimer);
      this.transientNoticeTimer = null;
    }
    this.clearEscClearPending();
    if (this.animationTimer) {
      clearInterval(this.animationTimer);
      this.animationTimer = null;
    }
    this.tui.terminal.write(DISABLE_MOUSE_TRACKING);
    this.unsubscribe();
  }

  /** 清除 ESC 双击清空输入框的待触发状态及其提示定时器。 */
  private clearEscClearPending(): void {
    this.escClearPending = false;
    if (this.transientNoticeTimer) {
      clearTimeout(this.transientNoticeTimer);
      this.transientNoticeTimer = null;
    }
  }

  invalidate(): void {
    this.editor.invalidate();
  }

  /** Enter FileViewer mode to view large content (e.g., formatted logs) */
  enterFileViewer(content: string, title: string, source: string): void {
    this.fileViewerState = {
      content,
      title,
      source,
      scrollOffset: 0,
      searchMode: false,
      searchTerm: "",
    };
    this.tui.requestRender();
  }

  /** Exit FileViewer mode and return to normal view */
  exitFileViewer(): void {
    this.fileViewerState = null;
    this.tui.requestRender();
  }

  /** Handle FileViewer input - scrolling and navigation */
  private handleFileViewerInput(data: string): void {
    if (!this.fileViewerState) return;

    const width = this.tui.terminal.columns || 80;
    const contentLines = this.getWrappedViewerLines(width);
    const height = this.tui.terminal.rows;
    const availableHeight = Math.max(1, height - 2); // Reserve for title + hint
    const maxScroll = Math.max(0, contentLines.length - availableHeight);

    switch (resolveAction("FileViewer", data)) {
      case "fileViewer:exit":
        this.exitFileViewer();
        return;
      case "fileViewer:lineUp":
        this.fileViewerState.scrollOffset = Math.max(0, this.fileViewerState.scrollOffset - 1);
        this.tui.requestRender();
        return;
      case "fileViewer:lineDown":
        this.fileViewerState.scrollOffset = Math.min(maxScroll, this.fileViewerState.scrollOffset + 1);
        this.tui.requestRender();
        return;
      case "fileViewer:pageUp":
        this.fileViewerState.scrollOffset = Math.max(0, this.fileViewerState.scrollOffset - availableHeight);
        this.tui.requestRender();
        return;
      case "fileViewer:pageDown":
        this.fileViewerState.scrollOffset = Math.min(maxScroll, this.fileViewerState.scrollOffset + availableHeight);
        this.tui.requestRender();
        return;
      case "fileViewer:top":
        this.fileViewerState.scrollOffset = 0;
        this.tui.requestRender();
        return;
      case "fileViewer:bottom":
        this.fileViewerState.scrollOffset = maxScroll;
        this.tui.requestRender();
        return;
      default:
        return;
    }
  }

  /** Render FileViewer mode - show content in a scrollable viewer */
  private renderFileViewer(width: number): string[] {
    if (!this.fileViewerState) return [];

    const height = Math.max(3, this.tui.terminal.rows);
    const safeWidth = Math.max(1, width);
    const lines: string[] = [];

    // Title bar (line 1)
    const titleText = `━━━ ${this.fileViewerState.title} ━━━`;
    lines.push(padToWidth(palette.border.panel(titleText), safeWidth));

    // Content area — wrap each source line to the available width so long
    // lines (e.g. a human prompt question) flow onto multiple rows instead
    // of being truncated to a single clipped row that the user must widen
    // the terminal to read.
    const contentLines = this.getWrappedViewerLines(safeWidth);
    const availableHeight = Math.max(1, height - 2);
    // Clamp scroll within range after a terminal resize changes the wrap row
    // count (e.g. narrowing wraps into more rows), so the view never lands
    // past the end of the content.
    const maxScroll = Math.max(0, contentLines.length - availableHeight);
    if (this.fileViewerState.scrollOffset > maxScroll) {
      this.fileViewerState.scrollOffset = maxScroll;
    }
    const scrollOffset = this.fileViewerState.scrollOffset;

    // Add visible content lines
    for (let i = 0; i < availableHeight; i++) {
      const lineIndex = scrollOffset + i;
      if (lineIndex < contentLines.length) {
        lines.push(padToWidth(contentLines[lineIndex] ?? "", safeWidth));
      } else {
        // Pad with empty lines
        lines.push(" ".repeat(safeWidth));
      }
    }

    // Hint bar (last line) - show scroll position
    const totalLines = contentLines.length;
    const scrollPercent = totalLines > 0 ? Math.round((scrollOffset / totalLines) * 100) : 0;
    const positionInfo = totalLines > availableHeight ? ` [${scrollOffset + 1}-${Math.min(scrollOffset + availableHeight, totalLines)}/${totalLines} (${scrollPercent}%)]` : "";
    const hintText = `↑/↓ scroll · PgUp/PgDown page · Esc/← back${positionInfo}`;
    lines.push(padToWidth(palette.text.dim(hintText), safeWidth));

    return lines;
  }

  /**
   * Wrap the FileViewer's raw content to a given width, returning the logical
   * rows the viewer should render and scroll over. Each source line is split
   * by ``wrapTextWithAnsi`` (ANSI-aware, CJK-wide-char aware) and then each
   * resulting row is truncated to the width as a safety net so a runaway row
   * can never exceed the terminal columns.
   */
  private getWrappedViewerLines(safeWidth: number): string[] {
    if (!this.fileViewerState) return [];
    const maxWidth = Math.max(1, safeWidth - 1);
    const out: string[] = [];
    for (const rawLine of this.fileViewerState.content.split("\n")) {
      const wrapped = wrapTextWithAnsi(rawLine, maxWidth);
      if (wrapped.length > 0) {
        for (const row of wrapped) {
          out.push(truncateToWidth(row, safeWidth, ""));
        }
      } else {
        out.push("");
      }
    }
    return out;
  }

  /** Enter DiffViewer mode to browse git/turn diffs interactively */
  enterDiffViewer(payload: Record<string, unknown>): void {
    const sources = buildDiffViewerSources(payload);

    this.diffViewerState = {
      viewMode: "list",
      selectedIndex: 0,
      sourceIndex: 0,
      sources,
      scrollOffset: 0,
    };
    this.tui.requestRender();
  }

  exitDiffViewer(): void {
    this.diffViewerState = null;
    this.tui.requestRender();
  }

  private _currentDiffSource(): DiffSourceEntry | null {
    if (!this.diffViewerState) return null;
    return this.diffViewerState.sources[this.diffViewerState.sourceIndex]
      ?? this.diffViewerState.sources[0]
      ?? null;
  }

  private _selectDiffSource(sourceIndex: number): void {
    if (!this.diffViewerState) return;
    const maxIndex = Math.max(0, this.diffViewerState.sources.length - 1);
    this.diffViewerState.sourceIndex = Math.max(0, Math.min(sourceIndex, maxIndex));
    this.diffViewerState.selectedIndex = 0;
    this.diffViewerState.scrollOffset = 0;
  }

  private _diffViewerHeaderLineCount(): number {
    if (!this.diffViewerState) return 0;
    return this.diffViewerState.sources.length > 1 ? 4 : 3;
  }

  private handleDiffViewerInput(data: string, height: number): void {
    if (!this.diffViewerState) return;

    if (matchesKey(data, "escape") || matchesKey(data, "ctrl+c")) {
      if (this.diffViewerState.viewMode === "detail") {
        this.diffViewerState.viewMode = "list";
        this.diffViewerState.scrollOffset = 0;
      } else {
        this.exitDiffViewer();
      }
      this.tui.requestRender();
      return;
    }

    if (this.diffViewerState.viewMode === "list") {
      const source = this._currentDiffSource();
      if (!source) return;

      if (matchesKey(data, "left") || data.toLowerCase() === "h") {
        this._selectDiffSource(this.diffViewerState.sourceIndex - 1);
        this.tui.requestRender();
        return;
      }
      if (matchesKey(data, "right") || data.toLowerCase() === "l") {
        this._selectDiffSource(this.diffViewerState.sourceIndex + 1);
        this.tui.requestRender();
        return;
      }
      // List view paginates a 5-file centered window derived from
      // selectedIndex at render time, so navigation only needs to move the
      // selection; the window follows automatically.
      if (matchesKey(data, "up") || data.toLowerCase() === "k") {
        if (this.diffViewerState.selectedIndex > 0) {
          this.diffViewerState.selectedIndex--;
          this.tui.requestRender();
        }
        return;
      }
      if (matchesKey(data, "down") || data.toLowerCase() === "j") {
        if (this.diffViewerState.selectedIndex < source.files.length - 1) {
          this.diffViewerState.selectedIndex++;
          this.tui.requestRender();
        }
        return;
      }
      if (matchesKey(data, "return")) {
        const file = source.files[this.diffViewerState.selectedIndex];
        if (file) {
          this.diffViewerState.viewMode = "detail";
          this.diffViewerState.scrollOffset = 0;
          this.tui.requestRender();
        }
        return;
      }
      if (matchesKey(data, "home") || data.toLowerCase() === "g") {
        this.diffViewerState.selectedIndex = 0;
        this.tui.requestRender();
        return;
      }
      if (matchesKey(data, "end") || data.toLowerCase() === "shift+g") {
        this.diffViewerState.selectedIndex = Math.max(0, source.files.length - 1);
        this.tui.requestRender();
        return;
      }
      return;
    }

    if (this.diffViewerState.viewMode === "detail") {
      const source = this._currentDiffSource();
      const file = source?.files[this.diffViewerState.selectedIndex];
      if (!file) return;

      if (matchesKey(data, "left") || data.toLowerCase() === "h") {
        this.diffViewerState.viewMode = "list";
        this.diffViewerState.scrollOffset = 0;
        this.tui.requestRender();
        return;
      }

      const totalLines = this._countDiffLines(file);
      const availableHeight = Math.max(1, height - this._diffViewerHeaderLineCount() - 1);

      if (matchesKey(data, "up") || data.toLowerCase() === "k") {
        this.diffViewerState.scrollOffset = Math.max(0, this.diffViewerState.scrollOffset - 1);
        this.tui.requestRender();
        return;
      }
      if (matchesKey(data, "down") || data.toLowerCase() === "j") {
        const maxScroll = Math.max(0, totalLines - availableHeight);
        this.diffViewerState.scrollOffset = Math.min(maxScroll, this.diffViewerState.scrollOffset + 1);
        this.tui.requestRender();
        return;
      }
      if (matchesKey(data, "pageUp")) {
        this.diffViewerState.scrollOffset = Math.max(0, this.diffViewerState.scrollOffset - availableHeight);
        this.tui.requestRender();
        return;
      }
      if (matchesKey(data, "pageDown")) {
        const maxScroll = Math.max(0, totalLines - availableHeight);
        this.diffViewerState.scrollOffset = Math.min(maxScroll, this.diffViewerState.scrollOffset + availableHeight);
        this.tui.requestRender();
        return;
      }
      if (matchesKey(data, "home") || data.toLowerCase() === "g") {
        this.diffViewerState.scrollOffset = 0;
        this.tui.requestRender();
        return;
      }
      if (matchesKey(data, "end") || data.toLowerCase() === "shift+g") {
        const maxScroll = Math.max(0, totalLines - availableHeight);
        this.diffViewerState.scrollOffset = maxScroll;
        this.tui.requestRender();
        return;
      }
      return;
    }
  }

  private _countDiffLines(file: DiffFileEntry): number {
    let count = 2;
    for (const hunk of file.hunks) {
      count++;
      count += hunk.lines.length;
    }
    return count;
  }

  private _toRelativePath(absPath: string): string {
    const cwd = getCurrentCwd() || process.cwd();
    const normalized = path.resolve(absPath);
    // On Windows, paths are case-insensitive — lower() for safe comparison
    const a = process.platform === "win32" ? normalized.toLowerCase() : normalized;
    const b = process.platform === "win32" ? cwd.toLowerCase() : cwd;
    if (a.startsWith(b + path.sep) || a === b) {
      const rel = path.relative(cwd, normalized);
      return rel || path.basename(absPath);
    }
    return absPath;
  }

  private _renderDiffDetailLines(file: DiffFileEntry, width: number): string[] {
    const lines: string[] = [];
    const displayPath = this._toRelativePath(file.filePath);
    const label = file.isUntracked
      ? palette.text.dim("(untracked)")
      : file.isNewFile
        ? palette.text.dim("(new)")
        : "";

    const added = palette.status.success(`+${file.linesAdded}`);
    const removed = palette.status.error(`-${file.linesRemoved}`);
    lines.push(`│   ${displayPath} ${label} ${added} ${removed}`);
    lines.push(`│   ${"─".repeat(Math.max(0, width - 4))}`);

    if (file.isBinary) {
      lines.push(palette.text.dim("│     Binary file - cannot display diff"));
      return lines;
    }

    if (file.isLargeFile) {
      lines.push(palette.text.dim("│     Large file - diff exceeds 1 MB limit"));
      return lines;
    }

    for (const hunk of file.hunks) {
      lines.push(
        palette.text.dim(
          `│     @@ -${hunk.oldStart},${hunk.oldLines} +${hunk.newStart},${hunk.newLines} @@`,
        ),
      );
      for (const line of hunk.lines) {
        const display = `│     ${line}`;
        let styled: string;
        if (line.startsWith("+")) {
          styled = palette.diff.add(display);
        } else if (line.startsWith("-")) {
          styled = palette.diff.remove(display);
        } else {
          styled = palette.diff.context(display);
        }
        lines.push(truncateToWidth(styled, width, ""));
      }
    }

    if (file.isTruncated) {
      lines.push(palette.text.dim("│     … diff truncated (exceeded 400 line limit)"));
    }
    return lines;
  }

  private renderDiffViewer(width: number): string[] {
    if (!this.diffViewerState) return [];

    const safeWidth = Math.max(1, width);
    const lines: string[] = [];
    const source = this._currentDiffSource();
    if (!source) return lines;

    // Title
    lines.push(padToWidth(palette.border.panel(`━━━ ${source.title} ━━━`), safeWidth));
    // Subtitle
    lines.push(padToWidth(palette.text.dim(`  ${source.subtitle}`), safeWidth));
    if (this.diffViewerState.sources.length > 1) {
      const selector = this.diffViewerState.sources
        .map((item, index) => {
          if (index === this.diffViewerState?.sourceIndex) {
            return palette.text.accent(`[${item.label}]`);
          }
          return palette.text.dim(item.label);
        })
        .join(palette.text.dim(" · "));
      lines.push(padToWidth(`  ${selector}`, safeWidth));
    }
    lines.push(padToWidth(palette.text.dim(`  ${"─".repeat(Math.max(0, safeWidth - 4))}`), safeWidth));

    if (this.diffViewerState.viewMode === "list") {
      // Paginate to MAX_VISIBLE files with the selected file centered,
      // mirroring Claude Code's DiffFileList. When there are more files than
      // the window, show ↑/↓ "N more files" hints above/below the window.
      const MAX_VISIBLE = 5;
      const total = source.files.length;

      if (total === 0) {
        lines.push(padToWidth(palette.text.dim(`  ${source.emptyMessage}`), safeWidth));
      } else {
        let start: number;
        let end: number;
        if (total <= MAX_VISIBLE) {
          start = 0;
          end = total;
        } else {
          start = Math.max(0, this.diffViewerState.selectedIndex - Math.floor(MAX_VISIBLE / 2));
          end = start + MAX_VISIBLE;
          if (end > total) {
            end = total;
            start = Math.max(0, end - MAX_VISIBLE);
          }
        }

        if (start > 0) {
          const more = start;
          lines.push(padToWidth(
            palette.text.dim(`  ↑ ${more} more ${more === 1 ? "file" : "files"}`),
            safeWidth,
          ));
        }

        for (let i = start; i < end; i++) {
          const file = source.files[i]!;
          const isSelected = i === this.diffViewerState.selectedIndex;
          const pointer = isSelected ? "❯ " : "  ";
          const relativePath = this._toRelativePath(file.filePath);
          const displayPath = truncateDiffPathStart(relativePath, Math.max(1, safeWidth - 16));

          // 构建右侧状态标签（对齐 Claude Code FileStats）:
          // - untracked → "untracked"
          // - binary → "Binary file"
          // - large file → "Large file modified"
          // - normal/truncated → +N -N [ (truncated)]
          let statsLabel: string;
          let statsStyled: string;
          if (file.isUntracked) {
            statsLabel = "untracked";
            statsStyled = palette.text.dim("untracked");
          } else if (file.isBinary) {
            statsLabel = "Binary file";
            statsStyled = palette.text.dim("Binary file");
          } else if (file.isLargeFile) {
            statsLabel = "Large file modified";
            statsStyled = palette.text.dim("Large file modified");
          } else {
            const statParts: string[] = [];
            const styledParts: string[] = [];
            if (file.linesAdded > 0) {
              statParts.push(`+${file.linesAdded}`);
              styledParts.push(palette.status.success(`+${file.linesAdded}`));
            }
            if (file.linesRemoved > 0) {
              statParts.push(`-${file.linesRemoved}`);
              styledParts.push(palette.status.error(`-${file.linesRemoved}`));
            }
            statsLabel = statParts.join(" ");
            statsStyled = styledParts.join(" ");
            if (file.isTruncated) {
              statsLabel = statsLabel ? `${statsLabel} (truncated)` : "(truncated)";
              statsStyled = statsStyled
                ? `${statsStyled}${palette.text.dim(" (truncated)")}`
                : palette.text.dim("(truncated)");
            }
          }

          const sourceLabel = file.isNewFile && !file.isUntracked ? "(new)" : "";
          const line = `${pointer}${displayPath}`;
          const rightLabel = [sourceLabel, statsLabel].filter(Boolean).join(" ");
          const rightStyled = [
            sourceLabel ? palette.text.dim(sourceLabel) : "",
            statsStyled,
          ].filter(Boolean).join(" ");
          const fullLine = rightLabel
            ? `${padToWidth(line, Math.max(1, safeWidth - rightLabel.length - 1))}${rightStyled}`
            : padToWidth(line, safeWidth);
          if (isSelected) {
            lines.push(palette.text.accent(fullLine));
          } else {
            lines.push(fullLine);
          }
        }

        if (end < total) {
          const more = total - end;
          lines.push(padToWidth(
            palette.text.dim(`  ↓ ${more} more ${more === 1 ? "file" : "files"}`),
            safeWidth,
          ));
        }
      }
    } else {
      const file = source.files[this.diffViewerState.selectedIndex];
      if (!file) return lines;

      const detailLines = this._renderDiffDetailLines(file, safeWidth);
      const availableHeight = Math.max(1, this.tui.terminal.rows - lines.length - 1);

      const offset = this.diffViewerState.scrollOffset;
      const maxLines = Math.min(detailLines.length, offset + availableHeight);

      for (let i = offset; i < maxLines; i++) {
        lines.push(detailLines[i] || "");
      }

      const totalLines = detailLines.length;
      const scrollPercent = totalLines > 0 ? Math.round((offset / totalLines) * 100) : 0;
      const positionInfo = totalLines > availableHeight
        ? ` [${offset + 1}-${Math.min(offset + availableHeight, totalLines)}/${totalLines} (${scrollPercent}%)]`
        : "";
      const hintText = `  ↑/↓ scroll · ← back · Esc back${positionInfo}`;
      lines.push(padToWidth(palette.text.dim(hintText), safeWidth));
      return lines;
    }

    const sourceHint = this.diffViewerState.sources.length > 1 ? "←/→ source · " : "";
    const hintText = `  ${sourceHint}↑/↓ to select · Enter to view · Esc to close`;
    lines.push(padToWidth(palette.text.dim(hintText), safeWidth));

    return lines;
  }

  /**
   * Ctrl+C / SIGINT 始终尝试向服务端发送当前 session 的中断请求。
   * 是否真的存在运行任务由服务端判断；CLI/TUI 本身不退出。
   */
  interruptTask(): void {
    this.state.cancel();
    this.editor.setText("");
    // 中断任务会清空输入框，同步复位 ESC 双击待触发状态，避免下次 Esc 被误判为“第二次”。
    if (this.escClearPending) {
      this.clearEscClearPending();
      this.transientNotice = null;
    }
    this.tui.requestRender();
  }

  handleInput(data: string): void {
    // 更新用户活动时间戳（用于 auto-recap 空闲检测）
    this.state.recordActivity();

    // FileViewer mode: handle input separately
    if (this.fileViewerState) {
      this.handleFileViewerInput(data);
      return;
    }

    // DiffViewer mode: handle input separately
    if (this.diffViewerState) {
      this.handleDiffViewerInput(data, this.tui.terminal.rows);
      return;
    }

    const snapshot = this.state.getSnapshot();
    const pendingQuestion = snapshot.pendingQuestion;
    const activeQuestion =
      pendingQuestion?.questions[this.activeQuestionIndex] ?? pendingQuestion?.questions[0];
    const permissionRequest = activeQuestion
      ? isPermissionRequest(pendingQuestion?.source, activeQuestion.question)
      : false;

    const hasOverlay =
      this.startupPromptList !== null ||
      this.resumeSessionList !== null ||
      this.statusViewState !== null ||
      this.mcpDetail !== null ||
      this.mcpToolDetail !== null ||
      this.mcpList !== null ||
      this.mcpTools !== null ||
      this.modelList !== null ||
      this.toolSelector !== null ||
      this.themeList !== null ||
      this.swarmWorkflowsViewState !== null ||
      this.configEditorState !== null ||
      this.diffViewerState !== null ||
      this.mvController !== null && this.mvController.isOpen;

    if (
      !pendingQuestion &&
      !hasOverlay &&
      snapshot.btwOverlay &&
      this.handleBtwOverlayScrollInput(data)
    ) {
      return;
    }

    if (!pendingQuestion && !hasOverlay && this.handleTranscriptScrollInput(data)) {
      return;
    }

    const isCancelWorkKey = !hasOverlay && resolveAction("Global", data) === "app:cancelWork";

    // BTW 浮层优先消费 Esc（不干扰主会话）
    // 只要 btw 浮层弹窗存在（可见、加载中、正在输出），按一次 Esc 只关闭/终止 btw 旁路
    if (!pendingQuestion && isCancelWorkKey && snapshot.btwActive) {
      // 关闭 BTW overlay（如果可见）
      if (snapshot.btwOverlay) {
        this.state.clearBtwOverlay();
        this.btwOverlayScrollOffset = 0;
      }
      // 取消正在进行的 BTW WS 请求（加载中状态），不影响主会话
      this.state.requestLocalInterrupt();
      this.state.setBtwActive(false);
      this.tui.requestRender();
      return;
    }

    if (!pendingQuestion && snapshot.cancellableWork && isCancelWorkKey) {
      if (isTeamMode(snapshot.mode)) {
        this.state.pause();
      } else {
        this.state.cancel();
      }
      return;
    }

    // 检查不可中断命令列表（ESC 显示提示）
    if (!pendingQuestion && snapshot.runningCommand && UNINTERRUPTIBLE_COMMANDS.includes(snapshot.runningCommand) && isCancelWorkKey) {
      this.transientNotice = `${snapshot.runningCommand} 命令执行中，无法中断`;
      if (this.transientNoticeTimer) {
        clearTimeout(this.transientNoticeTimer);
      }
      this.transientNoticeTimer = setTimeout(() => {
        this.transientNotice = null;
        this.transientNoticeTimer = null;
        this.tui.requestRender();
      }, 3500);
      this.tui.requestRender();
      return;
    }

    // ESC 关闭 /btw overlay（独立于 transcript 的覆盖层）
    if (!pendingQuestion && !hasOverlay && !snapshot.cancellableWork && isCancelWorkKey) {
      if (snapshot.btwOverlay) {
        this.state.clearBtwOverlay();
        this.btwOverlayScrollOffset = 0;
        this.tui.requestRender();
        return;
      }
    }

    // ESC 关闭 help 视图（只读，无输入栏）
    if (!pendingQuestion && !hasOverlay && !snapshot.cancellableWork && isCancelWorkKey) {
      if (this.state.dismissHelp()) {
        this.tui.requestRender();
        return;
      }
    }

    // ESC 双击清空输入框（仅空闲主屏 + 输入框非空）
    // 第一次 Esc：输入框非空时显示“再按一次清空”提示；3 秒内第二次 Esc：清空输入框。
    // 输入框为空时不响应。优先级低于 btw/cancellableWork/不可中断命令/help 等守卫。
    if (!pendingQuestion && !hasOverlay && !snapshot.cancellableWork && isCancelWorkKey) {
      const hasInput = this.editor.getText().length > 0;
      // 兜底：若 pending 仍为 true 但输入框已空（被其他途径清空且未触发 onChange 复位），
      // 先复位 pending，避免下次 Esc 被误判为“第二次”而清空用户新输入的文字。
      if (this.escClearPending && !hasInput) {
        this.clearEscClearPending();
        this.transientNotice = null;
      }
      if (this.escClearPending) {
        // 第二次 Esc（在窗口内）：清空输入框并清除提示
        this.clearEscClearPending();
        if (hasInput) {
          this.editor.setText("");
        }
        this.transientNotice = null;
        this.tui.requestRender();
        return;
      }
      if (hasInput) {
        // 第一次 Esc（输入框非空）：进入待清空状态并显示提示
        // 例外：/memory edit 和 /memory toggle 跳过双击清空，让 editor 处理 Esc 取消 completion 提示
        const trimmed = this.editor.getText().trimStart();
        const skipEscClear = trimmed === "/memory edit" || trimmed === "/memory toggle";
        if (skipEscClear) {
          // 不拦截，继续后续流程让 editor.handleInput 处理
        } else {
          this.escClearPending = true;
          this.transientNotice = "Press Esc again to clear input";
          if (this.transientNoticeTimer) {
            clearTimeout(this.transientNoticeTimer);
          }
          this.transientNoticeTimer = setTimeout(() => {
            this.escClearPending = false;
            this.transientNoticeTimer = null;
            this.transientNotice = null;
            this.tui.requestRender();
          }, 3000);
          this.tui.requestRender();
          return;
        }
      }
      // 输入框为空：不响应，继续后续流程
    }

    if (this.startupPromptList !== null && matchesKey(data, "ctrl+c")) {
      this.startupPromptList.handleInput(data);
      this.tui.requestRender();
      return;
    }

    // 审批框（pendingQuestion）激活时：Ctrl+C 或 Esc 按一次即中断任务并关闭审批框，
    // 不再需要双击，也不再退出 TUI 进程；Ctrl+D 不再响应。
    if (
      pendingQuestion &&
      (matchesKey(data, "ctrl+c") || matchesKey(data, "escape"))
    ) {
      this.transientNotice = null;
      this.interruptTask();
      this.tui.requestRender();
      return;
    }

    if (
      pendingQuestion &&
      (pendingQuestion.source === "harmonyos_dev_install_confirm" ||
        pendingQuestion.source === "harmonyos_dev_update_confirm" ||
        pendingQuestion.source === "harmonyos_knowledge_mcp_confirm") &&
      (isKeyRepeat(data) || isKeyRelease(data)) &&
      matchesKey(data, "enter")
    ) {
      // The Enter used to submit /harmonyos-dev-init can still emit Kitty
      // repeat/release events after the confirmation becomes visible. Ignore
      // those residual events, but keep the first option selected so a new
      // Enter press explicitly confirms installation/configuration.
      this.tui.requestRender();
      return;
    }

    if (pendingQuestion && this.handlePendingQuestionInput(data, snapshot)) {
      this.tui.requestRender();
      return;
    }

    // Swarmflow human reply input — lower priority than PendingQuestion, higher than chat.
    if (this.replyingToHumanPrompt && this.handleSwarmflowHumanReplyInput(data)) {
      this.tui.requestRender();
      return;
    }

    // Global shortcuts only apply on the main screen — defer while an overlay
    // or the team panel is active so context-specific bindings (e.g. ResumeList)
    // can use the same physical keys.
    // Exception: app:toggleTranscript (ctrl+o) is allowed even when overlays are
    // open, so users can fold/unfold the transcript behind the overlay.
    const globalAction = resolveAction("Global", data);
    const skipGlobalMainScreenKeys = hasOverlay || this.showTeamPanel;
    const allowGlobalAction =
      !skipGlobalMainScreenKeys ||
      (globalAction === "app:toggleTranscript" && !(this.mvController?.isOpen ?? false)) ||
      (this.showTeamPanel && !hasOverlay && globalAction === "app:toggleTeamPanel");
    let handled = false;
    if (allowGlobalAction) {
      const isToggleTranscript = globalAction === "app:toggleTranscript";
      const isToggleTeamPanel = globalAction === "app:toggleTeamPanel";
      handled = handleAppScreenKeyInput(data, {
        interruptTask: () => this.interruptTask(),
        exitApp: () => this.exit(),
        toggleTodos: () => {
          this.todosCollapsed = !this.todosCollapsed;
          this.tui.requestRender();
        },
        toggleTeamPanel: () => {
          this.showTeamPanel = !this.showTeamPanel;
          if (!this.showTeamPanel) {
            this.viewedTeamMemberId = null;
          }
          this.tui.requestRender();
        },
        toggleTranscript: () => {
          const snapshot = this.state.getSnapshot();
          this.state.setTranscriptMode(
            snapshot.transcriptMode === "detailed" ? "compact" : "detailed",
          );
        },
        viewHumanInputs: () => {
          if ((snapshot.pendingHumanPrompts?.size ?? 0) > 0) {
            void this.enterSwarmWorkflowsPendingList();
            return;
          }
          this.showTransientNotice("No human inputs waiting.");
        },
        redraw: () => {
          this.tui.invalidate();
          this.tui.requestRender(true);
          this.transientNotice = "Screen redrawn";
          if (this.transientNoticeTimer) {
            clearTimeout(this.transientNoticeTimer);
          }
          this.transientNoticeTimer = setTimeout(() => {
            this.transientNotice = null;
            this.transientNoticeTimer = null;
            this.tui.requestRender();
          }, 1200);
          this.tui.requestRender();
        },
        clearInput: () => {
          this.editor.setText("");
          // 清空输入框时同步复位 ESC 双击待触发状态，避免下次 Esc 被误判为“第二次”。
          if (this.escClearPending) {
            this.clearEscClearPending();
            this.transientNotice = null;
          }
          this.tui.requestRender();
        },
        isIdle: () => {
          return !snapshot.isProcessing && !snapshot.pendingQuestion && !snapshot.cancellableWork;
        },
        hasServerTask: () => this.state.hasServerTask(),
        requestLocalInterrupt: () => {
          return this.state.requestLocalInterrupt();
        },
        showExitHint: (key: string) => {
          if (this.transientNoticeTimer) {
            clearTimeout(this.transientNoticeTimer);
          }
          const keyLabel = key === "ctrl+c" ? "Ctrl+C" : "Ctrl+D";
          this.transientNotice = `Press ${keyLabel} again to exit`;
          this.transientNoticeTimer = setTimeout(() => {
            this.transientNotice = null;
            this.transientNoticeTimer = null;
            this.tui.requestRender();
          }, 3000);
          this.tui.requestRender();
        },
      });
      // For ctrl+o with overlays active, the delegate toggleTranscript is called
      // but handleAppScreenKeyInput only returns true when skipGlobalMainScreenKeys
      // is false (it matches Global context via keymap.ts). So we trust the
      // delegate callback has run and mark it handled ourselves.
      if (handled || isToggleTranscript || isToggleTeamPanel) {
        return;
      }
    }

    if (permissionRequest && activeQuestion) {
      const confirmAction = resolveAction("Confirmation", data);
      if (confirmAction === "confirm:yes") {
        const allow = activeQuestion.options.find((option) => isAllowOption(option.label));
        if (allow) {
          this.handleQuestionSelection(allow.label);
          return;
        }
      }
      if (confirmAction === "confirm:no") {
        const reject = activeQuestion.options.find((option) => isRejectOption(option.label));
        if (reject) {
          this.handleQuestionSelection(reject.label);
          return;
        }
      }
    }

    // Startup prompt for workspace trust (shown first)
    if (this.startupPromptList !== null) {
      this.startupPromptList.handleInput(data);
      this.tui.requestRender();
      return;
    }

    if (!snapshot.pendingQuestion && this.resumeSessionList !== null) {
      // 重命名态：Enter 保存，Esc 取消，其余按键编辑标题（允许空格）
      if (this.resumeSessionList.rename !== null) {
        if (matchesKey(data, "return")) {
          void this.submitResumeRename();
        } else if (matchesKey(data, "escape")) {
          this.resumeSessionList = { ...this.resumeSessionList, rename: null };
          this.tui.requestRender();
        } else if (matchesKey(data, "backspace")) {
          const r = this.resumeSessionList.rename;
          this.resumeSessionList = {
            ...this.resumeSessionList,
            rename: { ...r, value: r.value.slice(0, -1) },
          };
          this.tui.requestRender();
        } else {
          const ch = this.getPrintableChar(data);
          if (ch !== undefined) {
            const r = this.resumeSessionList.rename;
            this.resumeSessionList = {
              ...this.resumeSessionList,
              rename: { ...r, value: r.value + ch },
            };
            this.tui.requestRender();
          }
        }
        return;
      }
      // 只读预览态：Enter 恢复该会话，Space/Esc 返回列表，其余按键忽略
      if (this.resumeSessionList.preview !== null) {
        if (matchesKey(data, "return")) {
          this.setMouseTrackingEnabled(false);
          void this.handleResumeSessionSelection(this.resumeSessionList.preview.session_id);
        } else if (matchesKey(data, "space") || matchesKey(data, "escape")) {
          this.resumeSessionList = { ...this.resumeSessionList, preview: null, previewMessages: [], previewLoading: false };
          this.setMouseTrackingEnabled(false);
          this.tui.requestRender();
        } else {
          // Handle scroll in preview
          const wheelOffset = getSgrMouseWheelOffset(data, this.resumeSessionList.previewScrollOffset);
          if (wheelOffset !== null) {
            this.resumeSessionList = { ...this.resumeSessionList, previewScrollOffset: Math.max(0, wheelOffset) };
            this.tui.requestRender();
            return;
          }
          const pageSize = Math.max(1, Math.floor(this.tui.terminal.rows * 0.8));
          const scrollAction = resolveAction("Scroll", data);
          if (scrollAction === "scroll:pageUp" || matchesKey(data, "pageUp")) {
            this.resumeSessionList = { ...this.resumeSessionList, previewScrollOffset: this.resumeSessionList.previewScrollOffset + pageSize };
            this.tui.requestRender();
            return;
          }
          if (scrollAction === "scroll:pageDown" || matchesKey(data, "pageDown")) {
            this.resumeSessionList = { ...this.resumeSessionList, previewScrollOffset: Math.max(0, this.resumeSessionList.previewScrollOffset - pageSize) };
            this.tui.requestRender();
            return;
          }
        }
        return;
      }
      const resumeAction = resolveAction("ResumeList", data);
      // These shortcuts must win over search-text input: the default for
      // resume:preview is Space, which would otherwise be typed into the query
      // (intentionally sacrificing the ability to type a space in search).
      if (resumeAction === "resume:preview") {
        void this.openResumeSessionPreview();
        return;
      }
      if (resumeAction === "resume:rename") {
        this.openResumeRename();
        return;
      }
      const printableChar = this.getPrintableChar(data);
      if (printableChar !== undefined) {
        const newQuery = this.resumeSessionList.searchQuery + printableChar;
        this.updateResumeSearchQuery(newQuery);
      } else if (matchesKey(data, "backspace")) {
        const newQuery = this.resumeSessionList.searchQuery.slice(0, -1);
        this.updateResumeSearchQuery(newQuery);
      } else if (resumeAction === "resume:toggleAllProjects") {
        void this.toggleResumeAllProjects();
      } else if (resumeAction === "resume:toggleBranchFilter") {
        this.toggleResumeBranchFilter();
      } else if (resumeAction === "resume:close") {
        if (this.resumeSessionList.searchQuery) {
          this.updateResumeSearchQuery("");
        } else {
          this.resumeSessionList = null;
          this.tui.requestRender();
        }
      } else {
        this.resumeSessionList.list.handleInput(data);
      }
      this.tui.requestRender();
      return;
    }

    if (!snapshot.pendingQuestion && this.statusViewState !== null) {
      if (this.statusViewState.phase === "config_editor") {
        this.handleConfigEditorInput(data);
        this.tui.requestRender();
        return;
      }
      // Search mode for config tab
      if (this.statusViewState.tab === "config" && this.statusViewState.searchMode) {
        if (matchesKey(data, "escape")) {
          if (this.statusViewState.searchQuery.length > 0) {
            this.statusViewState.searchQuery = "";
          } else {
            this.statusViewState.searchMode = false;
          }
          this.rebuildStatusViewTabList();
          this.tui.requestRender();
          return;
        }
        if (matchesKey(data, "return") || matchesKey(data, "down")) {
          this.statusViewState.searchMode = false;
          this.tui.requestRender();
          return;
        }
        if (resolveAction("StatusView", data) === "status:prevTab") {
          this.switchStatusViewTab(-1);
          return;
        }
        if (resolveAction("StatusView", data) === "status:nextTab") {
          this.switchStatusViewTab(1);
          return;
        }
        if (matchesKey(data, "backspace") || matchesKey(data, "delete")) {
          if (this.statusViewState.searchQuery.length > 0) {
            this.statusViewState.searchQuery = this.statusViewState.searchQuery.slice(0, -1);
            this.rebuildStatusViewTabList();
          } else {
            this.statusViewState.searchMode = false;
          }
          this.tui.requestRender();
          return;
        }
        // Printable character → append to search query
        // Use getPrintableChar() to handle Kitty CSI-u sequences (VSCode terminal)
        // and UTF-8 multi-byte chars (IME input), not just raw data.length === 1
        const searchPrintableChar = this.getPrintableChar(data);
        if (searchPrintableChar !== undefined && !matchesKey(data, "up") && !matchesKey(data, "tab")) {
          this.statusViewState.searchQuery += searchPrintableChar;
          this.rebuildStatusViewTabList();
          this.tui.requestRender();
          return;
        }
        return;
      }
      // Normal mode: Esc to close, / or printable char on config tab enters search
      switch (resolveAction("StatusView", data)) {
        case "status:close":
          this.closeStatusView();
          return;
        case "status:prevTab":
          this.switchStatusViewTab(-1);
          return;
        case "status:nextTab":
          this.switchStatusViewTab(1);
          return;
        default:
          break;
      }
      // On config tab, / enters search mode; printable chars also enter search mode
      // Use getPrintableChar() for Kitty CSI-u (VSCode) + UTF-8 multi-byte (IME) support
      if (this.statusViewState.tab === "config") {
        if (data === "/") {
          this.statusViewState.searchMode = true;
          this.statusViewState.searchQuery = "";
          this.tui.requestRender();
          return;
        }
        const initialPrintableChar = this.getPrintableChar(data);
        if (initialPrintableChar !== undefined && !matchesKey(data, "up") && !matchesKey(data, "down") && !matchesKey(data, "return") && !matchesKey(data, "tab") && !matchesKey(data, "backspace") && !matchesKey(data, "delete") && !matchesKey(data, "escape")) {
          this.statusViewState.searchMode = true;
          this.statusViewState.searchQuery = initialPrintableChar;
          this.rebuildStatusViewTabList();
          this.tui.requestRender();
          return;
        }
      }
      this.statusViewState.list.handleInput(data);
      this.tui.requestRender();
      return;
    }

    // MemoryView 键盘：←/→ 切页签，Esc 关闭，Ctrl+O 切全路径，其余交给 SelectList
    if (!snapshot.pendingQuestion && this.mvController?.isOpen) {
      this.mvController.handleInput(data);
      return;
    }

    if (!snapshot.pendingQuestion && this.configEditorState !== null) {
      this.handleConfigEditorInput(data);
      this.tui.requestRender();
      return;
    }

    if (!snapshot.pendingQuestion && this.modelList !== null) {
      this.handleModelListInput(data);
      this.tui.requestRender();
      return;
    }

    if (!snapshot.pendingQuestion && this.toolSelector !== null) {
      this.toolSelector.list.handleInput(data);
      this.tui.requestRender();
      return;
    }

    if (!snapshot.pendingQuestion && this.mcpList !== null) {
      this.mcpList.list.handleInput(data);
      this.tui.requestRender();
      return;
    }

    if (this.mcpDetail !== null) {
      if (resolveAction("Overlay", data) === "overlay:close") {
        this.mcpDetail = null;
        this.openMcpList();
        return;
      }
      if (!snapshot.pendingQuestion) {
        this.mcpDetail.actions.handleInput(data);
      }
      this.tui.requestRender();
      return;
    }

    if (this.mcpToolDetail !== null) {
      if (resolveAction("Overlay", data) === "overlay:close") {
        const serverName = this.mcpToolDetail.serverName;
        this.mcpToolDetail = null;
        void this.openMcpToolsList(serverName);
        return;
      }
      return;
    }

    if (this.mcpTools !== null) {
      if (resolveAction("Overlay", data) === "overlay:close") {
        const serverName = this.mcpTools.serverName;
        this.mcpTools = null;
        void this.handleMcpSelection(serverName);
        return;
      }
      this.mcpTools.list.handleInput(data);
      this.tui.requestRender();
      return;
    }

    if (!snapshot.pendingQuestion && this.themeList !== null) {
      this.themeList.list.handleInput(data);
      this.tui.requestRender();
      return;
    }

    if (!snapshot.pendingQuestion && this.swarmWorkflowsViewState !== null) {
      this.handleSwarmWorkflowsInput(data);
      return;
    }

    if (!snapshot.pendingQuestion && this.showTeamPanel) {
      switch (resolveAction("TeamPanel", data)) {
        case "team:back":
          this.viewedTeamMemberId = null;
          this.tui.requestRender();
          return;
        case "team:viewMember":
          this.viewedTeamMemberId = this.selectedTeamMemberId;
          this.tui.requestRender();
          return;
        case "team:prev":
          this.moveTeamPanelSelection(snapshot, -1);
          this.tui.requestRender();
          return;
        case "team:next":
          this.moveTeamPanelSelection(snapshot, 1);
          this.tui.requestRender();
          return;
        default:
          break;
      }
    }

    if (!snapshot.pendingQuestion && this.state.isHelpVisible()) {
      return;
    }

    // Detect pasted file paths (drag-and-drop) in the terminal
    // When files are dragged in, they arrive as a pasted string.
    // Windows/PowerShell may not send bracketed paste markers,
    // so we detect file paths in any multi-character input.
    // Only intercept when the paste is *pure* file paths (drag-and-drop).
    // If there's command text interleaved, treat it as a normal paste.
    if (!snapshot.pendingQuestion && data.length > 4) {
      const hasPasteStart = data.includes("\x1b[200~");
      const hasPasteEnd = data.includes("\x1b[201~");
      if (hasPasteStart !== hasPasteEnd) {
        this.editor.handleInput(data);
        return;
      }
      const pastedContent = stripBracketedPasteMarkers(data);
      const filePaths = extractFilePathsFromPaste(pastedContent);
      if (filePaths.length > 0 && isPurePathPaste(pastedContent)) {
        // 若解析出路径但无一通过附件校验（扩展名不在白名单等），须把原文交给编辑器，避免粘贴被吞掉
        if (this.handleDroppedFiles(filePaths)) {
          return;
        }
      }
      if (this.handlePastedTextCollapse(pastedContent)) {
        return;
      }
    }

    this.editor.handleInput(data);
  }

  render(width: number): string[] {
    // FileViewer mode: render file viewer instead of normal view
    if (this.fileViewerState) {
      return this.renderFileViewer(width);
    }

    // DiffViewer mode: render diff viewer instead of normal view
    if (this.diffViewerState) {
      return this.renderDiffViewer(width);
    }

    const snapshot = this.state.getSnapshot();
    const teamWorking =
      isTeamMode(snapshot.mode) &&
      isTeamWorking(snapshot.teamMemberEvents, snapshot.teamMessageEvents);
    this.editor.borderColor = snapshot.pendingQuestion
      ? palette.border.question
      : palette.border.panel;
    // When config editor is active (any phase), hide the main editor to prevent
    // IME composing text from appearing in the bottom input box instead of the config panel.
    // input_value phase: editor is rendered inside buildConfigEditorLines.
    // search_list/select_value phase: no text input needed in the main editor.
    // The resume picker has its own search input, so hide the main editor while it is
    // open to avoid a misleading second input bar (restored on Esc). Mirrors Claude Code.
    // /help is read-only transcript content — hide the composer until Esc dismisses it.
    const isConfigEditorActive = this.configEditorState !== null;
    const hideEditorForInlinePlanReject = this.isEditingInlinePlanReject(snapshot);
    const hideMainEditor =
      isConfigEditorActive ||
      hideEditorForInlinePlanReject ||
      this.resumeSessionList !== null ||
      this.state.isHelpVisible() ||
      this.modelList !== null ||
      (this.mvController?.isOpen ?? false) ||
      this.replyingToHumanPrompt !== null;
    const editorLines = hideMainEditor
      ? []
      : this.applySlashCommandHint(this.editor.render(width), width);
    const composerPreviewLines: string[] = [];
    const questionLines = [
      ...this.buildStartupPromptLines(width),
      ...this.buildStatusViewLines(width),
      ...(this.mvController?.isOpen ? this.mvController.buildLines(width) : []),
      ...(!this.statusViewState ? this.buildConfigEditorLines(width) : []),
      ...this.buildResumeSessionListLines(width),
      ...this.buildModelListLines(width),
      ...this.buildToolSelectorLines(width),
      ...this.buildMcpListLines(width),
      ...this.buildMcpDetailLines(width),
      ...this.buildMcpToolsLines(width),
      ...this.buildMcpToolDetailLines(width),
      ...this.buildThemeListLines(width),
      ...(this.swarmWorkflowsViewState ? [] : this.buildWorkflowRuntimeLines(width)),
      ...(this.swarmWorkflowsViewState ? this.buildSwarmWorkflowsLines(width) : []),
      ...this.buildSwarmflowHumanReplyInputLines(width),
      ...this.buildPendingQuestionLines(snapshot, width),
    ];
    const showFullThinking = snapshot.transcriptMode === "detailed";
    const showToolDetails = snapshot.transcriptMode === "detailed";
    const pendingInput = this.pendingSubmittedInput ?? undefined;
    const pendingInputBaseline = this.pendingSubmittedInput
      ? this.pendingSubmittedBaseline
      : undefined;
    const transcriptLineCount = buildTranscriptLines(
      snapshot,
      width,
      showFullThinking,
      showToolDetails,
      this.animationPhase,
      pendingInput,
      pendingInputBaseline,
    ).length;
    const approximateFixedHeight =
      questionLines.length + editorLines.length + composerPreviewLines.length + 2;
    const transcriptMayScroll =
      transcriptLineCount > Math.max(0, this.tui.terminal.rows - approximateFixedHeight);
    const interactiveOverlayActive =
      this.startupPromptList !== null ||
      this.resumeSessionList !== null ||
      this.statusViewState !== null ||
      this.mcpDetail !== null ||
      this.mcpList !== null ||
      this.mcpTools !== null ||
      this.modelList !== null ||
      this.toolSelector !== null ||
      this.themeList !== null ||
      this.swarmWorkflowsViewState !== null ||
      this.configEditorState !== null ||
      this.questionList !== null;
    this.setMouseTrackingEnabled(
      shouldCaptureTerminalMouse(
        snapshot.pendingQuestion !== null,
        interactiveOverlayActive,
        transcriptMayScroll,
      ),
    );
    if (
      this.transcriptScrollOffset > 0 &&
      this.lastTranscriptLineWidth === width &&
      transcriptLineCount > this.lastTranscriptLineCount
    ) {
      this.transcriptScrollOffset += transcriptLineCount - this.lastTranscriptLineCount;
    }
    this.lastTranscriptLineCount = transcriptLineCount;
    this.lastTranscriptLineWidth = width;
    const btwOverlayKey = snapshot.btwOverlay
      ? `${snapshot.btwOverlay.question}\0${snapshot.btwOverlay.answer}`
      : null;
    if (btwOverlayKey !== this.lastBtwOverlayKey) {
      this.btwOverlayScrollOffset = 0;
      this.lastBtwOverlayKey = btwOverlayKey;
    }
    const screenLines = buildAppScreenLines(snapshot, {
      width,
      height: this.tui.terminal.rows,
      questionLines,
      editorLines,
      composerPreviewLines,
      pendingInput,
      pendingInputBaseline,
      showFullThinking,
      showToolDetails,
      showShortcutHelp: false,
      todosCollapsed: this.todosCollapsed,
      showTeamPanel: this.showTeamPanel,
      selectedTeamMemberId: this.selectedTeamMemberId,
      viewedTeamMemberId: this.viewedTeamMemberId,
      transientNotice: this.transientNotice,
      animationPhase: this.animationPhase,
      transcriptScrollOffset: this.transcriptScrollOffset,
      onTranscriptScrollOffsetChange: (offset) => {
        this.transcriptScrollOffset = offset;
      },
      btwOverlayScrollOffset: this.btwOverlayScrollOffset,
      onBtwOverlayScrollOffsetChange: (offset) => {
        this.btwOverlayScrollOffset = offset;
      },
      btwOverlayIndex: snapshot.btwOverlayIndex,
      btwOverlayTotal: snapshot.btwOverlayTotal,
      runningElapsedMs:
        !snapshot.isInterrupted &&
        (snapshot.isProcessing ||
          teamWorking ||
          snapshot.workflowRuns.some((workflow) => workflow.status === "running")) &&
        this.runningStartedAtMs !== null
          ? Date.now() - this.runningStartedAtMs
          : undefined,
    });
    this.updateQuestionOptionRows(screenLines, snapshot);
    return screenLines;
  }

  private async handleSubmit(raw: string): Promise<void> {
    const editorText = raw.trim();
    const text = this.expandPastedText(editorText).trim();
    if (!text) return;

    // 更新用户活动时间戳（用于 auto-recap 空闲检测）
    this.state.recordActivity();

    // When config editor is active (any phase), don't send chat messages
    if (this.configEditorState !== null) {
      if (this.configEditorState.phase === "input_value" && this.configEditorState.selectedKey) {
        const key = this.configEditorState.selectedKey;
        const schema = this.configEditorState.schemaList.find((s) => s.key === key);
        if (schema) {
          void this.applyConfigEditorSetAndStay(key, text, schema, this.configEditorState.currentValues);
        }
        this.editor.setText("");
        this.composerAttachments = [];
      }
      // For search_list / select_value phases, just ignore the submit
      return;
    }

    if (this.modelList !== null) {
      if (this.modelList.phase === "input") {
        void this.submitModelInput();
      } else if (this.modelList.phase === "delete_confirm") {
        void this.submitModelDelete();
      }
      return;
    }

    const { content, attachments } = this.buildOutgoingMessage(text);

    const snapshot = this.state.getSnapshot();
    // Other 自定义输入模式下空内容不得提交（#2330），避免触发黄色 thinking。
    if (!content) return;

    if (snapshot.pendingQuestion) {
      if (this.questionList !== null) {
        const selected = this.questionList.getSelectedItem();
        if (selected) {
          this.handleQuestionSelection(selected.value);
        }
        this.editor.setText("");
        return;
      }
      if (this.otherInputMode) {
        const pendingQuestion = snapshot.pendingQuestion;
        const pickedLabel = this.pendingQuestionAnswers.get(this.activeQuestionIndex) ?? "";
        this.pendingQuestionCustomInputs.set(this.activeQuestionIndex, text);
        this.otherInputMode = false;
        this.syncEditorSubmitState(this.state.getSnapshot());

        if (this.activeQuestionIndex < pendingQuestion.questions.length - 1) {
          if (!pickedLabel && !this.pendingMultiSelectAnswers.has(this.activeQuestionIndex)) {
            this.pendingQuestionAnswers.set(this.activeQuestionIndex, text);
          }
          this.activeQuestionIndex += 1;
          this.syncQuestionList(this.state.getSnapshot());
          this.editor.setText("");
          this.tui.requestRender();
          return;
        }

        const answers = pendingQuestion.questions.map((question, index) => {
          const label = this.pendingQuestionAnswers.get(index) ?? "";
          const multi = this.pendingMultiSelectAnswers.get(index);
          const isPlanRejectFeedback = shouldAppendPlanRejectFeedback(
            pendingQuestion.source,
            label,
            pendingQuestion.planApprovalKind,
          );
          const customInput = this.pendingQuestionCustomInputs.get(index);
          if (customInput || label === "Other" || isPlanRejectFeedback) {
            return {
              question: question.question,
              selected_options: multi ?? [label],
              custom_input: customInput,
            };
          }
          return {
            question: question.question,
            selected_options: multi ?? [label || text],
          };
        });
        this.state.submitQuestionAnswers(answers);
        this.editor.setText("");
        return;
      }
      const question =
        snapshot.pendingQuestion.questions[this.activeQuestionIndex]?.question ?? "";
      if (question) {
        this.state.submitQuestionAnswers([
          { selected_options: [text], custom_input: text },
        ]);
      } else {
        this.state.answerQuestion(text);
      }
      this.editor.setText("");
      return;
    }

    // Intercept /agents create to show tool selector
    if (/^\/agents\s+create/.test(text)) {
      const args = text.replace(/^\/agents\s+create\s*/, "").trim();
      let location = "user";
      let trimmed = args;
      if (trimmed.startsWith("--project ")) {
        location = "project";
        trimmed = trimmed.slice("--project ".length).trim();
      } else if (trimmed.startsWith("--local ")) {
        location = "local";
        trimmed = trimmed.slice("--local ".length).trim();
      }

      if (!trimmed) {
        this.state.addItem(
          addError(snapshot.sessionId, "用法: /agents create [--project|--local] <名称> <描述>"),
        );
        this.tui.requestRender();
        return;
      }

      const spaceIdx = trimmed.indexOf(" ");
      const rawName = spaceIdx > 0 ? trimmed.slice(0, spaceIdx).trim() : trimmed;
      const name = rawName.replace(/[,，]+$/, "").trim();
      const desc = spaceIdx > 0 ? trimmed.slice(spaceIdx + 1).trim() : (name || "");

      const when_to_use = `当你需要${desc}时使用`;
      const defaultPrompt = [
        `你是 ${name}，专注于：${desc}。`,
        "",
        "## 工作流程",
        "1. 理解任务：明确输入、目标和约束条件",
        "2. 收集信息：利用搜索和文件读取工具获取必要的上下文",
        "3. 分析处理：基于收集的信息进行系统性分析",
        "4. 输出结果：提供清晰、可执行的结论或方案",
        "",
        "## 核心原则",
        "- 先理解再行动，不盲目猜测",
        "- 用代码和证据说话，不做空洞判断",
        "- 不确定时主动说明，标注假设和风险",
        "- 复杂问题分步骤推进，每步确认结果",
        "",
        "## 输出规范",
        "- 关键结论前置，细节在后",
        "- 使用结构化格式（列表、表格、代码块）",
        "- 引用具体文件路径和行号",
        "- 区分事实结论和推测判断",
      ].join("\n");

      await this.openToolSelector(name, desc, when_to_use, defaultPrompt, location, true);
      this.editor.addToHistory(text);
      this.editor.setText("");
      this.state.addItem(addCommandEcho(snapshot.sessionId, text));
      return;
    }

    if (text.startsWith("/")) {
      // /<installedSkill> 行首分流：命中已装 skill 时当普通消息发送（content 原样
      // 保留 /<skill> 前缀，skill 名由 extractSkillsFromContent 提取注入 params.skills）。
      // 未命中已装 skill 的 /xxx 不在此拦截，继续走下面的命令分支（仍可能 Unknown command）。
      // 新建技能后缓存可能仍旧：若首 token 也不是注册命令，先 refresh 再重试，避免误报 Unknown command。
      {
        const slashMatch = text.match(/^\/(\S+)/);
        const firstToken = slashMatch?.[1] ?? "";
        let installedSkill = firstToken
          ? this.commands.getInstalledSkills().find((s) => s.name === firstToken)
          : undefined;
        if (!installedSkill && firstToken && !this.commands.resolve(firstToken)) {
          await this.commands.refreshSkills(this.state.getCommandContext());
          installedSkill = this.commands.getInstalledSkills().find((s) => s.name === firstToken);
        }
        if (installedSkill) {
          // 只有 /<skill> 而没有内容时没什么可发的。pi-tui 在补全弹窗上按回车会
          // 「应用补全」并顺势提交（见其 editor 的 tui.select.confirm 分支），这一下
          // 只是补全，所以把补全结果放回输入框等用户补内容；用户自己再回车才提示为空。
          if (text === `/${installedSkill.name}`) {
            const justCompleted =
              this.slashNameCompletion?.name === installedSkill.name &&
              Date.now() - this.slashNameCompletion.at < 1000;
            this.slashNameCompletion = null;
            if (justCompleted) {
              this.editor.setText(`${text} `);
              this.tui.requestRender();
              return;
            }
            this.editor.addToHistory(text);
            this.editor.setText("");
            this.state.addItem(addCommandEcho(snapshot.sessionId, text));
            this.state.addItem(
              addError(snapshot.sessionId, `${text} 后面需要跟内容，例如 ${text} 帮我做…`),
            );
            return;
          }
          this.slashNameCompletion = null;
          this.beginPendingSubmittedInput(text, snapshot);
          const extractedSkills = this.extractSkillsFromContent(content);
          const requestId = this.state.sendMessage(
            content,
            attachments,
            undefined,
            undefined,
            extractedSkills,
          );
          if (!requestId) {
            this.clearPendingSubmittedInput();
            this.state.addItem({
              kind: "error",
              id: `offline-${Date.now()}`,
              sessionId: snapshot.sessionId,
              content: "offline: waiting for reconnect",
              at: new Date().toISOString(),
            });
            return;
          }
          this.editor.addToHistory(text);
          this.editor.setText("");
          return;
        }
      }
      // Check for mode switch when there's ongoing work
      if (/^\/(?:mode|switch)\s/.test(text) && snapshot.cancellableWork) {
        const currentMode = snapshot.mode;
        // Parse the target mode from the command
        const modeMatch = text.match(/^\/(?:mode|switch)\s+(\S+)/);
        const targetMode = modeMatch?.[1] ?? "";
        if (currentMode !== targetMode) {
          const answers = await this.state.askQuestions(
            [
              {
                header: "模式切换",
                question: `当前有任务正在运行，切换到 ${targetMode} 模式会中断这些任务。`,
                options: [
                  { label: "中断任务并切换", description: "停止当前任务，切换到新模式" },
                  { label: "取消切换", description: "继续执行当前任务" },
                ],
              },
            ],
            "mode_switch_confirm",
          );
          const selected = answers[0]?.selected_options?.[0];
          if (selected !== "中断任务并切换") {
            this.state.addItem(addInfo(snapshot.sessionId, "模式切换已取消", "m"));
            this.editor.addToHistory(text);
            this.editor.setText("");
            return;
          }
          // User confirmed, send cancel request and wait for it to complete
          this.state.sendEventOnly("chat.interrupt", { intent: "cancel", mode: currentMode });
          // Wait for the interrupt to complete: streamingState changes and entries are updated
          // Since setStreamingState now calls emitChange, we just need to poll for state change
          const waitForInterrupt = (timeoutMs = 10000): Promise<void> => {
            return new Promise((resolve) => {
              const startTime = Date.now();
              const check = () => {
                const elapsed = Date.now() - startTime;
                if (elapsed >= timeoutMs) {
                  resolve();
                  return;
                }
                const snap = this.state.getSnapshot();
                if (!snap.cancellableWork && snap.streamingState !== "responding") {
                  // Give a brief delay to ensure termination entries are rendered
                  setTimeout(resolve, 50);
                } else {
                  setTimeout(check, 100);
                }
              };
              check();
            });
          };
          await waitForInterrupt();
        }
      }
      if (/^\/(?:resume|continue)\s*$/.test(text)) {
        this.editor.addToHistory(text);
        this.editor.setText("");
        this.state.addItem(addCommandEcho(snapshot.sessionId, text));
        // 默认仅列出当前项目的会话；进入后可按 Ctrl+A 查看全部项目
        await this.openResumeSessionList(false);
        return;
      }
      if (/^\/model\s+add\s*$/.test(text)) {
        this.editor.addToHistory(text);
        this.editor.setText("");
        this.state.addItem(addCommandEcho(snapshot.sessionId, text));
        await this.openModelList("add");
        return;
      }
      if (/^\/model\s+delete\s*$/.test(text)) {
        this.editor.addToHistory(text);
        this.editor.setText("");
        this.state.addItem(addCommandEcho(snapshot.sessionId, text));
        await this.openModelList("delete");
        return;
      }
      if (/^\/model\s*$/.test(text)) {
        this.editor.addToHistory(text);
        this.editor.setText("");
        this.state.addItem(addCommandEcho(snapshot.sessionId, text));
        await this.openModelList();
        return;
      }
      if (/^\/mcp(?:\s+list)?\s*$/.test(text)) {
        this.editor.addToHistory(text);
        this.editor.setText("");
        this.state.addItem(addCommandEcho(snapshot.sessionId, text));
        await this.openMcpList();
        return;
      }
      if (/^\/status(?:\s+\S*)?\s*$/.test(text)) {
        this.editor.addToHistory(text);
        this.editor.setText("");
        this.state.addItem(addCommandEcho(snapshot.sessionId, text));
        const subMatch = text.match(/^\/status\s+(\S+)/);
        const tab: StatusViewTab | undefined =
          subMatch?.[1] === "usage" ? "usage" :
          subMatch?.[1] === "config" ? "config" :
          undefined;
        await this.openStatusView(tab);
        return;
      }
      // /memory（无参）及 /memory <edit|status|toggle|open>（无后续参数）
      // 打开 MemoryView 页签控制台；带参数的形式（/memory edit <path>、/memory toggle <key>）
      // 不匹配，继续走命令分发器（保留 completion）。
      const memMatch = text.match(/^\/memory(?:\s+(edit|status|toggle|open))?$/);
      if (memMatch) {
        this.editor.addToHistory(text);
        this.editor.setText("");
        this.state.addItem(addCommandEcho(snapshot.sessionId, text));
        const memTab = (memMatch[1] as MemoryViewTab | undefined) ?? "edit";
        await this.ensureMvController().open(memTab);
        return;
      }
      if (/^\/theme\s*$/.test(text)) {
        this.editor.addToHistory(text);
        this.editor.setText("");
        this.state.addItem(addCommandEcho(snapshot.sessionId, text));
        this.openThemeList();
        return;
      }
      const swarmFlowsMatch = text.match(/^\/(?:swarmflows|swarmworkflows)\s*$/);
      if (swarmFlowsMatch) {
        this.editor.addToHistory(text);
        this.editor.setText("");
        this.state.addItem(addCommandEcho(snapshot.sessionId, text));
        await this.enterSwarmWorkflowsView();
        return;
      }
      this.beginPendingSubmittedInput(text, snapshot);
      this.editor.addToHistory(text);
      this.editor.setText("");
      this.state.addItem(addCommandEcho(snapshot.sessionId, text));
      try {
        await this.commands.execute(text, {
          ...this.state.getCommandContext(),
          exitApp: this.exit,
          setInput: (text: string) => {
            this.editor.setText(text);
          },
          enterConfigEditor: (focusKey, configPayload, mode) => {
            this.openConfigEditor(focusKey, configPayload, mode);
          },
          openInEditor: async (filePath: string, onDone?: (success?: boolean) => void) => {
            await openInExternalEditor(this.tui, filePath, onDone);
          },
          openFolder: (folderPath: string) => {
            return openFolderInExplorer(folderPath);
          },
          enterFileViewer: (content, title, source) => {
            this.enterFileViewer(content, title, source);
          },
          enterDiffViewer: (payload) => {
            this.enterDiffViewer(payload);
          },
        });
      } finally {
        this.clearPendingSubmittedInput();
      }
      return;
    }

    // Team 模式持续对话走 chat.send（interact），不通过 supplement 中断当前 stream。
    if ((snapshot.isProcessing || snapshot.isPaused) && !isTeamMode(snapshot.mode)) {
      this.beginPendingSubmittedInput(text, snapshot);
      const requestId = this.state.supplement(content, attachments);
      if (!requestId) {
        this.clearPendingSubmittedInput();
        this.state.addItem({
          kind: "error",
          id: `offline-${Date.now()}`,
          sessionId: snapshot.sessionId,
          content: "offline: waiting for reconnect",
          at: new Date().toISOString(),
        });
        return;
      }
      this.editor.addToHistory(text);
      this.editor.setText("");
      return;
    }

    this.beginPendingSubmittedInput(text, snapshot);
    const extractedSkills = this.extractSkillsFromContent(content);
    const requestId = this.state.sendMessage(content, attachments, undefined, undefined, extractedSkills);
    if (!requestId) {
      this.clearPendingSubmittedInput();
      this.state.addItem({
        kind: "error",
        id: `offline-${Date.now()}`,
        sessionId: snapshot.sessionId,
        content: "offline: waiting for reconnect",
        at: new Date().toISOString(),
      });
      return;
    }

    this.editor.addToHistory(text);
    this.editor.setText("");
  }

  private handleStateChange(): void {
    const snapshot = this.state.getSnapshot();
    if (snapshot.sessionId !== this.workflowUiSessionId) {
      this.workflowUiSessionId = snapshot.sessionId;
      this.shownBudgetExhaustedWorkflowKeys.clear();
    }
    const currentWorkflowKeys = new Set(
      snapshot.workflowRuns.map((workflow) => `${snapshot.sessionId}:${workflow.id}`),
    );
    for (const key of this.shownBudgetExhaustedWorkflowKeys) {
      if (!currentWorkflowKeys.has(key)) this.shownBudgetExhaustedWorkflowKeys.delete(key);
    }
    if (this.commands.setSkillEvolutionEnabled(snapshot.skillEvolutionEnabled)) {
      this.composerAutocompleteProvider = this.rebuildAutocompleteProvider();
      this.editor.setAutocompleteProvider(this.composerAutocompleteProvider);
    }
    // Populate the skill cache as soon as the WebSocket connection is established
    if (!this.didEagerFetchSkills && snapshot.connectionStatus === "connected") {
      this.didEagerFetchSkills = true;
      void this.commands.refreshSkills(this.state.getCommandContext());
    }
    // Hide /sandbox subcommand inline hints when sandbox.type=yuanrong.
    if (!this.didEagerFetchSandboxMeta && snapshot.connectionStatus === "connected") {
      this.didEagerFetchSandboxMeta = true;
      void import("../core/commands/builtins/sandbox.js")
        .then(({ refreshSandboxCommandPresentation }) =>
          refreshSandboxCommandPresentation(this.state.getCommandContext()),
        )
        .then(() => {
          this.composerAutocompleteProvider = this.rebuildAutocompleteProvider();
          this.editor.setAutocompleteProvider(this.composerAutocompleteProvider);
        })
        .catch(() => {
          // Best-effort; /sandbox action still probes on use.
        });
    }
    if (
      this.pendingSubmittedInput &&
      (snapshot.sessionId !== this.pendingSubmittedSessionId ||
        snapshot.entries.length !== this.pendingSubmittedBaseline)
    ) {
      this.clearPendingSubmittedInput(false);
    }
    const questionId = snapshot.pendingQuestion?.requestId ?? null;
    if (questionId && questionId !== this.activeQuestionId) {
      this.activeQuestionId = questionId;
      this.activeQuestionIndex = 0;
      this.pendingQuestionAnswers.clear();
      this.pendingMultiSelectAnswers.clear();
      this.pendingQuestionCustomInputs.clear();
      this.draftBeforeQuestion = this.editor.getText();
      this.editor.setText("");
      const pendingQuestion = snapshot.pendingQuestion;
      const firstQuestion = pendingQuestion?.questions[0];
      this.otherInputMode =
        pendingQuestion?.source === "ask_user_interrupt" &&
        !!firstQuestion &&
        firstQuestion.options.length === 0;
      this.syncQuestionList(snapshot);
    } else if (questionId && this.activeQuestionId) {
      // Same question still active — preserve existing questionList to keep cursor position
      // syncQuestionList recreates SelectList from scratch, losing the transient selectedIndex
    } else if (!questionId && this.activeQuestionId) {
      this.activeQuestionId = null;
      this.activeQuestionIndex = 0;
      this.otherInputMode = false;
      this.pendingQuestionAnswers.clear();
      this.pendingMultiSelectAnswers.clear();
      this.pendingQuestionCustomInputs.clear();
      this.questionList = null;
      this.questionCheckboxList = null;
      this.questionDetailsMap = null;
      this.questionPreviewMap = null;
      this.setMouseTrackingEnabled(false);
      if (!this.editor.getText() && this.draftBeforeQuestion) {
        this.editor.setText(this.draftBeforeQuestion);
      }
      this.draftBeforeQuestion = "";
    }
    this.syncEditorSubmitState(snapshot);
    this.syncTeamPanelSelection(snapshot);
    this.refreshSwarmWorkflowsView();
    this.maybeOpenCurrentWorkflowBudgetExhausted();
    this.syncAnimationLoop(snapshot);
    // Sync terminal window title with session title when it changes
    if (snapshot.sessionTitle !== this.previousSessionTitle) {
      this.previousSessionTitle = snapshot.sessionTitle;
      // Truncate to 30 chars for terminal window title (same as status bar)
      const rawTitle = snapshot.sessionTitle || "jiuwenswarm";
      const displayTitle = rawTitle.length > 30 ? rawTitle.slice(0, 30) + "..." : rawTitle;
      this.tui.terminal.setTitle(displayTitle);
    }
    this.tui.requestRender();
  }

  private beginPendingSubmittedInput(
    text: string,
    snapshot: ReturnType<CliPiAppState["getSnapshot"]>,
  ): void {
    this.transcriptScrollOffset = 0;
    this.pendingSubmittedInput = text;
    this.pendingSubmittedBaseline = snapshot.entries.length;
    this.pendingSubmittedSessionId = snapshot.sessionId;
    this.tui.requestRender();
  }

  private handleTranscriptScrollInput(data: string): boolean {
    const wheelOffset = getSgrMouseWheelOffset(data, this.transcriptScrollOffset);
    if (wheelOffset !== null) {
      this.transcriptScrollOffset = wheelOffset;
      this.tui.requestRender();
      return true;
    }

    const pageSize = Math.max(1, Math.floor(this.tui.terminal.rows * 0.8));
    const scrollAction = resolveAction("Scroll", data);
    switch (scrollAction) {
      case "scroll:pageUp":
        this.transcriptScrollOffset += pageSize;
        this.tui.requestRender();
        return true;
      case "scroll:pageDown":
        this.transcriptScrollOffset = Math.max(0, this.transcriptScrollOffset - pageSize);
        this.tui.requestRender();
        return true;
      case "scroll:top":
        this.transcriptScrollOffset = Number.MAX_SAFE_INTEGER;
        this.tui.requestRender();
        return true;
      case "scroll:bottom":
        this.transcriptScrollOffset = 0;
        this.tui.requestRender();
        return true;
      default:
        return false;
    }
  }

  private handleBtwOverlayScrollInput(data: string): boolean {
    const wheelOffset = getSgrMouseWheelOffset(data, this.btwOverlayScrollOffset);
    if (wheelOffset !== null) {
      this.btwOverlayScrollOffset = wheelOffset;
      this.tui.requestRender();
      return true;
    }

    // 输入框为空时，Enter / Space 关闭 btw 浮层；输入框有内容时保留原有 composer 行为。
    // ctrl+c 始终优先关闭浮层，但只在确有 btw 请求进行中时发送中断。
    const dismissWithCtrlC = matchesKey(data, "ctrl+c");
    const dismissWithEnterOrSpace =
      this.editor.getText().length === 0 &&
      (matchesKey(data, "enter") ||
        matchesKey(data, "return") ||
        matchesKey(data, "space"));
    if (dismissWithCtrlC || dismissWithEnterOrSpace) {
      const hasPendingBtwRequest = this.state.getSnapshot().btwPendingQuestion !== null;
      this.state.clearBtwOverlay();
      this.btwOverlayScrollOffset = 0;
      if (hasPendingBtwRequest) {
        this.state.requestLocalInterrupt();
      }
      this.state.setBtwActive(false);
      this.tui.requestRender();
      return true;
    }

    // ←/→ 在 btw 历史间切换（必须在 scroll 之前消费，避免落入 composer）
    if (matchesKey(data, "left")) {
      this.state.navigateBtw(-1);
      this.tui.requestRender();
      return true;
    }
    if (matchesKey(data, "right")) {
      this.state.navigateBtw(1);
      this.tui.requestRender();
      return true;
    }
    // 复制当前 /btw 整条记录（问题+回答）到剪贴板（按 c —— 对齐 claudecode /btw 快捷键）。
    // 注意：overlay 显示时输入栏仍在，小写 c 会被吞触发复制；
    // 想在输入框输入小写 c 需先 Esc 关闭 overlay。
    if (matchesKey(data, "c")) {
      const overlay = this.state.getSnapshot().btwOverlay;
      if (overlay) {
        void this.copyBtwEntry(overlay.question, overlay.answer);
      }
      return true;
    }
    // x 删除当前 btw 条目（剩余非空则跳到相邻，为空则关闭 overlay）
    if (data.toLowerCase() === "x") {
      this.state.deleteCurrentBtwEntry();
      this.tui.requestRender();
      return true;
    }

    const pageSize = Math.max(1, Math.floor(this.tui.terminal.rows * 0.8));
    // ctrl+p / ctrl+n 翻页（对齐 PgUp/PgDn）
    if (matchesKey(data, "ctrl+p")) {
      this.btwOverlayScrollOffset = Math.max(0, this.btwOverlayScrollOffset - pageSize);
      this.tui.requestRender();
      return true;
    }
    if (matchesKey(data, "ctrl+n")) {
      this.btwOverlayScrollOffset += pageSize;
      this.tui.requestRender();
      return true;
    }
    if (matchesKey(data, "up")) {
      this.btwOverlayScrollOffset = Math.max(0, this.btwOverlayScrollOffset - 1);
      this.tui.requestRender();
      return true;
    }
    if (matchesKey(data, "down")) {
      this.btwOverlayScrollOffset += 1;
      this.tui.requestRender();
      return true;
    }

    const scrollAction = resolveAction("Scroll", data);
    switch (scrollAction) {
      case "scroll:pageUp":
        this.btwOverlayScrollOffset = Math.max(0, this.btwOverlayScrollOffset - pageSize);
        this.tui.requestRender();
        return true;
      case "scroll:pageDown":
        this.btwOverlayScrollOffset += pageSize;
        this.tui.requestRender();
        return true;
      case "scroll:top":
        this.btwOverlayScrollOffset = 0;
        this.tui.requestRender();
        return true;
      case "scroll:bottom":
        this.btwOverlayScrollOffset = Number.MAX_SAFE_INTEGER;
        this.tui.requestRender();
        return true;
      default:
        return false;
    }
  }

  /**
   * 复制 /btw 整条记录（问题 + 回答）到系统剪贴板，并在状态栏显示短暂反馈。
   * 使用 transientNotice（独立于 transcript，固定在屏幕底部渲染），
   * 与 /copy 命令的反馈保持一致。格式仿 overlay 标题行：/btw <question> + 回答。
   */
  private async copyBtwEntry(question: string, answer: string): Promise<void> {
    const text = `/btw ${question}\n\n${answer}`;
    const ok = await copyToClipboard(text);
    this.transientNotice = ok
      ? "已复制 /btw 问答到剪贴板"
      : "无法访问剪贴板（系统不支持）";
    if (this.transientNoticeTimer) {
      clearTimeout(this.transientNoticeTimer);
    }
    this.transientNoticeTimer = setTimeout(() => {
      this.transientNotice = null;
      this.transientNoticeTimer = null;
      this.tui.requestRender();
    }, 2500);
    this.tui.requestRender();
  }

  private clearPendingSubmittedInput(requestRender = true): void {
    this.pendingSubmittedInput = null;
    this.pendingSubmittedBaseline = 0;
    this.pendingSubmittedSessionId = null;
    if (requestRender) {
      this.tui.requestRender();
    }
  }

  private syncTeamPanelSelection(snapshot: ReturnType<CliPiAppState["getSnapshot"]>): void {
    const memberIds = orderedMemberIds(snapshot.teamMemberEvents, snapshot.teamMessageEvents);
    if (memberIds.length === 0) {
      this.selectedTeamMemberId = null;
      this.viewedTeamMemberId = null;
      return;
    }
    if (!this.selectedTeamMemberId || !memberIds.includes(this.selectedTeamMemberId)) {
      this.selectedTeamMemberId = memberIds[0] ?? null;
    }
    if (this.viewedTeamMemberId && !memberIds.includes(this.viewedTeamMemberId)) {
      this.viewedTeamMemberId = null;
    }
  }

  private moveTeamPanelSelection(
    snapshot: ReturnType<CliPiAppState["getSnapshot"]>,
    delta: -1 | 1,
  ): void {
    const memberIds = orderedMemberIds(snapshot.teamMemberEvents, snapshot.teamMessageEvents);
    if (memberIds.length === 0) {
      this.selectedTeamMemberId = null;
      return;
    }
    const currentIndex = this.selectedTeamMemberId
      ? memberIds.indexOf(this.selectedTeamMemberId)
      : 0;
    const baseIndex = currentIndex >= 0 ? currentIndex : 0;
    const nextIndex = Math.max(0, Math.min(memberIds.length - 1, baseIndex + delta));
    const nextMemberId = memberIds[nextIndex] ?? memberIds[0] ?? null;
    this.selectedTeamMemberId = nextMemberId;
    if (this.viewedTeamMemberId !== null) {
      this.viewedTeamMemberId = nextMemberId;
    }
  }

  private makeResumeSelectList(items: SelectItem[]): SelectList {
    const labelCounts = new Map<string, number>();
    for (const item of items) {
      labelCounts.set(item.label, (labelCounts.get(item.label) ?? 0) + 1);
    }
    const labelOccurrences = new Map<string, number>();
    const idHints = new Map<string, string>();
    for (const item of items) {
      if ((labelCounts.get(item.label) ?? 0) < 2) continue;
      const occurrence = (labelOccurrences.get(item.label) ?? 0) + 1;
      labelOccurrences.set(item.label, occurrence);
      idHints.set(item.value, getResumeSessionIdHint(item.value, occurrence));
    }

    const list = new SelectList(items, Math.min(Math.max(items.length, 1), 8), selectListTheme, {
      minPrimaryColumnWidth: 24,
      maxPrimaryColumnWidth: 42,
      truncatePrimary: (context) =>
        truncateResumeSessionPrimary(context, idHints.get(context.item.value)),
    });
    list.onSelect = (item) => {
      if (item && item.value) {
        void this.handleResumeSessionSelection(item.value);
      }
    };
    list.onCancel = () => {
      this.resumeSessionList = null;
      this.tui.requestRender();
    };
    return list;
  }

  private async openResumeSessionList(allProjects = false): Promise<void> {
    const snapshot = this.state.getSnapshot();
    try {
      const payload = await this.state.request<SessionListPayload>("session.list", {
        all_projects: allProjects,
      });
      const sessions = sanitizeSessionList(payload.sessions);
      const total = payload.total ?? sessions.length;
      const currentBranch = payload.current_branch ?? "HEAD";
      // 全部项目仍为空：确无可恢复会话，直接提示，不打开选择器
      if (sessions.length === 0 && allProjects) {
        this.resumeSessionList = null;
        this.state.addItem(addInfo(snapshot.sessionId, "No sessions found", "r"));
        return;
      }
      // 当前项目为空：仍打开（空）选择器，便于用户按 Ctrl+A 切到全部项目

      const items = computeResumeItems(sessions, {
        query: "",
        showProject: allProjects,
        branchFilter: false,
        currentBranch,
      });
      this.resumeSessionList = {
        list: this.makeResumeSelectList(items),
        sessions,
        total,
        searchQuery: "",
        showAllProjects: allProjects,
        branchFilterEnabled: false,
        currentBranch,
        preview: null,
        previewMessages: [],
        previewLoading: false,
        previewScrollOffset: 0,
        rename: null,
      };
      this.tui.requestRender();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.resumeSessionList = null;
      this.state.addItem(addError(snapshot.sessionId, `resume failed: ${message}`));
    }
  }

  private async toggleResumeAllProjects(): Promise<void> {
    if (!this.resumeSessionList) return;
    const next = !this.resumeSessionList.showAllProjects;
    const { searchQuery, branchFilterEnabled } = this.resumeSessionList;
    const snapshot = this.state.getSnapshot();
    try {
      const payload = await this.state.request<SessionListPayload>("session.list", {
        all_projects: next,
      });
      const sessions = sanitizeSessionList(payload.sessions);
      const total = payload.total ?? sessions.length;
      const currentBranch = payload.current_branch ?? this.resumeSessionList.currentBranch;
      const items = computeResumeItems(sessions, {
        query: searchQuery,
        showProject: next,
        branchFilter: branchFilterEnabled,
        currentBranch,
      });
      this.resumeSessionList = {
        list: this.makeResumeSelectList(items),
        sessions,
        total,
        searchQuery,
        showAllProjects: next,
        branchFilterEnabled,
        currentBranch,
        preview: null,
        previewMessages: [],
        previewLoading: false,
        previewScrollOffset: 0,
        rename: null,
      };
      this.tui.requestRender();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.state.addItem(addError(snapshot.sessionId, `resume failed: ${message}`));
    }
  }

  private toggleResumeBranchFilter(): void {
    if (!this.resumeSessionList) return;
    const st = this.resumeSessionList;
    const next = !st.branchFilterEnabled;
    const items = computeResumeItems(st.sessions, {
      query: st.searchQuery,
      showProject: st.showAllProjects,
      branchFilter: next,
      currentBranch: st.currentBranch,
    });
    this.resumeSessionList = {
      ...st,
      list: this.makeResumeSelectList(items),
      branchFilterEnabled: next,
    };
    this.tui.requestRender();
  }

  private async handleResumeSessionSelection(sessionId: string): Promise<void> {
    if (!sessionId || typeof sessionId !== "string") {
      return;
    }
    const nextSessionId = sessionId.trim();
    if (!nextSessionId) {
      return;
    }
    // 在清空 resumeSessionList 之前获取 accent_color 和项目信息
    const sessions = this.resumeSessionList?.sessions ?? [];
    const matchedSession = sessions.find((s) => s.session_id === nextSessionId);
    const accentColor = matchedSession?.accent_color ?? "default";

    // 检测目标 session 是否已在其他 TUI 窗口打开，避免多窗口 session 冲突
    if (matchedSession?.active_in_window) {
      const title = matchedSession.title?.trim() || nextSessionId;
      this.state.addItem(
        addInfo(
          this.state.getSnapshot().sessionId,
          `Session "${title}" is already open in another TUI window. Close that window first or choose a different session.`,
          "r",
        ),
      );
      this.tui.requestRender();
      return;
    }

    // 跨项目目录已由后端 session.list 完成过滤（_session_matches_project），
    // 此处不再重复校验，避免前后端路径规范化差异（resolve vs realpath）
    // 导致误拦截本已通过后端过滤的会话。

    this.resumeSessionList = null;
    const snapshot = this.state.getSnapshot();
    const previousSessionId = snapshot.sessionId;
    // 历史 session 可能存旧 canonical 串（agent.plan / team / code.team），
    // 走 normalizeToClientMode 归一到新串；空/未知串回退当前 mode。
    const targetMode =
      normalizeToClientMode(matchedSession?.mode ?? "") ?? snapshot.mode;
    try {
      await this.state.request("session.switch", {
        session_id: nextSessionId,
        previous_session_id: previousSessionId,
        previous_mode: snapshot.mode,
        mode: targetMode,
      });
    } catch (error) {
      if (isTeamMode(targetMode)) {
        const message = error instanceof Error ? error.message : String(error);
        this.state.addItem(
          addError(previousSessionId, "Failed to switch team session: " + message),
        );
        this.tui.requestRender();
        return;
      }
    }
    this.state.setMode(targetMode);
    this.state.updateSession(nextSessionId);
    this.state.clearEntries();
    this.state.setAccentColor(accentColor);
    try {
      await this.state.restoreHistory(nextSessionId);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.state.addItem(
        addError(this.state.getSnapshot().sessionId, "Failed to restore session: " + message),
      );
      this.tui.requestRender();
      return;
    }
    try {
      await this.state.loadWorkflowSnapshot(nextSessionId);
    } catch {
      // Workflow snapshots are optional for sessions without workflow state.
    }
    // 异步获取被恢复会话的标题并更新终端窗口标题
    void (async () => {
      try {
        const meta = await this.state.request<{ session_id: string; title: string }>(
          "session.rename",
          { session_id: nextSessionId },
        );
        this.state.setSessionTitle(meta.title || "");
      } catch {
        this.state.setSessionTitle("");
      }
    })();
    this.tui.requestRender();
  }

  private getPrintableChar(data: string): string | undefined {
    // Kitty protocol printable character
    const kittyChar = decodeKittyPrintable(data);
    if (kittyChar) return kittyChar;

    // Check for printable Unicode character (not control sequences)
    if (data.length === 1) {
      const code = data.charCodeAt(0);
      // Control characters (0-31, 127) and DEL (127) are not printable
      // Extended ASCII (128-255) and Unicode (>255) printable chars are accepted
      if (code >= 32 && code !== 127) return data;
    }

    // UTF-8 multi-byte characters (Chinese, etc.)
    // Check if data looks like a valid UTF-8 printable string (not an escape sequence)
    if (data.length > 1 && !data.startsWith("\x1b")) {
      try {
        // Verify it's a valid printable string
        const firstChar = data[0];
        if (firstChar && firstChar.charCodeAt(0) >= 32) {
          return data;
        }
      } catch {
        // Invalid UTF-8, ignore
      }
    }

    return undefined;
  }

  private updateResumeSearchQuery(query: string): void {
    if (!this.resumeSessionList) return;
    const st = this.resumeSessionList;
    const filteredItems = computeResumeItems(st.sessions, {
      query,
      showProject: st.showAllProjects,
      branchFilter: st.branchFilterEnabled,
      currentBranch: st.currentBranch,
    });
    this.resumeSessionList = {
      ...st,
      list: this.makeResumeSelectList(filteredItems),
      searchQuery: query,
    };
    this.tui.requestRender();
  }

  /** 打开选中会话的只读预览（信息卡 + 最新对话）。Space 触发，对齐 Claude Code 的 preview。 */
  private async openResumeSessionPreview(): Promise<void> {
    if (!this.resumeSessionList) return;
    const selected = this.resumeSessionList.list.getSelectedItem();
    if (!selected) return;
    const session = this.resumeSessionList.sessions.find(
      (s) => s.session_id === selected.value,
    );
    if (!session) return;
    // 先设置 preview 状态并标记加载中，显示基本信息
    this.resumeSessionList = {
      ...this.resumeSessionList,
      preview: session,
      previewMessages: [],
      previewLoading: true,
      previewScrollOffset: 0,
    };
    this.setMouseTrackingEnabled(true);
    this.tui.requestRender();
    // 异步获取预览消息
    try {
      const resp = await this.state.request<{ session_id: string; preview_messages: PreviewMessage[] }>(
        "session.preview",
        { session_id: session.session_id, count: 30 },
      );
      if (this.resumeSessionList && this.resumeSessionList.preview?.session_id === session.session_id) {
        this.resumeSessionList = {
          ...this.resumeSessionList,
          previewMessages: resp.preview_messages ?? [],
          previewLoading: false,
        };
        this.tui.requestRender();
      }
    } catch (error) {
      // 获取失败不影响预览基本信息显示，但需结束加载态
      console.debug("[openResumeSessionPreview] session.preview failed:", error);
      if (this.resumeSessionList && this.resumeSessionList.preview?.session_id === session.session_id) {
        this.resumeSessionList = { ...this.resumeSessionList, previewLoading: false };
        this.tui.requestRender();
      }
    }
  }

  /** 进入重命名态，初始值取当前标题。Ctrl+R 触发，对齐 Claude Code。 */
  private openResumeRename(): void {
    if (!this.resumeSessionList) return;
    const selected = this.resumeSessionList.list.getSelectedItem();
    if (!selected) return;
    const session = this.resumeSessionList.sessions.find(
      (s) => s.session_id === selected.value,
    );
    if (!session) return;
    this.resumeSessionList = {
      ...this.resumeSessionList,
      rename: { sessionId: session.session_id, value: session.title?.trim() ?? "" },
    };
    this.tui.requestRender();
  }

  /** 提交重命名：调用 session.rename 写入标题，成功后就地更新列表并退出重命名态。 */
  private async submitResumeRename(): Promise<void> {
    if (!this.resumeSessionList || !this.resumeSessionList.rename) return;
    const st = this.resumeSessionList;
    const { sessionId, value } = st.rename!;
    const title = value.trim();
    try {
      const resp = await this.state.request<{ session_id: string; title: string }>(
        "session.rename",
        { session_id: sessionId, title },
      );
      const newTitle = resp.title ?? title;
      // 就地更新本地会话标题并重建列表项
      const sessions = sanitizeSessionList(
        st.sessions.map((s) =>
          s.session_id === sessionId ? { ...s, title: newTitle } : s,
        ),
      );
      const items = computeResumeItems(sessions, {
        query: st.searchQuery,
        showProject: st.showAllProjects,
        branchFilter: st.branchFilterEnabled,
        currentBranch: st.currentBranch,
      });
      this.resumeSessionList = {
        ...st,
        sessions,
        list: this.makeResumeSelectList(items),
        rename: null,
      };
      // 若重命名的是当前活动会话，同步终端窗口标题
      if (sessionId === this.state.getSnapshot().sessionId) {
        this.state.setSessionTitle(newTitle);
      }
      this.tui.requestRender();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.resumeSessionList = { ...st, rename: null };
      this.state.addItem(
        addError(this.state.getSnapshot().sessionId, `rename failed: ${message}`),
      );
      this.tui.requestRender();
    }
  }

  private buildResumeSessionRenameLines(width: number, session: SessionMeta | undefined, value: string): string[] {
    const placeholder = session?.title?.trim() || session?.session_id || "";
    return [
      padToWidth(palette.status.warning("Rename session"), width),
      padToWidth(palette.text.dim(session?.session_id ?? ""), width),
      "",
      padToWidth(`${palette.text.dim("Title: ")}${palette.text.primary(value)}${END_CURSOR}`, width),
      value.length === 0
        ? padToWidth(palette.text.dim(`(current: ${placeholder})`), width)
        : "",
      "",
      padToWidth(palette.text.dim("Enter save · Esc cancel · empty clears title"), width),
    ].filter((l) => l !== "");
  }

  private buildResumeSessionPreviewLines(width: number, session: SessionMeta, previewMessages: PreviewMessage[]): string[] {
    const title = session.title?.trim() || "(untitled)";
    const project = session.project_dir?.trim() || "-";
    const titleLines = renderWrappedText(width, title, palette.text.primary);
    const sessionPrefix = "Session:   ";
    const sessionLines = prefixedLines(
      renderWrappedText(
        Math.max(1, width - sessionPrefix.length),
        session.session_id,
        palette.text.primary,
      ),
      width,
      sessionPrefix,
      palette.text.dim,
      " ".repeat(sessionPrefix.length),
    );

    // Build preview message lines: full transcript style, matching Claude Code SessionPreview
    const messageLines: string[] = [];
    if (previewMessages.length > 0) {
      previewMessages.forEach((msg, msgIdx) => {
        const isUser = msg.role === "user";
        if (isUser) {
          const lines = renderStyledMarkdownLines(
            Math.max(1, width - 2),
            msg.content,
            { color: palette.text.dim },
            0,
            0,
          );
          messageLines.push(...prefixedLines(lines, width, "> ", palette.text.user, "  "));
        } else {
          const lines = renderStyledMarkdownLines(
            width,
            msg.content,
            { color: palette.text.assistant },
            0,
            0,
          );
          messageLines.push(...lines);
        }
        if (msgIdx < previewMessages.length - 1) {
          messageLines.push("");
        }
      });
    } else if (this.resumeSessionList?.previewLoading) {
      messageLines.push(padToWidth(palette.text.dim("Loading session\u2026"), width));
    } else {
      messageLines.push(padToWidth(palette.text.dim("No conversation to preview"), width));
    }

    // Clip message lines to fit terminal height with scroll offset.
    // Six fixed header lines, the wrapped title/session identity, two footer
    // lines, and ~4 lines for status bar / welcome / transcript.
    const overhead = 12 + titleLines.length + sessionLines.length;
    const availableHeight = Math.max(3, this.tui.terminal.rows - overhead);
    let visibleMessages = messageLines;
    let scrollHint = "";
    if (messageLines.length > availableHeight) {
      const maxOffset = messageLines.length - availableHeight;
      // previewScrollOffset can temporarily exceed maxOffset (pageUp past bounds);
      // Math.min clamps it here so display is always correct.
      const offset = Math.min(maxOffset, this.resumeSessionList?.previewScrollOffset ?? 0);
      // start from the end of the conversation, moving backwards as offset increases
      const start = messageLines.length - availableHeight - offset;
      visibleMessages = messageLines.slice(start, start + availableHeight);
      const pct = Math.round((offset / maxOffset) * 100);
      scrollHint = `  \u2195 scroll (${pct}%)`;
    }

    return [
      padToWidth(palette.status.warning("Session preview"), width),
      ...titleLines,
      ...sessionLines,
      "",
      padToWidth(`${palette.text.dim("Project:   ")}${palette.text.primary(project)}`, width),
      "",
      padToWidth(palette.text.dim("Recent conversation"), width),
      "",
      ...visibleMessages,
      "",
      padToWidth(palette.text.dim(`Enter resume · Space/Esc back${scrollHint}`), width),
    ];
  }

  private buildStartupPromptLines(width: number): string[] {
    if (!this.startupPromptList) {
      return [];
    }
    const cwd = process.cwd();
    return [
      "",
      padToWidth(palette.status.warning("Safety Check"), width),
      "",
      padToWidth(palette.text.primary(`Current folder: ${cwd}`), width),
      "",
      padToWidth(palette.text.dim("Is this a project you created or one you trust?"), width),
      padToWidth(palette.text.dim("(e.g. your own code, well-known open source, or team project)"), width),
      padToWidth(palette.text.dim("If unfamiliar, please review the folder contents before proceeding."), width),
      "",
      ...this.startupPromptList.render(width),
      padToWidth(palette.text.dim("↑/↓ choose · Enter confirm · Esc / Ctrl+C use default workspace"), width),
    ];
  }

  private buildResumeSessionListLines(width: number): string[] {
    if (!this.resumeSessionList) {
      return [];
    }
    if (this.resumeSessionList.rename !== null) {
      const r = this.resumeSessionList.rename;
      const session = this.resumeSessionList.sessions.find((s) => s.session_id === r.sessionId);
      return this.buildResumeSessionRenameLines(width, session, r.value);
    }
    if (this.resumeSessionList.preview !== null) {
      return this.buildResumeSessionPreviewLines(width, this.resumeSessionList.preview, this.resumeSessionList.previewMessages);
    }
    const showAll = this.resumeSessionList.showAllProjects;
    const branchOn = this.resumeSessionList.branchFilterEnabled;
    const scopeLabel = showAll ? "all projects" : "current dir";
    const projectHint = showAll ? "Ctrl+A to show current dir" : "Ctrl+A to show all projects";
    const branchHint = branchOn
      ? `Ctrl+B to show all branches`
      : `Ctrl+B to filter by branch`;
    const toggleHint = `${projectHint} · ${branchHint}`;
    const scopeSuffix = branchOn ? ` · branch:${this.resumeSessionList.currentBranch}` : "";
    const searchBox = this.resumeSessionList.searchQuery
      ? padToWidth(palette.text.primary(`🔍: ${this.resumeSessionList.searchQuery}${END_CURSOR}`), width)
      : padToWidth(palette.text.dim(`🔍: Type to search · Esc to cancel`), width);
    const st = this.resumeSessionList;
    const visibleCount = computeResumeItems(st.sessions, {
      query: st.searchQuery,
      showProject: st.showAllProjects,
      branchFilter: st.branchFilterEnabled,
      currentBranch: st.currentBranch,
    }).length;
    const emptyMessage = st.searchQuery
      ? "No matches"
      : showAll
        ? "No sessions found"
        : "No sessions in current project · press Ctrl+A to search all projects";
    const listLines =
      visibleCount === 0
        ? [padToWidth(palette.text.dim(emptyMessage), width)]
        : this.resumeSessionList.list.render(width);
    return [
      padToWidth(
        palette.status.warning(
          `Resume session (${this.resumeSessionList.total} total · ${scopeLabel}${scopeSuffix})`,
        ),
        width,
      ),
      searchBox,
      ...listLines,
      padToWidth(
        palette.text.dim(
          this.resumeSessionList.searchQuery
            ? `Backspace to delete · Enter to resume · Space to preview · Ctrl+R to rename · ${toggleHint} · Esc to clear`
            : `↑/↓ to choose · Enter to resume · Space to preview · Ctrl+R to rename · ${toggleHint} · Esc to cancel`
        ),
        width,
      ),
    ];
  }

  async openModelList(openAction?: "add" | "delete"): Promise<void> {
    const snapshot = this.state.getSnapshot();
    try {
      const payload = await this.state.request<ModelListPayload>("command.model", {});
      const current = payload.current ?? "unknown";
      const modelsMeta = payload.models ?? [];
      const skipped = modelsMeta.filter((m) => isReservedMultimodalModelKey(m.name));
      const selectableWithOrigIdx = modelsMeta
        .filter((meta) => meta.name && !isReservedMultimodalModelKey(meta.name))
        .map((meta) => ({ name: meta.name, origIdx: meta.index, meta }));
      const selectable = selectableWithOrigIdx.map((entry) => entry.name);
      if (modelsMeta.length === 0) {
        this.openEmptyModelList(current, "No models configured");
        return;
      }
      if (skipped.length > 0) {
        this.state.addItem(
          addInfo(
            snapshot.sessionId,
            "video, audio, and vision are not offered as the default chat model here (multimodal-only). To configure them, use /config edit → Vision / Audio / Video, or /config set on keys such as vision_model, audio_model, video_model.",
            "m",
          ),
        );
      }
      if (selectable.length === 0) {
        this.openEmptyModelList(current, "No switchable models");
        return;
      }

      // 优先用后端 is_current 标记判断当前模型（同名模型仅靠名字无法区分），
      // 回退到 name-matching（兼容不带 is_current 的旧后端）
      const currentIdx = selectableWithOrigIdx.findIndex((entry) => {
        return entry.meta?.is_current === true;
      });
      const fallbackCurrentIdx = currentIdx < 0 ? selectable.findIndex((m) => m === current) : currentIdx;
      const nameOccurrence: Record<string, number> = {};
      const items = selectableWithOrigIdx.map((entry, i) => {
        const m = entry.name;
        const meta = entry.meta;
        const isCurrent = i === fallbackCurrentIdx;
        const seq = (nameOccurrence[m] ?? 0) + 1;
        nameOccurrence[m] = seq;
        const sameNameTotal = selectable.filter((x) => x === m).length;
        let displayName: string;
        if (sameNameTotal > 1) {
          displayName = meta?.model_name
            ? `${meta.model_name} #${seq}`
            : `${m} #${seq}`;
        } else if (meta?.model_name && meta.model_name !== m) {
          displayName = `${m} (${meta.model_name})`;
        } else {
          displayName = m;
        }
        // 仅当同名模型且 provider+api_base 也完全相同时（真正无法区分）才显示 key 末4位
        // 避免泄露过多 key 明文，且只在必要时露出尾号
        let labelSuffix = "";
        if (sameNameTotal > 1 && meta?.api_key_suffix) {
          const _mk = (mm: ModelMeta | undefined) =>
            `${mm?.model_provider ?? ""}|${mm?.api_base ?? ""}`;
          const myFingerprint = _mk(meta);
          const conflictCount = selectableWithOrigIdx.reduce((acc, ent) => {
            const xm = ent.meta;
            return xm && _mk(xm) === myFingerprint ? acc + 1 : acc;
          }, 0);
          if (conflictCount > 1) {
            labelSuffix = ` […${meta.api_key_suffix}]`;
          }
        }
        const provider = meta?.model_provider ? ` · ${meta.model_provider}` : "";
        const apiBase = meta?.api_base ? ` · ${meta.api_base}` : "";
        const reasoning = meta?.reasoning_level ? ` · reasoning:${meta.reasoning_level}` : "";
        // agentos 模型标记：仅展示用，提示用户这是手动添加的 AgentOS 模型，
        // 切换走请求级注入而非 defaults 重排 reload。
        const agentosBadge = meta?.is_agentos === true ? " [agentos]" : "";
        return {
          label: `${i + 1}. ${displayName}${agentosBadge}${labelSuffix}${isCurrent ? " (current)" : ""}`,
          description: `${provider}${apiBase}${reasoning}`.replace(/^ · /, ""),
          value: `${m}${MODEL_VALUE_SEPARATOR}${entry.origIdx}`,
        };
      });
      const list = new SelectList(items, Math.min(Math.max(items.length, 1), 8), selectListTheme, {
        minPrimaryColumnWidth: 24,
        maxPrimaryColumnWidth: 42,
      });
      if (currentIdx >= 0) {
        list.setSelectedIndex(currentIdx);
      }
      list.onSelect = (item) => {
        void this.handleModelSelection(item.value);
      };
      list.onCancel = () => {
        this.modelList = null;
        this.tui.requestRender();
      };
      this.modelList = {
        phase: "list",
        list,
        models: selectable,
        current,
        modelsMeta,
      };
      if (openAction === "add") {
        this.openModelInput("add");
      } else if (openAction === "delete") {
        const target = this.getSelectedModelTarget();
        if (target) {
          this.openModelDeleteConfirm(target);
        }
      }
      this.tui.requestRender();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.modelList = null;
      this.state.addItem(addError(snapshot.sessionId, `Failed to load models: ${message}`));
    }
  }

  private async openToolSelector(
    name: string,
    description: string,
    when_to_use: string,
    defaultPrompt: string,
    location: string,
    generate: boolean,
  ): Promise<void> {
    const snapshot = this.state.getSnapshot();
    try {
      const payload = await this.state.request<{
        tools?: Array<{ name: string; internal_name: string; description: string; group: string }>;
        groups?: string[];
        disallowed_for_subagents?: string[];
      }>("agents.tools_list", {});

      const toolDefs = payload.tools || [];
      const groupsOrder = payload.groups || [];
      const disallowed = new Set(payload.disallowed_for_subagents || []);

      // Default checked groups: 核心, 搜索, 代码智能
      const defaultCheckedGroups = new Set(["核心", "搜索", "代码智能"]);

      // Build CheckboxGroups
      const groups: CheckboxGroupType[] = [];
      const groupMap = new Map<string, Array<{ label: string; value: string; checked: boolean; description?: string }>>();

      for (const group of groupsOrder) {
        groupMap.set(group, []);
      }

      for (const t of toolDefs) {
        // Skip tools that are disallowed for subagents
        if (disallowed.has(t.name) || disallowed.has(t.internal_name)) continue;

        const items = groupMap.get(t.group) || groupMap.get("高级") || [];
        items.push({
          label: t.name,
          value: t.name,
          checked: defaultCheckedGroups.has(t.group),
          description: t.description,
        });
        groupMap.set(t.group, items);
      }

      for (const group of groupsOrder) {
        const items = groupMap.get(group) || [];
        if (items.length > 0) {
          groups.push({ name: group, items });
        }
      }

      const list = new CheckboxList(groups, 10);
      list.onSelect = (selectedTools) => {
        this.handleToolSelection(selectedTools, name, description, when_to_use, defaultPrompt, location, generate);
      };
      list.onCancel = () => {
        this.toolSelector = null;
        this.tui.requestRender();
      };

      this.toolSelector = {
        list,
        name,
        description,
        when_to_use,
        defaultPrompt,
        location,
        generate,
      };
      this.tui.requestRender();
    } catch (e) {
      this.state.addItem(
        addError(snapshot.sessionId, `获取工具列表失败: ${e}`),
      );
      this.tui.requestRender();
    }
  }

  private openEmptyModelList(current: string, emptyMessage: string): void {
    this.modelList = {
      phase: "list",
      list: null,
      models: [],
      current,
      modelsMeta: [],
      emptyMessage,
    };
    this.tui.requestRender();
  }

  private parseModelValue(modelValue: string): { modelName: string; modelIndex?: number } {
    const sepIdx = modelValue.indexOf(MODEL_VALUE_SEPARATOR);
    const modelName = sepIdx >= 0 ? modelValue.substring(0, sepIdx) : modelValue;
    const modelIndex = sepIdx >= 0 ? parseInt(modelValue.substring(sepIdx + 1), 10) : undefined;
    return { modelName, modelIndex };
  }

  private getSelectedModelTarget(): { name: string; index: number | string; value: string; isAgentos: boolean } | null {
    const selected = this.modelList?.list?.getSelectedItem();
    if (!selected) return null;
    const { modelName, modelIndex } = this.parseModelValue(selected.value);
    // agentos 条目的 index 是 "a{i}" 字符串（parseModelValue 的 parseInt 返回 NaN），
    // 此处需放行：后端按 name/alias 匹配，不靠数字 index。NaN 仅表示"非数字 index"，
    // 对 agentos 合法，不能因此返回 null 导致列表选中失败。
    // defaults 条目的 index 为纯数字，parseInt 正常返回数值。
    if (modelIndex === undefined) return null;
    const isAgentos = isNaN(modelIndex);
    // 对 defaults 数字 index 正常返回；对 agentos（NaN）用原始 value 中的 index 串
    const idx = isAgentos ? this.parseAgentosIndex(selected.value) : modelIndex;
    return { name: modelName, index: idx, value: selected.value, isAgentos };
  }

  /** 从 "modelName\x00a0" 形态抽取 agentos 的字符串 index（"a0"）。 */
  private parseAgentosIndex(modelValue: string): string {
    const sepIdx = modelValue.indexOf(MODEL_VALUE_SEPARATOR);
    return sepIdx >= 0 ? modelValue.substring(sepIdx + 1) : "";
  }

  private createModelForm(mode: "add" | "edit", target?: { index: number }): ModelFormState {
    const meta = target ? this.modelList?.modelsMeta.find((m) => m.index !== undefined && m.index === target.index) : undefined;
    const fields: Record<ModelFormField, string> = {
      model_name: mode === "edit" ? meta?.model_name ?? "" : "",
      alias: mode === "edit" ? meta?.alias ?? "" : "",
      api_base: mode === "edit" ? meta?.api_base ?? "" : "",
      api_key: "",
      model_provider: mode === "edit"
        ? meta?.model_provider ?? DEFAULT_MODEL_PROVIDER
        : DEFAULT_MODEL_PROVIDER,
      reasoning_level: mode === "edit" ? meta?.reasoning_level ?? "" : "",
    };
    return {
      fields,
      selectedField: 0,
      original: { ...fields },
    };
  }

  private openModelInput(mode: "add" | "edit", target?: { name: string; index: number | string }): void {
    if (!this.modelList) return;
    // agentos 备份模型只读：禁止通过 TUI 编辑（仅 config.yaml 手动管理）
    if (target && typeof target.index === "string" && target.index.startsWith("a")) {
      this.state.addItem(addInfo(this.state.getSnapshot().sessionId, "AgentOS models are read-only; edit them in config.yaml.", "m"));
      return;
    }
    this.editor.setText("");
    this.modelList = {
      ...this.modelList,
      phase: "input",
      inputMode: mode,
      target: target as { name: string; index: number } | undefined,
      form: this.createModelForm(mode, target as { index: number } | undefined),
    };
    this.tui.requestRender();
  }

  private openModelDeleteConfirm(target: { name: string; index: number | string }): void {
    if (!this.modelList) return;
    // agentos 备份模型只读：禁止通过 TUI 删除（仅 config.yaml 手动管理）
    if (typeof target.index === "string" && target.index.startsWith("a")) {
      this.state.addItem(addInfo(this.state.getSnapshot().sessionId, "AgentOS models are read-only; remove them in config.yaml.", "m"));
      return;
    }
    this.modelList = {
      ...this.modelList,
      phase: "delete_confirm",
      target: target as { name: string; index: number },
    };
    this.tui.requestRender();
  }

  private returnToModelList(): void {
    if (!this.modelList) return;
    this.editor.setText("");
    this.modelList = {
      ...this.modelList,
      phase: "list",
      inputMode: undefined,
      target: undefined,
      form: undefined,
    };
    this.tui.requestRender();
  }

  private handleModelFormInput(data: string): void {
    const state = this.modelList;
    const form = state?.form;
    if (!form) return;
    state.inputError = undefined;
    if (matchesKey(data, "up")) {
      form.selectedField = form.selectedField === 0 ? MODEL_FORM_FIELDS.length - 1 : form.selectedField - 1;
      return;
    }
    if (matchesKey(data, "down") || matchesKey(data, "tab")) {
      form.selectedField = form.selectedField === MODEL_FORM_FIELDS.length - 1 ? 0 : form.selectedField + 1;
      return;
    }
    const field = MODEL_FORM_FIELDS[form.selectedField];
    if (field === "reasoning_level" || field === "model_provider") {
      if (matchesKey(data, "left") || matchesKey(data, "backspace") || matchesKey(data, "delete")) {
        this.cycleModelFormOption(field, -1);
        return;
      }
      if (matchesKey(data, "right") || matchesKey(data, "space")) {
        this.cycleModelFormOption(field, 1);
        return;
      }
    }
    if (matchesKey(data, "backspace") || matchesKey(data, "delete")) {
      form.fields[field] = form.fields[field].slice(0, -1);
      return;
    }
    if (data === "\x15") {
      form.fields[field] = "";
      return;
    }
    const printableChar = this.getPrintableChar(data);
    if (printableChar !== undefined) {
      form.fields[field] += printableChar;
    }
  }

  private cycleModelFormOption(field: "model_provider" | "reasoning_level", direction: -1 | 1): void {
    const form = this.modelList?.form;
    if (!form) return;
    const options = field === "model_provider" ? MODEL_PROVIDER_OPTIONS : REASONING_LEVEL_OPTIONS;
    const current = form.fields[field].trim();
    const currentIndex = options.indexOf(current);
    const nextIndex = currentIndex >= 0
      ? (currentIndex + direction + options.length) % options.length
      : 0;
    form.fields[field] = options[nextIndex];
  }

  private modelFormConfigForSubmit(state: ModelListState): Record<string, string> {
    const form = state.form;
    if (!form) return {};
    const config: Record<string, string> = {};
    for (const field of MODEL_FORM_FIELDS) {
      const value = form.fields[field].trim();
      const configKey = field === "model_provider" ? "provider" : field;
      if (state.inputMode === "edit") {
        if (field === "api_key") {
          if (value) config[configKey] = value;
          continue;
        }
        if (field === "alias") {
          if (value !== form.original[field]) config[configKey] = value;
          continue;
        }
        if (value !== form.original[field]) config[configKey] = value;
      } else if (value) {
        config[configKey] = value;
      }
    }
    return config;
  }

  private handleModelListInput(data: string): void {
    if (!this.modelList) return;
    if (this.modelList.phase === "input") {
      if (matchesKey(data, "escape")) {
        this.returnToModelList();
        return;
      }
      if (matchesKey(data, "return")) {
        void this.submitModelInput();
        return;
      }
      this.handleModelFormInput(data);
      return;
    }
    if (this.modelList.phase === "delete_confirm") {
      if (matchesKey(data, "escape")) {
        this.returnToModelList();
        return;
      }
      if (matchesKey(data, "return")) {
        void this.submitModelDelete();
      }
      return;
    }

    const lower = data.toLowerCase();
    if (matchesKey(data, "escape")) {
      this.modelList = null;
      return;
    }
    if (lower === "a") {
      this.openModelInput("add");
      return;
    }
    if (matchesKey(data, "return")) {
      const target = this.getSelectedModelTarget();
      if (target) {
        void this.handleModelSelection(target.value);
      }
      return;
    }
    if (lower === "e") {
      const target = this.getSelectedModelTarget();
      if (target) {
        this.openModelInput("edit", target);
      }
      return;
    }
    if (lower === "d") {
      const target = this.getSelectedModelTarget();
      if (target) {
        this.openModelDeleteConfirm(target);
      }
      return;
    }
    this.modelList.list?.handleInput(data);
  }

  private async submitModelInput(): Promise<void> {
    const state = this.modelList;
    if (!state || state.phase !== "input" || !state.inputMode || !state.form) return;
    state.inputError = undefined;
    const config = this.modelFormConfigForSubmit(state);
    const validationError = this.validateModelForm(state);
    if (validationError) {
      state.inputError = validationError;
      this.tui.requestRender();
      return;
    }
    if (state.inputMode === "add") {
      try {
        await this.state.request("command.model", {
          action: "add_model",
          target: config.model_name,
          config,
        });
        this.state.addItem(addInfo(this.state.getSnapshot().sessionId, `Added model: ${config.alias || config.model_name}`, "m"));
        await this.state.refreshModelInfo();
        this.editor.setText("");
        await this.openModelList();
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        state.inputError = message.startsWith("Failed to add model")
          ? message
          : `Failed to add model: ${message}`;
        this.tui.requestRender();
      }
      return;
    }

    if (!state.target) return;
    if (Object.keys(config).length === 0) {
      this.state.addItem(addInfo(this.state.getSnapshot().sessionId, "No model changes", "m"));
      this.returnToModelList();
      return;
    }
    try {
      const payload = await this.state.request<{ type?: string }>("command.model", {
        action: "update_model",
        index: state.target.index,
        config,
      });
      if (payload.type !== "model_updated") {
        state.inputError = "Failed to edit model: backend does not support model editing yet. Restart the TUI/backend and try again.";
        this.tui.requestRender();
        return;
      }
      this.state.addItem(addInfo(this.state.getSnapshot().sessionId, `Updated model: ${state.target.name}`, "m"));
      await this.state.refreshModelInfo();
      this.editor.setText("");
      await this.openModelList();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      state.inputError = message.startsWith("Failed to edit model")
        ? message
        : `Failed to edit model: ${message}`;
      this.tui.requestRender();
    }
  }

  private validateModelForm(state: ModelListState): string | null {
    const form = state.form;
    if (!form) return null;
    const fields = form.fields;
    const trimmed = {
      model_name: fields.model_name.trim(),
      alias: fields.alias.trim(),
      api_base: fields.api_base.trim(),
      api_key: fields.api_key.trim(),
      model_provider: fields.model_provider.trim(),
      reasoning_level: fields.reasoning_level.trim(),
    };
    const missing = MODEL_REQUIRED_FIELDS
      .filter((field) => !(state.inputMode === "edit" && field === "api_key"))
      .filter((field) => !trimmed[field]);
    if (missing.length > 0) {
      return `Missing: ${missing.join(", ")}`;
    }
    if (trimmed.model_name.length > MAX_MODEL_NAME_LENGTH) {
      return `model_name must be ${MAX_MODEL_NAME_LENGTH} characters or fewer`;
    }
    if (trimmed.alias.length > MAX_ALIAS_LENGTH) {
      return `alias must be ${MAX_ALIAS_LENGTH} characters or fewer`;
    }
    if (trimmed.api_base.length > MAX_API_BASE_LENGTH) {
      return `api_base must be ${MAX_API_BASE_LENGTH} characters or fewer`;
    }
    if (trimmed.api_key.length > MAX_API_KEY_LENGTH) {
      return `api_key must be ${MAX_API_KEY_LENGTH} characters or fewer`;
    }
    if (trimmed.api_base && !/^https?:\/\//i.test(trimmed.api_base)) {
      return "api_base must start with http:// or https://";
    }
    if (!MODEL_PROVIDER_OPTIONS.includes(trimmed.model_provider)) {
      return `model_provider must be one of: ${MODEL_PROVIDER_OPTIONS.join(", ")}`;
    }
    if (!REASONING_LEVEL_OPTIONS.includes(trimmed.reasoning_level)) {
      return "reasoning_level must be default, off, low, medium, or high";
    }
    if (trimmed.alias) {
      const conflict = state.modelsMeta.find((model) => {
        if (state.inputMode === "edit" && model.index !== undefined && model.index === state.target?.index) return false;
        return (model.alias || "") === trimmed.alias || model.model_name === trimmed.alias;
      });
      if (conflict) {
        return `Alias '${trimmed.alias}' is already used by model '${conflict.model_name || conflict.name}'`;
      }
    }
    return null;
  }

  private async submitModelDelete(): Promise<void> {
    const target = this.modelList?.target;
    if (!target) return;
    try {
      // 删除前重新拉取列表并按 name 稳态标识解析当前 index，
      // 避免"确认页停留期间 defaults 被其他窗口切换重排"导致 index 漂移删错。
      // 后端 delete_model 会用 model 字段按 model_name/alias 匹配，index 仅兜底。
      // 指纹必须取自用户当时选中的条目（旧 modelsMeta 快照），而非新列表中占据旧
      // index 位置的那条——后者在重排后会指向另一条模型，导致指纹匹配删错。
      const oldMeta = this.modelList?.modelsMeta.find(
        (m) => m.index !== undefined && m.index === target.index,
      );
      const mk = (mm: ModelMeta | undefined): string =>
        `${mm?.alias ?? ""}|${mm?.model_provider ?? ""}|${mm?.api_base ?? ""}`;
      const targetFingerprint = oldMeta ? mk(oldMeta) : undefined;
      const payload = await this.state.request<ModelListPayload>("command.model", {});
      const metas = payload.models ?? [];
      // 1) 同名区分：name + alias + provider + api_base 完全相同 → 用户当时选的那一条
      // 2) 退化为纯 name 匹配（后端再按 model 字段做同名消歧与 index 校验）
      const sameName = metas.filter((m) => m.name === target.name && !m.is_agentos);
      let match: ModelMeta | undefined;
      if (sameName.length > 1) {
        // 同名多条：优先用原指纹在新列表定位（index 重排后不可靠，仅作弱提示）
        if (targetFingerprint !== undefined) {
          match = sameName.find((m) => mk(m) === targetFingerprint);
        }
        // 指纹未命中或旧快照未取到指纹：退化为原 index 匹配
        if (!match) {
          match = sameName.find((m) => m.index === target.index);
        }
      } else {
        match = sameName[0];
      }
      if (!match) {
        this.state.addItem(
          addError(
            this.state.getSnapshot().sessionId,
            `Model '${target.name}' no longer exists; the list may have changed. Refreshed.`,
          ),
        );
        await this.openModelList();
        return;
      }
      const resp = await this.state.request<{ name?: string; current?: string }>(
        "command.model",
        {
          action: "delete_model",
          index: typeof match.index === "number" ? match.index : undefined,
          model: target.name,
        },
      );
      // C: 以后端实际删除名为准（防 index 漂移后"显示 A 实删 B"的静默错位）
      this.state.addItem(
        addInfo(
          this.state.getSnapshot().sessionId,
          `Deleted model: ${resp.name ?? target.name}`,
          "m",
        ),
      );
      await this.state.refreshModelInfo();
      await this.openModelList();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.state.addItem(addError(this.state.getSnapshot().sessionId, `Failed to delete model: ${message}`));
      this.returnToModelList();
    }
  }

  private async handleModelSelection(modelValue: string): Promise<void> {
    if (!modelValue) {
      return;
    }
    // Parse "modelName\x00origIdx" format from SelectList (null separator avoids collision with model names)
    const { modelName, modelIndex } = this.parseModelValue(modelValue);

    if (isReservedMultimodalModelKey(modelName)) {
      this.modelList = null;
      this.state.addItem(
        addError(
          this.state.getSnapshot().sessionId,
          "Cannot select video, audio, or vision as the default chat model. Configure multimodal APIs in /config edit (Vision / Audio / Video) or /config set (e.g. vision_model, audio_model, video_model).",
        ),
      );
      this.tui.requestRender();
      return;
    }
    this.modelList = null;
    try {
      const reqParams: Record<string, unknown> = { model: modelName };
      if (modelIndex !== undefined && !isNaN(modelIndex)) {
        reqParams.index = modelIndex;
      }
      const payload = await this.state.request<{
        current?: string;
        model_key?: string;
        requested?: string;
        applied?: boolean;
        type?: string;
        is_agentos?: boolean;
        provider?: string;
      }>("command.model", reqParams);
      // agentos 备份模型：后端走"请求级注入"路径，回包 type=switched_agentos。
      // 不改 config、不抢启动默认，仅全局记录选中名，后续 chat.send 注入 model_name。
      // model_key（model_name#global_idx）用于精确注入同名 agentos 条目，避免
      // 纯名被后端解析到同名 defaults 首条；current 为纯名仅作展示。
      // defaults 模型仍走 setModel（更新当前模型回显 + config reload）。
      if (payload.type === "switched_agentos" || payload.is_agentos === true) {
        const agentosName = payload.current ?? modelName;
        this.state.setSelectedAgentosModel(agentosName, payload.provider, payload.model_key);
        this.state.addItem(
          addInfo(
            this.state.getSnapshot().sessionId,
            `Switched to agentos model (request-level): ${agentosName}`,
            "m",
          ),
        );
        this.tui.requestRender();
        return;
      }
      const nextModel = payload.current ?? modelName;
      this.state.setModel(nextModel);
      this.state.addItem(
        addInfo(this.state.getSnapshot().sessionId, `Switched model to: ${nextModel}`, "m"),
      );
      this.tui.requestRender();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.state.addItem(
        addError(this.state.getSnapshot().sessionId, `Failed to switch model: ${message}`),
      );
      this.tui.requestRender();
    }
  }

  private async handleToolSelection(
    selectedTools: string[],
    name: string,
    description: string,
    when_to_use: string,
    defaultPrompt: string,
    location: string,
    generate: boolean,
  ): Promise<void> {
    this.toolSelector = null;
    this.tui.requestRender();

    try {
      const payload = await this.state.request<{
        agent?: { file_path?: string };
        error?: string;
        generated?: boolean;
        applied?: boolean;
        reload_error?: string | null;
      }>(
        "agents.create",
        {
          name,
          description,
          when_to_use,
          prompt: defaultPrompt,
          location,
          tools: selectedTools.length > 0 ? selectedTools : ["*"],
          generate,
        },
        60000,
      );

      const snapshot = this.state.getSnapshot();
      if (payload.error) {
        this.state.addItem(
          addError(snapshot.sessionId, `创建失败: ${payload.error}`),
        );
      } else {
        const generated = payload.generated ? " (LLM 生成)" : "";
        const locLabel = location !== "user" ? ` (${location})` : "";
        const toolsLabel = selectedTools.length > 0 ? ` | 工具: ${selectedTools.join(", ")}` : " | 工具: *";
        this.state.addItem(
          addInfo(
            snapshot.sessionId,
            `Agent 已创建: ${name}${generated}${locLabel}${toolsLabel}\n文件: ${payload.agent?.file_path ?? `~/.jiuwenswarm/agents/${name}.md`}\n使用 /agents get ${name} 查看详情`,
          ),
        );
      }
    } catch (e) {
      this.state.addItem(
        addError(this.state.getSnapshot().sessionId, `创建失败: ${e}`),
      );
    }
    this.tui.requestRender();
  }

  private buildModelListLines(width: number): string[] {
    if (!this.modelList) {
      return [];
    }
    if (this.modelList.phase === "input") {
      const isAdd = this.modelList.inputMode === "add";
      const targetName = this.modelList.target?.name ?? "";
      const title = isAdd ? "Add model" : `Edit model: ${targetName}`;
      const hint = "  ↑/↓ field · type value · ←/→ option · Ctrl+U clear · Enter save · Esc back";
      return [
        padToWidth(palette.status.warning(title), width),
        padToWidth(palette.text.dim(hint), width),
        ...this.buildModelFormLines(width),
      ];
    }
    if (this.modelList.phase === "delete_confirm") {
      return [
        padToWidth(palette.status.error(`Delete model: ${this.modelList.target?.name ?? "unknown"}`), width),
        padToWidth(palette.text.dim("Enter confirm · Esc cancel"), width),
      ];
    }
    const listLines = this.modelList.list
      ? this.modelList.list.render(width)
      : [padToWidth(palette.text.dim(this.modelList.emptyMessage ?? "No models configured"), width)];
    const hint = this.modelList.models.length > 0
      ? "↑/↓ choose · Enter switch · a add · e edit · d delete · Esc close"
      : "a add · Esc close";
    return [
      padToWidth(
        palette.status.warning(`Available models (${this.modelList.models.length} total)`),
        width,
      ),
      ...listLines,
      padToWidth(palette.text.dim(hint), width),
    ];
  }

  private buildModelFormLines(width: number): string[] {
    const state = this.modelList;
    const form = state?.form;
    if (!state || !form) return [];
    const activeField = MODEL_FORM_FIELDS[form.selectedField];
    const activeValue = this.formatModelFormValue(activeField, form.fields[activeField], state.inputMode);
    const activeHint = activeField === "reasoning_level"
      ? "    ←/→ default, off, low, medium, high"
      : activeField === "model_provider"
        ? `    ←/→ ${MODEL_PROVIDER_OPTIONS.join(", ")}`
        : "";
    const lines = [
      padToWidth(`${palette.text.primary(`${activeField}: ${activeValue}${END_CURSOR}`)}${palette.text.dim(activeHint)}`, width),
    ];
    if (state.inputError) {
      lines.push(padToWidth(palette.status.error(`! ${state.inputError}`), width));
    }
    for (const [index, field] of MODEL_FORM_FIELDS.entries()) {
      const selected = index === form.selectedField;
      const required = MODEL_REQUIRED_FIELDS.includes(field) && !(state.inputMode === "edit" && field === "api_key");
      const label = `${selected ? "> " : "  "}${field}${required ? " *" : ""}`;
      const value = this.formatModelFormValue(field, form.fields[field], state.inputMode);
      const line = `${label.padEnd(24, " ")}${value}`;
      lines.push(padToWidth(selected ? palette.text.primary(line) : palette.text.dim(line), width));
    }
    return lines;
  }

  private formatModelFormValue(field: ModelFormField, rawValue: string, mode?: "add" | "edit"): string {
    if (field === "reasoning_level" && !rawValue) {
      return "<default>";
    }
    if (field !== "api_key") {
      return rawValue;
    }
    if (rawValue) {
      return "*".repeat(Math.min(rawValue.length, 12));
    }
    return mode === "edit" ? "<unchanged>" : "";
  }

  private buildToolSelectorLines(width: number): string[] {
    if (this.toolSelector === null) return [];
    return this.toolSelector.list.render(width);
  }

  private async openMcpList(): Promise<void> {
    const snapshot = this.state.getSnapshot();
    try {
      const payload = await this.state.request<McpListPayload>("command.mcp", { action: "list" });
      const items = payload.items ?? [];
      if (items.length === 0) {
        this.mcpList = null;
        this.state.addItem(addInfo(snapshot.sessionId, "No MCP servers configured", "m"));
        return;
      }

      const selectItems: SelectItem[] = items.map((x) => ({
        label: `${x.name} | ${x.transport}${x.enabled ? " · ✔ enabled" : " · ○ disabled"}`,
        value: x.name,
      }));
      const list = new SelectList(
        selectItems,
        Math.min(Math.max(selectItems.length, 1), 8),
        selectListTheme,
        { minPrimaryColumnWidth: 24, maxPrimaryColumnWidth: 42 },
      );
      list.onSelect = (item) => {
        void this.handleMcpSelection(item.value);
      };
      list.onCancel = () => {
        this.mcpList = null;
        this.tui.requestRender();
      };
      this.mcpList = { list, items };
      this.tui.requestRender();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.mcpList = null;
      this.state.addItem(addError(snapshot.sessionId, `mcp list failed: ${message}`));
    }
  }

  private async handleMcpSelection(serverName: string): Promise<void> {
    const snapshot = this.state.getSnapshot();
    try {
      const payload = await this.state.request<{
        type: string;
        item?: Record<string, unknown>;
      }>("command.mcp", { action: "show", name: serverName });
      if (payload.type === "detail" && payload.item) {
        const enabled = Boolean(payload.item.enabled !== false);
        const actionItems: SelectItem[] = [];
        actionItems.push({ label: "View tools", value: "view_tools", description: "Browse tools from this server" });
        if (enabled) {
          actionItems.push({ label: "Disable", value: "disable", description: "Stop and disable this server" });
        } else {
          actionItems.push({ label: "Enable", value: "enable", description: "Enable this server" });
        }
        actionItems.push({ label: "Remove", value: "remove", description: "Remove this server from config" });
        const actionsList = new SelectList(actionItems, actionItems.length, selectListTheme, {
          minPrimaryColumnWidth: 24,
          maxPrimaryColumnWidth: 42,
        });
        actionsList.onSelect = (item) => {
          void this.handleMcpDetailAction(serverName, item.value);
        };
        actionsList.onCancel = () => {
          this.mcpDetail = null;
          this.openMcpList();
        };
        this.mcpList = null;
        this.mcpDetail = {
          serverName,
          info: payload.item,
          enabled,
          actions: actionsList,
        };
        this.tui.requestRender();
      } else {
        this.mcpList = null;
        this.state.addItem(addError(snapshot.sessionId, `MCP server '${serverName}' not found`));
        this.tui.requestRender();
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.mcpList = null;
      this.state.addItem(addError(snapshot.sessionId, `mcp show failed: ${message}`));
      this.tui.requestRender();
    }
  }

  private async handleMcpDetailAction(serverName: string, action: string): Promise<void> {
    const snapshot = this.state.getSnapshot();
    try {
      if (action === "view_tools") {
        await this.openMcpToolsList(serverName);
        return;
      }
      if (action === "enable" || action === "disable" || action === "remove") {
        await this.state.request("command.mcp", { action, name: serverName });
        this.mcpDetail = null;
        if (action === "remove") {
          this.state.addItem(addInfo(snapshot.sessionId, `MCP server removed: ${serverName}`, "m"));
          this.tui.requestRender();
        } else {
          // After enable/disable, reopen the MCP list to show updated status
          await this.openMcpList();
        }
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.mcpDetail = null;
      this.state.addItem(addError(snapshot.sessionId, `mcp ${action} failed: ${message}`));
      this.tui.requestRender();
    }
  }

  private buildMcpListLines(width: number): string[] {
    if (!this.mcpList) return [];
    return [
      padToWidth(palette.status.warning(`MCP servers (${this.mcpList.items.length})`), width),
      ...this.mcpList.list.render(width),
      padToWidth(palette.text.dim("↑/↓ choose · Enter show detail · Esc cancel"), width),
    ];
  }

  private buildMcpDetailLines(width: number): string[] {
    if (!this.mcpDetail) return [];
    const { serverName, info, enabled, actions } = this.mcpDetail;
    const lines: string[] = [];

    const borderFn = palette.border.panel;
    const borderV = "│";
    // Layout: " " + "│" + " " + content + " " + "│" = 6 extra chars
    const contentWidth = Math.max(1, width - 6);
    const innerWidth = contentWidth + 2;

    // Collect all boxed lines: title, detail fields, separator, actions
    const boxedLines: string[] = [];

    // Title line
    boxedLines.push(padToWidth(palette.status.warning(`MCP Server: ${serverName}`), contentWidth));

    // Detail fields
    boxedLines.push(padToWidth(
      `  Status: ${enabled ? palette.status.success("✔ enabled") : palette.text.dim("○ disabled")}`,
      contentWidth,
    ));
    if (info.transport) {
      boxedLines.push(padToWidth(palette.text.dim(`  Transport: ${String(info.transport)}`), contentWidth));
    }
    if (info.command) {
      boxedLines.push(padToWidth(palette.text.dim(`  Command: ${String(info.command)}`), contentWidth));
    }
    if (typeof info.tool_count === "number") {
      boxedLines.push(padToWidth(palette.text.dim(`  Tools: ${info.tool_count} tool${info.tool_count === 1 ? "" : "s"}`), contentWidth));
    }
    if (info.args) {
      const argsStr = Array.isArray(info.args) ? info.args.join(" ") : String(info.args);
      boxedLines.push(padToWidth(palette.text.dim(`  Args: ${argsStr}`), contentWidth));
    }
    if (info.url) {
      boxedLines.push(padToWidth(palette.text.dim(`  URL: ${String(info.url)}`), contentWidth));
    }
    if (info.timeout_s) {
      boxedLines.push(padToWidth(palette.text.dim(`  Timeout: ${String(info.timeout_s)}s`), contentWidth));
    }

    // Blank separator line
    boxedLines.push(padToWidth("", contentWidth));

    // Actions rendered inside the box
    const actionLines = actions.render(contentWidth);
    boxedLines.push(...actionLines);

    // Top border
    lines.push(" " + borderFn("╭" + "─".repeat(innerWidth) + "╮"));
    // Boxed content
    for (const bl of boxedLines) {
      lines.push(" " + borderFn(borderV) + " " + padToWidth(bl, contentWidth) + " " + borderFn(borderV));
    }
    // Bottom border
    lines.push(" " + borderFn("╰" + "─".repeat(innerWidth) + "╯"));

    lines.push(padToWidth(palette.text.dim("↑/↓ choose · Enter select · Esc back"), width));
    return lines;
  }

  private async openMcpToolsList(serverName: string): Promise<void> {
    const snapshot = this.state.getSnapshot();
    try {
      const payload = await this.state.request<{
        type: string;
        tools: McpToolItem[];
        server_name: string;
      }>("command.mcp", { action: "list_tools", name: serverName });
      const tools = payload.tools ?? [];
      const toolItems: SelectItem[] = tools.map((t) => ({
        label: t.name,
        value: t.id,
        description: t.description ? (t.description.length > 60 ? t.description.slice(0, 57) + "..." : t.description) : "",
      }));
      const list = new SelectList(toolItems, Math.min(Math.max(toolItems.length, 1), 10), selectListTheme, {
        minPrimaryColumnWidth: 24,
        maxPrimaryColumnWidth: 50,
      });
      list.onSelect = (item) => {
        const tool = tools.find((t) => t.id === item.value);
        if (tool) {
          this.mcpToolDetail = { serverName, tool };
          this.tui.requestRender();
        }
      };
      list.onCancel = () => {
        this.mcpTools = null;
        void this.handleMcpSelection(serverName);
      };
      this.mcpDetail = null;
      this.mcpTools = { serverName, tools, list };
      this.tui.requestRender();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.mcpDetail = null;
      this.state.addItem(addError(snapshot.sessionId, `mcp list_tools failed: ${message}`));
      this.tui.requestRender();
    }
  }

  private buildMcpToolsLines(width: number): string[] {
    if (!this.mcpTools || this.mcpToolDetail) return [];
    const { serverName, tools, list } = this.mcpTools;
    const lines: string[] = [];

    const borderFn = palette.border.panel;
    const borderV = "│";
    const contentWidth = Math.max(1, width - 6);
    const innerWidth = contentWidth + 2;

    const boxedLines: string[] = [];
    boxedLines.push(padToWidth(palette.status.warning(`Tools for ${serverName} (${tools.length} tool${tools.length === 1 ? "" : "s"})`), contentWidth));

    if (tools.length === 0) {
      boxedLines.push(padToWidth(palette.text.dim("  No tools available. Enable the server first."), contentWidth));
    } else {
      const listLines = list.render(contentWidth);
      boxedLines.push(...listLines);
    }

    // Top border
    lines.push(" " + borderFn("╭" + "─".repeat(innerWidth) + "╮"));
    for (const bl of boxedLines) {
      lines.push(" " + borderFn(borderV) + " " + padToWidth(bl, contentWidth) + " " + borderFn(borderV));
    }
    lines.push(" " + borderFn("╰" + "─".repeat(innerWidth) + "╯"));

    lines.push(padToWidth(palette.text.dim("↑/↓ choose · Enter view detail · Esc back"), width));
    return lines;
  }

  private buildMcpToolDetailLines(width: number): string[] {
    if (!this.mcpToolDetail) return [];
    const { serverName, tool } = this.mcpToolDetail;
    const lines: string[] = [];

    const borderFn = palette.border.panel;
    const borderV = "│";
    const contentWidth = Math.max(1, width - 6);
    const innerWidth = contentWidth + 2;

    const boxedLines: string[] = [];

    // Title: toolname (serverName)
    boxedLines.push(padToWidth(palette.status.warning(`${tool.name} (${serverName})`), contentWidth));

    // Tool name / Full name
    boxedLines.push(padToWidth("", contentWidth));
    boxedLines.push(padToWidth(`Tool name: ${tool.name}`, contentWidth));
    boxedLines.push(padToWidth(`Full name: mcp__${serverName}__${tool.name}`, contentWidth));

    // Description
    if (tool.description) {
      boxedLines.push(padToWidth("", contentWidth));
      boxedLines.push(padToWidth("Description:", contentWidth));
      const descLines = wrapText(tool.description, contentWidth - 2);
      for (const dl of descLines) {
        boxedLines.push(padToWidth(`  ${dl}`, contentWidth));
      }
    }

    // Parameters
    if (tool.parameters && typeof tool.parameters === "object") {
      const params = tool.parameters as Record<string, unknown>;
      const properties = params.properties as Record<string, unknown> | undefined;
      if (properties && Object.keys(properties).length > 0) {
        boxedLines.push(padToWidth("", contentWidth));
        boxedLines.push(padToWidth("Parameters:", contentWidth));
        for (const [paramName, paramDef] of Object.entries(properties)) {
          const def = paramDef as Record<string, unknown>;
          const typeStr = def.type ? String(def.type) : "any";
          const required = Array.isArray(params.required) && params.required.includes(paramName);
          const reqMark = required ? " (required)" : "";
          const descText = def.description ? ` - ${String(def.description)}` : "";
          const paramLine = `  • ${paramName}${reqMark}: ${typeStr}${descText}`;
          const paramLines = wrapText(paramLine, contentWidth - 2);
          for (const pl of paramLines) {
            boxedLines.push(padToWidth(pl, contentWidth));
          }
        }
      }
    }

    // Top border
    lines.push(" " + borderFn("╭" + "─".repeat(innerWidth) + "╮"));
    for (const bl of boxedLines) {
      lines.push(" " + borderFn(borderV) + " " + padToWidth(bl, contentWidth) + " " + borderFn(borderV));
    }
    lines.push(" " + borderFn("╰" + "─".repeat(innerWidth) + "╯"));

    lines.push(padToWidth(palette.text.dim("Esc to go back"), width));
    return lines;
  }

  private openThemeList(): void {
    const snapshot = this.state.getSnapshot();
    const current = snapshot.themeName ?? "dark";
    const options: readonly ["dark", "light"] = ["dark", "light"];
    const items: SelectItem[] = options.map((theme) => ({
      value: theme,
      label: theme === current ? `${theme} ✔` : theme,
    }));
    const list = new SelectList(items, Math.min(Math.max(items.length, 1), 8), selectListTheme, {
      minPrimaryColumnWidth: 24,
      maxPrimaryColumnWidth: 42,
    });
    list.onSelect = (item) => {
      this.themeList = null;
      this.state.setThemeName(item.value as "dark" | "light");
      this.state.addItem(
        addInfo(this.state.getSnapshot().sessionId, `Theme set to ${item.value}`, "t"),
      );
      this.tui.requestRender();
    };
    list.onCancel = () => {
      this.themeList = null;
      this.tui.requestRender();
    };
    this.themeList = { list, current };
    this.tui.requestRender();
  }

  private buildThemeListLines(width: number): string[] {
    if (!this.themeList) {
      return [];
    }
    return [
      padToWidth(palette.status.warning("Theme"), width),
      ...this.themeList.list.render(width),
      padToWidth(palette.text.dim("↑/↓ choose · Enter to select · Esc to cancel"), width),
    ];
  }

  private swarmActionKeyLabel(
    action: "swarm:budget" | "swarm:pauseResume" | "swarm:stop",
  ): string {
    const key = getContextBindings("SwarmWorkflows").find(
      (binding) => binding.action === action,
    )?.key;
    if (!key) {
      return action === "swarm:budget" ? "B" : action === "swarm:pauseResume" ? "P" : "S";
    }
    // Shifted single letters display as uppercase (shift+b -> B, shift+p -> P, shift+s -> S).
    if (key.startsWith("shift+")) {
      const base = key.slice("shift+".length);
      if (base.length === 1) return base.toUpperCase();
    }
    return key;
  }

  private sendSwarmWorkflowControl(
    workflowId: string,
    action: "pause" | "resume" | "stop",
  ): void {
    const snapshot = this.state.getSnapshot();
    this.state.sendEventOnly(`swarmflow.${action}`, {
      session_id: snapshot.sessionId,
      run_id: workflowId,
    });
  }

  private globalActionKeyLabel(action: "app:viewHumanInputs"): string | null {
    return getContextBindings("Global").find(
      (binding) => binding.action === action,
    )?.key ?? null;
  }

  private workflowIdForBudgetView(): string | null {
    const state = this.swarmWorkflowsViewState;
    if (!state) return null;
    if (state.phase === "list") return state.list.getSelectedItem()?.value ?? null;
    if (state.phase === "pending-list") {
      return this.getSelectedPendingListWaiting(state)?.workflowId ?? null;
    }
    return state.workflowId;
  }

  private workflowBudgetViewerContent(workflow: WorkflowRun): string {
    const budget = workflow.budget;
    const runBudget = workflow.workflow_budget;
    if (!budget && !runBudget) return "Budget data unavailable";

    const lines: string[] = [];
    if (budget) {
      const spent = formatTokenCount(budget.spent) ?? "—";
      const total = formatTokenCount(budget.total);
      const remaining = formatTokenCount(budget.remaining);
      const usedPercent = workflowBudgetUsedPercent(budget);
      const low = isWorkflowBudgetLow(budget);
      lines.push(
        "Team budget",
        "",
        "Scope       Session shared",
        `Spent       ${spent}`,
        total ? `Total       ${total}` : "Limit       Unbounded",
      );
      if (total) {
        const remainingLine = `Remaining   ${remaining ?? "—"}`;
        const usedLine = `Used        ${usedPercent === null ? "—" : `${usedPercent}%`}`;
        lines.push(low ? palette.status.warning(`⚠ ${remainingLine}`) : remainingLine);
        lines.push(low ? palette.status.warning(`⚠ ${usedLine}`) : usedLine);
      }
    }
    if (runBudget) {
      const spent = formatTokenCount(runBudget.spent) ?? "—";
      const total = formatTokenCount(runBudget.total);
      const remaining = formatTokenCount(runBudget.remaining);
      const usedPercent = workflowBudgetUsedPercent(runBudget);
      const low = isWorkflowBudgetLow(runBudget);
      if (lines.length > 0) lines.push("");
      lines.push(
        "Run budget (this invocation)",
        "",
        "Scope       Workflow (META.workflow_token_limit)",
        `Spent       ${spent}`,
        total ? `Total       ${total}` : "Limit       Unbounded",
      );
      if (total) {
        const remainingLine = `Remaining   ${remaining ?? "—"}`;
        const usedLine = `Used        ${usedPercent === null ? "—" : `${usedPercent}%`}`;
        lines.push(low ? palette.status.warning(`⚠ ${remainingLine}`) : remainingLine);
        lines.push(low ? palette.status.warning(`⚠ ${usedLine}`) : usedLine);
      }
    }
    const exhaustedScope = workflowBudgetExhaustedScope(workflow);
    if (exhaustedScope === "workflow") {
      lines.push(
        "",
        palette.status.error("Run budget exhausted"),
        palette.status.error(
          "Revise the workflow (or raise META.workflow_token_limit) and relaunch.",
        ),
      );
    } else if (exhaustedScope === "session") {
      lines.push(
        "",
        palette.status.error("Team budget exhausted"),
        palette.status.error("This workflow cannot be resumed."),
      );
    }
    return lines.join("\n");
  }

  private async openSwarmWorkflowBudget(
    workflowId = this.workflowIdForBudgetView(),
  ): Promise<void> {
    if (!workflowId) return;
    let workflow = this.state.getSnapshot().workflowRuns.find((item) => item.id === workflowId);
    if (workflow && !workflow.budget && !workflow.workflow_budget) {
      try {
        await this.state.loadWorkflowDetail(workflowId);
      } catch {
        // The read-only viewer below provides the unavailable state.
      }
      workflow = this.state.getSnapshot().workflowRuns.find((item) => item.id === workflowId);
    }
    if (!workflow) return;
    this.enterFileViewer(
      this.workflowBudgetViewerContent(workflow),
      `Budget - ${workflow.name}`,
      workflow.name,
    );
  }

  private maybeOpenCurrentWorkflowBudgetExhausted(): void {
    const state = this.swarmWorkflowsViewState;
    if (!state || state.phase === "list" || state.phase === "pending-list") return;
    const workflow = this.state
      .getSnapshot()
      .workflowRuns.find((item) => item.id === state.workflowId);
    const exhaustedScope = workflow ? workflowBudgetExhaustedScope(workflow) : null;
    if (!workflow || exhaustedScope === null) return;
    const key = `${this.workflowUiSessionId}:${workflow.id}`;
    if (this.shownBudgetExhaustedWorkflowKeys.has(key)) return;
    this.shownBudgetExhaustedWorkflowKeys.add(key);
    this.enterFileViewer(
      this.workflowBudgetViewerContent(workflow),
      `${exhaustedScope === "workflow" ? "Run" : "Team"} budget exhausted - ${workflow.name}`,
      workflow.name,
    );
  }

  private async openSwarmWorkflowsView(): Promise<void> {
    const selectedWorkflowId =
      this.swarmWorkflowsViewState?.phase === "list"
        ? this.swarmWorkflowsViewState.list.getSelectedItem()?.value
        : this.swarmWorkflowsViewState?.phase === "workflow" ||
            this.swarmWorkflowsViewState?.phase === "agent"
          ? this.swarmWorkflowsViewState.workflowId
          : undefined;
    this.swarmWorkflowsViewState = this.buildSwarmWorkflowsListState(true, selectedWorkflowId);
    this.tui.requestRender();
    try {
      await this.state.loadWorkflowSnapshot();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.state.addItem(
        addError(this.state.getSnapshot().sessionId, `workflow list failed: ${message}`),
      );
    }
    this.swarmWorkflowsViewState = this.buildSwarmWorkflowsListState(false, selectedWorkflowId);
    this.tui.requestRender();
  }

  private async enterSwarmWorkflowsView(): Promise<void> {
    this.state.beginDeferredTranscript();
    this.transcriptScrollOffset = 0;
    await this.openSwarmWorkflowsView();
  }

  private async enterSwarmWorkflowsPendingList(): Promise<void> {
    this.state.beginDeferredTranscript();
    this.transcriptScrollOffset = 0;
    this.swarmWorkflowsViewState = this.buildPendingListState("chat");
    this.tui.requestRender();
    try {
      await this.state.loadWorkflowSnapshot();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.state.addItem(
        addError(this.state.getSnapshot().sessionId, `workflow list failed: ${message}`),
      );
    }
    const detailIds = new Set<string>();
    for (const pending of this.state.getSnapshot().pendingHumanPrompts?.values() ?? []) {
      detailIds.add(pending.workflow_run_id);
    }
    for (const wf of this.state.getSnapshot().workflowRuns) {
      for (const phase of wf.phases ?? []) {
        for (const agent of phase.agents ?? []) {
          if (agent.status === "waiting_for_human" && agent.kind === "human") {
            detailIds.add(wf.id);
          }
        }
      }
    }
    await Promise.all(
      [...detailIds].map((workflowId) =>
        this.state.loadWorkflowDetail(workflowId).catch(() => undefined),
      ),
    );
    // After get_workflow, phases carry summaries only — fetch agents per phase
    // to surface waiting-for-human nodes (list snapshot had no phases/agents).
    const phaseAgentLoads: Array<Promise<void>> = [];
    for (const wf of this.state.getSnapshot().workflowRuns) {
      if (!detailIds.has(wf.id)) continue;
      for (const phase of wf.phases ?? []) {
        if (Array.isArray(phase.agents) && phase.agents.length > 0) continue;
        phaseAgentLoads.push(
          this.state.loadPhaseAgents(wf.id, phase.id).catch(() => undefined),
        );
      }
    }
    await Promise.all(phaseAgentLoads);
    const promptLoads: Array<Promise<void>> = [];
    for (const wf of this.state.getSnapshot().workflowRuns) {
      for (const phase of wf.phases ?? []) {
        for (const agent of phase.agents ?? []) {
          if (agent.status === "waiting_for_human" && agent.kind === "human") {
            promptLoads.push(this.state.ensureHumanPromptLoaded(wf.id, agent.id));
          }
        }
      }
    }
    await Promise.all(promptLoads.map((task) => task.catch(() => undefined)));
    this.swarmWorkflowsViewState = this.buildPendingListState("chat");
    this.tui.requestRender();
  }

  private lookupPendingHumanPrompt(workflowId: string, agentId: string) {
    const lookup = findWorkflowAgent(this.state.getSnapshot().workflowRuns, workflowId, agentId);
    const corr = lookup?.agent.correlation_id ?? agentId;
    return this.state.getSnapshot().pendingHumanPrompts?.get(`${workflowId}:${corr}`);
  }

  private getHumanQuestionText(workflowId: string, agentId: string): string {
    const lookup = findWorkflowAgent(this.state.getSnapshot().workflowRuns, workflowId, agentId);
    if (lookup && isHumanTurnCached(lookup.agent)) {
      return HUMAN_TURN_CACHED_QUESTION;
    }
    if (lookup?.agent.human_prompt?.trim()) {
      return lookup.agent.human_prompt.trim();
    }
    const pending = this.lookupPendingHumanPrompt(workflowId, agentId);
    if (pending?.prompt?.trim()) {
      return pending.prompt.trim();
    }
    return "";
  }

  private async openAgentDetailFromPendingList(
    waiting: Extract<PendingListEntry, { kind: "waiting" }>,
    previous_phase: PendingListPreviousPhase,
  ): Promise<void> {
    this.replyingToHumanPrompt = null;
    this.editor.setText("");
    this.editor.focused = false;
    try {
      await this.state.loadWorkflowDetail(waiting.workflowId);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.state.addItem(
        addError(this.state.getSnapshot().sessionId, `workflow detail failed: ${message}`),
      );
    }
    this.swarmWorkflowsViewState = {
      phase: "agent",
      workflowId: waiting.workflowId,
      agentId: waiting.agentId,
      returnTo: { kind: "pending-list", previous_phase },
    };
    this.tui.requestRender();
  }

  private async reloadSwarmWorkflowsKeepingView(): Promise<void> {
    const current = this.swarmWorkflowsViewState;
    try {
      await this.state.loadWorkflowSnapshot();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.state.addItem(
        addError(this.state.getSnapshot().sessionId, `workflow refresh failed: ${message}`),
      );
    }
    if (current?.phase === "pending-list") {
      const detailIds = new Set<string>();
      for (const entry of current.entries) {
        if (entry.kind === "waiting") detailIds.add(entry.workflowId);
      }
      await Promise.all(
        [...detailIds].map((id) => this.state.loadWorkflowDetail(id).catch(() => undefined)),
      );
    } else if (
      current?.phase === "workflow" ||
      current?.phase === "agent" ||
      current?.phase === "session-detail"
    ) {
      try {
        await this.state.loadWorkflowDetail(current.workflowId);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        this.state.addItem(
          addError(this.state.getSnapshot().sessionId, `workflow detail refresh failed: ${message}`),
        );
      }
    }
    this.refreshSwarmWorkflowsView();
    this.tui.requestRender();
  }

  private closeSwarmWorkflowsView(): void {
    if (!this.swarmWorkflowsViewState) return;
    this.swarmWorkflowsViewState = null;
    this.lastRepliedHumanPrompt = null;
    this.state.flushDeferredTranscript();
    this.tui.requestRender();
  }

  private refreshSwarmWorkflowsView(): void {
    const current = this.swarmWorkflowsViewState;
    if (!current) return;
    if (current.phase === "list") {
      this.swarmWorkflowsViewState = this.buildSwarmWorkflowsListState(
        false,
        current.list.getSelectedItem()?.value,
      );
      return;
    }
    if (current.phase === "pending-list") {
      // The pending list exists solely to collect waiting-for-human turns.
      // Once there are none left — typically because the user just submitted a
      // reply and the backend resumed (or the run completed) — return to the
      // previous screen instead of stranding the user on an empty "No pending
      // replies" page that only Esc can leave.
      const snapshot = this.state.getSnapshot();
      const stillWaiting = snapshot.workflowRuns.some(
        (wf) => countWaitingForHuman(wf) > 0,
      );
      if (!stillWaiting) {
        this.restoreFromPendingList(current.previous_phase);
        return;
      }
      this.swarmWorkflowsViewState = this.buildPendingListState(
        current.previous_phase ?? "list",
        current.selectedIndex,
      );
      return;
    }
    if (current.phase === "workflow") {
      const selectedAgentId = current.agentList.getSelectedItem()?.value;
      this.swarmWorkflowsViewState = this.buildSwarmWorkflowDetailState(
        current.workflowId,
        current.selectedPhaseId,
        current.focus,
        selectedAgentId,
      );
      return;
    }
    if (current.phase === "session-detail") {
      // A submitted turn may complete while the workflow immediately opens its
      // next human turn. Leave this history view as soon as the exact replied
      // turn stops waiting, returning the user to chat where the new pending
      // input indicator is visible. Merely browsing completed history does not
      // trigger this path because no submitted-turn marker is present.
      const replied = this.lastRepliedHumanPrompt;
      if (replied?.workflowRunId === current.workflowId) {
        const agentId = this.findAgentIdForHumanReply(
          replied.workflowRunId,
          replied.correlationId,
        );
        const lookup = findWorkflowAgent(
          this.state.getSnapshot().workflowRuns,
          replied.workflowRunId,
          agentId,
        );
        if (lookup && lookup.agent.status !== "waiting_for_human") {
          this.closeSwarmWorkflowsView();
          return;
        }
      }
      // Re-render only — buildSessionDetailLines reads live workflow snapshot.
      return;
    }
    // current.phase === "agent"
    const lookup = findWorkflowAgent(
      this.state.getSnapshot().workflowRuns,
      current.workflowId,
      current.agentId,
    );
    if (!lookup) {
      this.swarmWorkflowsViewState = this.buildSwarmWorkflowsListState(false, current.workflowId);
    } else {
      this.swarmWorkflowsViewState = {
        phase: "agent",
        workflowId: lookup.workflow.id,
        agentId: lookup.agent.id,
        returnTo: current.returnTo,
      };
    }
  }

  private buildSwarmWorkflowsListState(
    loading = false,
    selectedWorkflowId?: string,
  ): SwarmWorkflowsViewState {
    const workflows = this.state.getSnapshot().workflowRuns;
    const items: SelectItem[] = workflows.map((workflow) => {
      const total = workflow.agent_count ?? 0;
      const completed = workflow.completed_agent_count ?? 0;
      const progress = workflow.status === "running" ? `${completed}/${total}` : `${total}`;
      const tokens = formatTokenCount(workflow.token_count);
      const budget = formatWorkflowBudgetInline(workflow.budget);
      const runBudget = formatWorkflowRunBudgetInline(workflow.workflow_budget);
      const description = [`${progress} agents`];
      if (tokens) description.push(`${tokens} tok`);
      if (budget) {
        description.push(
          workflowBudgetExhaustedScope(workflow) === "session"
            ? palette.status.error(budget)
            : isWorkflowBudgetLow(workflow.budget)
              ? palette.status.warning(budget)
              : budget,
        );
      }
      if (runBudget) {
        description.push(
          workflow.workflow_budget?.exhausted === true
            ? palette.status.error(runBudget)
            : isWorkflowBudgetLow(workflow.workflow_budget)
              ? palette.status.warning(runBudget)
              : runBudget,
        );
      }
      return {
        value: workflow.id,
        label: `${formatSwarmWorkflowListStatus(workflow.status)} ${workflow.name}`,
        description: description.join(" · "),
      };
    });
    const list = new SelectList(items, Math.min(Math.max(items.length, 1), 8), selectListTheme, {
      minPrimaryColumnWidth: 24,
      maxPrimaryColumnWidth: 42,
    });
    const selectedWorkflowIndex = selectedWorkflowId
      ? items.findIndex((workflow) => workflow.value === selectedWorkflowId)
      : -1;
    if (selectedWorkflowIndex >= 0) {
      list.setSelectedIndex(selectedWorkflowIndex);
    }
    list.onSelect = (item) => {
      void this.openSwarmWorkflowDetail(item.value);
    };
    list.onCancel = () => {
      this.closeSwarmWorkflowsView();
    };
    return { phase: "list", list, loading };
  }

  private async openSwarmWorkflowDetail(
    workflowId: string,
    selectedPhaseId?: string,
    focus: "phases" | "agents" = "phases",
    selectedAgentId?: string,
  ): Promise<void> {
    this.swarmWorkflowsViewState = this.buildSwarmWorkflowDetailState(
      workflowId,
      selectedPhaseId,
      focus,
      selectedAgentId,
      true,
    );
    this.tui.requestRender();
    try {
      await this.state.loadWorkflowDetail(workflowId);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.state.addItem(
        addError(this.state.getSnapshot().sessionId, `workflow detail failed: ${message}`),
      );
    }
    this.swarmWorkflowsViewState = this.buildSwarmWorkflowDetailState(
      workflowId,
      selectedPhaseId,
      focus,
      selectedAgentId,
      false,
    );
    this.tui.requestRender();
  }

  /** Open agent detail or session history from the agents SelectList. */
  private selectSwarmWorkflowAgentListItem(
    workflowId: string,
    activePhaseId: string,
    item: SelectItem,
  ): void {
    const sessionRef = parseSessionSelectValue(item.value);
    if (sessionRef) {
      this.openSessionDetail(
        workflowId,
        sessionRef.sessionLabel,
        sessionRef.phaseId,
        sessionRef.nodeType,
        {
          kind: "workflow",
          workflowId,
          selectedPhaseId: activePhaseId,
          selectedAgentId: item.value,
        },
      );
      return;
    }
    const lookup = findWorkflowAgent(
      this.state.getSnapshot().workflowRuns,
      workflowId,
      item.value,
    );
    if (!lookup) return;
    // Agent 进入详情视图前，若仅为 get_phase 摘要（detail_pending，无 prompt/outcome
    // 等大文本），按需拉取完整体（get_agent，通用，不限 human 节点）。
    if (lookup.agent.detail_pending === true) {
      void this.state
        .loadAgentDetail(workflowId, lookup.phase.id, item.value)
        .catch(() => undefined);
    }
    this.swarmWorkflowsViewState = {
      phase: "agent",
      workflowId,
      agentId: item.value,
    };
    this.tui.requestRender();
  }

  private buildSwarmWorkflowDetailState(
    workflowId: string,
    selectedPhaseId?: string,
    focus: "phases" | "agents" = "phases",
    selectedAgentId?: string,
    loadingDetail = false,
  ): SwarmWorkflowsViewState {
    const workflow = this.state.getSnapshot().workflowRuns.find((item) => item.id === workflowId);
    if (!workflow) return this.buildSwarmWorkflowsListState(false, workflowId);
    const phaseEntries = workflowPhaseSelectEntries(workflow);
    const resolvedPhaseId =
      selectedPhaseId && phaseEntries.some((entry) => entry.phaseId === selectedPhaseId)
        ? selectedPhaseId
        : (phaseEntries[0]?.phaseId ?? "");
    const selectedPhaseIndex = Math.max(
      0,
      phaseEntries.findIndex((entry) => entry.phaseId === resolvedPhaseId),
    );
    const selectedPhase =
      workflow.phases?.find((phase) => phase.id === resolvedPhaseId) ?? workflow.phases?.[0];
    const activePhaseId = selectedPhase?.id ?? "";
    const phaseItems: SelectItem[] = phaseEntries.map((entry) => ({
      value: entry.phaseId,
      label: `${formatWorkflowStatus(entry.status)} ${entry.isChild ? `  ${entry.name}` : entry.name}`,
      description: `${entry.completed}/${entry.total}`,
    }));
    const phaseList = new SelectList(
      phaseItems,
      Math.min(Math.max(phaseItems.length, 1), 8),
      swarmWorkflowSelectListTheme,
      {
        minPrimaryColumnWidth: 22,
        maxPrimaryColumnWidth: 36,
      },
    );
    phaseList.setSelectedIndex(selectedPhaseIndex);
    phaseList.onSelectionChange = (item) => {
      this.swarmWorkflowsViewState = this.buildSwarmWorkflowDetailState(
        workflowId,
        item.value,
        "phases",
      );
      this.tui.requestRender();
    };
    phaseList.onSelect = (item) => {
      this.swarmWorkflowsViewState = this.buildSwarmWorkflowDetailState(
        workflowId,
        item.value,
        "agents",
      );
      // Lazy-load agents for the selected phase (get_workflow returns
      // summaries without agents — fetch on first drill-in).
      const phase = workflow.phases?.find((p) => p.id === item.value);
      if (phase && (!Array.isArray(phase.agents) || phase.agents.length === 0)) {
        void this.state.loadPhaseAgents(workflowId, item.value).then(() =>
          this.refreshSwarmWorkflowsView(),
        );
      }
      this.tui.requestRender();
    };
    phaseList.onCancel = () => {
      this.swarmWorkflowsViewState = this.buildSwarmWorkflowsListState(false, workflowId);
      this.tui.requestRender();
    };

    // Build agent SelectList: session trees (incl. single-turn) + one-shots
    // Collapse only controls the inline details tree. Enter/→ must still open
    // the selected phase's agents even when that tree branch is collapsed.
    const agents = selectedPhase?.agents ?? [];
    const agentItems: SelectItem[] = [];
    const { sessions, oneShots } = groupWorkflowAgentsByName(agents);

    for (const { label, members } of sessions) {
      const firstMember = members[0]!;
      const aggregateStatus = this.aggregateSessionStatus(members);
      const modelOrHuman = formatWorkflowAgentKindLabel(firstMember);
      const nodeType = sessionNodeTypeOf(firstMember);
      agentItems.push({
        value: sessionSelectValue(label, activePhaseId, nodeType),
        label: `${formatWorkflowStatus(aggregateStatus)} ${label}`,
        description: `${this.formatSessionProgress(members)} · ${modelOrHuman}`,
      });

      for (let i = 0; i < members.length; i++) {
        const member = members[i]!;
        const turn = this.agentTurnNumber(member, i);
        const isLast = i === members.length - 1;
        const branch = isLast ? "└" : "├";
        const turnModelOrHuman = formatWorkflowAgentKindLabel(member);
        const turnTokens = formatTokenCount(member.token_count);
        agentItems.push({
          value: member.id,
          label: this.formatSessionTurnSelectLabel(branch, turn, member.status),
          description: isHumanTurnCached(member)
            ? `${turnModelOrHuman} · cached${turnTokens ? ` · ${turnTokens} tok` : ""}`
            : `${turnModelOrHuman}${turnTokens ? ` · ${turnTokens} tok` : ""}`,
        });
      }
    }

    for (const agent of oneShots) {
      const tokens = formatTokenCount(agent.token_count);
      agentItems.push({
        value: agent.id,
        label: `${formatWorkflowStatus(agent.status)} ${agent.name}`,
        description: `${formatWorkflowAgentKindLabel(agent)}${tokens ? ` · ${tokens} tok` : ""}`,
      });
    }
    const agentList = new SelectList(
      agentItems,
      Math.min(Math.max(agentItems.length, 1), 10),
      swarmWorkflowSelectListTheme,
      {
        minPrimaryColumnWidth: 24,
        maxPrimaryColumnWidth: 44,
      },
    );
    const selectedAgentIndex = selectedAgentId
      ? Math.max(0, agentItems.findIndex((agent) => agent.value === selectedAgentId))
      : 0;
    agentList.setSelectedIndex(selectedAgentIndex);
    agentList.onSelect = (item) => {
      this.selectSwarmWorkflowAgentListItem(workflowId, activePhaseId, item);
    };
    agentList.onCancel = () => {
      this.swarmWorkflowsViewState = this.buildSwarmWorkflowDetailState(
        workflowId,
        activePhaseId,
        "phases",
      );
      this.tui.requestRender();
    };
    return {
      phase: "workflow",
      workflowId,
      selectedPhaseId: activePhaseId,
      focus,
      phaseList,
      agentList,
      loadingDetail,
    };
  }

  private handleSwarmWorkflowsInput(data: string): void {
    const state = this.swarmWorkflowsViewState;
    if (!state) return;
    const action = resolveAction("SwarmWorkflows", data);
    // Fallback: resolveAction's matchesKey doesn't match shift+letter on some
    // terminals (Windows Terminal). Check the raw data for the uppercase letter
    // that a shifted key produces in legacy terminal mode. Same systemic issue
    // affects shift+b (budget, shipped in SDD-0010) — it never fires either.
    let effectiveAction = action;
    if (effectiveAction === null) {
      const lower = data.toLowerCase();
      if (data === "P" || lower === "shift+p") {
        effectiveAction = "swarm:pauseResume";
      } else if (data === "S" || lower === "shift+s") {
        effectiveAction = "swarm:stop";
      } else if (data === "B" || lower === "shift+b") {
        effectiveAction = "swarm:budget";
      }
    }
    if (effectiveAction === "swarm:budget") {
      void this.openSwarmWorkflowBudget();
      return;
    }
    if (effectiveAction === "swarm:back" || effectiveAction === "swarm:left") {
      if (state.phase === "pending-list") {
        this.restoreFromPendingList(state.previous_phase);
        this.tui.requestRender();
        return;
      }
      if (state.phase === "list") {
        this.closeSwarmWorkflowsView();
      } else if (state.phase === "workflow") {
        this.swarmWorkflowsViewState =
          state.focus === "agents"
            ? this.buildSwarmWorkflowDetailState(
                state.workflowId,
                state.selectedPhaseId,
                "phases",
                state.agentList.getSelectedItem()?.value,
              )
            : this.buildSwarmWorkflowsListState(false, state.workflowId);
      } else if (state.phase === "agent") {
        if (state.returnTo?.kind === "pending-list") {
          this.replyingToHumanPrompt = null;
          this.editor.setText("");
          this.editor.focused = false;
          this.swarmWorkflowsViewState = this.buildPendingListState(
            state.returnTo.previous_phase ?? "chat",
          );
        } else {
          const lookup = findWorkflowAgent(
            this.state.getSnapshot().workflowRuns,
            state.workflowId,
            state.agentId,
          );
          this.swarmWorkflowsViewState = this.buildSwarmWorkflowDetailState(
            state.workflowId,
            lookup?.phase.id,
            "agents",
            state.agentId,
          );
        }
      } else if (state.phase === "session-detail") {
        this.restoreFromSessionDetail(state.returnTo);
      }
      // pending-list phase is handled separately above
      this.tui.requestRender();
      return;
    }
    if (effectiveAction === "swarm:nextFocus") {
      if (state.phase === "list") {
        const item = state.list.getSelectedItem();
        if (item) {
          void this.openSwarmWorkflowDetail(item.value);
        }
        return;
      }
      if (state.phase === "workflow") {
        if (state.focus === "phases") {
          this.swarmWorkflowsViewState = this.buildSwarmWorkflowDetailState(
            state.workflowId,
            state.selectedPhaseId,
            "agents",
          );
          this.tui.requestRender();
        } else {
          const item = state.agentList.getSelectedItem();
          if (item) {
            this.selectSwarmWorkflowAgentListItem(
              state.workflowId,
              state.selectedPhaseId,
              item,
            );
          }
        }
      }
      return;
    }
    if (state.phase === "workflow" && effectiveAction === "swarm:logs") {
      this.openSwarmWorkflowLogs(state.workflowId);
      return;
    }
    if (effectiveAction === "swarm:pauseResume" || effectiveAction === "swarm:stop") {
      const workflowId = this.workflowIdForBudgetView();
      if (!workflowId) {
        this.showTransientNotice("Select a workflow first.");
        return;
      }
      const workflow = this.state
        .getSnapshot()
        .workflowRuns.find((item) => item.id === workflowId);
      if (!workflow) return;
      if (effectiveAction === "swarm:stop") {
        if (workflow.status === "running" || workflow.status === "paused") {
          this.sendSwarmWorkflowControl(workflowId, "stop");
        } else {
          this.showTransientNotice(
            `This workflow is ${workflow.status} and cannot be stopped.`,
          );
        }
        return;
      }
      // swarm:pauseResume toggles: running -> pause, paused -> resume.
      if (workflow.status === "paused") {
        this.sendSwarmWorkflowControl(workflowId, "resume");
      } else if (workflow.status === "running") {
        this.sendSwarmWorkflowControl(workflowId, "pause");
      } else {
        this.showTransientNotice(
          `This workflow is ${workflow.status} and cannot be paused or resumed.`,
        );
      }
      return;
    }
    if (state.phase === "agent") {
      // For human nodes, Q key shows question; for agent nodes, P key shows prompt
      const lookup = findWorkflowAgent(
        this.state.getSnapshot().workflowRuns,
        state.workflowId,
        state.agentId,
      );
      const isHuman = lookup?.agent.kind === "human";

      if (effectiveAction === "swarm:viewPrompt" || (!isHuman && matchesKey(data, "p"))) {
        this.openSwarmWorkflowAgentText(state.workflowId, state.agentId, "prompt");
        return;
      }
      if (isHuman && matchesKey(data, "q")) {
        this.openSwarmWorkflowAgentText(state.workflowId, state.agentId, "human_prompt");
        return;
      }
      if (isHuman && matchesKey(data, "a")) {
        this.openSwarmWorkflowAgentText(state.workflowId, state.agentId, "human_reply");
        return;
      }
      if (!isHuman && effectiveAction === "swarm:viewOutcome") {
        this.openSwarmWorkflowAgentText(state.workflowId, state.agentId, "outcome");
        return;
      }
      if (effectiveAction === "swarm:viewError") {
        this.openSwarmWorkflowAgentText(state.workflowId, state.agentId, "error");
        return;
      }
      // S: session history for agent_session / human_session nodes only
      if (
        matchesKey(data, "s") &&
        lookup &&
        canOpenSessionHistory(lookup.agent)
      ) {
        if (
          this.tryOpenSessionDetailFromAgent(state.workflowId, state.agentId, {
            kind: "agent",
            workflowId: state.workflowId,
            agentId: state.agentId,
          })
        ) {
          return;
        }
      }
      // Tab on a waiting_for_human node enters reply mode.
      if (matchesKey(data, "tab")) {
        if (lookup?.agent.status === "waiting_for_human") {
          this.replyingToHumanPrompt = {
            workflowRunId: state.workflowId,
            correlationId: lookup.agent.correlation_id ?? lookup.agent.id,
            label: lookup.agent.name,
            turn: sessionTurnLabelNumber(lookup.agent, lookup.phase.agents ?? []) ?? 0,
            isSession: shouldShowTurnInDetailOrReply(lookup.agent),
          };
          this.editor.setText("");
          this.editor.focused = true;
          this.tui.requestRender();
          return;
        }
        this.showHumanReplyUnavailableNotice(lookup?.agent.status, lookup?.agent.kind);
        return;
      }
    }
    if (state.phase === "session-detail") {
      // Scroll / paging over the (potentially long) session history body.
      // Render clamps scrollOffset to the real max, so overshoot is safe here.
      const page = Math.max(1, this.sessionDetailBodyViewport(5) - 1);
      if (matchesKey(data, "up") || matchesKey(data, "k")) {
        state.scrollOffset = Math.max(0, state.scrollOffset - 1);
        this.tui.requestRender();
        return;
      }
      if (matchesKey(data, "down") || matchesKey(data, "j")) {
        state.scrollOffset = state.scrollOffset + 1;
        this.tui.requestRender();
        return;
      }
      if (matchesKey(data, "pageUp")) {
        state.scrollOffset = Math.max(0, state.scrollOffset - page);
        this.tui.requestRender();
        return;
      }
      if (matchesKey(data, "pageDown")) {
        state.scrollOffset = state.scrollOffset + page;
        this.tui.requestRender();
        return;
      }
      if (matchesKey(data, "home")) {
        state.scrollOffset = 0;
        this.tui.requestRender();
        return;
      }
      if (matchesKey(data, "end")) {
        state.scrollOffset = Number.MAX_SAFE_INTEGER;
        this.tui.requestRender();
        return;
      }
      // Tab: enter reply mode for the waiting turn (if any)
      if (matchesKey(data, "tab")) {
        const workflow = this.state.getSnapshot().workflowRuns.find((w) => w.id === state.workflowId);
        const phase = workflow?.phases?.find((item) => item.id === state.phaseId);
        if (phase) {
          for (const agent of phase.agents ?? []) {
            if (
              agent.name === state.sessionLabel &&
              agent.status === "waiting_for_human" &&
              agent.kind === "human" &&
              sessionNodeTypeOf(agent) === state.nodeType
            ) {
              this.replyingToHumanPrompt = {
                workflowRunId: state.workflowId,
                correlationId: agent.correlation_id ?? agent.id,
                label: agent.name,
                turn: sessionTurnLabelNumber(agent, phase.agents ?? []) ?? 0,
                isSession: shouldShowTurnInDetailOrReply(agent),
              };
              this.editor.setText("");
              this.editor.focused = true;
              this.tui.requestRender();
              return;
            }
          }
        }
        const sessionAgents = sessionMembersInPhase(
          phase?.agents ?? [],
          state.sessionLabel,
          state.nodeType,
        );
        const sessionCompleted =
          sessionAgents.length > 0 &&
          sessionAgents.every((agent) => agent.status === "completed");
        this.showTransientNotice(
          sessionCompleted
            ? "This session is completed and can no longer accept replies."
            : "This session has no turn waiting for a reply.",
        );
        return;
      }
    }
    if (effectiveAction === "swarm:refresh") {
      if (
        state.phase === "session-detail" ||
        state.phase === "agent" ||
        state.phase === "workflow" ||
        state.phase === "pending-list"
      ) {
        void this.reloadSwarmWorkflowsKeepingView();
        return;
      }
      void this.openSwarmWorkflowsView();
      this.tui.requestRender();
      return;
    }
    // Esc / navigation from pending-list
    if (state.phase === "pending-list") {
      if (matchesKey(data, "return") || matchesKey(data, "right")) {
        const waiting = this.getSelectedPendingListWaiting(state);
        if (waiting) {
          void this.openAgentDetailFromPendingList(waiting, state.previous_phase);
        }
        return;
      }
      if (matchesKey(data, "q")) {
        const waiting = this.getSelectedPendingListWaiting(state);
        if (waiting) {
          void this.state.ensureHumanPromptLoaded(waiting.workflowId, waiting.agentId).then(() => {
            this.openSwarmWorkflowAgentText(waiting.workflowId, waiting.agentId, "human_prompt");
            this.tui.requestRender();
          });
        }
        return;
      }
      if (matchesKey(data, "s")) {
        const waiting = this.getSelectedPendingListWaiting(state);
        if (
          waiting &&
          this.tryOpenSessionDetailFromAgent(waiting.workflowId, waiting.agentId, {
            kind: "pending-list",
            previous_phase: state.previous_phase,
          })
        ) {
          return;
        }
      }
      if (matchesKey(data, "tab")) {
        void this.startReplyFromPendingListWaiting(state);
        return;
      }
      if (matchesKey(data, "up") || matchesKey(data, "k")) {
        state.selectedIndex = Math.max(0, state.selectedIndex - 1);
        this.tui.requestRender();
        return;
      }
      if (matchesKey(data, "down") || matchesKey(data, "j")) {
        const count = this.countPendingListWaitingEntries(state);
        state.selectedIndex = Math.min(Math.max(count - 1, 0), state.selectedIndex + 1);
        this.tui.requestRender();
        return;
      }
      return;
    }
    if (state.phase === "list") {
      state.list.handleInput(data);
      this.tui.requestRender();
      return;
    }
    if (state.phase === "workflow") {
      // Entry B: Tab key in agents focus mode → quick reply to selected waiting human turn
      if (matchesKey(data, "tab") && state.focus === "agents") {
        const selectedItem = state.agentList.getSelectedItem();
        if (selectedItem && !selectedItem.value.startsWith("session:")) {
          const lookup = findWorkflowAgent(
            this.state.getSnapshot().workflowRuns,
            state.workflowId,
            selectedItem.value,
          );
          if (lookup && lookup.agent.status === "waiting_for_human" && lookup.agent.kind === "human") {
            this.replyingToHumanPrompt = {
              workflowRunId: state.workflowId,
              correlationId: lookup.agent.correlation_id ?? lookup.agent.id,
              label: lookup.agent.name,
              turn: sessionTurnLabelNumber(lookup.agent, lookup.phase.agents ?? []) ?? 0,
              isSession: shouldShowTurnInDetailOrReply(lookup.agent),
            };
            this.editor.setText("");
            this.editor.focused = true;
            this.tui.requestRender();
            return;
          }
          this.showHumanReplyUnavailableNotice(lookup?.agent.status, lookup?.agent.kind);
          return;
        }
        this.showTransientNotice("Select a human turn that is waiting for a reply.");
        return;
      }
      const activeList = state.focus === "phases" ? state.phaseList : state.agentList;
      activeList.handleInput(data);
      this.tui.requestRender();
    }
  }

  private openSwarmWorkflowLogs(workflowId: string): void {
    const workflow = this.state
      .getSnapshot()
      .workflowRuns.find((item) => item.id === workflowId);
    if (!workflow) return;
    const logs = (workflow.logs ?? []).filter((log) => log.trim().length > 0);
    this.enterFileViewer(
      logs.length > 0 ? logs.join("\n") : "No logs",
      `Workflow logs - ${workflow.name}`,
      workflow.id,
    );
  }

  private openSwarmWorkflowAgentText(
    workflowId: string,
    agentId: string,
    field: "prompt" | "outcome" | "error" | "human_prompt" | "human_reply",
  ): void {
    const lookup = findWorkflowAgent(this.state.getSnapshot().workflowRuns, workflowId, agentId);
    if (!lookup) return;
    const cached = isHumanTurnCached(lookup.agent);
    const value =
      field === "human_prompt"
        ? cached
          ? HUMAN_TURN_CACHED_QUESTION
          : lookup.agent.human_prompt || this.getHumanQuestionText(workflowId, agentId)
        : field === "human_reply"
          ? cached
            ? HUMAN_TURN_CACHED_ANSWER
            : lookup.agent.human_reply
          : lookup.agent[field];
    if (!value) return;
    const label =
      field === "human_reply"
        ? "Answer"
        : field === "human_prompt"
          ? "Question"
          : field.charAt(0).toUpperCase() + field.slice(1);
    let display = value;
    try {
      const parsed = JSON.parse(value);
      display = JSON.stringify(parsed, null, 2);
    } catch {
      // not JSON, use as-is
    }
    this.enterFileViewer(display, `${label} - ${lookup.agent.name}`, lookup.workflow.name);
  }

  private buildSwarmWorkflowsLines(width: number): string[] {
    const state = this.swarmWorkflowsViewState;
    if (!state) return [];
    if (state.phase === "list") {
      return this.buildSwarmWorkflowsListLines(state, width);
    }
    if (state.phase === "pending-list") {
      return this.buildPendingListLines(state, width);
    }
    if (state.phase === "workflow") {
      return this.buildSwarmWorkflowDetailLines(state, width);
    }
    if (state.phase === "session-detail") {
      return this.buildSessionDetailLines(state, width);
    }
    return this.buildSwarmWorkflowAgentLines(state, width);
  }

  private buildWorkflowRuntimeLines(width: number): string[] {
    const snapshot = this.state.getSnapshot();
    const workflowRuns = snapshot.workflowRuns;
    const pendingPrompts = snapshot.pendingHumanPrompts;

    const runningWorkflows = workflowRuns.filter((item) => item.status === "running");
    const pausedWorkflows = workflowRuns.filter((item) => item.status === "paused");
    const totalPending = pendingPrompts?.size ?? 0;

    if (
      runningWorkflows.length === 0 &&
      pausedWorkflows.length === 0 &&
      totalPending === 0
    ) {
      return [];
    }

    const waitingIcon = workflowStatusIcon("waiting_for_human");
    const pausedIcon = workflowStatusIcon("paused");
    const spinner =
      totalPending > 0
        ? palette.text.humanInput(`${waitingIcon} `)
        : palette.status.warning(["◐", "◓", "◑", "◒"][this.animationPhase % 4]!);

    const renderRow = (workflow: WorkflowRun): string => {
      const pendingCount = countWaitingForHuman(workflow);
      const nameAndTime = `${palette.text.dim(workflow.name)} ${palette.text.dim(formatWorkflowTimingText(workflow))}`;
      const suffix = pendingCount > 0 ? ` ${palette.text.dim(`· ${pendingCount} waiting`)}` : "";
      return padToWidth(`  ${nameAndTime}${suffix}`, width);
    };

    const renderPausedRow = (workflow: WorkflowRun): string => {
      const nameAndTime = `${palette.text.dim(workflow.name)} ${palette.text.dim(formatWorkflowTimingText(workflow))}`;
      return padToWidth(`  ${palette.status.warning(`${pausedIcon} `)}${nameAndTime}`, width);
    };

    if (totalPending > 0) {
      const humanInputsKey = this.globalActionKeyLabel("app:viewHumanInputs");
      const runningSuffix =
        runningWorkflows.length > 0
          ? ` · ${palette.text.assistant(runningWorkflowsBannerText(runningWorkflows.length))}`
          : "";
      const pausedSuffix =
        pausedWorkflows.length > 0
          ? ` · ${palette.text.assistant(pausedWorkflowsBannerText(pausedWorkflows.length))}`
          : "";
      const firstLine = padToWidth(
        `${spinner}${palette.text.humanInput(pendingInputsBannerText(totalPending))} · ${palette.text.dim(pendingHumanViewHint(humanInputsKey))}${runningSuffix}${pausedSuffix}`,
        width,
      );
      const lines = [firstLine];
      for (const wf of runningWorkflows) {
        lines.push(renderRow(wf));
      }
      for (const wf of pausedWorkflows) {
        lines.push(renderPausedRow(wf));
      }
      return lines;
    }

    // Paused workflows present (no pending) — surface them alongside running ones.
    if (pausedWorkflows.length > 0) {
      const banner = [
        runningWorkflowsBannerText(runningWorkflows.length),
        pausedWorkflowsBannerText(pausedWorkflows.length),
      ]
        .filter(Boolean)
        .join(" · ");
      const prefix =
        runningWorkflows.length > 0 ? spinner : palette.status.warning(`${pausedIcon} `);
      const lines = [padToWidth(`${prefix} ${palette.text.assistant(banner)}`, width)];
      for (const wf of runningWorkflows) {
        lines.push(renderRow(wf));
      }
      for (const wf of pausedWorkflows) {
        lines.push(renderPausedRow(wf));
      }
      return lines;
    }

    // No pending — original behaviour.
    if (runningWorkflows.length === 1) {
      const workflow = runningWorkflows[0]!;
      return [
        padToWidth(`${spinner} ${palette.text.assistant(runningWorkflowsBannerText(1))}`, width),
        padToWidth(
          `  ${palette.text.dim(workflow.name)} ${palette.text.dim(formatWorkflowTimingText(workflow))}`,
          width,
        ),
      ];
    }

    const lines = [
      padToWidth(
        `${spinner} ${palette.text.assistant(runningWorkflowsBannerText(runningWorkflows.length))}`,
        width,
      ),
    ];
    for (const wf of runningWorkflows) {
      lines.push(renderRow(wf));
    }
    return lines;
  }

  private buildSwarmWorkflowsListLines(
    state: Extract<SwarmWorkflowsViewState, { phase: "list" }>,
    width: number,
  ): string[] {
    const workflows = this.state.getSnapshot().workflowRuns;
    const budgetKey = this.swarmActionKeyLabel("swarm:budget");
    const headerLines = [
      padToWidth(palette.text.accent("Swarm workflows"), width),
      padToWidth(
        palette.text.dim(
          state.loading ? "Loading workflows..." : formatSwarmWorkflowsSummary(workflows),
        ),
        width,
      ),
    ];
    const helpLine = padToWidth(
      palette.text.dim(
        `↑/↓ select · Enter/→ load detail · ${budgetKey} budget · r refresh · Esc/← close`,
      ),
      width,
    );
    if (state.loading || workflows.length === 0) {
      return [...headerLines, "", helpLine];
    }
    return [
      ...headerLines,
      "",
      ...state.list.render(width),
      helpLine,
    ];
  }

  private countPendingListWaitingEntries(
    state: Extract<SwarmWorkflowsViewState, { phase: "pending-list" }>,
  ): number {
    return state.entries.filter((entry) => entry.kind === "waiting").length;
  }

  private getSelectedPendingListWaiting(
    state: Extract<SwarmWorkflowsViewState, { phase: "pending-list" }>,
  ): Extract<PendingListEntry, { kind: "waiting" }> | null {
    const waitingEntries = state.entries.filter(
      (entry): entry is Extract<PendingListEntry, { kind: "waiting" }> => entry.kind === "waiting",
    );
    return waitingEntries[state.selectedIndex] ?? null;
  }

  private prefetchPendingListQuestion(waiting: Extract<PendingListEntry, { kind: "waiting" }>): void {
    void this.state.ensureHumanPromptLoaded(waiting.workflowId, waiting.agentId).then(() => {
      if (this.swarmWorkflowsViewState?.phase === "pending-list") {
        this.tui.requestRender();
      }
    });
  }

  private startReplyFromPendingListWaiting(
    state: Extract<SwarmWorkflowsViewState, { phase: "pending-list" }>,
  ): boolean {
    const waiting = this.getSelectedPendingListWaiting(state);
    if (!waiting) return false;
    void this.state.ensureHumanPromptLoaded(waiting.workflowId, waiting.agentId).then(() => {
      if (this.startReplyFromPendingListWaitingSync(state)) {
        this.tui.requestRender();
      }
    });
    return true;
  }

  private startReplyFromPendingListWaitingSync(
    state: Extract<SwarmWorkflowsViewState, { phase: "pending-list" }>,
  ): boolean {
    const waiting = this.getSelectedPendingListWaiting(state);
    if (!waiting) return false;
    const lookup = findWorkflowAgent(
      this.state.getSnapshot().workflowRuns,
      waiting.workflowId,
      waiting.agentId,
    );
    if (!lookup || lookup.agent.status !== "waiting_for_human") return false;
    this.replyingToHumanPrompt = {
      workflowRunId: waiting.workflowId,
      correlationId: lookup.agent.correlation_id ?? lookup.agent.id,
      label: lookup.agent.name,
      turn: waiting.turn,
      isSession: shouldShowTurnInDetailOrReply(lookup.agent),
    };
    this.editor.setText("");
    this.editor.focused = true;
    this.tui.requestRender();
    return true;
  }

  private restoreFromPendingList(previous_phase: PendingListPreviousPhase): void {
    this.replyingToHumanPrompt = null;
    this.editor.setText("");
    this.editor.focused = false;
    if (previous_phase === "chat") {
      this.closeSwarmWorkflowsView();
      return;
    }
    if (previous_phase === "list") {
      this.swarmWorkflowsViewState = this.buildSwarmWorkflowsListState(false);
      return;
    }
    if (previous_phase === "workflow") {
      const workflows = this.state.getSnapshot().workflowRuns;
      if (workflows.length > 0) {
        this.swarmWorkflowsViewState = this.buildSwarmWorkflowDetailState(workflows[0]!.id);
      } else {
        this.swarmWorkflowsViewState = this.buildSwarmWorkflowsListState(false);
      }
      return;
    }
    if (previous_phase === "agent" || previous_phase === "session-detail") {
      this.swarmWorkflowsViewState = this.buildSwarmWorkflowsListState(false);
      return;
    }
    this.closeSwarmWorkflowsView();
  }

  private buildPendingListState(
    previous_phase: PendingListPreviousPhase,
    selectedIndex = 0,
  ): SwarmWorkflowsViewState {
    const workflows = this.state.getSnapshot().workflowRuns;
    const entries: PendingListEntry[] = [];

    for (const workflow of workflows) {
      let workflowHeaderAdded = false;

      for (const phase of workflow.phases ?? []) {
        const waitingAgents = (phase.agents ?? []).filter(
          (agent) => agent.status === "waiting_for_human" && agent.kind === "human",
        );
        if (waitingAgents.length === 0) continue;

        if (!workflowHeaderAdded) {
          entries.push({
            kind: "workflow-header",
            workflowId: workflow.id,
            workflowName: workflow.name,
          });
          workflowHeaderAdded = true;
        }

        const { sessions, oneShots } = groupWorkflowAgentsByName(phase.agents ?? []);
        const waitingIds = new Set(waitingAgents.map((agent) => agent.id));

        for (const { label, members } of sessions) {
          const waitingMembers = members.filter((member) => waitingIds.has(member.id));
          if (waitingMembers.length === 0) continue;

          entries.push({
            kind: "session-parent",
            sessionLabel: label,
            done: this.sessionDoneCount(members),
            total: this.sessionTotalCount(members),
          });

          for (const member of waitingMembers) {
            const turnIndex = members.findIndex((item) => item.id === member.id);
            entries.push({
              kind: "waiting",
              workflowId: workflow.id,
              phaseId: phase.id,
              agentId: member.id,
              sessionLabel: label,
              turn: this.agentTurnNumber(member, Math.max(turnIndex, 0)),
              isSessionChild: true,
              isSession: true,
            });
          }
        }

        for (const agent of oneShots) {
          if (!waitingIds.has(agent.id)) continue;
          const isSession = isSessionNode(agent);
          entries.push({
            kind: "waiting",
            workflowId: workflow.id,
            phaseId: phase.id,
            agentId: agent.id,
            sessionLabel: agent.name,
            turn: sessionTurnLabelNumber(agent, phase.agents ?? []) ?? 0,
            isSessionChild: false,
            isSession,
          });
        }
      }
    }

    const waitingCount = entries.filter((entry) => entry.kind === "waiting").length;
    return {
      phase: "pending-list",
      entries,
      selectedIndex: Math.min(Math.max(selectedIndex, 0), Math.max(waitingCount - 1, 0)),
      previous_phase,
    };
  }

  private buildPendingListLines(
    state: Extract<SwarmWorkflowsViewState, { phase: "pending-list" }>,
    width: number,
    options: { overlay?: boolean } = {},
  ): string[] {
    const overlay = options.overlay ?? false;
    const budgetKey = this.swarmActionKeyLabel("swarm:budget");
    const divider = padToWidth(palette.text.dim("─".repeat(Math.max(1, width))), width);
    const waitingCount = this.countPendingListWaitingEntries(state);
    const headerLines = overlay
      ? [
          divider,
          padToWidth(
            palette.text.humanInput(
              `${workflowStatusIcon("waiting_for_human")} Pending human replies`,
            ),
            width,
          ),
          padToWidth(
            palette.text.secondary(
              waitingCount === 0
                ? "No pending replies · Esc/← back to chat"
                : `${waitingCount} waiting · Esc/← back to chat`,
            ),
            width,
          ),
          divider,
        ]
      : [
          padToWidth(palette.text.humanInput("Pending human replies"), width),
          padToWidth(
            palette.text.secondary(
              waitingCount === 0 ? "No pending replies" : `${waitingCount} waiting`,
            ),
            width,
          ),
        ];
    const helpLine = padToWidth(
      palette.text.secondary(
        overlay
          ? `↑/↓ select · Enter detail · Tab reply · s session · q question · ${budgetKey} budget · Esc/← back to chat`
          : `↑/↓ select · Enter detail · Tab reply · s session · q question · ${budgetKey} budget · Esc/← back`,
      ),
      width,
    );
    if (waitingCount === 0) {
      return [...headerLines, "", helpLine, ...(overlay ? ["", divider] : [])];
    }

    const lines: string[] = [...headerLines, ""];
    const waitingIcon = palette.text.humanInput(workflowStatusIcon("waiting_for_human"));
    const waitingPrefix = `${waitingIcon} `;
    let waitingCursor = 0;
    for (const entry of state.entries) {
      if (entry.kind === "workflow-header") {
        lines.push(padToWidth(palette.text.accent(entry.workflowName), width));
        continue;
      }
      if (entry.kind === "session-parent") {
        lines.push(
          padToWidth(
            `  ${entry.sessionLabel} ${palette.text.secondary(`· human · ${entry.done}/${entry.total}`)}`,
            width,
          ),
        );
        continue;
      }

      const selected = waitingCursor === state.selectedIndex;
      waitingCursor += 1;
      const marker = selected ? palette.text.accent("›") : " ";
      const label = entry.isSessionChild
        ? `${palette.text.dim("    └")} ${waitingPrefix}turn ${entry.turn} ${palette.text.secondary("· waiting")}`
        : entry.isSession
          ? `${waitingPrefix}${entry.sessionLabel} ${palette.text.secondary(`· turn ${entry.turn} · waiting`)}`
          : `${waitingPrefix}${entry.sessionLabel} ${palette.text.secondary("· waiting")}`;
      lines.push(padToWidth(`${marker}${selected ? " " : "  "}${label}`, width));
      if (selected) {
        const question = this.getHumanQuestionText(entry.workflowId, entry.agentId);
        if (question) {
          const previewWidth = Math.max(20, width - 10);
          const { lines: previewLines, truncated } = prepareHumanQuestionPreviewLines(
            question,
            previewWidth,
            3,
          );
          lines.push(padToWidth(palette.text.secondary("      Question"), width));
          for (const qLine of previewLines) {
            lines.push(padToWidth(palette.text.dim(`        ${qLine}`), width));
          }
          if (truncated) {
            lines.push(padToWidth(palette.text.dim("        … press q for full question"), width));
          }
        } else {
          lines.push(
            padToWidth(palette.text.dim("      Question: (loading…)"), width),
          );
        }
      }
    }

    lines.push("", ...(overlay ? [divider, ""] : []), helpLine);
    return lines;
  }

  private buildSwarmWorkflowDetailLines(
    state: Extract<SwarmWorkflowsViewState, { phase: "workflow" }>,
    width: number,
  ): string[] {
    const workflow = this.state
      .getSnapshot()
      .workflowRuns.find((item) => item.id === state.workflowId);
    if (!workflow) return [padToWidth(palette.status.error("Workflow not found"), width)];
    const total = workflow.agent_count ?? 0;
    const completed = workflow.completed_agent_count ?? 0;
    const statusBanner = workflowStatusBannerText(workflow.status);
    const selectedPhase =
      workflow.phases?.find((phase) => phase.id === state.selectedPhaseId) ?? workflow.phases?.[0];
    const selectedAgentId = state.agentList.getSelectedItem()?.value;
    const selectedAgent = selectedPhase?.agents?.find((agent) => agent.id === selectedAgentId);
    const replyHint =
      selectedAgent?.kind === "human" && selectedAgent.status === "waiting_for_human"
        ? " · Tab reply"
        : "";
    const workflowSummary = workflow.summary.trim();
    const budgetKey = this.swarmActionKeyLabel("swarm:budget");
    const pauseKey = this.swarmActionKeyLabel("swarm:pauseResume");
    const stopKey = this.swarmActionKeyLabel("swarm:stop");
    const controlHintParts: string[] = [];
    if (workflow.status === "running") {
      controlHintParts.push(`${pauseKey} pause · ${stopKey} stop`);
    } else if (workflow.status === "paused") {
      controlHintParts.push(`${pauseKey} resume`);
    }
    const runTokens = formatTokenCount(workflow.token_count);
    const budgetDetail = formatWorkflowBudgetDetail(workflow.budget);
    const runBudgetDetail = formatWorkflowRunBudgetDetail(workflow.workflow_budget);
    const usageParts: string[] = [];
    if (runTokens) usageParts.push(`Run tokens ${runTokens}`);
    if (budgetDetail) {
      usageParts.push(
        workflowBudgetExhaustedScope(workflow) === "session"
          ? palette.status.error(budgetDetail)
          : isWorkflowBudgetLow(workflow.budget)
            ? palette.status.warning(budgetDetail)
            : budgetDetail,
      );
    }
    if (runBudgetDetail) {
      usageParts.push(
        workflowBudgetExhaustedScope(workflow) === "workflow"
          ? palette.status.error(runBudgetDetail)
          : isWorkflowBudgetLow(workflow.workflow_budget)
            ? palette.status.warning(runBudgetDetail)
            : runBudgetDetail,
      );
    }
    const summaryLines =
      workflowSummary.length > 0 && workflowSummary !== workflow.name.trim()
        ? wrapPlainText(workflowSummary, width).map((line) =>
            padToWidth(palette.text.dim(line), width),
          )
        : [];
    const lines: string[] = [
      padToWidth(palette.text.accent(workflow.name), width),
      ...summaryLines,
      padToWidth(
        `${formatWorkflowStatus(workflow.status)} ${palette.text.dim(`· ${completed}/${total} agents`)}`,
        width,
      ),
      padToWidth(palette.text.dim(formatWorkflowTimingText(workflow)), width),
      ...(usageParts.length > 0
        ? [padToWidth(palette.text.secondary(usageParts.join(" · ")), width)]
        : []),
      ...(state.loadingDetail
        ? [padToWidth(palette.text.dim("Loading workflow details…"), width)]
        : []),
      ...(workflow.status === "failed" && workflow.error
        ? wrapPlainText(workflow.error, width).map((line) =>
            padToWidth(palette.status.error(line), width),
          )
        : []),
      ...(workflowBudgetExhaustedScope(workflow) === "workflow"
        ? [
            padToWidth(palette.status.error("Run budget exhausted"), width),
            padToWidth(
              palette.status.error(
                "Revise the workflow (or raise META.workflow_token_limit) and relaunch.",
              ),
              width,
            ),
          ]
        : []),
      ...(workflowBudgetExhaustedScope(workflow) === "session"
        ? [
            padToWidth(palette.status.error("Team budget exhausted"), width),
            padToWidth(palette.status.error("This workflow cannot be resumed."), width),
          ]
        : []),
      ...(statusBanner
        ? [padToWidth(workflowStatusTone(workflow.status)(statusBanner), width)]
        : []),
      "",
      padToWidth(palette.text.secondary("Logs"), width),
      ...this.renderSwarmWorkflowLogRows(workflow, width),
      "",
      padToWidth(
        state.focus === "phases"
          ? palette.text.accent("Phases")
          : palette.text.secondary("Phases"),
        width,
      ),
    ];
    if (state.focus === "phases") {
      lines.push(...this.renderSwarmWorkflowDetailTree(workflow, state.selectedPhaseId, width));
      lines.push("");
      const agentsTitle = selectedPhase ? `Agents · ${selectedPhase.name}` : "Agents";
      lines.push(padToWidth(palette.text.secondary(agentsTitle), width));
      if (selectedPhase) {
        lines.push(
          ...this.renderSwarmWorkflowAgentRows(
            selectedPhase.agents ?? [],
            width,
            SWARM_WORKFLOW_AGENT_PREVIEW_LIMIT,
          ),
        );
      } else {
        lines.push(padToWidth(palette.text.dim("No agents"), width));
      }
    } else {
      lines.push(...this.renderSwarmWorkflowPhaseRows(workflow, state.selectedPhaseId, width));
      lines.push("");
      const agentsTitle = selectedPhase ? `select agents · ${selectedPhase.name}` : "select agents";
      lines.push(padToWidth(palette.text.accent(agentsTitle), width));
      lines.push(...state.agentList.render(width));
    }
    lines.push(
      padToWidth(
        palette.text.secondary(
          [`press l to see full logs`, `${budgetKey} budget`, ...controlHintParts].join(" · "),
        ),
        width,
      ),
    );
    lines.push(
      padToWidth(
        palette.text.secondary(
          state.focus === "phases"
            ? `↑/↓ select phase · Enter/→ show agents · Esc/← back`
            : `↑/↓ select · Enter/→ show detail or session${replyHint} · Esc/← back to phases`,
        ),
        width,
      ),
    );
    return lines;
  }

  private renderSwarmWorkflowDetailTree(
    workflow: WorkflowRun,
    selectedPhaseId: string,
    width: number,
  ): string[] {
    const phases = workflow.phases ?? [];
    if (phases.length === 0) {
      return [padToWidth(palette.text.dim("No phases"), width)];
    }

    const lines: string[] = [];
    const childrenByParent = new Map<string, WorkflowPhase[]>();
    const orderedParents: WorkflowPhase[] = [];
    for (const phase of phases) {
      if (phase.phase_type === "child") {
        const parent = phase.parent_phase || "";
        if (!childrenByParent.has(parent)) childrenByParent.set(parent, []);
        childrenByParent.get(parent)!.push(phase);
      } else {
        orderedParents.push(phase);
      }
    }

    for (const parent of orderedParents) {
      const selected = parent.id === selectedPhaseId;
      const marker = selected ? palette.text.accent("›") : " ";
      const children = childrenByParent.get(parent.name) ?? [];
      const pDone = parent.completed_agent_count ?? 0;
      const pTotal = parent.agent_count ?? 0;
      lines.push(padToWidth(`${marker} ${formatWorkflowStatus(parent.status)} ${parent.name} ${palette.text.dim(`${pDone}/${pTotal}`)}`, width));

      for (const child of children) {
        const cSelected = child.id === selectedPhaseId;
        const cMarker = cSelected ? palette.text.accent("›") : " ";
        const cTotal = child.agent_count ?? 0;
        const cDone = child.completed_agent_count ?? 0;
        lines.push(padToWidth(`  ${cMarker} ${formatWorkflowStatus(child.status)} ${child.name} ${palette.text.dim(`${cDone}/${cTotal}`)}`, width));
      }
    }

    // orphan children (no matching parent)
    for (const [parentName, children] of childrenByParent) {
      if (orderedParents.some((p) => p.name === parentName)) continue;
      for (const child of children) {
        const cSelected = child.id === selectedPhaseId;
        const cMarker = cSelected ? palette.text.accent("›") : " ";
        const cTotal = child.agent_count ?? 0;
        const cDone = child.completed_agent_count ?? 0;
        lines.push(padToWidth(`  ${cMarker} ${formatWorkflowStatus(child.status)} ${child.name} ${palette.text.dim(`${cDone}/${cTotal}`)}`, width));
      }
    }
    return lines;
  }

  private renderSwarmWorkflowLogRows(workflow: WorkflowRun, width: number): string[] {
    const logs = (workflow.logs ?? []).filter((log) => log.trim().length > 0);
    if (logs.length === 0) return [padToWidth(palette.text.dim("No logs"), width)];
    const rows = logs.flatMap((log) =>
      wrapPlainText(`  ${log}`, width).map((line) => padToWidth(palette.text.dim(line), width)),
    );
    if (rows.length <= SWARM_WORKFLOW_LOG_PREVIEW_ROWS) return rows;
    const visibleRows = rows.slice(-(SWARM_WORKFLOW_LOG_PREVIEW_ROWS - 1));
    return [
      padToWidth(
        palette.text.dim(`  ... ${rows.length - visibleRows.length} earlier log lines`),
        width,
      ),
      ...visibleRows,
    ];
  }

  private renderSwarmWorkflowPhaseRows(
    workflow: WorkflowRun,
    selectedPhaseId: string,
    width: number,
  ): string[] {
    const childrenByParent = new Map<string, WorkflowPhase[]>();
    const orderedParents: WorkflowPhase[] = [];
    for (const phase of workflow.phases ?? []) {
      if (phase.phase_type === "child") {
        const parent = phase.parent_phase || "";
        if (!childrenByParent.has(parent)) childrenByParent.set(parent, []);
        childrenByParent.get(parent)!.push(phase);
      } else {
        orderedParents.push(phase);
      }
    }

    const lines: string[] = [];
    for (const parent of orderedParents) {
      const selected = parent.id === selectedPhaseId;
      const marker = selected ? palette.text.accent("›") : palette.text.dim(" ");
      const children = childrenByParent.get(parent.name) ?? [];
      const pDone = parent.completed_agent_count ?? 0;
      const pTotal = parent.agent_count ?? 0;
      lines.push(padToWidth(`${marker} ${formatWorkflowStatus(parent.status)} ${parent.name} ${palette.text.dim(`${pDone}/${pTotal}`)}`, width));

      for (const child of children) {
        const cSelected = child.id === selectedPhaseId;
        const cMarker = cSelected ? palette.text.accent("›") : palette.text.dim(" ");
        const cTotal = child.agent_count ?? 0;
        const cDone = child.completed_agent_count ?? 0;
        lines.push(padToWidth(`  ${cMarker} ${formatWorkflowStatus(child.status)} ${child.name} ${palette.text.dim(`${cDone}/${cTotal}`)}`, width));
      }
    }

    for (const [parentName, children] of childrenByParent) {
      if (orderedParents.some((p) => p.name === parentName)) continue;
      for (const child of children) {
        const cSelected = child.id === selectedPhaseId;
        const cMarker = cSelected ? palette.text.accent("›") : palette.text.dim(" ");
        const cTotal = child.agent_count ?? 0;
        const cDone = child.completed_agent_count ?? 0;
        lines.push(padToWidth(`  ${cMarker} ${formatWorkflowStatus(child.status)} ${child.name} ${palette.text.dim(`${cDone}/${cTotal}`)}`, width));
      }
    }
    return lines;
  }

  /** Phase-local 0-based turn label (see ``phaseLocalTurnNumber`` in workflows.ts). */
  private agentTurnNumber(
    agent: WorkflowAgent,
    indexInSession: number,
  ): number {
    return indexInSession;
  }

  private formatSessionTurnSelectLabel(
    branch: string,
    turn: number,
    status: WorkflowStatus,
  ): string {
    const icon = workflowStatusIcon(status);
    const tone = workflowStatusTone(status);
    const statusWord = formatWorkflowStatusWord(status);
    return `  ${branch} ${tone(`${icon} turn ${turn} · ${statusWord}`)}`;
  }

  /** Total session turns visible in the current phase. */
  private sessionTotalCount(members: WorkflowAgent[]): number {
    return members.length;
  }

  private sessionDetailReturnToForCurrentView(
    workflowId: string,
    agentId: string,
  ): SessionDetailReturnTo {
    const state = this.swarmWorkflowsViewState;
    if (state?.phase === "pending-list") {
      return { kind: "pending-list", previous_phase: state.previous_phase };
    }
    if (state?.phase === "agent") {
      return { kind: "agent", workflowId: state.workflowId, agentId: state.agentId };
    }
    if (state?.phase === "workflow") {
      return {
        kind: "workflow",
        workflowId: state.workflowId,
        selectedPhaseId: state.selectedPhaseId,
        selectedAgentId:
          state.focus === "agents" ? state.agentList.getSelectedItem()?.value : undefined,
      };
    }
    return { kind: "agent", workflowId, agentId };
  }

  private openSessionDetail(
    workflowId: string,
    sessionLabel: string,
    phaseId: string,
    nodeType: WorkflowNodeType,
    returnTo: SessionDetailReturnTo,
  ): void {
    this.swarmWorkflowsViewState = {
      phase: "session-detail",
      workflowId,
      sessionLabel,
      phaseId,
      nodeType,
      returnTo,
      scrollOffset: 0,
    };
    this.tui.requestRender();
  }

  private restoreFromSessionDetail(returnTo: SessionDetailReturnTo): void {
    // Manual leave clears the pending auto-jump so re-entering the session
    // view later (e.g. to browse a finished run's history) never triggers it.
    this.lastRepliedHumanPrompt = null;
    switch (returnTo.kind) {
      case "pending-list":
        this.swarmWorkflowsViewState = this.buildPendingListState(returnTo.previous_phase ?? "list");
        break;
      case "agent":
        this.swarmWorkflowsViewState = {
          phase: "agent",
          workflowId: returnTo.workflowId,
          agentId: returnTo.agentId,
        };
        break;
      case "workflow":
        this.swarmWorkflowsViewState = this.buildSwarmWorkflowDetailState(
          returnTo.workflowId,
          returnTo.selectedPhaseId,
          "agents",
          returnTo.selectedAgentId,
        );
        break;
    }
  }

  private tryOpenSessionDetailFromAgent(
    workflowId: string,
    agentId: string,
    returnTo: SessionDetailReturnTo,
  ): boolean {
    const lookup = findWorkflowAgent(this.state.getSnapshot().workflowRuns, workflowId, agentId);
    if (!lookup) return false;
    if (!canOpenSessionHistory(lookup.agent)) {
      return false;
    }
    this.openSessionDetail(
      workflowId,
      lookup.agent.name,
      lookup.phase.id,
      sessionNodeTypeOf(lookup.agent),
      returnTo,
    );
    return true;
  }

  private aggregateSessionStatus(
    members: WorkflowAgent[],
  ): WorkflowStatus {
    let aggregateStatus: WorkflowStatus = "completed";
    for (const member of members) {
      if (member.status === "running") {
        return "running";
      }
      if (member.status === "waiting_for_human") {
        aggregateStatus = "waiting_for_human";
      } else if (member.status === "failed" && aggregateStatus === "completed") {
        aggregateStatus = "failed";
      } else if (member.status !== "completed" && aggregateStatus === "completed") {
        aggregateStatus = member.status;
      }
    }
    return aggregateStatus;
  }

  /** Done turns for session parent progress (completed / failed / stopped). */
  private sessionDoneCount(members: WorkflowAgent[]): number {
    return members.filter(
      (member: WorkflowAgent) =>
        member.status === "completed" ||
        member.status === "failed" ||
        member.status === "stopped",
    ).length;
  }

  private formatSessionProgress(members: WorkflowAgent[]): string {
    return `${this.sessionDoneCount(members)}/${this.sessionTotalCount(members)}`;
  }

  private formatWorkflowAgentFieldText(value: string): string {
    try {
      const parsed = JSON.parse(value);
      return JSON.stringify(parsed, null, 2);
    } catch {
      return value;
    }
  }

  private appendLabeledFullText(
    lines: string[],
    width: number,
    label: string,
    value?: string,
    error = false,
  ): void {
    if (!value) return;
    lines.push(
      padToWidth(error ? palette.status.error(label) : palette.text.secondary(label), width),
    );
    const color = error ? palette.status.error : palette.text.secondary;
    const display = this.formatWorkflowAgentFieldText(value);
    lines.push(
      ...wrapPlainText(display, width).map((line) => padToWidth(color(line), width)),
    );
    lines.push("");
  }

  /** Cached human turn — honest placeholder, not replayed journal text. */
  private appendHumanTurnCachedPlaceholder(
    lines: string[],
    width: number,
    label: string,
    placeholder: string,
  ): void {
    lines.push(padToWidth(palette.text.secondary(label), width));
    lines.push(padToWidth(palette.text.dim(placeholder), width));
    lines.push("");
  }

  /** Human / human_session turn-detail action hint. */
  private formatHumanAgentActionHint(
    agent: WorkflowAgent,
    options?: { tabReply?: boolean },
  ): string {
    const parts: string[] = [];
    if (options?.tabReply) parts.push("Tab to reply");
    if (canOpenSessionHistory(agent)) parts.push("s session history");
    parts.push("press q to see full question");
    if (agent.status !== "waiting_for_human") {
      parts.push("a answer");
    }
    // Match agent / agent_session: always advertise e error (no-op when empty).
    parts.push("e error");
    return parts.join(" · ");
  }

  /** Agent / agent_session turn-detail action hint — mirrors human hint structure. */
  private formatAgentAgentActionHint(agent: WorkflowAgent): string {
    const parts: string[] = [];
    if (canOpenSessionHistory(agent)) parts.push("s session history");
    parts.push("press p to see full prompt");
    parts.push("o outcome");
    parts.push("e error");
    return parts.join(" · ");
  }

  private formatSwarmSessionDetailFooterHint(
    scrollHint: string,
    options: { waiting?: boolean; composing?: boolean },
  ): string {
    const parts: string[] = [];
    if (scrollHint) parts.push(scrollHint);
    if (options.composing) {
      parts.push("Enter submit");
    } else if (options.waiting) {
      parts.push("Tab to reply");
    }
    parts.push(`${this.swarmActionKeyLabel("swarm:budget")} budget`);
    parts.push("Esc/← back to agents");
    return parts.join(" · ");
  }

  private renderSwarmWorkflowAgentRows(
    agents: WorkflowAgent[] | undefined,
    width: number,
    maxRows = agents?.length ?? 0,
  ): string[] {
    if (!agents || agents.length === 0) return [padToWidth(palette.text.dim("No agents"), width)];

    const { sessions, oneShots } = groupWorkflowAgentsByName(agents);

    type DisplayGroup =
      | { type: "session"; label: string; members: WorkflowAgent[] }
      | { type: "oneshot"; agent: WorkflowAgent };

    const displayGroups: DisplayGroup[] = [
      ...sessions.map(({ label, members }) => ({ type: "session" as const, label, members })),
      ...oneShots.map((agent) => ({ type: "oneshot" as const, agent })),
    ];

    const lines: string[] = [];
    let renderedRows = 0;

    for (const group of displayGroups) {
      if (renderedRows >= maxRows) break;

      if (group.type === "session") {
        const { label, members } = group;
        const firstMember = members[0];
        const modelOrHuman = formatWorkflowAgentKindLabel(firstMember ?? {});
        const tokenValues = members
          .map((member: WorkflowAgent) => member.token_count)
          .filter((value: number | null | undefined): value is number =>
            typeof value === "number" && Number.isFinite(value));
        const tokenText =
          tokenValues.length > 0
            ? formatTokenCount(tokenValues.reduce((total: number, value: number) => total + value, 0))
            : null;
        const tokenSuffix = tokenText ? ` · ${tokenText} tok` : "";

        // Aggregate status: any running → ◐, any waiting → ☺, all completed → ✓
        const aggregateStatus = this.aggregateSessionStatus(members);

        const icon = workflowStatusIcon(aggregateStatus);
        const statusColor =
          aggregateStatus === "waiting_for_human"
            ? palette.text.humanInput
            : (s: string) => s;

        // Session parent summary row (phases preview only)
        lines.push(
          padToWidth(
            `  ${statusColor(icon)} ${label} ${palette.text.dim(`· ${this.formatSessionProgress(members)} · ${modelOrHuman}${tokenSuffix}`)}`,
            width,
          ),
        );
        renderedRows++;

        const hasActiveTurn = members.some(
          (member: WorkflowAgent) =>
            member.status === "waiting_for_human" ||
            member.status === "running" ||
            member.status === "pending",
        );
        if (hasActiveTurn || renderedRows < maxRows) {
          for (let i = 0; i < members.length && renderedRows < maxRows; i++) {
            const member = members[i]!;
            const turn = this.agentTurnNumber(member, i);
            const isLast = i === members.length - 1;
            const branch = isLast ? "└" : "├";
            const turnIcon = workflowStatusIcon(member.status);
            const turnStatusColor =
              member.status === "waiting_for_human"
                ? palette.text.humanInput
                : (s: string) => s;
            const statusWord = formatWorkflowStatusWord(member.status);
            const cachedSuffix = isHumanTurnCached(member)
              ? palette.text.dim(" · cached")
              : "";
            const tokenText = formatTokenCount(member.token_count);
            const tokenSuffix = tokenText ? palette.text.dim(` · ${tokenText} tok`) : "";

            lines.push(
              padToWidth(
                `    ${palette.text.dim(branch)} ${turnStatusColor(turnIcon)} turn ${turn} ${palette.text.secondary(`· ${statusWord}`)}${cachedSuffix}${tokenSuffix}`,
                width,
              ),
            );
            renderedRows++;
          }
        }
      } else {
        // Plain agent() / human() one-shot (session nodes always use the tree above)
        const { agent } = group;
        const icon = workflowStatusIcon(agent.status);
        const statusWord = formatWorkflowStatusWord(agent.status);
        const label = formatWorkflowAgentKindLabel(agent);
        const statusPart = ` ${palette.text.secondary(`· ${statusWord}`)}`;
        const labelPart = label ? ` ${palette.text.dim(`· ${label}`)}` : "";
        const tokenText = formatTokenCount(agent.token_count);
        const tokenPart = tokenText ? ` ${palette.text.dim(`· ${tokenText} tok`)}` : "";
        const statusColor =
          agent.status === "waiting_for_human"
            ? palette.text.humanInput
            : (s: string) => s;

        lines.push(
          padToWidth(
            `  ${statusColor(icon)} ${agent.name}${statusPart}${labelPart}${tokenPart}`,
            width,
          ),
        );
        renderedRows++;
      }
    }

    const totalGroups = displayGroups.length;
    const hiddenGroups = Math.max(0, totalGroups - displayGroups.slice(0, displayGroups.findIndex((_, i) => {
      let count = 0;
      for (let j = 0; j <= i; j++) {
        const g = displayGroups[j];
        if (!g) continue;
        if (g.type === "oneshot") {
          count++;
        } else {
          count++; // summary row
          const hasWaiting = g.members.some((m: WorkflowAgent) => m.status === "waiting_for_human");
          if (hasWaiting) {
            count += g.members.length; // all turn sub-rows
          }
        }
      }
      return count >= maxRows;
    }) + 1).length);

    if (renderedRows >= maxRows && hiddenGroups > 0) {
      lines.push(
        padToWidth(
          palette.text.dim(`  ... ${hiddenGroups} more - Enter/→ show agents`),
          width,
        ),
      );
    }

    return lines;
  }

  private buildSessionDetailLines(
    state: Extract<SwarmWorkflowsViewState, { phase: "session-detail" }>,
    width: number,
  ): string[] {
    const workflow = this.state.getSnapshot().workflowRuns.find((w) => w.id === state.workflowId);
    if (!workflow) return [padToWidth(palette.status.error("Workflow not found"), width)];

    const phase = workflow.phases?.find((item) => item.id === state.phaseId);

    const sessionAgents = sessionMembersInPhase(
      phase?.agents ?? [],
      state.sessionLabel,
      state.nodeType,
    ).map((agent) => ({ agent }));

    if (sessionAgents.length === 0) {
      return [padToWidth(palette.status.error("Session not found"), width)];
    }

    const firstAgent = sessionAgents[0]!.agent;
    const isHumanSession =
      state.nodeType === "human_session" ||
      firstAgent.node_type === "human_session" ||
      (firstAgent.node_type === undefined && firstAgent.kind === "human");
    const sessionType =
      state.nodeType === "human_session" || state.nodeType === "agent_session"
        ? state.nodeType
        : isHumanSession
          ? "human_session"
          : "agent_session";
    const phaseName = phase?.name ?? state.phaseId;
    const aggregateStatus = this.aggregateSessionStatus(sessionAgents.map(({ agent }) => agent));

    const divider = padToWidth(palette.text.dim("─".repeat(Math.max(1, width))), width);

    const headerLines: string[] = [
      padToWidth(palette.text.accent(`${state.sessionLabel} · ${phaseName}`), width),
      padToWidth(
        palette.text.dim(
          `${workflow.name} · ${sessionType} · ${formatWorkflowStatus(aggregateStatus)} · ${this.formatSessionProgress(sessionAgents.map(({ agent }) => agent))} turns`,
        ),
        width,
      ),
      divider,
      "",
    ];

    const bodyLines: string[] = [];
    const memberAgents = sessionAgents.map(({ agent }) => agent);
    for (let i = 0; i < sessionAgents.length; i++) {
      const { agent } = sessionAgents[i]!;
      const turn = this.agentTurnNumber(agent, i);
      const turnLabel = `Turn ${turn}`;
      const isWaiting = agent.status === "waiting_for_human";
      const isRunning = agent.status === "running";
      const duration = formatWorkflowDuration(agent.duration_ms);
      const tokenText = formatTokenCount(agent.token_count);

      let turnHeader: string;
      if (isWaiting) {
        turnHeader = `${turnLabel} ${palette.text.humanInput(`${workflowStatusIcon("waiting_for_human")} waiting`)}`;
      } else if (isRunning) {
        turnHeader = `${turnLabel} ${palette.text.dim(`◐ ${agent.status}`)}`;
      } else if (agent.status === "completed") {
        turnHeader = `${turnLabel} ${palette.text.dim("✓ completed")}`;
      } else {
        turnHeader = `${turnLabel} ${palette.status.error(workflowStatusIcon(agent.status))} ${palette.text.dim(agent.status)}`;
      }
      bodyLines.push(padToWidth(turnHeader, width));
      if (duration) {
        bodyLines.push(padToWidth(palette.text.dim(`  duration ${duration}`), width));
      }
      if (tokenText) {
        bodyLines.push(padToWidth(palette.text.dim(`  tokens ${tokenText}`), width));
      }
      bodyLines.push("");

      if (isHumanSession) {
        if (isHumanTurnCached(agent)) {
          this.appendHumanTurnCachedPlaceholder(
            bodyLines,
            width,
            "Question",
            HUMAN_TURN_CACHED_QUESTION,
          );
          if (!isRunning && !isWaiting) {
            this.appendHumanTurnCachedPlaceholder(
              bodyLines,
              width,
              "Answer",
              HUMAN_TURN_CACHED_ANSWER,
            );
          }
        } else {
          this.appendLabeledFullText(
            bodyLines,
            width,
            "Question",
            this.getHumanQuestionText(state.workflowId, agent.id),
          );
          if (isWaiting) {
            bodyLines.push(padToWidth(palette.text.secondary("Answer"), width));
            if (this.replyingToHumanPrompt) {
              bodyLines.push(padToWidth(palette.text.dim("  (composing reply below)"), width));
            } else {
              bodyLines.push(padToWidth(palette.text.dim("  (waiting for reply)"), width));
            }
            bodyLines.push("");
          } else if (!isRunning) {
            this.appendLabeledFullText(bodyLines, width, "Answer", agent.human_reply);
          }
        }
        // Always surface Error when present (e.g. timeout → failed), same as agent_session.
        this.appendLabeledFullText(bodyLines, width, "Error", agent.error, true);
      } else {
        this.appendLabeledFullText(bodyLines, width, "Prompt", agent.prompt);
        if (agent.activity?.length) {
          bodyLines.push(padToWidth(palette.text.secondary("Activity"), width));
          for (const item of agent.activity) {
            const prefix = item.type ? `${item.type}: ` : "";
            bodyLines.push(
              ...wrapPlainText(`- ${prefix}${item.content}`, width).map((line) =>
                padToWidth(palette.text.dim(line), width),
              ),
            );
          }
          bodyLines.push("");
        }
        if (isRunning) {
          bodyLines.push(padToWidth(palette.text.secondary("Outcome"), width));
          bodyLines.push(padToWidth(palette.text.dim("  (running...)"), width));
          bodyLines.push("");
        } else {
          this.appendLabeledFullText(bodyLines, width, "Outcome", agent.outcome);
        }
        // Show Error whenever set — not only after the turn leaves running.
        this.appendLabeledFullText(bodyLines, width, "Error", agent.error, true);
      }

      if (i < sessionAgents.length - 1) {
        bodyLines.push(divider);
        bodyLines.push("");
      }
    }

    // Windowed viewport: header/footer stay fixed, body scrolls.
    // The layout pins questionLines to the top and drops overflow, so we clamp
    // the panel to the terminal height ourselves and window the body region.
    const bodyViewport = this.sessionDetailBodyViewport(headerLines.length);
    const maxScroll = Math.max(0, bodyLines.length - bodyViewport);
    const scrollOffset = Math.min(Math.max(0, state.scrollOffset), maxScroll);
    state.scrollOffset = scrollOffset;
    const canScroll = bodyLines.length > bodyViewport;
    const visibleBody = canScroll
      ? bodyLines.slice(scrollOffset, scrollOffset + bodyViewport)
      : bodyLines;

    const lines: string[] = [...headerLines];
    if (canScroll && scrollOffset > 0) {
      lines.push(padToWidth(palette.text.dim(`  ↑ ${scrollOffset} more lines above`), width));
    }
    lines.push(...visibleBody);
    const belowCount = bodyLines.length - (scrollOffset + visibleBody.length);
    if (canScroll && belowCount > 0) {
      lines.push(padToWidth(palette.text.dim(`  ↓ ${belowCount} more lines below`), width));
    }

    const scrollHint = canScroll ? "↑/↓ scroll · PgUp/PgDn page" : "";
    const waitingAgent = sessionAgents.find(({ agent }) => agent.status === "waiting_for_human");
    lines.push(
      padToWidth(
        palette.text.secondary(
          this.formatSwarmSessionDetailFooterHint(scrollHint, {
            waiting: Boolean(waitingAgent) && !this.replyingToHumanPrompt,
            composing: Boolean(waitingAgent) && Boolean(this.replyingToHumanPrompt),
          }),
        ),
        width,
      ),
    );

    return lines;
  }

  /** Rows of session-detail body visible per page, mirrors buildSessionDetailLines. */
  private sessionDetailBodyViewport(headerRows: number): number {
    const reservedChrome = (this.replyingToHumanPrompt ? 4 : 0) + 6;
    const maxPanelRows = Math.max(8, this.tui.terminal.rows - reservedChrome);
    // Reserve 3 rows for the two scroll indicators plus the footer hint.
    return Math.max(3, maxPanelRows - headerRows - 3);
  }

  private buildSwarmWorkflowAgentLines(
    state: Extract<SwarmWorkflowsViewState, { phase: "agent" }>,
    width: number,
  ): string[] {
    const lookup = findWorkflowAgent(
      this.state.getSnapshot().workflowRuns,
      state.workflowId,
      state.agentId,
    );
    if (!lookup) return [padToWidth(palette.status.error("Agent not found"), width)];
    const { workflow, phase, agent } = lookup;
    const budgetKey = this.swarmActionKeyLabel("swarm:budget");
    const duration = formatWorkflowDuration(agent.duration_ms);
    const tokenText = formatTokenCount(agent.token_count);
    const isHuman = agent.kind === "human";
    const kindLabel = formatWorkflowAgentKindLabel(agent);

    const statusStr = isHuman
      ? `${workflowStatusTone(agent.status)(`${workflowStatusIcon(agent.status)} ${agent.status}`)} · ${kindLabel}`
      : `${workflowStatusTone(agent.status)(`${workflowStatusIcon(agent.status)} ${agent.status}`)}${agent.model ? ` · ${palette.text.secondary(agent.model)}` : ""}`;

    const turnNumber = sessionTurnLabelNumber(agent, phase.agents ?? []);
    const titleLine = turnNumber !== null ? `${agent.name} · turn ${turnNumber}` : agent.name;
    const lines: string[] = [
      padToWidth(palette.text.accent(titleLine), width),
      padToWidth(palette.text.dim(`${workflow.name} · ${phase.name}`), width),
      padToWidth(statusStr, width),
      "",
    ];
    if (duration) {
      lines.splice(3, 0, padToWidth(palette.text.dim(`duration ${duration}`), width));
    }
    if (tokenText) {
      lines.splice(3, 0, padToWidth(palette.text.dim(`tokens ${tokenText}`), width));
    }

    // Show full details for current turn
    if (isHuman) {
      if (isHumanTurnCached(agent)) {
        this.appendHumanTurnCachedPlaceholder(
          lines,
          width,
          "Question",
          HUMAN_TURN_CACHED_QUESTION,
        );
        this.appendHumanTurnCachedPlaceholder(lines, width, "Answer", HUMAN_TURN_CACHED_ANSWER);
        this.appendLabeledWrappedPreview(lines, width, "Error", agent.error, "e", true);
        lines.push(
          padToWidth(
            palette.text.secondary(this.formatHumanAgentActionHint(agent)),
            width,
          ),
        );
      } else {
        this.appendLabeledWrappedPreview(lines, width, "Question", agent.human_prompt || this.getHumanQuestionText(state.workflowId, state.agentId), "q");
        if (agent.status === "waiting_for_human") {
          lines.push(padToWidth(palette.text.secondary("Answer"), width));
          lines.push(padToWidth(palette.text.secondary("  (waiting for reply)"), width));
          lines.push("");
        } else {
          this.appendLabeledWrappedPreview(lines, width, "Answer", agent.human_reply, "a");
        }
        this.appendLabeledWrappedPreview(lines, width, "Error", agent.error, "e", true);
        lines.push(
          padToWidth(
            palette.text.secondary(
              this.formatHumanAgentActionHint(agent, {
                tabReply: agent.status === "waiting_for_human",
              }),
            ),
            width,
          ),
        );
      }
    } else {
      // Agent / agent_session: Prompt + Outcome + Error + Activity
      this.appendLabeledWrappedPreview(lines, width, "Prompt", agent.prompt, "p");
      if (agent.activity?.length) {
        lines.push(padToWidth(palette.text.secondary("Activity"), width));
        for (const item of agent.activity) {
          const prefix = item.type ? `${item.type}: ` : "";
          lines.push(
            ...wrapPlainText(`- ${prefix}${item.content}`, width).map((line) =>
              padToWidth(palette.text.dim(line), width),
            ),
          );
        }
        lines.push("");
      }
      this.appendLabeledWrappedPreview(lines, width, "Outcome", agent.outcome, "o");
      this.appendLabeledWrappedPreview(lines, width, "Error", agent.error, "e", true);
      lines.push(
        padToWidth(palette.text.secondary(this.formatAgentAgentActionHint(agent)), width),
      );
    }

    const backHint =
      state.returnTo?.kind === "pending-list"
        ? "Esc/← back to pending list"
        : "Esc/← back to agents";
    lines.push(padToWidth(palette.text.secondary(backHint), width));
    return lines;
  }

  private appendLabeledWrappedPreview(
    lines: string[],
    width: number,
    label: string,
    value?: string,
    fullViewKey?: string,
    error = false,
  ): void {
    if (!value) return;
    lines.push(
      padToWidth(error ? palette.status.error(label) : palette.text.secondary(label), width),
    );
    const color = error ? palette.status.error : palette.text.secondary;
    const wrapped = wrapPlainText(value, width);
    const visible = wrapped.slice(0, SWARM_WORKFLOW_AGENT_TEXT_PREVIEW_ROWS);
    lines.push(...visible.map((line) => padToWidth(color(line), width)));
    if (wrapped.length > visible.length) {
      const keyHint = fullViewKey
        ? ` - press ${fullViewKey} to see full ${label.toLowerCase()}`
        : "";
      lines.push(
        padToWidth(
          palette.text.dim(`... ${wrapped.length - visible.length} more lines${keyHint}`),
          width,
        ),
      );
    }
    lines.push("");
  }

  private buildOutgoingMessage(text: string): { content: string; attachments: FileAttachment[] } {
    const expandedText = this.expandPastedText(text);
    return {
      content: this.expandPastedText(text.replace(/[ \t]{2,}/g, " ").replace(/[ \t]+\n/g, "\n").trim()),
      attachments: this.collectComposerAttachments(expandedText),
    };
  }

  /**
   * 从消息文本里提取被 /<skillName> 标记的已装 skill 名（用于 params.skills）。
   *
   * 规则：
   * - 遍历已装 skill 名，在 content 里搜 `/<完整名>`。无空格也识别（如 `/doc写文档`）。
   * - `/` 前必须是行首或空白（`(^|\s)/name`），避免 `路径a/doc` 这种误命中。
   * - skill 名后必须是词边界（`/name\b`），避免 `/docs`、`/doc123` 这类更长非 skill
   *   文本被当成短 skill 名误命中（如 `/docs` 不该命中 `doc`）。
   *   u 模式下 CJK 字符不属于 `\w`，故 `/doc写文档` 的 `doc` 后是 `\b` 边界，正常命中。
   * - content 本身不改动，仅返回命中的 skill 名（去重，按 content 中出现位置排序）。
   */
  private extractSkillsFromContent(content: string): string[] {
    if (!content) return [];
    const installed = this.commands.getInstalledSkills();
    const found: { name: string; idx: number }[] = [];
    for (const skill of installed) {
      const escaped = skill.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const re = new RegExp(`(^|\\s)/${escaped}\\b`, "u");
      const match = re.exec(content);
      if (match && !found.some((f) => f.name === skill.name)) {
        found.push({ name: skill.name, idx: match.index });
      }
    }
    found.sort((a, b) => a.idx - b.idx);
    return found.map((f) => f.name);
  }

  private handleConfigEditorInput(data: string): void {
    if (!this.configEditorState) return;
    const state = this.configEditorState;

    // ── search_list phase ──
    if (state.phase === "search_list") {
      if (state.searchMode) {
        // Search mode: intercept printable chars, backspace, ESC
        const printableChar = this.getPrintableChar(data);
        if (printableChar !== undefined) {
          const newQuery = state.searchQuery + printableChar;
          this.updateConfigSearchQuery(newQuery);
        } else if (matchesKey(data, "backspace")) {
          const newQuery = state.searchQuery.slice(0, -1);
          this.updateConfigSearchQuery(newQuery);
        } else if (matchesKey(data, "escape")) {
          // One-ESC exit: leave the editor entirely (back to the StatusView config
          // tab when invoked from /status, or closed when invoked via /config).
          // We do not first clear the search query — a single ESC returns to the
          // original page, no intermediate search_list step.
          this.closeConfigEditor();
        } else if (matchesKey(data, "return") || matchesKey(data, "space")) {
          const selectedItem = state.list.getSelectedItem();
          if (selectedItem) {
            if (selectedItem.value.startsWith("__group_")) return;
            const schema = state.schemaList.find((s) => s.key === selectedItem.value);
            if (schema) {
              this.handleConfigItemSelectionFromFlatList(schema);
            }
          }
        } else if (matchesKey(data, "up") || matchesKey(data, "down")) {
          state.list.handleInput(data);
        }
      } else {
        // Browse mode: navigation + actions
        const printableChar = this.getPrintableChar(data);
        if (data === "/" || printableChar !== undefined) {
          // Re-enter search mode
          const initialChar = data === "/" ? "" : (printableChar ?? "");
          this.configEditorState = { ...this.configEditorState!, searchMode: true };
          this.updateConfigSearchQuery(initialChar);
        } else if (matchesKey(data, "escape")) {
          this.closeConfigEditor();
        } else if (matchesKey(data, "return") || matchesKey(data, "space")) {
          const selectedItem = state.list.getSelectedItem();
          if (selectedItem) {
            if (selectedItem.value.startsWith("__group_")) return;
            const schema = state.schemaList.find((s) => s.key === selectedItem.value);
            if (schema) {
              this.handleConfigItemSelectionFromFlatList(schema);
            }
          }
        } else {
          state.list.handleInput(data);
        }
      }
      return;
    }

    // ── select_value phase ──
    if (state.phase === "select_value") {
      if (matchesKey(data, "escape")) {
        // One-ESC exit: leave the editor entirely (back to the StatusView config
        // tab when invoked from /status, or closed when invoked via /config).
        // We intentionally do NOT return to the search_list intermediate page.
        this.closeConfigEditor();
        return;
      }
      // Delegate to list for navigation + selection
      state.list.handleInput(data);
      return;
    }

    // ── input_value phase ──
    if (state.phase === "input_value") {
      if (matchesKey(data, "escape")) {
        // One-ESC exit: leave the editor entirely (back to the StatusView config
        // tab when invoked from /status, or closed when invoked via /config).
        // We intentionally do NOT return to the search_list intermediate page.
        this.editor.setText("");
        this.closeConfigEditor();
        return;
      }
      if (matchesKey(data, "return")) {
        const text = this.editor.getText().trim();
        if (text && state.selectedKey) {
          const key = state.selectedKey;
          const schema = state.schemaList.find((s) => s.key === key);
          if (schema) {
            void this.applyConfigEditorSetAndStay(key, text, schema, state.currentValues);
          }
          this.editor.setText("");
        }
        return;
      }
      this.editor.handleInput(data);
      return;
    }
  }

  private buildConfigEditorLines(width: number): string[] {
    if (!this.configEditorState) {
      return [];
    }
    const state = this.configEditorState;
    const blank = "";  // Spacer line for visual breathing room

    const lines: string[] = [];

    // Title line
    if (state.phase === "search_list") {
      const title = state.mode === "reset" ? "重置配置项" : "配置编辑器";
      lines.push(padToWidth(palette.status.warning(title), width));
    } else if (state.phase === "select_value") {
      const schema = state.schemaList.find((s) => s.key === state.selectedKey);
      lines.push(padToWidth(palette.status.warning(`选择 "${schema?.label ?? state.selectedKey}" 的值`), width));
    } else {
      const schema = state.schemaList.find((s) => s.key === state.selectedKey);
      lines.push(padToWidth(palette.status.warning(`输入 "${schema?.label ?? state.selectedKey}" 的新值`), width));
    }

    lines.push(blank);  // Gap between title and content

    // Search box for search_list phase
    if (state.phase === "search_list") {
      const searchHint = state.mode === "reset"
        ? "输入搜索 · ↑/↓ 选择 · Enter/空格 重置 · / 搜索 · Esc 关闭"
        : "输入搜索 · ↑/↓ 选择 · Enter/空格 修改 · / 搜索 · Esc 关闭";
      if (state.searchMode) {
        lines.push(padToWidth(palette.text.primary(`搜索: ${state.searchQuery}${END_CURSOR}`), width));
      } else {
        lines.push(padToWidth(palette.text.dim(searchHint), width));
      }
      lines.push(blank);  // Gap between search box and list
    }

    // Current value display for select_value / input_value
    if ((state.phase === "select_value" || state.phase === "input_value") && state.selectedKey) {
      const schema = state.schemaList.find((s) => s.key === state.selectedKey);
      const rawVal = state.currentValues[state.selectedKey] ?? "";
      const currentVal = formatConfigValue(schema!, rawVal);
      lines.push(padToWidth(palette.text.dim(`当前值: ${currentVal}`), width));
      lines.push(blank);  // Gap between current value and content
    }

    // Content area
    if (state.phase === "input_value") {
      lines.push(...this.editor.render(width));
    } else {
      lines.push(...state.list.render(width));
    }

    lines.push(blank);  // Gap between content and hint

    // Hint line
    const actionLabel = state.mode === "reset" ? "重置" : "修改";
    let hint: string;
    if (state.phase === "search_list") {
      hint = state.searchMode
        ? "Backspace 删除 · ↑/↓ 导航 · Enter 选择 · Esc 清除搜索"
        : `↑/↓ 选择 · Enter/空格 ${actionLabel} · / 搜索 · Esc 关闭`;
    } else if (state.phase === "input_value") {
      hint = "输入值 · Enter 确认 · Esc 返回";
    } else {
      hint = "↑/↓ 选择 · Enter 确认 · Esc 返回";
    }
    lines.push(padToWidth(palette.text.dim(hint), width));
    return lines;
  }

  private updateConfigSearchQuery(query: string): void {
    if (!this.configEditorState) return;
    const filteredItems = filterConfigItems(
      this.configEditorState.schemaList,
      this.configEditorState.currentValues,
      query,
    );
    const list = new SelectList(
      filteredItems,
      Math.min(Math.max(filteredItems.length, 1), 10),
      selectListTheme,
      { minPrimaryColumnWidth: 24, maxPrimaryColumnWidth: 42 },
    );
    list.onSelect = (item) => {
      if (item.value.startsWith("__group_")) return; // Skip group headers
      const schema = this.configEditorState!.schemaList.find((s) => s.key === item.value);
      if (!schema) return;
      this.handleConfigItemSelectionFromFlatList(schema);
    };
    list.onCancel = () => {
      this.closeConfigEditor();
    };
    this.configEditorState = {
      ...this.configEditorState,
      list,
      searchQuery: query,
    };
    this.tui.requestRender();
  }

  private handleConfigItemSelectionFromFlatList(
    schema: ConfigItemSchema,
  ): void {
    const currentValues = this.configEditorState!.currentValues;
    const mode = this.configEditorState!.mode;

    // reset 模式：直接重置为默认值，不需要子面板
    if (mode === "reset") {
      const defaultValue = schema.default ?? "";
      if (defaultValue) {
        void this.applyConfigEditorSetAndStay(schema.key, defaultValue, schema, currentValues);
      } else {
        this.state.addItem(addInfo(this.state.getSnapshot().sessionId, `${schema.key} has no default value`, "c"));
      }
      return;
    }

    // edit 模式：原有逻辑
    if (schema.type === "toggle") {
      const currentVal = currentValues[schema.key] ?? "false";
      const newValue = currentVal === "true" ? "false" : "true";
      void this.applyConfigEditorSetAndStay(schema.key, newValue, schema, currentValues);
      return;
    }

    // Save current list for returning later
    const savedList = this.configEditorState!.list;

    if (schema.type === "select" && schema.options) {
      const valueList = this.buildConfigValueSelectList(schema, currentValues);
      this.configEditorState = {
        ...this.configEditorState!,
        phase: "select_value",
        selectedKey: schema.key,
        previousPhase: "search_list",
        savedList,
        list: valueList,
      };
      this.tui.requestRender();
      return;
    }

    // string / password → input mode
    this.editor.setText("");
    this.configEditorState = {
      ...this.configEditorState!,
      phase: "input_value",
      selectedKey: schema.key,
      previousPhase: "search_list",
      savedList,
    };
    this.tui.requestRender();
  }

  private buildConfigValueSelectList(
    schema: ConfigItemSchema,
    currentValues: Record<string, string>,
  ): SelectList {
    const currentValue = currentValues[schema.key] ?? "";
    const items: SelectItem[] = (schema.options ?? []).map((option) => ({
      value: option,
      label: option,
      description: option === currentValue ? "(current)" : undefined,
    }));
    const list = new SelectList(items, Math.min(Math.max(items.length, 1), 8), selectListTheme, {
      minPrimaryColumnWidth: 24,
      maxPrimaryColumnWidth: 42,
    });
    list.onSelect = (item) => {
      void this.applyConfigEditorSetAndStay(schema.key, item.value, schema, currentValues);
    };
    return list;
  }

  private async applyConfigEditorSetAndStay(
    key: string,
    value: string,
    schema: ConfigItemSchema,
    currentValues: Record<string, string>,
  ): Promise<void> {
    const isReset = this.configEditorState?.mode === "reset";
    const valueDisplay = schema.sensitive ? "******" : value;
    const statusLabel = isReset ? "已重置" : "已应用";
    const restartLabel = isReset ? "已重置(需重启)" : "需重启";

    // Handle frontend-only config keys (theme)
    if (key === "theme") {
      this.state.setThemeName(value as import("./theme.js").ThemeName);
      currentValues[key] = value;
      this.state.addItem(addInfo(this.state.getSnapshot().sessionId, `✓ ${key}: ${valueDisplay} (${statusLabel})`, "c"));
      this.refreshConfigEditorList();
      return;
    }
    try {
      const result = await this.state.request<{
        updated: string[];
        applied_without_restart: boolean;
      }>("config.set", { [key]: value });
      currentValues[key] = value;
      const msg = result.applied_without_restart
        ? `✓ ${key}: ${valueDisplay} (${statusLabel})`
        : `✓ ${key}: ${valueDisplay} (${restartLabel})`;
      this.state.addItem(addInfo(this.state.getSnapshot().sessionId, msg, "c"));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.state.addItem(addError(this.state.getSnapshot().sessionId, `config.set failed: ${message}`));
    }
    this.refreshConfigEditorList();
  }

  private refreshConfigEditorList(): void {
    if (!this.configEditorState) return;

    // After a change, return to the flat list with browse mode so user can see updated values
    const filteredItems = filterConfigItems(
      this.configEditorState.schemaList,
      this.configEditorState.currentValues,
      this.configEditorState.searchQuery,
    );
    const list = new SelectList(
      filteredItems,
      Math.min(Math.max(filteredItems.length, 1), 10),
      selectListTheme,
      { minPrimaryColumnWidth: 24, maxPrimaryColumnWidth: 42 },
    );
    list.onSelect = (item) => {
      if (item.value.startsWith("__group_")) return; // Skip group headers
      const schema = this.configEditorState!.schemaList.find((s) => s.key === item.value);
      if (!schema) return;
      this.handleConfigItemSelectionFromFlatList(schema);
    };
    list.onCancel = () => {
      this.closeConfigEditor();
    };

    // If we were in StatusView, return to config tab
    if (this.statusViewState) {
      this.statusViewState.phase = "tab_view";
      this.statusViewState.tab = "config";
      this.rebuildStatusViewTabList();
      this.configEditorState = null;
      this.tui.requestRender();
      // Re-fetch status & config payloads so the status tab shows updated values
      // (e.g. model name changed via config editor)
      this.refreshStatusViewPayloads();
      return;
    }

    this.configEditorState = {
      ...this.configEditorState,
      phase: "search_list",
      selectedKey: null,
      previousPhase: null,
      savedList: null,
      searchMode: false,
      list,
    };
    this.tui.requestRender();
  }

  private closeConfigEditor(): void {
    if (this.statusViewState) {
      this.statusViewState.phase = "tab_view";
      this.statusViewState.tab = "config";
      this.statusViewState.searchMode = false;
      this.statusViewState.searchQuery = "";
      this.rebuildStatusViewTabList();
      // Re-fetch payloads so status tab reflects any config changes made in the editor
      this.refreshStatusViewPayloads();
    }
    this.configEditorState = null;
    this.tui.requestRender();
  }

  private openConfigEditor(
    focusKey?: string,
    configPayload?: Record<string, unknown> & { schema?: ConfigItemSchema[] },
    mode?: ConfigEditorMode,
  ): void {
    const editorMode: ConfigEditorMode = mode ?? "edit";
    const schemaList = configPayload?.schema ?? [];
    if (schemaList.length === 0) {
      this.state.addItem(addError(this.state.getSnapshot().sessionId, "No config schema available"));
      return;
    }
    const currentValues: Record<string, string> = {};
    for (const schema of schemaList) {
      currentValues[schema.key] = String(configPayload?.[schema.key] ?? "");
    }

    if (focusKey) {
      const schema = schemaList.find((s) => s.key === focusKey);
      if (schema) {
        // Initialize base state then immediately transition to the item's sub-panel
        const filteredItems = filterConfigItems(schemaList, currentValues, "");
        const baseList = new SelectList(
          filteredItems,
          Math.min(Math.max(filteredItems.length, 1), 10),
          selectListTheme,
          { minPrimaryColumnWidth: 24, maxPrimaryColumnWidth: 42 },
        );
        this.configEditorState = {
          phase: "search_list",
          mode: editorMode,
          schemaList,
          currentValues,
          selectedKey: null,
          searchQuery: "",
          searchMode: true,
          list: baseList,
          previousPhase: null,
          savedList: null,
        };
        this.handleConfigItemSelectionFromFlatList(schema);
        return;
      }
    }

    // Default: start in search_list mode with search enabled (search-first approach)
    const filteredItems = filterConfigItems(schemaList, currentValues, "");
    const list = new SelectList(
      filteredItems,
      Math.min(Math.max(filteredItems.length, 1), 10),
      selectListTheme,
      { minPrimaryColumnWidth: 24, maxPrimaryColumnWidth: 42 },
    );
    list.onSelect = (item) => {
      if (item.value.startsWith("__group_")) return; // Skip group headers
      const schema = this.configEditorState!.schemaList.find((s) => s.key === item.value);
      if (!schema) return;
      this.handleConfigItemSelectionFromFlatList(schema);
    };
    list.onCancel = () => {
      this.closeConfigEditor();
    };

    this.configEditorState = {
      phase: "search_list",
      mode: editorMode,
      schemaList,
      currentValues,
      selectedKey: null,
      searchQuery: "",
      searchMode: true,
      list,
      previousPhase: null,
      savedList: null,
    };
    this.tui.requestRender();
  }

  // ──────────────────────────── StatusView ────────────────────────────

  private async openStatusView(tab?: StatusViewTab): Promise<void> {
    const initialTab: StatusViewTab = tab ?? "status";

    // Fetch status payload
    let statusPayload: import("../core/commands/builtins/status.js").StatusPayload | null = null;
    try {
      statusPayload = await this.state.request<import("../core/commands/builtins/status.js").StatusPayload>(
        "command.status",
        {},
      );
    } catch {
      // proceed with null — tab will show placeholder
    }

    // Fetch config payload (needed for Config tab)
    let configPayload: (Record<string, unknown> & { schema?: ConfigItemSchema[] }) | null = null;
    try {
      configPayload = await this.state.request<Record<string, unknown> & { schema?: ConfigItemSchema[] }>(
        "config.get",
        {},
      );
    } catch {
      // proceed with null
    }

    this.statusViewState = {
      phase: "tab_view",
      tab: initialTab,
      list: this.buildStatusViewTabState(initialTab, statusPayload, configPayload, ""),
      statusPayload,
      configPayload,
      searchMode: false,
      searchQuery: "",
    };
    this.tui.requestRender();
  }

  private buildStatusViewTabState(
    tab: StatusViewTab,
    statusPayload: import("../core/commands/builtins/status.js").StatusPayload | null,
    configPayload: (Record<string, unknown> & { schema?: ConfigItemSchema[] }) | null,
    searchQuery: string,
  ): SelectList {
    const items: SelectItem[] =
      tab === "status"
        ? this.buildStatusTabItems(statusPayload)
        : tab === "usage"
          ? this.buildUsageTabItems()
          : this.buildConfigTabItems(configPayload, searchQuery);

    const list = new SelectList(items, tab === "status" ? items.length : Math.min(Math.max(items.length, 1), 10), selectListTheme, {
      minPrimaryColumnWidth: 20,
      maxPrimaryColumnWidth: 50,
    });
    list.onSelect = (item) => {
      if (tab === "config" && item.value !== "__display__") {
        this.transitionToConfigEditor(item.value);
      }
    };
    list.onCancel = () => {
      this.closeStatusView();
    };
    return list;
  }

  private buildStatusTabItems(
    payload: import("../core/commands/builtins/status.js").StatusPayload | null,
  ): SelectItem[] {
    if (!payload) {
      return [{ value: "__display__", label: "Failed to load status data", description: "" }];
    }
    const snapshot = this.state.getSnapshot();
    const items: SelectItem[] = [
      { value: "__display__", label: `version: ${payload.version || "unknown"}`, description: "" },
      { value: "__display__", label: `session: ${payload.session_id || snapshot.sessionId}`, description: "" },
      { value: "__display__", label: `name: ${snapshot.sessionTitle || "/rename to add a name"}`, description: "" },
      { value: "__display__", label: `cwd: ${payload.cwd || "unknown"}`, description: "" },
      { value: "__display__", label: `mode: ${formatModeForDisplay(snapshot.mode)}`, description: "" },
      { value: "__display__", label: `model: ${payload.model || "unknown"}`, description: "" },
      { value: "__display__", label: `provider: ${payload.provider || "unknown"}`, description: "" },
      { value: "__display__", label: `api_base: ${payload.api_base || "unknown"}`, description: "" },
      { value: "__display__", label: `connection: ${payload.connection_status || snapshot.connectionStatus}`, description: "" },
    ];

    const mcpServers = payload.mcp_servers ?? [];
    for (const srv of mcpServers) {
      items.push({
        value: "__display__",
        label: `mcp: ${srv.name}`,
        description: `${srv.transport} | ${srv.enabled ? "enabled" : "disabled"}`,
      });
    }

    const sources = payload.settings_sources ?? [];
    for (const s of sources) {
      items.push({ value: "__display__", label: `config_source: ${s}`, description: "" });
    }
    items.push({ value: "__display__", label: `config_path: ${payload.config_path || "unknown"}`, description: "" });

    const warnings = payload.memory_warnings ?? [];
    for (const w of warnings) {
      items.push({ value: "__display__", label: `⚠ memory: ${w.message}`, description: "" });
    }

    return items;
  }

  private buildUsageTabItems(): SelectItem[] {
    const summary = this.state.getUsageSummary();
    const fmt = (n: number) => n.toLocaleString("en-US");
    const items: SelectItem[] = [
      { value: "__display__", label: `input_tokens: ${fmt(summary.total_input_tokens)}`, description: "" },
      { value: "__display__", label: `output_tokens: ${fmt(summary.total_output_tokens)}`, description: "" },
      { value: "__display__", label: `total_tokens: ${fmt(summary.total_tokens)}`, description: "" },
    ];

    for (const entry of summary.byModel) {
      items.push(
        { value: "__display__", label: `model: ${entry.model}`, description: `${fmt(entry.total_tokens)} tokens` },
        { value: "__display__", label: `  input`, description: fmt(entry.input_tokens) },
        { value: "__display__", label: `  output`, description: fmt(entry.output_tokens) },
      );
    }
    return items;
  }

  private buildConfigTabItems(
    configPayload: (Record<string, unknown> & { schema?: ConfigItemSchema[] }) | null,
    searchQuery: string,
  ): SelectItem[] {
    if (!configPayload?.schema?.length) {
      return [{ value: "__display__", label: "No config schema available", description: "" }];
    }
    const schemaList = configPayload.schema;

    const filteredSchemaList = searchQuery
      ? schemaList.filter((schema) => {
          const q = searchQuery.toLowerCase();
          return (
            schema.key.toLowerCase().includes(q) ||
            schema.label.toLowerCase().includes(q) ||
            (schema.description ?? "").toLowerCase().includes(q) ||
            (schema.group ?? "").toLowerCase().includes(q)
          );
        })
      : schemaList;

    if (filteredSchemaList.length === 0) {
      return [{ value: "__display__", label: `No config items match "${searchQuery}"`, description: "" }];
    }

    const groups: Record<string, ConfigItemSchema[]> = {};
    for (const schema of filteredSchemaList) {
      const group = schema.group || "Other";
      if (!groups[group]) groups[group] = [];
      groups[group].push(schema);
    }

    const items: SelectItem[] = [];
    for (const groupName of Object.keys(groups)) {
      const groupSchemas = groups[groupName];
      items.push({ value: "__display__", label: groupName, description: `${groupSchemas.length} items` });
      for (const schema of groupSchemas) {
        const val = String(configPayload[schema.key] ?? "");
        const displayVal =
          schema.type === "toggle"
            ? val === "true" ? "Enabled" : "Disabled"
            : schema.sensitive
              ? val ? "******" : "(empty)"
              : val || "(empty)";
        items.push({
          value: schema.key,
          label: `  ${schema.label}: ${displayVal}`,
          description: schema.description,
        });
      }
    }
    return items;
  }

  private renderTabBar(width: number): string[] {
    const tabs: StatusViewTab[] = ["status", "usage", "config"];
    const labels = tabs.map((t) => (t === this.statusViewState!.tab ? `[${t}]` : ` ${t} `));
    const barText = labels.join("  ");
    const activeIndex = tabs.indexOf(this.statusViewState!.tab);
    // Highlight active tab
    const parts: string[] = [];
    let pos = 0;
    for (let i = 0; i < labels.length; i++) {
      const seg = labels[i];
      if (i === activeIndex) {
        parts.push(palette.status.warning(seg));
      } else {
        parts.push(palette.text.dim(seg));
      }
      pos += seg.length;
      if (i < labels.length - 1) {
        parts.push(palette.text.dim("  "));
        pos += 2;
      }
    }
    const combined = parts.join("");
    return [padToWidth(combined, width)];
  }

  private getTabHint(tab: StatusViewTab, inSearchMode: boolean): string {
    if (tab === "status" || tab === "usage") {
      return "←/→ switch tab · Esc close";
    }
    if (inSearchMode) {
      return "Esc clear/exit search · Enter ↓ to list · ←/→ switch tab";
    }
    return "←/→ switch tab · / search · Enter edit item · Esc close";
  }

  private buildStatusViewLines(width: number): string[] {
    if (!this.statusViewState) return [];
    if (this.statusViewState.phase === "config_editor") {
      return this.buildConfigEditorLines(width);
    }
    const lines: string[] = [];
    lines.push(...this.renderTabBar(width));
    // Search input line on config tab
    if (this.statusViewState.tab === "config" && this.statusViewState.searchMode) {
      const queryDisplay = this.statusViewState.searchQuery || "";
      const placeholder = queryDisplay.length === 0 ? "Search settings…" : "";
      const searchText = placeholder || queryDisplay;
      const searchLine = `⌕ ${searchText}`;
      lines.push(padToWidth(palette.status.warning(searchLine), width));
    } else {
      lines.push(padToWidth(palette.status.warning("Status"), width));
    }
    lines.push(...this.statusViewState.list.render(width));
    lines.push(padToWidth(palette.text.dim(this.getTabHint(this.statusViewState.tab, this.statusViewState.searchMode)), width));
    return lines;
  }

  private switchStatusViewTab(direction: -1 | 1): void {
    if (!this.statusViewState || this.statusViewState.phase !== "tab_view") return;
    // Exit search mode when switching tabs
    this.statusViewState.searchMode = false;
    this.statusViewState.searchQuery = "";
    const tabs: StatusViewTab[] = ["status", "usage", "config"];
    const current = tabs.indexOf(this.statusViewState.tab);
    const next = (current + direction + tabs.length) % tabs.length;
    this.statusViewState.tab = tabs[next];
    this.rebuildStatusViewTabList();
    this.tui.requestRender();
  }

  private rebuildStatusViewTabList(): void {
    if (!this.statusViewState) return;
    this.statusViewState.list = this.buildStatusViewTabState(
      this.statusViewState.tab,
      this.statusViewState.statusPayload,
      this.statusViewState.configPayload,
      this.statusViewState.searchQuery,
    );
    this.tui.requestRender();
  }

/** Re-fetch command.status and config.get payloads to refresh the StatusView
   *  after a config change (e.g. model name update). */
  private async refreshStatusViewPayloads(): Promise<void> {
    if (!this.statusViewState) return;
    try {
      const statusPayload = await this.state.request<import("../core/commands/builtins/status.js").StatusPayload>(
        "command.status",
        {},
      );
      this.statusViewState.statusPayload = statusPayload;
    } catch {
      // keep stale payload if refresh fails
    }
    try {
      const configPayload = await this.state.request<Record<string, unknown> & { schema?: ConfigItemSchema[] }>(
        "config.get",
        {},
      );
      this.statusViewState.configPayload = configPayload;
    } catch {
      // keep stale payload if refresh fails
    }
    // Rebuild list with fresh payloads so the current tab reflects updated data
    this.rebuildStatusViewTabList();
  }

  private transitionToConfigEditor(key: string): void {
    if (!this.statusViewState?.configPayload?.schema?.length) return;
    const schemaList = this.statusViewState.configPayload.schema;
    const schema = schemaList.find((s) => s.key === key);
    if (!schema) return;

    const currentValues: Record<string, string> = {};
    for (const s of schemaList) {
      currentValues[s.key] = String(this.statusViewState.configPayload?.[s.key] ?? "");
    }

    this.statusViewState.phase = "config_editor";

    // Initialize search_list state then navigate to the item's sub-panel
    const filteredItems = filterConfigItems(schemaList, currentValues, "");
    const baseList = new SelectList(
      filteredItems,
      Math.min(Math.max(filteredItems.length, 1), 10),
      selectListTheme,
      { minPrimaryColumnWidth: 24, maxPrimaryColumnWidth: 42 },
    );
    this.configEditorState = {
      phase: "search_list",
      mode: "edit",
      schemaList,
      currentValues,
      selectedKey: null,
      searchQuery: "",
      searchMode: true,
      list: baseList,
      previousPhase: null,
      savedList: null,
    };
    // Navigate directly to the item's sub-panel
    this.handleConfigItemSelectionFromFlatList(schema);
  }

  private closeStatusView(): void {
    const sessionId = this.state.getSnapshot().sessionId;
    this.state.addItem(addInfo(sessionId, "Status dialog dismissed", "✓"));
    this.statusViewState = null;
    this.configEditorState = null;
    this.tui.requestRender();
  }

  // ──────────────────────────── MemoryView ────────────────────────────

  /** 懒初始化 MemoryViewController（复用实例，状态在 controller 内部管理） */
  private ensureMvController(): MemoryViewController {
    if (!this.mvController) {
      this.mvController = new MemoryViewController(this.state, this.tui);
    }
    return this.mvController;
  }

  // MemoryView 的具体逻辑已提取到 ui/memory-view.ts（MemoryViewController）

  private clearPastedTextState(): void {
    this.cancelPastedTextStateClear();
    this.pastedTextById.clear();
    this.pastedTextIdByContent.clear();
    this.nextPastedTextId = 1;
  }

  private cancelPastedTextStateClear(): void {
    if (!this.pastedTextClearTimer) {
      return;
    }
    clearTimeout(this.pastedTextClearTimer);
    this.pastedTextClearTimer = null;
  }

  private schedulePastedTextStateClear(): void {
    if (this.pastedTextClearTimer) {
      clearTimeout(this.pastedTextClearTimer);
    }
    // Editor clears text and emits onChange("") before onSubmit(value), so keep paste data for that submit.
    this.pastedTextClearTimer = setTimeout(() => {
      this.clearPastedTextState();
    }, 0);
  }

  private expandPastedText(text: string): string {
    return expandPastedTextMarkers(text, this.pastedTextById);
  }

  private syncComposerAttachmentsFromEditor(): void {
    if (this.syncingComposerInput) {
      return;
    }

    const originalText = this.editor.getText();
    const { normalizedText, attachments } = syncComposerImageTokens(
      originalText,
      this.composerAttachments,
      (path) => this.isComposerImageFile(path),
    );

    this.composerAttachments = attachments;

    if (normalizedText !== originalText) {
      this.syncingComposerInput = true;
      this.editor.setText(normalizedText);
      this.syncingComposerInput = false;
    }
  }

  private deleteComposerAttachmentTokenBackwards(): boolean {
    const cursor = this.editor.getCursor();
    const lines = this.editor.getLines();
    const currentLine = lines[cursor.line] ?? "";
    const tokenRange = findAttachmentTokenAtCursor(currentLine, cursor.col);
    if (!tokenRange) {
      return false;
    }

    const nextLine =
      `${currentLine.slice(0, tokenRange.start)}${currentLine.slice(tokenRange.end)}`.replace(
        / {2,}/g,
        " ",
      );
    const nextLines = [...lines];
    nextLines[cursor.line] = nextLine;
    const nextText = nextLines.join("\n");
    const nextCol = Math.min(tokenRange.start, nextLine.length);

    this.syncingComposerInput = true;
    this.editor.setText(nextText);
    const ed = this.editor as unknown as {
      state: { cursorLine: number };
      setCursorCol: (col: number) => void;
    };
    ed.state.cursorLine = cursor.line;
    ed.setCursorCol(nextCol);
    this.syncingComposerInput = false;
    this.syncComposerAttachmentsFromEditor();
    this.tui.requestRender();
    return true;
  }

  private collectComposerAttachments(text: string): FileAttachment[] {
    const cwd = getCurrentCwd() || process.cwd();
    return extractAttachmentsFromText(text, {
      cwd,
      classifyAttachment: (path) => (this.isAcceptedAttachment(path) ? (isImageAttachment(path) ? "image" : "file") : null),
    }).map(({ resolvedPath, ...attachment }) => attachment);
  }

  private isAcceptedAttachment(path: string): boolean {
    if (!isSupportedAttachment(path)) {
      return false;
    }

    try {
      const stats = fs.statSync(path);
      if (!stats.isFile()) {
        return false;
      }
      return true;
    } catch {
      return false;
    }
  }

  private isComposerImageFile(path: string): boolean {
    return this.isAcceptedAttachment(path) && isImageAttachment(path);
  }

  private handlePastedTextCollapse(text: string): boolean {
    const normalizedText = normalizePastedText(text);
    if (!shouldCollapsePastedText(normalizedText)) {
      return false;
    }

    let pasteId = this.pastedTextIdByContent.get(normalizedText);
    if (!pasteId) {
      pasteId = this.nextPastedTextId;
      this.nextPastedTextId += 1;
      this.pastedTextIdByContent.set(normalizedText, pasteId);
      this.pastedTextById.set(pasteId, normalizedText);
    }

    this.editor.insertTextAtCursor(formatPastedTextMarker(pasteId, normalizedText));
    this.tui.requestRender();
    return true;
  }

  /** Handle pasted/dragged content - detects file paths and converts to @path references. */
  private handleDroppedFiles(filePaths: string[]): boolean {
    const insertText = filePaths
      .filter((path) => this.isAcceptedAttachment(path))
      .map((path) => formatAttachmentMention(path))
      .join(" ");

    if (!insertText) return false;

    const currentText = this.editor.getText();
    const newText = currentText ? `${currentText}\n${insertText}` : insertText;
    this.syncingComposerInput = true;
    this.editor.setText(newText);
    this.syncingComposerInput = false;
    this.tui.requestRender();
    return true;
  }

  private syncAnimationLoop(snapshot: ReturnType<CliPiAppState["getSnapshot"]>): void {
    const hasRunningTools = snapshot.toolExecutions.some(
      (execution) => execution.tool.status === "running",
    );
    const teamWorking =
      isTeamMode(snapshot.mode) &&
      isTeamWorking(snapshot.teamMemberEvents, snapshot.teamMessageEvents);
    const teamStartedAt = teamWorkingStartedAtMs(
      snapshot.teamMemberEvents,
      snapshot.teamMessageEvents,
    );
    const runningWorkflows = snapshot.workflowRuns.filter(
      (workflow) => workflow.status === "running",
    );
    const hasRunningWorkflow = runningWorkflows.length > 0;
    const hasBtwLoading = snapshot.btwPendingQuestion !== null;
    const runningWorkflow = runningWorkflows[0];
    const shouldAnimate =
      hasBtwLoading ||
      (!snapshot.isInterrupted &&
        (snapshot.isProcessing || hasRunningTools || teamWorking || hasRunningWorkflow));
    if (!shouldAnimate) {
      const nowMs = Date.now();
      if (this.runningStoppedAtMs === null) {
        this.runningStoppedAtMs = nowMs;
      }
      if (this.animationTimer) {
        clearInterval(this.animationTimer);
        this.animationTimer = null;
      }
      this.animationPhase = 0;
      if (
        this.runningStartedAtMs !== null &&
        nowMs - this.runningStoppedAtMs >= RUNNING_TIMER_RESET_GRACE_MS
      ) {
        this.runningStartedAtMs = null;
        this.runningStoppedAtMs = null;
      }
      return;
    }
    if (snapshot.isProcessing) {
      if (
        this.runningStartedAtMs === null ||
        this.runningStoppedAtMs !== null
      ) {
        this.runningStartedAtMs = Date.now();
      }
    } else if (teamWorking) {
      this.runningStartedAtMs = teamStartedAt ?? this.runningStartedAtMs ?? Date.now();
    } else if (hasRunningWorkflow) {
      const earliestStartedAt = runningWorkflows.reduce((earliest, workflow) => {
        const startedAt = Date.parse(workflow.started_at ?? "") || Date.now();
        return startedAt < earliest ? startedAt : earliest;
      }, Date.now());
      this.runningStartedAtMs = earliestStartedAt || this.runningStartedAtMs || Date.now();
    }
    this.runningStoppedAtMs = null;
    if (this.animationTimer) {
      return;
    }
    this.animationTimer = setInterval(() => {
      this.animationPhase = (this.animationPhase + 1) % 12;
      this.tui.requestRender();
    }, 220);
  }

  private applySlashCommandHint(editorLines: string[], width: number): string[] {
    const hint = this.getInlineSlashCommandHint();
    if (!hint || editorLines.length < 3) {
      return editorLines;
    }

    const contentIndex = 1;
    const line = editorLines[contentIndex] ?? "";
    const cursorIndex = line.indexOf(END_CURSOR);
    if (cursorIndex === -1) {
      return editorLines;
    }

    const hintedLine = padToWidth(
      line.replace(END_CURSOR, `${END_CURSOR}${palette.text.dim(` ${hint}`)}`),
      width,
    );

    const nextLines = [...editorLines];
    nextLines[contentIndex] = hintedLine;
    return nextLines;
  }

  private getInlineSlashCommandHint(): string | null {
    const text = this.editor.getText();
    if (!text.startsWith("/") || text.includes("\n")) {
      return null;
    }

    const cursor = this.editor.getCursor();
    const lines = this.editor.getLines();
    const currentLine = lines[cursor.line] ?? "";
    if (cursor.line !== 0 || cursor.col !== currentLine.length) {
      return null;
    }

    const parsed = parseSlashCommand(text, this.commands.getAll());
    if (!parsed.command) {
      return null;
    }

    const args = parsed.args.trim();

    // No args → show top-level usage hint (existing behavior)
    if (!args) {
      const usage = parsed.command.usage?.trim() ?? "";
      if (!usage.startsWith("/")) {
        return null;
      }
      const suffix = usage.replace(/^\/[^\s]+/, "").trim();
      return suffix || null;
    }

    // Args present → check if they match a sub-command name
    const subCommands = parsed.command.subCommands;
    if (!subCommands?.length) {
      // No sub-commands defined; if the command has an argGuide, show it
      // only when the user hasn't started filling in key=value args yet.
      const argGuide = parsed.command.argGuide;
      if (argGuide && !args.includes("=")) {
        return argGuide;
      }
      return null;
    }

    const argTokens = args.split(/\s+/);
    const firstArg = argTokens[0]?.toLowerCase();

    // Find matching sub-command
    const matchedSub = subCommands.find(
      (sub) => sub.name.toLowerCase() === firstArg
        || sub.altNames?.some((alt) => alt.toLowerCase() === firstArg),
    );

    if (!matchedSub) {
      // Partial match or no match
      const partialMatch = firstArg
        ? subCommands.filter((sub) => sub.name.toLowerCase().startsWith(firstArg))
        : subCommands;
      if (partialMatch.length === 1) {
        // Unique partial match — show its argGuide directly
        const unique = partialMatch[0];
        if (unique.argGuide) {
          return unique.argGuide;
        }
        const subUsage = unique.usage?.trim() ?? "";
        if (subUsage.startsWith("/")) {
          return subUsage.replace(/^\/[^\s]+/, "").trim() || null;
        }
        return unique.name;
      }
      if (partialMatch.length > 1 && partialMatch.length <= 6) {
        return partialMatch.map((sub) => sub.name).join(" | ");
      }
      return null;
    }

    // Sub-command matched, check remaining args after the sub-command name
    const remainingArgs = argTokens.slice(1).join(" ").trim();

    if (!remainingArgs) {
      // User typed just "/cron add" — show argGuide or usage hint
      if (matchedSub.argGuide) {
        return matchedSub.argGuide;
      }
      const subUsage = matchedSub.usage?.trim() ?? "";
      if (subUsage.startsWith("/")) {
        const suffix = subUsage.replace(/^\/[^\s]+/, "").trim();
        return suffix || null;
      }
      return matchedSub.description || null;
    }

    // User is typing key=value args after sub-command
    if (matchedSub.argGuide && !remainingArgs.includes("=")) {
      return matchedSub.argGuide;
    }

    return null;
  }

  /**
   * Builds a fresh {@link ComposerAutocompleteProvider} wrapping a new
   * {@link CombinedAutocompleteProvider}.  Skill shorthands are prepended to
   * the regular slash-command list so they appear first in the dropdown.
   *
   * @param skills - snapshot of the installed-skills cache; defaults to the
   *   current cache exposed by {@link CommandService.getInstalledSkills}.
   */
  private rebuildAutocompleteProvider(
    skills: readonly InstalledSkillEntry[] = this.commands.getInstalledSkills(),
  ): ComposerAutocompleteProvider {
    // Convert each installed skill to a TuiSlashCommand so CombinedAutocompleteProvider
    // treats /<skillName> exactly like any other slash command for name completion.
    const registeredNames = new Set(this.commands.getAll(true).map((c) => c.name));
    const skillCommands: TuiSlashCommand[] = skills
      .filter((skill) => !registeredNames.has(skill.name))
      .map((skill) => ({
        name: skill.name,
        description: skill.description || `Use the "${skill.name}" skill`,
      }));

    return new ComposerAutocompleteProvider(
      new CombinedAutocompleteProvider(
        // Skill shorthands come last so they appear at the bottom of the dropdown.
        [...this.buildSlashCommands(), ...skillCommands],
        getCurrentCwd() || process.cwd(),
        resolveFdBinary(),
      ),
      getCurrentCwd() || process.cwd(),
      // /memory edit|toggle 参数 completion 回调（绕过 CombinedAutocompleteProvider 的子命令名候选项）
      async (sub: string) => {
        return this.ensureMvController().getMemoryCompletions(sub);
      },
      skills, // ← 传给外层，用于行内 skill 补全
      (name: string) => {
        this.slashNameCompletion = { name, at: Date.now() };
      },
    );
  }

  private buildSlashCommands(): TuiSlashCommand[] {
    const hasAnyCompletion = (cmd: SlashCommand): boolean =>
      !!cmd.completion || (cmd.subCommands?.some(hasAnyCompletion) ?? false);
    const result: TuiSlashCommand[] = [];
    for (const command of this.commands.getAll()) {
      result.push({
        name: command.name,
        description: command.description,
        getArgumentCompletions: hasAnyCompletion(command)
          ? async (argumentPrefix: string): Promise<AutocompleteItem[] | null> => {
            const trimmed = argumentPrefix.trim();
            // Traverse subcommand chain to find the deepest command with completion
            let currentCommand: typeof command = command;
            let matchedPath: string[] = [];
            let remainingTokens: string[] = [];

            if (currentCommand.subCommands?.length && trimmed.length > 0) {
              const tokens = trimmed.split(/\s+/).filter(Boolean);
              let matchIndex = 0;

              for (let i = 0; i < tokens.length; i++) {
                const token = tokens[i];
                const matchedSub = currentCommand.subCommands?.find(
                  (sub) => sub.name === token || sub.altNames?.includes(token)
                );
                if (!matchedSub) {
                  // No more subcommand matches, remaining tokens are args
                  remainingTokens = tokens.slice(i);
                  break;
                }

                matchedPath.push(matchedSub.name);
                currentCommand = matchedSub;
                matchIndex = i + 1;
              }

              // If all tokens matched subcommands, remainingTokens is empty
              if (matchIndex >= tokens.length) {
                remainingTokens = [];
              }
            }

            // Use the deepest matched command's completion if available
            if (currentCommand.completion) {
              if (currentCommand.name === "mode") {
                return buildModeAutocompleteItems();
              }
              // Special handling for auto-harness completions with descriptions
              // Check top-level command name (command.name) since matchedPath only contains subcommands
              if (command.name === "auto-harness") {
                const remainingArgs = remainingTokens.length > 0 ? remainingTokens.join(" ") : trimmed;
                const items = await currentCommand.completion(this.state.getCommandContext(), remainingArgs);
                const prefix = matchedPath.length > 0 ? matchedPath.join(" ") + " " : "";

                // Map completions to AutocompleteItem with descriptions
                return items.map((value) => {
                  let desc = "";

                  // Check for subcommand descriptions (schedule -> start, list, etc.)
                  if (currentCommand.subCommands) {
                    const subCmd = currentCommand.subCommands.find(s => value.includes(s.name) || value === s.name);
                    if (subCmd) {
                      desc = subCmd.description;
                    }
                  }

                  // Check for flag descriptions (--interval, --pipeline, -i, -p)
                  const flagMatch = Object.keys(FLAG_OPTIONS).find(f => value.includes(f));
                  if (flagMatch) {
                    const flagDesc = FLAG_OPTIONS[flagMatch as keyof typeof FLAG_OPTIONS]?.desc || "";
                    desc = desc ? `${desc} | ${flagDesc}` : flagDesc;
                  }

                  // Check for pipeline descriptions
                  const pipelineName = PIPELINE_VALUES.find((p: string) => value.includes(p));
                  if (pipelineName) {
                    const pipelineDesc = PIPELINE_OPTIONS[pipelineName as keyof typeof PIPELINE_OPTIONS]?.desc || "";
                    desc = desc ? `${desc} | ${pipelineDesc}` : pipelineDesc;
                  }

                  // Check for interval descriptions
                  const intervalValue = INTERVAL_VALUES.find((v: string) => {
                    const parts = value.split(/\s+/);
                    return parts.includes(v) && (parts.includes("--interval") || parts.includes("-i"));
                  });
                  if (intervalValue) {
                    const intervalDesc = INTERVAL_OPTIONS[intervalValue as keyof typeof INTERVAL_OPTIONS]?.desc || "";
                    desc = desc ? `${desc} | ${intervalDesc}` : intervalDesc;
                  }

                  return {
                    value: prefix + value,
                    label: value,
                    description: desc,
                  };
                });
              }
              const remainingArgs = remainingTokens.length > 0 ? remainingTokens.join(" ") : trimmed;
              const items = await currentCommand.completion(this.state.getCommandContext(), remainingArgs);
              const prefix = matchedPath.length > 0 ? matchedPath.join(" ") + " " : "";
              const suffix = currentCommand.completionSuffix ?? "";
              return items.map((value) => ({
                value: prefix + value + suffix,
                label: value,
                description: "",
              }));
            }

            // 子命令名补全：当输入前缀未精确匹配任何子命令时，
            // 提示以该前缀开头的子命令名（如 /memory edi → edit）。
            // 输入为空时提示全部子命令（如 /memory <space>）。
            if (matchedPath.length === 0 && command.subCommands?.length) {
              const prefix = trimmed.toLowerCase();
              const matchingSubs = command.subCommands.filter(
                (sub) => !prefix || sub.name.toLowerCase().startsWith(prefix),
              );
              if (matchingSubs.length > 0) {
                return matchingSubs.map((sub) => ({
                  label: sub.name,
                  description: sub.description ?? "",
                  value: sub.name,
                  usage: sub.usage,
                  example: sub.example,
                }));
              }
            }

            return null;
          }
        : undefined,
      });
    }
    return result;
  }

  private buildPendingQuestionLines(
    snapshot: ReturnType<CliPiAppState["getSnapshot"]>,
    width: number,
  ): string[] {
    const pendingQuestion = snapshot.pendingQuestion;
    if (!pendingQuestion) {
      return [];
    }

    const question =
      pendingQuestion.questions[this.activeQuestionIndex] ?? pendingQuestion.questions[0];
    if (!question) {
      return [];
    }

    const total = pendingQuestion.questions.length;
    const progress = total > 1 ? ` (${this.activeQuestionIndex + 1}/${total})` : "";
    const planApprovalRequest = isPlanApprovalRequest(
      pendingQuestion.source,
      pendingQuestion.planApprovalKind,
    );
    const permissionRequest = !planApprovalRequest &&
      isPermissionRequest(pendingQuestion.source, question.question);
    const lines: string[] = [];

    if (planApprovalRequest && !this.otherInputMode) {
      const title = getPendingQuestionTitle(
        pendingQuestion.source,
        progress,
        this.activeQuestionIndex,
        total,
        pendingQuestion.planApprovalKind,
      );
      lines.push(
        ...wrapPlainText(title, width).map((line) => padToWidth(palette.status.warning(line), width)),
      );
    } else if (permissionRequest && !this.otherInputMode) {
      const summary = parsePermissionSummary(question.question);
      const title = getPendingQuestionTitle(
        pendingQuestion.source,
        progress,
        this.activeQuestionIndex,
        total,
        pendingQuestion.planApprovalKind,
      );
      lines.push(...renderPermissionBlock(width, summary, title));
    } else if (this.otherInputMode) {
      lines.push(
        ...wrapPlainText(
          `[${question.header || "Question"}${progress}] ${question.question}`,
          width,
        ).map((line) => padToWidth(palette.status.warning(line), width)),
      );
      if (question.options.length > 0) {
        lines.push("");
        for (const opt of question.options) {
          const optLine = `  ${opt.label}${opt.description ? ` - ${opt.description}` : ""}`;
          lines.push(
            ...wrapPlainText(optLine, width).map((line) =>
              padToWidth(palette.text.dim(line), width),
            ),
          );
        }
      }
      lines.push("");
      lines.push(
        ...wrapPlainText(`[Answer] Please enter your answer:`, width).map((line) =>
          padToWidth(palette.status.info(line), width),
        ),
      );
      lines.push(
        padToWidth(
          palette.text.dim(
            planApprovalRequest
              ? "tell jiuwenswarm what to change · Enter submit · Esc back to options"
              : "Type your answer · Enter submit · Esc back to options",
          ),
          width,
        ),
      );
    } else {
      lines.push(
        ...wrapPlainText(
          `[${question.header || "Question"}${progress}] ${question.question}`,
          width,
        ).map((line) => padToWidth(palette.status.warning(line), width)),
      );
    }

    if (this.questionCheckboxList !== null) {
      const checkboxLines = this.questionCheckboxList.render(width);
      lines.push(...checkboxLines);
    } else if (this.questionList !== null) {
      const wrappedOptions =
        pendingQuestion.source === "ask_user_interrupt"
          ? renderWrappedQuestionOptions(
              this.questionList["filteredItems"] ?? [],
              this.questionList["selectedIndex"] ?? 0,
              this.questionList["maxVisible"] ?? 20,
              width,
            )
          : null;
      const listLines = wrappedOptions?.lines ?? this.questionList.render(width);

      // Insert preview / details sub-lines right after the currently selected item
      // instead of appending them after the entire list.
      const selectedItem = this.questionList.getSelectedItem();
      if (selectedItem) {
        const previewText = this.questionPreviewMap?.get(selectedItem.value);
        const details = this.questionDetailsMap?.get(selectedItem.value);
        const hasPreview = !!previewText;
        const hasDetails = !!(details && details.length > 0);
        if (hasPreview || hasDetails) {
          const previewIndent = "  ";
          const detailIndent = "              ";
          const subLines: string[] = [];
          // Markdown preview (ASCII mockups / code snippets) rendered with the
          // same renderer as assistant messages so monospace alignment survives.
          if (hasPreview) {
            const mdLines = renderMarkdownLines(
              Math.max(1, width - previewIndent.length),
              previewText!,
            );
            for (const l of mdLines) {
              subLines.push(previewIndent + l);
            }
          }
          // Plain-text detail lines (e.g. rewind file-change summaries).
          if (hasDetails) {
            for (const d of details!) {
              subLines.push(
                ...renderWrappedText(Math.max(1, width), `${detailIndent}${d}`, palette.text.dim),
              );
            }
          }
          // SelectList.render() layout: [visible item 0..N-1, (scroll indicator?)]
          // Replicate its scroll-window calculation to find where the selected
          // item sits, then splice sub-lines right after it.
          const filteredLen: number = this.questionList["filteredItems"]?.length ?? 0;
          const selectedIdx: number = this.questionList["selectedIndex"] ?? 0;
          const maxVis: number = this.questionList["maxVisible"] ?? 6;
          const scrollStart = Math.max(
            0,
            Math.min(selectedIdx - Math.floor(maxVis / 2), filteredLen - maxVis),
          );
          const insertAt = wrappedOptions
            ? wrappedOptions.selectedEndIndex
            : Math.max(0, Math.min(selectedIdx - scrollStart + 1, listLines.length));
          listLines.splice(insertAt, 0, ...subLines);
        }
      }

      lines.push(...listLines);
      lines.push(
        padToWidth(
          palette.text.dim(
            permissionRequest
              ? planApprovalRequest
                ? "↑/↓ review · Type feedback on Reject · Enter/click confirm"
                : "↑/↓ review · Enter/click confirm · Esc reject"
              : "↑/↓ choose · Enter/click confirm · Esc reject",
          ),
          width,
        ),
      );
    }
    return lines;
  }

  private handlePendingQuestionInput(
    data: string,
    snapshot: ReturnType<CliPiAppState["getSnapshot"]>,
  ): boolean {
    if (!snapshot.pendingQuestion) {
      return false;
    }

    if (this.questionCheckboxList !== null) {
      this.questionCheckboxList.handleInput(data);
      this.tui.requestRender();
      return true;
    }

    if (this.questionList !== null) {
      const question =
        snapshot.pendingQuestion.questions[this.activeQuestionIndex] ??
        snapshot.pendingQuestion.questions[0];
      const selected = this.questionList.getSelectedItem();
      const editingPlanReject = !!question &&
        !!selected &&
        shouldAppendPlanRejectFeedback(
          snapshot.pendingQuestion.source,
          selected.value,
          snapshot.pendingQuestion.planApprovalKind,
        );
      const printableChar = this.getPrintableChar(data);
      if (
        editingPlanReject &&
        (printableChar !== undefined ||
          matchesKey(data, "backspace") ||
          matchesKey(data, "delete") ||
          this.isInlinePlanRejectCursorInput(data))
      ) {
        this.pendingQuestionAnswers.set(this.activeQuestionIndex, selected.value);
        this.editor.handleInput(data);
        this.syncQuestionList(this.state.getSnapshot());
        this.syncEditorSubmitState(this.state.getSnapshot());
        this.tui.requestRender();
        return true;
      }
      this.questionList.handleInput(data);
      return true;
    }

    if (this.otherInputMode) {
      if (matchesKey(data, "escape")) {
        this.otherInputMode = false;
        this.editor.setText("");
        this.syncQuestionList(this.state.getSnapshot());
        this.syncEditorSubmitState(snapshot);
        return true;
      }
      this.editor.handleInput(data);
      return true;
    }

    return false;
  }

  private updateQuestionOptionRows(
    screenLines: string[],
    snapshot: ReturnType<CliPiAppState["getSnapshot"]>,
  ): void {
    this.questionOptionRows = [];
    if (!snapshot.pendingQuestion || !this.questionList) {
      return;
    }

    const question =
      snapshot.pendingQuestion.questions[this.activeQuestionIndex] ??
      snapshot.pendingQuestion.questions[0];
    if (!question) {
      return;
    }

    const planApprovalRequest = isPlanApprovalRequest(
      snapshot.pendingQuestion.source,
      snapshot.pendingQuestion.planApprovalKind,
    );
    const rowItems = planApprovalRequest
      ? buildPlanApprovalQuestionItems(
          question.options,
          this.editor.getText(),
          this.isEditingInlinePlanReject(snapshot),
          this.editor.getCursor().col,
        )
      : question.options.map((option) => ({
          value: option.label,
          label: formatQuestionOptionLabelForDisplay(option.label, false),
        }));

    for (const option of rowItems) {
      const displayLabel = option.label ?? option.value;
      for (let i = 0; i < screenLines.length; i++) {
        const plain = stripAnsi(screenLines[i]);
        if (
          (plain.startsWith("→ ") || plain.startsWith("  ")) &&
          plain.includes(displayLabel)
        ) {
          this.questionOptionRows.push({ row: i + 1, value: option.value });
          break;
        }
      }
    }
  }

  private syncEditorSubmitState(snapshot: ReturnType<CliPiAppState["getSnapshot"]>): void {
    const pendingQuestion = snapshot.pendingQuestion;
    this.editor.disableSubmit =
      !!pendingQuestion &&
      !this.otherInputMode &&
      !this.isEditingInlinePlanReject(snapshot) &&
      (this.questionList !== null || this.questionCheckboxList !== null || (pendingQuestion.questions[0]?.options.length ?? 0) > 0);
  }

  private isEditingInlinePlanReject(snapshot: ReturnType<CliPiAppState["getSnapshot"]>): boolean {
    const pendingQuestion = snapshot.pendingQuestion;
    if (!pendingQuestion || this.questionList === null) {
      return false;
    }
    const question =
      pendingQuestion.questions[this.activeQuestionIndex] ?? pendingQuestion.questions[0];
    const selected = this.questionList.getSelectedItem();
    return !!question &&
      !!selected &&
      shouldAppendPlanRejectFeedback(
        pendingQuestion.source,
        selected.value,
        pendingQuestion.planApprovalKind,
      );
  }

  private isInlinePlanRejectCursorInput(data: string): boolean {
    return (
      matchesKey(data, "left") ||
      matchesKey(data, "right") ||
      matchesKey(data, "home") ||
      matchesKey(data, "end") ||
      matchesKey(data, "ctrl+b") ||
      matchesKey(data, "ctrl+f") ||
      matchesKey(data, "ctrl+a") ||
      matchesKey(data, "ctrl+e") ||
      matchesKey(data, "alt+left") ||
      matchesKey(data, "alt+right") ||
      matchesKey(data, "ctrl+left") ||
      matchesKey(data, "ctrl+right") ||
      matchesKey(data, "alt+b") ||
      matchesKey(data, "alt+f")
    );
  }

  private syncQuestionList(snapshot: ReturnType<CliPiAppState["getSnapshot"]>): void {
    const pendingQuestion = snapshot.pendingQuestion;
    if (!pendingQuestion) {
      this.questionList = null;
      this.questionCheckboxList = null;
      this.questionDetailsMap = null;
      this.questionPreviewMap = null;
      this.setMouseTrackingEnabled(false);
      return;
    }

    const question = pendingQuestion.questions[this.activeQuestionIndex];
    const planApprovalRequest = isPlanApprovalRequest(
      pendingQuestion.source,
      pendingQuestion.planApprovalKind,
    );
    if (!question || question.options.length === 0) {
      this.questionList = null;
      this.questionCheckboxList = null;
      this.questionDetailsMap = null;
      this.questionPreviewMap = null;
      this.setMouseTrackingEnabled(false);
      return;
    }

    // --- Multi-select: use CheckboxList ---
    if (question.multiSelect) {
      this.questionList = null;
      this.questionDetailsMap = null;
      this.questionPreviewMap = null;

      const groups: CheckboxGroupType[] = [
        {
          name: question.header || question.question,
          items: question.options.map((option) => ({
            label: option.label,
            value: option.label,
            checked: false,
            description: option.description,
          })),
        },
      ];

      const checkboxList = new CheckboxList(
        groups,
        Math.min(Math.max(question.options.length, 1), 20),
      );
      checkboxList.onSelect = (selectedValues: string[]) => {
        this.handleMultiSelectConfirm(selectedValues);
      };
      checkboxList.onCancel = () => {
        this.handleQuestionSelection("");
      };
      this.questionCheckboxList = checkboxList;
      this.setMouseTrackingEnabled(true);
      return;
    }

    // --- Single-select: use SelectList ---
    this.questionCheckboxList = null;

    const currentSelectedValue = this.questionList?.getSelectedItem()?.value;
    const showRejectCursor =
      planApprovalRequest &&
      !!currentSelectedValue &&
      shouldAppendPlanRejectFeedback(
        pendingQuestion.source,
        currentSelectedValue,
        pendingQuestion.planApprovalKind,
      );

    const items: SelectItem[] = planApprovalRequest
      ? buildPlanApprovalQuestionItems(
          question.options,
          this.editor.getText(),
          showRejectCursor,
          this.editor.getCursor().col,
        )
      : question.options.map((option) => ({
          value: option.label,
          label:
            pendingQuestion.source === "permission_interrupt" ||
            pendingQuestion.source === "confirm_interrupt"
              ? formatQuestionOptionLabelForDisplay(option.label, false)
              : option.label,
          description: option.description,
        }));

    // Build details map for options that have sub-lines (e.g. rewind file changes)
    const detailsMap = new Map<string, string[]>();
    for (const option of question.options) {
      if (option.details && option.details.length > 0) {
        detailsMap.set(option.label, option.details);
      }
    }
    this.questionDetailsMap = detailsMap;

    // Build preview map for options that carry markdown preview content
    // (LLM-supplied ASCII mockups / code snippets). Rendered only for single-select.
    const previewMap = new Map<string, string>();
    for (const option of question.options) {
      if (option.preview && option.preview.trim()) {
        previewMap.set(option.label, option.preview);
      }
    }
    this.questionPreviewMap = previewMap;

    const maxVisible =
      pendingQuestion.source === "permission_interrupt" ||
      pendingQuestion.source === "confirm_interrupt"
        ? 4
        : 20;
    // For memory edit, use a layout that mirrors Claude Code's /memory selector:
    //   - Short labels ("Project memory", "User memory", ".jiuwen/rules/foo.md")
    //   - Descriptions ("Checked in at ./JIUWENSWARM.md", "Saved in ~/.jiuwen/...")
    //   - Allow wider primary column so rule paths aren't truncated
    // For rewind and other questions with details sub-lines, use a narrower label column
    // so the description starts sooner and details can align beneath it.
    const layout = planApprovalRequest
      ? getPlanApprovalListLayout()
      : pendingQuestion.source === "local_command_memory_edit"
        ? { minPrimaryColumnWidth: 20, maxPrimaryColumnWidth: 50 }
        : detailsMap.size > 0
          ? { minPrimaryColumnWidth: 10, maxPrimaryColumnWidth: 10 }
          : { minPrimaryColumnWidth: 34, maxPrimaryColumnWidth: 42 };
    const list = new SelectList(
      items,
      Math.min(Math.max(items.length, 1), maxVisible),
      planApprovalRequest ? planApprovalSelectListTheme : selectListTheme,
      layout,
    );
    list.onSelect = (item) => {
      this.handleQuestionSelection(item.value);
    };
    list.onCancel = () => {
      const reject = question.options.find((option) => option.label === "拒绝");
      if (reject) {
        this.handleQuestionSelection(reject.label);
      } else {
        this.handleQuestionSelection("");
      }
    };
    list.onSelectionChange = () => {
      this.invalidate();
    };
    let selectedValue =
      currentSelectedValue ?? this.pendingQuestionAnswers.get(this.activeQuestionIndex);
    // For memory edit, restore cursor to the last selected file within this session
    if (!selectedValue && pendingQuestion.source === "local_command_memory_edit" && lastMemorySelection) {
      selectedValue = lastMemorySelection;
    }
    const selectedIndex = selectedValue
      ? items.findIndex((item) => item.value === selectedValue)
      : 0;
    if (selectedIndex >= 0) {
      list.setSelectedIndex(selectedIndex);
    }
    this.questionList = list;
    this.setMouseTrackingEnabled(true);
  }

  private handleMultiSelectConfirm(selectedValues: string[]): void {
    const snapshot = this.state.getSnapshot();
    const pendingQuestion = snapshot.pendingQuestion;
    if (!pendingQuestion) {
      return;
    }

    this.pendingMultiSelectAnswers.set(this.activeQuestionIndex, selectedValues);
    if (selectedValues.includes("Other")) {
      this.otherInputMode = true;
      this.questionCheckboxList = null;
      this.setMouseTrackingEnabled(false);
      this.syncEditorSubmitState(snapshot);
      this.tui.requestRender();
      return;
    }

    if (this.activeQuestionIndex < pendingQuestion.questions.length - 1) {
      // Multiple questions: advance to the next one
      this.activeQuestionIndex += 1;
      this.syncQuestionList(this.state.getSnapshot());
      this.tui.requestRender();
      return;
    }

    const answers = pendingQuestion.questions.map((question, index) => {
      // Current multi-select question uses the just-confirmed selectedValues;
      // earlier multi-select questions use their stored arrays.
      const multi =
        index === this.activeQuestionIndex
          ? selectedValues
          : this.pendingMultiSelectAnswers.get(index);
      const answer = {
        question: question.question,
        selected_options: multi ?? [this.pendingQuestionAnswers.get(index) ?? ""],
      };
      const customInput = this.pendingQuestionCustomInputs.get(index);
      return customInput ? { ...answer, custom_input: customInput } : answer;
    });
    this.state.submitQuestionAnswers(answers);
  }

  private handleQuestionSelection(label: string): void {
    const snapshot = this.state.getSnapshot();
    const pendingQuestion = snapshot.pendingQuestion;
    if (!pendingQuestion) {
      return;
    }

    const question =
      pendingQuestion.questions[this.activeQuestionIndex] ?? pendingQuestion.questions[0];
    const collectPlanRejectFeedback = shouldCollectPlanRejectFeedback(
      pendingQuestion.source,
      label,
      pendingQuestion.planApprovalKind,
    );

    if (label === "Other") {
      this.otherInputMode = true;
      this.pendingQuestionAnswers.set(this.activeQuestionIndex, label);
      this.questionList = null;
      this.setMouseTrackingEnabled(false);
      this.syncEditorSubmitState(snapshot);
      this.tui.requestRender();
      return;
    }

    this.pendingQuestionAnswers.set(this.activeQuestionIndex, label);
    if (this.activeQuestionIndex < pendingQuestion.questions.length - 1) {
      this.activeQuestionIndex += 1;
      this.syncQuestionList(this.state.getSnapshot());
      this.tui.requestRender();
      return;
    }

    // Remember memory edit selection so cursor restores to the same file within this session
    if (pendingQuestion.source === "local_command_memory_edit") {
      lastMemorySelection = label;
    }

    const answers = pendingQuestion.questions.map((question, index) => {
      const multi = this.pendingMultiSelectAnswers.get(index);
      const answerValue = this.pendingQuestionAnswers.get(index) ?? question.options[0]?.label ?? "";
      const answer = {
        question: question.question,
        selected_options: multi ?? [answerValue],
      };
      const customInput = this.pendingQuestionCustomInputs.get(index);
      if (customInput) {
        return { ...answer, custom_input: customInput };
      }
      if (
        index === this.activeQuestionIndex &&
        collectPlanRejectFeedback &&
        shouldAppendPlanRejectFeedback(
          pendingQuestion.source,
          answerValue,
          pendingQuestion.planApprovalKind,
        )
      ) {
        const feedback = this.editor.getText().trim();
        if (feedback) {
          return { ...answer, custom_input: feedback };
        }
      }
      return answer;
    });
    this.state.submitQuestionAnswers(answers);
    if (collectPlanRejectFeedback) {
      this.editor.setText("");
    }
  }

  /**
   * Handle keyboard input when the user is replying to a swarmflow human-session turn.
   * Enter submits; Esc cancels without sending.
   */
  private handleSwarmflowHumanReplyInput(data: string): boolean {
    if (!this.replyingToHumanPrompt) return false;

    if (matchesKey(data, "escape")) {
      this.replyingToHumanPrompt = null;
      this.editor.setText("");
      this.editor.focused = false;
      return true;
    }

    if (matchesKey(data, "return") || matchesKey(data, "enter")) {
      const answer = this.editor.getText().trim();
      if (!answer) return true; // block empty submit
      const { workflowRunId, correlationId } = this.replyingToHumanPrompt;
      const snapshot = this.state.getSnapshot();
      this.state.sendEventOnly("chat.swarmflow_reply", {
        session_id: snapshot.sessionId,
        run_id: workflowRunId,
        correlation_id: correlationId,
        answer,
      });
      // Track the exact turn so session-detail can close as soon as this reply
      // is accepted, even if the workflow continues or opens another turn.
      this.lastRepliedHumanPrompt = { workflowRunId, correlationId };
      const currentView = this.swarmWorkflowsViewState;
      if (
        currentView?.phase === "agent" &&
        currentView.returnTo?.kind === "pending-list"
      ) {
        // Entering a pending turn's detail is a temporary drill-down. Once the
        // reply is submitted, return to the screen that opened the pending list
        // instead of leaving the user on the now-completed detail page.
        this.restoreFromPendingList(currentView.returnTo.previous_phase);
        return true;
      }
      this.replyingToHumanPrompt = null;
      this.editor.setText("");
      this.editor.focused = false;
      return true;
    }

    this.editor.handleInput(data);
    return true;
  }

  private showHumanReplyUnavailableNotice(
    status?: WorkflowStatus,
    kind?: WorkflowAgent["kind"],
  ): void {
    if (kind && kind !== "human") {
      this.showTransientNotice("Only human nodes can accept replies.");
      return;
    }
    if (status === "completed") {
      this.showTransientNotice("This node is completed and can no longer accept replies.");
      return;
    }
    if (status === "failed" || status === "stopped") {
      this.showTransientNotice(`This node is ${status} and can no longer accept replies.`);
      return;
    }
    this.showTransientNotice("This node is not waiting for a reply.");
  }

  private showTransientNotice(message: string, durationMs = 3000): void {
    this.transientNotice = message;
    if (this.transientNoticeTimer) {
      clearTimeout(this.transientNoticeTimer);
    }
    this.transientNoticeTimer = setTimeout(() => {
      this.transientNotice = null;
      this.transientNoticeTimer = null;
      this.tui.requestRender();
    }, durationMs);
    this.tui.requestRender();
  }

  private findAgentIdForHumanReply(workflowId: string, correlationId: string): string {
    const workflow = this.state.getSnapshot().workflowRuns.find((item) => item.id === workflowId);
    if (!workflow) return correlationId;
    for (const phase of workflow.phases ?? []) {
      for (const agent of phase.agents ?? []) {
        if (agent.id === correlationId) return agent.id;
        if ((agent.correlation_id ?? agent.id) === correlationId) return agent.id;
      }
    }
    return correlationId;
  }

  /** Build the reply input banner shown when `replyingToHumanPrompt` is set. */
  private buildSwarmflowHumanReplyInputLines(width: number): string[] {
    if (!this.replyingToHumanPrompt) return [];
    const { label, turn, workflowRunId, correlationId, isSession } = this.replyingToHumanPrompt;
    const agentId = this.findAgentIdForHumanReply(workflowRunId, correlationId);
    const question = this.getHumanQuestionText(workflowRunId, agentId);
    const header = isSession ? `Reply to ${label} · turn ${turn}` : `Reply to ${label}`;
    const lines = [padToWidth(palette.text.humanInput(header), width)];
    if (question) {
      lines.push(padToWidth(palette.text.secondary("Question"), width));
      const { lines: previewLines, truncated } = prepareHumanQuestionPreviewLines(
        question,
        width,
        6,
      );
      for (const qLine of previewLines) {
        lines.push(padToWidth(palette.text.dim(`  ${qLine}`), width));
      }
      if (truncated) {
        lines.push(padToWidth(palette.text.dim("  …"), width));
      }
      lines.push("");
    }
    lines.push(
      padToWidth(palette.text.secondary("Your answer"), width),
      ...this.editor.render(width),
      padToWidth(palette.text.dim("Enter submit · Esc cancel"), width),
    );
    return lines;
  }
}
