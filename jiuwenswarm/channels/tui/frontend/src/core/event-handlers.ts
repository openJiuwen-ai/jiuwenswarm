import {
  parseHistoryFrame,
  createAttachmentInfoEntry,
  createSessionResultToolDisplay,
  extractMediaItems,
  isHistoryDonePayload,
} from "./history-parser.js";
import { normalizeFinalContent } from "./final-content.js";
import {
  formatCompressionStartedLine,
  formatCompressionUsage,
} from "./compression-formatters.js";
import type { EventFrame } from "./protocol.js";
import {
  StreamingState,
  type ContextCompressionStats,
  type HistoryItem,
  type JsonObject,
  type SubtaskState,
  type TeamMemberEvent,
  type TeamMessageEvent,
  type TeamTaskEvent,
  type TodoItem,
  type ToolCallDisplay,
} from "./types.js";
import type { ConnectionStatus } from "./ws-client.js";
import { createId, findLastIndex, isIgnorableHistoryRestoreError } from "./app-state-helpers.js";
import { isClientMode, type ClientMode } from "./modes.js";
import type { WorkflowRun } from "./workflows.js";

type PreferredLanguage = "zh" | "en";

export interface PendingQuestion {
  requestId: string;
  source?: string;
  /** Mode active when the interrupt was raised; used when resuming the tool call. */
  resumeMode?: ClientMode;
  planApprovalKind?: string;
  planContent?: string;
  planLanguage?: "cn" | "en";
  planPath?: string;
  planSlug?: string;
  evolutionMeta?: Record<string, unknown>;
  questions: PendingQuestionItem[];
}

export interface PendingQuestionItem {
  header: string;
  question: string;
  options: PendingQuestionOption[];
  multiSelect?: boolean;
  planPath?: string;
  planSlug?: string;
}

export interface PendingQuestionOption {
  label: string;
  description?: string;
  value?: string;
  details?: string[];
  preview?: string;
}

export interface UserAnswer {
  selected_options: string[];
  custom_input?: string;
}

/**
 * A pending swarmflow human-session turn awaiting the person's reply.
 * Not the leader HITL channel (PendingQuestion) — this is a workflow human
 * node (human_session / human) paused on a real person's input.
 *
 * Composite map key is `${workflow_run_id}:${correlation_id}` so concurrent
 * runs with the same correlation id don't clobber each other's pending entry.
 */
export interface PendingHumanPrompt {
  workflow_run_id: string;
  workflow_id: string;
  workflow_name: string;
  agent_id: string;
  correlation_id: string;
  prompt: string;
  label: string;
  /** True when reply/detail chrome should show a turn label (session nodes only). */
  is_session: boolean;
  /** Turn index parsed from correlation_id ({phase}:{label}:{turn}); 0 for one-shot. */
  turn: number;
  /** Set when the reply has been submitted (entry stays dimmed in the list). */
  replied?: boolean;
  /** Short reply summary shown after the entry is replied. */
  answer?: string;
}

// Harness extension ready info
export interface HarnessExtensionReady {
  extensionName: string;
  runtimePath: string;
  sessionRuntimePath?: string;
  extensionRuntimePath?: string;
  configPath: string;
  runtimeExtensions?: RuntimeExtensionInfo[];
  verifyReport?: Record<string, unknown>;
  componentsSummary?: Record<string, unknown>;
}

export interface RuntimeExtensionInfo {
  extensionName: string;
  runtimePath: string;
  configPath: string;
}

// Harness activate interaction state
export interface HarnessActivateInteraction {
  interactionId: string;
  extensionName: string;
  runtimePath: string;
  options: string[];
  pending: boolean;
}

export interface AppEventDelegate {
  getConnectionStatus(): ConnectionStatus;
  getSessionId(): string;
  setSessionId(sessionId: string): void;
  setMode(mode: ClientMode): void;
  getMode(): ClientMode;
  getPreferredLanguage(): PreferredLanguage;
  getEntries(): HistoryItem[];
  setEntries(entries: HistoryItem[]): void;
  setStreamingState(state: StreamingState): void;
  setPendingQuestion(question: PendingQuestion | null): void;
  setLastError(error: string | null): void;
  getActiveSubtasks(): Map<string, SubtaskState>;
  setTodos(todos: TodoItem[]): void;
  appendTeamMemberEvent(event: TeamMemberEvent): void;
  appendTeamTaskEvent(event: TeamTaskEvent): void;
  appendTeamMessageEvent(event: TeamMessageEvent): void;
  applyWorkflowUpdate(workflow: WorkflowRun): void;
  setPendingHumanPrompts(prompts: Map<string, PendingHumanPrompt> | null): void;
  setEvolutionStatus(status: "idle" | "running"): void;
  setContextCompression(stats: ContextCompressionStats | null): void;
  setContextWindowLimit(n: number | null): void;
  setContextUsedPercentage(n: number | null): void;
  setSessionTitle(title: string): void;
  safeFetchSessionTitle(sessionId: string): void;
  addToolCallPayload(
    payload: Record<string, unknown>,
    sessionId: string,
    requestId?: string,
    startedAt?: string,
  ): void;
  addToolResultPayload(
    payload: Record<string, unknown>,
    sessionId: string,
    requestId?: string,
    updatedAt?: string,
  ): void;
  addSyntheticToolExecution(
    tool: ToolCallDisplay,
    sessionId: string,
    requestId?: string,
    at?: string,
  ): void;
  setCurrentWorkspaceFromTool(path: string): void;
  clearToolExecutionState(): void;
  /** 用户中断：将 running 的工具标为已结束，避免 TUI 继续转圈 */
  markRunningToolsInterrupted(): void;
  /** 退出前 cancel({showNotice:false}) 置 true，抑制 interrupt_result UI 通知。 */
  getSuppressInterruptResult(): boolean;
  clearSuppressInterruptResult(): void;
  /** 清除本地中断请求标志（streaming 结束后调用） */
  clearInterruptRequested(): void;
  pushHistoryEntry(entry: HistoryItem): void;
  scheduleHistoryFlush(): void;
  safeRestoreHistory(sessionId: string): void;
  /** 放行统一启动屏障；普通启动分配 ID，`--session` 恢复或登记显式 ID。 */
  initializeBootSession(): void;
  /** 报告 history.get 流返回的分页元数据（本页 page_idx / total_pages）。 */
  reportHistoryPageMeta(meta: { pageIdx?: number; totalPages?: number }): void;
  /** 某一页 history.get 流已结束（收到 `status: done` 帧），由 app-state 决定是否继续拉下一页。 */
  notifyHistoryPageDone(pageIdx: number): void;
  /** cancel 成功后判断是否需要自动回退 */
  tryAutoRestoreAfterCancel(): Promise<void>;
  /** 累加 chat.usage_summary 事件的 token/cost 数据（按 model 分桶）。 */
  appendUsageSummary(usage: Record<string, unknown>, model?: string): void;
  /** 累计已完成 model call 的 provider token 用量。 */
  appendUsageMetadata(usage: Record<string, unknown>): void;
  /** 回合结束时记录执行耗时条目到对话区。 */
  addWorkedForEntry(): void;
  /** Set harness extension ready info (for file tree display) */
  setHarnessExtensionReady(info: HarnessExtensionReady | null): void;
  /** Set harness activate interaction state (for user confirmation) */
  setHarnessActivateInteraction(state: HarnessActivateInteraction | null): void;
  /** Get current harness activate interaction state */
  getHarnessActivateInteraction(): HarnessActivateInteraction | null;
  /** Auto-activate extension (send activate_response with action="accept") */
  autoActivateExtension(interactionId: string): void;
  /**
   * 由 event-handlers 在收到 chat.interrupt_result 时调用；
   * 通知所有订阅器（等待型取消按 requestId + sessionId 关联）。
   */
  notifyInterruptResult(
    requestId: string,
    sessionId: string,
    success: boolean,
    message?: string,
  ): void;
}

function _handleAgentModeToolResult(
  delegate: AppEventDelegate,
  payload: Record<string, unknown>,
): void {
  const toolName = typeof payload.tool_name === "string" ? payload.tool_name : "";
  if (toolName !== "switch_mode" && toolName !== "exit_plan_mode") return;

  if (toolName === "exit_plan_mode") {
    // Plan exit is deferred until the user approves in chat; keep UI plan mode.
    return;
  }

  const resultRaw = payload.result;
  let subMode: string | null = null;

  if (typeof resultRaw === "object" && resultRaw !== null) {
    // result 已经是解析后的对象
    const data = (resultRaw as Record<string, unknown>).data;
    if (typeof data === "object" && data !== null) {
      const cm = (data as Record<string, unknown>).current_mode;
      if (typeof cm === "string") subMode = cm;
    }
  } else if (typeof resultRaw === "string") {
    // 先尝试 JSON 解析
    try {
      const parsed = JSON.parse(resultRaw);
      if (typeof parsed === "object" && parsed !== null) {
        const data = (parsed as Record<string, unknown>).data;
        if (typeof data === "object" && data !== null) {
          const cm = (data as Record<string, unknown>).current_mode;
          if (typeof cm === "string") subMode = cm;
        }
      }
    } catch {
      // JSON 解析失败，尝试从 Python str 表示中提取
      // 格式如: "success=True data={'current_mode': 'normal', 'message': '...'} error=None"
      const match = resultRaw.match(/current_mode['"]\s*:\s*['"](\w+)['"]/);
      if (match) subMode = match[1];
    }
  }

  if (!subMode) return;

  const existingMode = delegate.getMode();
  let newMode: ClientMode | null = null;
  if (existingMode.startsWith("code.")) {
    newMode = subMode === "team" ? "code.team" : "code.normal";
  } else if (existingMode.startsWith("agent.")) {
    newMode = subMode === "plan" ? "agent.plan" : "agent.fast";
  }

  if (newMode && newMode !== existingMode) {
    delegate.setMode(newMode);
  }
}

function _getToolResultPayload(payload: Record<string, unknown>): Record<string, unknown> {
  const nested = payload.tool_result;
  if (nested && typeof nested === "object" && !Array.isArray(nested)) {
    return nested as Record<string, unknown>;
  }
  return payload;
}

function _extractPathFromToolResult(
  payload: Record<string, unknown>,
  pathKeys: string[],
): string | null {
  const toolPayload = _getToolResultPayload(payload);
  const data = toolPayload.data;
  if (typeof toolPayload.success === "boolean" && toolPayload.success === false) {
    return null;
  }
  for (const source of [toolPayload, payload]) {
    for (const key of pathKeys) {
      const path = source[key];
      if (typeof path === "string" && path.trim()) {
        return path.trim();
      }
    }
  }
  if (data && typeof data === "object" && !Array.isArray(data)) {
    const record = data as Record<string, unknown>;
    for (const key of pathKeys) {
      const path = record[key];
      if (typeof path === "string" && path.trim()) {
        return path.trim();
      }
    }
  }

  const resultRaw = toolPayload.result ?? payload.result;
  if (typeof resultRaw !== "string") {
    return null;
  }
  if (/success\s*=\s*False/.test(resultRaw)) {
    return null;
  }

  try {
    const parsed = JSON.parse(resultRaw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      const parsedData = (parsed as Record<string, unknown>).data;
      if (parsedData && typeof parsedData === "object" && !Array.isArray(parsedData)) {
        const record = parsedData as Record<string, unknown>;
        for (const key of pathKeys) {
          const path = record[key];
          if (typeof path === "string" && path.trim()) {
            return path.trim();
          }
        }
      }
    }
  } catch {
    // ToolOutput is often rendered as a Python repr string.
  }

  for (const key of pathKeys) {
    const match = resultRaw.match(new RegExp(`${key}['"]\\s*:\\s*['"]([^'"]+)['"]`));
    if (match?.[1]?.trim()) {
      return match[1].trim();
    }
  }
  return null;
}

function _getToolName(payload: Record<string, unknown>): string {
  const toolPayload = _getToolResultPayload(payload);
  return typeof payload.tool_name === "string"
    ? payload.tool_name
    : typeof toolPayload.tool_name === "string"
      ? toolPayload.tool_name
      : typeof toolPayload.name === "string"
        ? toolPayload.name
        : "";
}

function _handleWorktreeToolResult(
  delegate: AppEventDelegate,
  payload: Record<string, unknown>,
): void {
  const toolName = _getToolName(payload);
  if (toolName === "enter_worktree") {
    const worktreePath = _extractPathFromToolResult(payload, ["worktree_path"]);
    if (worktreePath) {
      delegate.setCurrentWorkspaceFromTool(worktreePath);
    }
    return;
  }

  if (toolName === "exit_worktree") {
    const restorePath = _extractPathFromToolResult(payload, [
      "original_cwd",
      "workspace_path",
    ]);
    if (restorePath) {
      delegate.setCurrentWorkspaceFromTool(restorePath);
    }
  }
}

function appendEntry(delegate: AppEventDelegate, entry: HistoryItem): void {
  delegate.setEntries([...delegate.getEntries(), entry]);
}

function formatInterruptResultMessage(language: PreferredLanguage, intent: string, success: boolean, payloadMessage: unknown): string {
  const rawMessage = typeof payloadMessage === "string" ? payloadMessage.trim() : "";
  if (language !== "en") {
    if (rawMessage) return rawMessage;
    return success ? "当前会话任务已终止" : "当前会话任务终止失败";
  }
  const englishDefaults: Record<string, { success: string; failure: string }> = {
    cancel: { success: "Task cancelled", failure: "Failed to cancel task" },
    pause: { success: "Task paused", failure: "Failed to pause task" },
    resume: { success: "Task resumed", failure: "Failed to resume task" },
    switch: { success: "Task switched", failure: "Failed to switch task" },
  };
  const defaults = englishDefaults[intent] ?? englishDefaults.cancel!;
  if (!rawMessage) return success ? defaults.success : defaults.failure;
  const knownChineseMessages: Record<string, string> = {
    "任务已取消": "Task cancelled",
    "当前会话任务已终止": "Task cancelled",
    "当前会话任务终止失败": "Failed to cancel task",
    "任务中断失败": "Failed to interrupt task",
    "任务暂停失败": "Failed to pause task",
    "任务恢复失败": "Failed to resume task",
    "任务切换失败": "Failed to switch task",
    "已切换到新任务": "Switched to new task",
  };
  if (knownChineseMessages[rawMessage]) return knownChineseMessages[rawMessage];
  if (/[\u4e00-\u9fff]/.test(rawMessage)) return success ? defaults.success : defaults.failure;
  return rawMessage;
}

function appendThinkingChunk(
  delegate: AppEventDelegate,
  activeSessionId: string,
  content: string,
): void {
  const entries = delegate.getEntries();
  const lastEntry = entries[entries.length - 1];
  const at = new Date().toISOString();
  if (lastEntry && lastEntry.kind === "thinking" && lastEntry.sessionId === activeSessionId) {
    delegate.setEntries([
      ...entries.slice(0, -1),
      {
        ...lastEntry,
        content: `${lastEntry.content}${content}`,
        at,
      },
    ]);
    return;
  }

  appendEntry(delegate, {
    kind: "thinking",
    id: createId("reasoning"),
    sessionId: activeSessionId,
    content,
    at,
  });
}

function addSessionResultEntry(
  delegate: AppEventDelegate,
  payload: Record<string, unknown>,
  activeSessionId: string,
  effectiveEvent: string,
): void {
  const tool = createSessionResultToolDisplay(payload, effectiveEvent);
  delegate.addSyntheticToolExecution(
    tool,
    activeSessionId,
    typeof payload.request_id === "string" ? payload.request_id : undefined,
    new Date().toISOString(),
  );
}

function handleConnectionAck(delegate: AppEventDelegate, frame: EventFrame): boolean {
  if (frame.event !== "connection.ack") {
    return false;
  }
  if (delegate.getConnectionStatus() === "connected") {
    delegate.initializeBootSession();
  }
  return true;
}

function normalizePendingQuestion(payload: Record<string, unknown>): PendingQuestionItem[] {
  const rawQuestions = Array.isArray(payload.questions) ? payload.questions : [];
  const normalized = rawQuestions
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"))
    .map((item) => ({
      header: typeof item.header === "string" ? item.header : "Question",
      question: typeof item.question === "string" ? item.question : "",
      planPath: typeof item.plan_path === "string" ? item.plan_path : undefined,
      planSlug: typeof item.plan_slug === "string" ? item.plan_slug : undefined,
      options: Array.isArray(item.options)
        ? item.options
            .filter((option): option is Record<string, unknown> =>
              Boolean(option && typeof option === "object"),
            )
            .map((option) => ({
              label: typeof option.label === "string" ? option.label : "",
              description: typeof option.description === "string" ? option.description : undefined,
              value: typeof option.value === "string" ? option.value : undefined,
              preview:
                typeof option.preview === "string" && option.preview.trim()
                  ? option.preview
                  : undefined,
            }))
            .filter((option) => option.label.length > 0)
        : [],
      multiSelect: item.multi_select === true,
    }))
    .filter((item) => item.question.length > 0);

  if (normalized.length > 0) {
    return normalized;
  }

  const fallbackText =
    typeof payload.text === "string"
      ? payload.text
      : typeof payload.content === "string"
        ? payload.content
        : "";
  if (!fallbackText) {
    return [];
  }

  return [
    {
      header: "Question",
      question: fallbackText,
      options: [],
      multiSelect: false,
    },
  ];
}

function handleDelta(
  delegate: AppEventDelegate,
  payload: Record<string, unknown>,
  activeSessionId: string,
): boolean {
  const content = typeof payload.content === "string" ? payload.content : "";
  if (!content) return false;

  const entries = delegate.getEntries();
  if (payload.source_chunk_type === "llm_reasoning") {
    appendThinkingChunk(delegate, activeSessionId, content);
    delegate.setStreamingState(StreamingState.Responding);
    return true;
  }

  const requestId = typeof payload.request_id === "string" ? payload.request_id : undefined;
  const existingIndex = findLastIndex(
    entries,
    (entry) => entry.kind === "assistant" && entry.streaming === true,
  );
  if (existingIndex === -1) {
    delegate.setEntries([
      ...entries,
      {
        kind: "assistant",
        id: createId("stream"),
        sessionId: activeSessionId,
        content,
        requestId,
        streaming: true,
        at: new Date().toISOString(),
      },
    ]);
  } else {
    delegate.setEntries(
      entries.map((entry, index) =>
        index === existingIndex && entry.kind === "assistant"
          ? { ...entry, content: entry.content + content, requestId: entry.requestId ?? requestId }
          : entry,
      ),
    );
  }
  delegate.setStreamingState(StreamingState.Responding);
  return true;
}

function chooseFinalAssistantContent(streamedContent: string, finalContent: string): string {
  if (!streamedContent) {
    return finalContent;
  }
  if (!finalContent) {
    return streamedContent;
  }
  if (finalContent === streamedContent || finalContent.startsWith(streamedContent)) {
    return finalContent;
  }
  if (streamedContent.includes(finalContent)) {
    return streamedContent;
  }
  return finalContent.length >= streamedContent.length ? finalContent : streamedContent;
}

function handleFinal(
  delegate: AppEventDelegate,
  payload: Record<string, unknown>,
  activeSessionId: string,
): boolean {
  const content = normalizeFinalContent(payload);
  const finalizedAt = new Date().toISOString();
  const entries = delegate.getEntries();
  const streamingIndex = findLastIndex(
    entries,
    (entry) => entry.kind === "assistant" && entry.streaming === true,
  );
  const streamedContent =
    streamingIndex !== -1 && entries[streamingIndex]?.kind === "assistant"
      ? entries[streamingIndex].content
      : "";
  const finalContent = chooseFinalAssistantContent(streamedContent, content);
  delegate.setEntries(
    streamingIndex !== -1
      ? [
          ...entries.filter(
            (entry, index) => !(index === streamingIndex && entry.kind === "assistant"),
          ),
          {
            ...(entries[streamingIndex] as Extract<HistoryItem, { kind: "assistant" }>),
            content: finalContent,
            requestId:
              typeof payload.request_id === "string"
                ? payload.request_id
                : entries[streamingIndex]?.kind === "assistant"
                  ? entries[streamingIndex].requestId
                  : undefined,
            at: finalizedAt,
            streaming: false,
          },
        ]
      : [
          ...entries,
          {
            kind: "assistant",
            id: createId("assistant-final"),
            sessionId: activeSessionId,
            content,
            requestId: typeof payload.request_id === "string" ? payload.request_id : undefined,
            streaming: false,
            at: finalizedAt,
          },
        ],
  );
  delegate.addWorkedForEntry();
  // Defensive: chat.final is the definitive end-of-response marker.
  // The primary Idle transition is driven by chat.processing_status
  // (is_processing=false), but if that frame is lost (server crash,
  // connection drop, cancel path), the UI would be stuck in Responding.
  // Setting Idle here is safe — processing_status will override if needed.
  delegate.setStreamingState(StreamingState.Idle);
  return true;
}

function handleReasoning(
  delegate: AppEventDelegate,
  payload: Record<string, unknown>,
  activeSessionId: string,
): boolean {
  const content = typeof payload.content === "string" ? payload.content : "";
  if (!content) return false;
  appendThinkingChunk(delegate, activeSessionId, content);
  delegate.setStreamingState(StreamingState.Responding);
  return true;
}

function handleError(
  delegate: AppEventDelegate,
  payload: Record<string, unknown>,
  activeSessionId: string,
): boolean {
  const message =
    typeof payload.error === "string"
      ? payload.error
      : typeof payload.content === "string"
        ? payload.content
        : "Unknown error";
  if (isIgnorableHistoryRestoreError(message)) {
    return false;
  }
  appendEntry(delegate, {
    kind: "error",
    id: createId("error"),
    sessionId: activeSessionId,
    content: message,
    at: new Date().toISOString(),
  });
  delegate.setLastError(message);
  delegate.setStreamingState(StreamingState.Idle);
  delegate.addWorkedForEntry();
  return true;
}

function handleMediaEvent(
  delegate: AppEventDelegate,
  payload: Record<string, unknown>,
  activeSessionId: string,
  effectiveEvent: "chat.media" | "chat.file",
): boolean {
  const at = new Date().toISOString();
  const content = typeof payload.content === "string" ? payload.content.trim() : "";
  const mediaItems = extractMediaItems(payload);
  if (effectiveEvent === "chat.media" && (content || mediaItems.length > 0)) {
    const entries = delegate.getEntries();
    const assistantIndex = findLastIndex(
      entries,
      (entry) => entry.kind === "assistant" && (entry.streaming === true || !entry.streaming),
    );
    if (assistantIndex !== -1) {
      delegate.setEntries(
        entries.map((entry, index) =>
          index === assistantIndex && entry.kind === "assistant"
            ? {
                ...entry,
                ...(content ? { content } : {}),
                ...(mediaItems.length > 0 ? { mediaItems } : {}),
                streaming: false,
              }
            : entry,
        ),
      );
    } else {
      appendEntry(delegate, {
        kind: "assistant",
        id: createId("assistant-media"),
        sessionId: activeSessionId,
        content,
        ...(mediaItems.length > 0 ? { mediaItems } : {}),
        at,
        streaming: false,
      });
    }
    return true;
  }

  const infoEntry = createAttachmentInfoEntry(payload, activeSessionId, effectiveEvent, at);
  if (infoEntry) {
    appendEntry(delegate, infoEntry);
    return true;
  }

  appendEntry(delegate, {
    kind: "system",
    id: createId("system"),
    sessionId: activeSessionId,
    content: `[${effectiveEvent}]`,
    at,
    meta: {
      eventType: effectiveEvent,
      rawPayload: payload as JsonObject,
    },
  });
  return true;
}

function handleContextCompressed(
  delegate: AppEventDelegate,
  payload: Record<string, unknown>,
): boolean {
  const rate = typeof payload.rate === "number" ? payload.rate : 0;
  const before = typeof payload.context_max === "number"
    ? payload.context_max
    : typeof payload.before_compressed === "number"
      ? payload.before_compressed
      : null;
  const after = typeof payload.tokens_used === "number"
    ? payload.tokens_used
    : typeof payload.after_compressed === "number"
      ? payload.after_compressed
      : null;
  delegate.setContextCompression({
    rate,
    beforeCompressed: before,
    afterCompressed: after,
  });
  return true;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function readString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function formatNumber(value: unknown): string {
  const n = readNumber(value);
  return n == null ? "-" : Math.round(n).toLocaleString("en-US");
}

function formatPercent(value: unknown): string {
  const n = readNumber(value);
  return n == null ? "-" : `${Math.round(n)}%`;
}

function formatDuration(value: unknown): string {
  const n = readNumber(value);
  if (n == null) return "-";
  return n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${Math.round(n)}ms`;
}

function formatChange(before: unknown, after: unknown, formatter = formatNumber): string {
  const beforeText = formatter(before);
  const afterText = formatter(after);
  if (afterText === "-") return beforeText;
  return `${beforeText} -> ${afterText}`;
}

function handleContextCompressionState(
  delegate: AppEventDelegate,
  payload: Record<string, unknown>,
  activeSessionId: string,
): boolean {
  const before = asRecord(payload.before);
  const after = asRecord(payload.after);
  const saved = asRecord(payload.saved);
  const compressionUsage = asRecord(payload.compression_usage);
  const status = readString(payload.status, "unknown");
  const phase = readString(payload.phase, "unknown");
  const processor = readString(payload.processor, "unknown") || "unknown";
  const model = readString(payload.model);
  const summary = readString(payload.compact_summary);
  const statsSummary = readString(payload.summary);
  const error = readString(payload.error);

  const savedParts = [
    `${formatNumber(saved.tokens)} tokens`,
    `${formatNumber(saved.messages)} messages`,
    formatPercent(saved.percent),
  ].filter((part) => !part.startsWith("-"));

  const normalizedStatus = status.trim().toLowerCase();
  const hasAfterStats = readNumber(after.tokens) !== null;
  const savedTokens = readNumber(saved.tokens) ?? 0;
  const savedMessages = readNumber(saved.messages) ?? 0;
  const shouldUpdateCompressionStats =
    hasAfterStats && (normalizedStatus === "completed" || normalizedStatus === "compressed");
  if (shouldUpdateCompressionStats) {
    delegate.setContextCompression({
      rate: readNumber(after.context_percent) ?? 0,
      beforeCompressed: readNumber(before.tokens),
      afterCompressed: readNumber(after.tokens),
      ...(summary ? { summary } : {}),
      trigger: "auto",
    });
  }

  const isCompacted =
    (Boolean(summary) || savedTokens > 0 || savedMessages > 0) &&
    !error &&
    normalizedStatus !== "error" &&
    normalizedStatus !== "failed" &&
    normalizedStatus !== "skipped";
  const isError = Boolean(error) || normalizedStatus === "error" || normalizedStatus === "failed";
  const detailItems = [
    { label: "Processor", value: processor },
    { label: "Phase", value: phase },
    ...(model ? [{ label: "Model", value: model }] : []),
    { label: "Messages", value: formatChange(before.messages, after.messages) },
    { label: "Tokens", value: formatChange(before.tokens, after.tokens) },
    {
      label: "Context",
      value: formatChange(before.context_percent, after.context_percent, formatPercent),
    },
    ...(savedParts.length ? [{ label: "Saved", value: savedParts.join(" | ") }] : []),
    ...(Object.keys(compressionUsage).length
      ? [{ label: "Compression usage", value: formatCompressionUsage(compressionUsage) }]
      : []),
    { label: "Duration", value: formatDuration(payload.duration_ms) },
    ...(statsSummary ? [{ label: "Summary", description: statsSummary }] : []),
    ...(error ? [{ label: "Error", description: error }] : []),
  ];

  if (!isCompacted && !isError) {
    if (normalizedStatus === "started" || normalizedStatus === "noop") {
      const detailEntry: HistoryItem = {
        kind: "info",
        id: createId("context-compression-detail"),
        sessionId: activeSessionId,
        content: `Context compression ${status}`,
        icon: "i",
        transcriptOnly: true,
        meta: {
          title: `Context compression ${status}`,
          items: detailItems,
        },
        at: new Date().toISOString(),
      };
      if (normalizedStatus === "started") {
        appendEntry(delegate, {
          kind: "info",
          id: createId("context-compression-started"),
          sessionId: activeSessionId,
          content: formatCompressionStartedLine(processor, phase, before),
          icon: "i",
          transcriptOnly: true,
          meta: { view: "dim" },
          at: new Date().toISOString(),
        });
      }
      appendEntry(delegate, detailEntry);
    }
    return true;
  }

  appendEntry(delegate, {
    kind: "info",
    id: createId("context-compression"),
    sessionId: activeSessionId,
    content: isCompacted ? "Conversation compacted" : `Context compression ${status}`,
    icon: "i",
    meta: {
      ...(isCompacted ? { view: "compact_boundary" as const } : {}),
      title: isCompacted ? "Conversation compacted" : `Context compression ${status}`,
      items: detailItems,
    },
    at: new Date().toISOString(),
  });
  if (isCompacted) {
    appendEntry(delegate, {
      kind: "info",
      id: createId("context-compact-summary"),
      sessionId: activeSessionId,
      content: summary,
      icon: "i",
      transcriptOnly: true,
      meta: {
        view: "compact_summary",
        title: "Compaction summary",
      },
      at: new Date().toISOString(),
    });
  }
  return true;
}

function handleSubtaskUpdate(
  delegate: AppEventDelegate,
  payload: Record<string, unknown>,
): boolean {
  const taskId = typeof payload.task_id === "string" ? payload.task_id : "";
  if (!taskId) return false;
  const subtasks = delegate.getActiveSubtasks();
  if (payload.status === "completed" || payload.status === "error") {
    subtasks.delete(taskId);
    return true;
  }
  subtasks.set(taskId, {
    task_id: taskId,
    description: typeof payload.description === "string" ? payload.description : "",
    status: (typeof payload.status === "string"
      ? payload.status
      : "starting") as SubtaskState["status"],
    index: typeof payload.index === "number" ? payload.index : 0,
    total: typeof payload.total === "number" ? payload.total : 0,
    tool_name: typeof payload.tool_name === "string" ? payload.tool_name : undefined,
    tool_count: typeof payload.tool_count === "number" ? payload.tool_count : 0,
    message: typeof payload.message === "string" ? payload.message : undefined,
    is_parallel: payload.is_parallel === true,
  });
  return true;
}

function normalizeTodoStatus(status: unknown): TodoItem["status"] | null {
  if (status === "deleted" || status === "cancelled" || status === "canceled") {
    return null;
  }
  if (status === "in_progress" || status === "completed" || status === "error") {
    return status;
  }
  if (status === "failed") {
    return "error";
  }
  return "pending";
}

function isTodoPayloadItem(item: unknown): item is Record<string, unknown> {
  return Boolean(item && typeof item === "object" && !Array.isArray(item));
}

function handleTodoUpdated(delegate: AppEventDelegate, payload: Record<string, unknown>): boolean {
  const todos = Array.isArray(payload.todos) ? payload.todos : [];
  delegate.setTodos(
    todos
      .filter(isTodoPayloadItem)
      .map((item): TodoItem | null => {
        const status = normalizeTodoStatus(item.status);
        if (status === null) {
          return null;
        }
        return {
          id: typeof item.id === "string" ? item.id : "",
          content: typeof item.content === "string" ? item.content : "",
          activeForm: typeof item.activeForm === "string" ? item.activeForm : "",
          status,
          createdAt: typeof item.createdAt === "string" ? item.createdAt : new Date().toISOString(),
          updatedAt: typeof item.updatedAt === "string" ? item.updatedAt : new Date().toISOString(),
        };
      })
      .filter((item): item is TodoItem => item !== null && item.id.length > 0),
  );
  return true;
}

function normalizeNestedPayload(payload: Record<string, unknown>): Record<string, unknown> {
  const nested = payload.payload;
  if (nested && typeof nested === "object" && !Array.isArray(nested)) {
    return nested as Record<string, unknown>;
  }
  return payload;
}

function normalizeTimestamp(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    if (!Number.isNaN(parsed)) {
      return parsed;
    }
  }
  return Date.now();
}

function handleTeamMemberEvent(
  delegate: AppEventDelegate,
  payload: Record<string, unknown>,
): boolean {
  const normalized = normalizeNestedPayload(payload);
  const event = normalized.event;
  if (!event || typeof event !== "object" || Array.isArray(event)) {
    return false;
  }
  const record = event as Record<string, unknown>;
  const memberId = typeof record.member_id === "string" ? record.member_id.trim() : "";
  if (!memberId) {
    return false;
  }
  delegate.appendTeamMemberEvent({
    id: createId("team-member"),
    type: typeof record.type === "string" ? record.type : "team.member",
    teamId: typeof record.team_id === "string" ? record.team_id : "",
    memberId,
    oldStatus: typeof record.old_status === "string" ? record.old_status : undefined,
    newStatus: typeof record.new_status === "string" ? record.new_status : undefined,
    reason: typeof record.reason === "string" ? record.reason : undefined,
    restartCount: typeof record.restart_count === "number" ? record.restart_count : undefined,
    force: typeof record.force === "boolean" ? record.force : undefined,
    timestamp: normalizeTimestamp(record.timestamp),
  });
  return true;
}

function handleTeamTaskEvent(
  delegate: AppEventDelegate,
  payload: Record<string, unknown>,
): boolean {
  const normalized = normalizeNestedPayload(payload);
  const event = normalized.event;
  if (!event || typeof event !== "object" || Array.isArray(event)) {
    return false;
  }
  const record = event as Record<string, unknown>;
  const taskId = typeof record.task_id === "string" ? record.task_id.trim() : "";
  if (!taskId) {
    return false;
  }
  delegate.appendTeamTaskEvent({
    id: createId("team-task"),
    type: typeof record.type === "string" ? record.type : "team.task",
    teamId: typeof record.team_id === "string" ? record.team_id : "",
    taskId,
    status: typeof record.status === "string" ? record.status : undefined,
    timestamp: normalizeTimestamp(record.timestamp),
  });
  return true;
}

function handleTeamMessageEvent(
  delegate: AppEventDelegate,
  payload: Record<string, unknown>,
): boolean {
  const normalized = normalizeNestedPayload(payload);
  const event = normalized.event;
  if (!event || typeof event !== "object" || Array.isArray(event)) {
    return false;
  }
  const record = event as Record<string, unknown>;
  const fromMember = typeof record.from_member === "string" ? record.from_member.trim() : "";
  if (!fromMember) {
    return false;
  }
  delegate.appendTeamMessageEvent({
    id: createId("team-message"),
    type: typeof record.type === "string" ? record.type : "team.message",
    teamId: typeof record.team_id === "string" ? record.team_id : "",
    messageId: typeof record.message_id === "string" ? record.message_id : undefined,
    fromMember,
    toMember: typeof record.to_member === "string" ? record.to_member : undefined,
    content: typeof record.content === "string" ? record.content : "",
    timestamp: normalizeTimestamp(record.timestamp),
  });
  return true;
}

function handleWorkflowUpdated(
  delegate: AppEventDelegate,
  payload: Record<string, unknown>,
): boolean {
  const normalized = normalizeNestedPayload(payload);
  const workflow = normalized.workflow;
  if (!workflow || typeof workflow !== "object" || Array.isArray(workflow)) {
    return false;
  }
  if (typeof (workflow as Record<string, unknown>).id !== "string") {
    return false;
  }

  delegate.applyWorkflowUpdate(workflow as unknown as WorkflowRun);
  return true;
}

export function handleIncomingFrame(delegate: AppEventDelegate, frame: EventFrame): boolean {
  const connectionChanged = handleConnectionAck(delegate, frame);

  const payload = frame.payload;
  const effectiveEvent = typeof payload.event_type === "string" ? payload.event_type : frame.event;
  const activeSessionId = delegate.getSessionId();
  const eventSessionId = typeof payload.session_id === "string" ? payload.session_id : "";
  if (effectiveEvent === "chat.processing_status" && !eventSessionId) {
    return connectionChanged;
  }
  if (eventSessionId && eventSessionId !== activeSessionId) {
    return connectionChanged;
  }

  switch (effectiveEvent) {
    case "chat.delta":
      return handleDelta(delegate, payload, activeSessionId) || connectionChanged;

    case "chat.final":
      return handleFinal(delegate, payload, activeSessionId) || connectionChanged;

    case "chat.reasoning":
      return handleReasoning(delegate, payload, activeSessionId) || connectionChanged;

    case "chat.error":
      return handleError(delegate, payload, activeSessionId) || connectionChanged;

    case "chat.tool_call":
      delegate.addToolCallPayload(
        payload,
        activeSessionId,
        typeof payload.request_id === "string" ? payload.request_id : undefined,
      );
      return true;

    case "chat.tool_result":
      _handleAgentModeToolResult(delegate, payload);
      delegate.addToolResultPayload(
        payload,
        activeSessionId,
        typeof payload.request_id === "string" ? payload.request_id : undefined,
      );
      _handleWorktreeToolResult(delegate, payload);
      return true;

    case "chat.symphony_status": {
      const content = typeof payload.content === "string" ? payload.content.trim() : "";
      if (!content) return true;
      if (payload.status === "failed") {
        appendEntry(delegate, {
          kind: "error",
          id: createId("symphony-status"),
          sessionId: activeSessionId,
          content,
          at: new Date().toISOString(),
        });
      } else {
        appendEntry(delegate, {
          kind: "info",
          id: createId("symphony-status"),
          sessionId: activeSessionId,
          content,
          icon: "i",
          at: new Date().toISOString(),
        });
      }
      return true;
    }

    case "chat.processing_status":
      delegate.setStreamingState(
        payload.is_processing === true ? StreamingState.Responding : StreamingState.Idle,
      );
      if (payload.is_processing !== true) {
        delegate.getActiveSubtasks().clear();
        delegate.setTodos([]);
        delegate.setEvolutionStatus("idle");
        delegate.clearInterruptRequested();
      }
      return true;

    case "chat.interrupt_result": {
      const intent = typeof payload.intent === "string" ? payload.intent : "cancel";
      const requestId = typeof payload.request_id === "string" ? payload.request_id : "";
      const eventSessionId =
        typeof payload.session_id === "string" ? payload.session_id : activeSessionId;
      if (intent === "cancel") {
        // 先通知等待型 waiter，让其 resolve/reject。
        // 服务端可能不回显 TUI 的 requestId（使用自己的 interrupt_xxx 格式），
        // 因此即使 requestId 为空也要通知，由 waiter 按 sessionId 关联。
        // 迟到事件（waiter 已清除）由 listener 自行过滤。
        const success = payload.success !== false;
        const message =
          typeof payload.message === "string" ? payload.message : undefined;
        delegate.notifyInterruptResult(requestId, eventSessionId, success, message);
        const suppressed = delegate.getSuppressInterruptResult();
        if (suppressed) {
          delegate.clearSuppressInterruptResult();
          return true;
        }
        const uiMessage = formatInterruptResultMessage(delegate.getPreferredLanguage(), intent, success, payload.message);
        if (success) {
          delegate.setStreamingState(StreamingState.Interrupted);
          delegate.getActiveSubtasks().clear();
          delegate.setEvolutionStatus("idle");
          delegate.markRunningToolsInterrupted();
          delegate.clearInterruptRequested();
          appendEntry(delegate, {
            kind: "info",
            id: createId("info"),
            sessionId: activeSessionId,
            content: uiMessage,
            icon: "i",
            at: new Date().toISOString(),
          });
          // 仅当目标 turn 之后无实质性 assistant 输出时执行
          delegate.tryAutoRestoreAfterCancel().catch(() => {
            // 自动回退失败不影响 cancel 本身，用户可手动 /rewind
          });
        } else {
          appendEntry(delegate, {
            kind: "error",
            id: createId("error"),
            sessionId: activeSessionId,
            content: uiMessage,
            at: new Date().toISOString(),
          });
          delegate.setLastError(uiMessage);
          // 失败也视为本次 interrupt 请求已结束，清本地标志 + 解除 esc 去抖窗口，
          // 否则去抖窗口内后续 esc 会被一直抑制，无法重新发起取消。
          delegate.clearInterruptRequested();
        }
      } else if (intent === "pause") {
        delegate.setStreamingState(StreamingState.Paused);
      } else {
        delegate.setStreamingState(StreamingState.Responding);
      }
      return true;
    }

    case "plan.approval_required":
      // Text-only plan approval: plan + marker already appear in the chat stream.
      return connectionChanged;

    case "plan.mode_exited": {
      const mode = typeof payload.mode === "string" ? payload.mode : "code.normal";
      if (mode === "code.normal" && delegate.getMode().startsWith("code.")) {
        delegate.setMode("code.normal");
      }
      return true;
    }

    case "chat.ask_user_question": {
      const requestId = typeof payload.request_id === "string" ? payload.request_id : "";
      const questions = normalizePendingQuestion(payload);
      if (!requestId || questions.length === 0) {
        return connectionChanged;
      }
      const evolutionMeta =
        payload.evolution_meta && typeof payload.evolution_meta === "object"
          ? (payload.evolution_meta as Record<string, unknown>)
          : payload._evolution_meta && typeof payload._evolution_meta === "object"
            ? (payload._evolution_meta as Record<string, unknown>)
            : undefined;
      const source = typeof payload.source === "string" ? payload.source : undefined;
      const planApprovalKind =
        typeof payload.plan_approval_kind === "string" ? payload.plan_approval_kind : undefined;
      const planContent =
        typeof payload.plan_content === "string" ? payload.plan_content : undefined;
      const planLanguage =
        payload.plan_language === "cn" || payload.plan_language === "en"
          ? payload.plan_language
          : undefined;
      const planPath =
        typeof payload.plan_path === "string" && payload.plan_path.trim()
          ? payload.plan_path.trim()
          : questions[0]?.planPath;
      const planSlug =
        typeof payload.plan_slug === "string" && payload.plan_slug.trim()
          ? payload.plan_slug.trim()
          : questions[0]?.planSlug;
      delegate.setPendingQuestion({
        requestId,
        source,
        resumeMode: delegate.getMode(),
        planApprovalKind,
        planContent,
        planLanguage,
        planPath,
        planSlug,
        evolutionMeta,
        questions,
      });
      delegate.setStreamingState(StreamingState.WaitingForConfirmation);
      return true;
    }

    case "history.message": {
      // 先感知分页元数据（done 帧不会产生 entry，但必须让 app-state 感知）。
      const pageIdxRaw = payload.page_idx;
      const totalPagesRaw = payload.total_pages;
      delegate.reportHistoryPageMeta({
        pageIdx: typeof pageIdxRaw === "number" ? pageIdxRaw : undefined,
        totalPages: typeof totalPagesRaw === "number" ? totalPagesRaw : undefined,
      });
      if (isHistoryDonePayload(payload)) {
        if (typeof pageIdxRaw === "number") {
          delegate.notifyHistoryPageDone(pageIdxRaw);
        }
        return connectionChanged;
      }
      const entry = parseHistoryFrame(frame);
      if (!entry) {
        return connectionChanged;
      }
      delegate.pushHistoryEntry(entry);
      delegate.scheduleHistoryFlush();
      return connectionChanged;
    }

    case "chat.media":
    case "chat.file":
      return handleMediaEvent(delegate, payload, activeSessionId, effectiveEvent);

    case "context.usage":
      return handleContextCompressed(delegate, payload);

    case "context.compression_state":
      return handleContextCompressionState(delegate, payload, activeSessionId);

    case "chat.subtask_update":
      return handleSubtaskUpdate(delegate, payload);

    case "chat.session_result":
    case "session_result":
      addSessionResultEntry(delegate, payload, activeSessionId, effectiveEvent);
      return true;

    case "chat.evolution_status":
      delegate.setEvolutionStatus(payload.status === "start" ? "running" : "idle");
      return true;

    case "todo.updated":
      return handleTodoUpdated(delegate, payload);

    case "session.updated": {
      const mode = typeof payload.mode === "string" ? payload.mode : "";
      if (isClientMode(mode)) {
        delegate.setMode(mode);
      }
      if (typeof payload.title === "string") {
        delegate.setSessionTitle(payload.title);
      }
      return true;
    }

    case "team.member":
      return handleTeamMemberEvent(delegate, payload);

    case "team.task":
      return handleTeamTaskEvent(delegate, payload);

    case "team.message":
      return handleTeamMessageEvent(delegate, payload);

    case "workflow.updated":
      return handleWorkflowUpdated(delegate, payload);

    case "chat.usage_metadata": {
      const metadata =
        typeof payload.metadata === "object" && payload.metadata !== null
          ? (payload.metadata as Record<string, unknown>)
          : {};
      const usage =
        typeof metadata.usage_metadata === "object" && metadata.usage_metadata !== null
          ? (metadata.usage_metadata as Record<string, unknown>)
          : {};
      delegate.appendUsageMetadata(usage);
      return true;
    }

    case "chat.llm_usage":
      delegate.appendUsageMetadata(
        typeof payload.usage_metadata === "object" && payload.usage_metadata !== null
          ? (payload.usage_metadata as Record<string, unknown>)
          : {},
      );
      return true;

    case "chat.usage_summary":
      delegate.appendUsageSummary(
        typeof payload.usage === "object" && payload.usage !== null
          ? (payload.usage as Record<string, unknown>)
          : {},
        typeof payload.model === "string" ? payload.model : undefined,
      );
      if (typeof payload.usage_percent === "number") {
        delegate.setContextUsedPercentage(payload.usage_percent);
      }
      if (typeof payload.context_window_tokens === "number") {
        delegate.setContextWindowLimit(payload.context_window_tokens);
      }
      return true;

    case "harness.extension_ready": {
      const extensionName = typeof payload.extension_name === "string" ? payload.extension_name : "";
      const runtimePath = typeof payload.runtime_path === "string" ? payload.runtime_path : "";
      const sessionRuntimePath = typeof payload.session_runtime_path === "string" ? payload.session_runtime_path : runtimePath;
      const extensionRuntimePath = typeof payload.extension_runtime_path === "string" ? payload.extension_runtime_path : "";
      const configPath = typeof payload.config_path === "string" ? payload.config_path : "";
      const runtimeExtensions = Array.isArray(payload.runtime_extensions)
        ? payload.runtime_extensions
            .filter((item) => typeof item === "object" && item !== null)
            .map((item) => {
              const obj = item as Record<string, unknown>;
              return {
                extensionName: typeof obj.extension_name === "string" ? obj.extension_name : "",
                runtimePath: typeof obj.runtime_path === "string" ? obj.runtime_path : "",
                configPath: typeof obj.config_path === "string" ? obj.config_path : "",
              };
            })
            .filter((item) => item.extensionName && item.runtimePath)
        : [];
      const verifyReport = typeof payload.verify_report === "object" && payload.verify_report !== null && !Array.isArray(payload.verify_report)
        ? payload.verify_report as Record<string, unknown>
        : {};
      const componentsSummary = typeof payload.components_summary === "object" && payload.components_summary !== null && !Array.isArray(payload.components_summary)
        ? payload.components_summary as Record<string, unknown>
        : {};

      delegate.setHarnessExtensionReady({
        extensionName,
        runtimePath,
        sessionRuntimePath,
        extensionRuntimePath,
        configPath,
        runtimeExtensions,
        verifyReport,
        componentsSummary,
      });

      // Show info entry for extension ready
      appendEntry(delegate, {
        kind: "info",
        id: createId("harness-extension-ready"),
        sessionId: activeSessionId,
        content: `🔧 扩展已生成: ${extensionName}`,
        icon: "i",
        meta: {
          title: `扩展生成完成: ${extensionName}`,
          items: [
            { label: "运行路径", value: runtimePath },
            { label: "配置路径", value: configPath },
            ...(runtimeExtensions.length > 0 ? [{ label: "依赖扩展", value: runtimeExtensions.map(e => e.extensionName).join(", ") }] : []),
          ],
        },
        at: new Date().toISOString(),
      });
      return true;
    }

    case "harness.activate_interaction": {
      const interactionId = typeof payload.interaction_id === "string" ? payload.interaction_id : "";
      const extensionName = typeof payload.extension_name === "string" ? payload.extension_name : "";
      const runtimePath = typeof payload.runtime_path === "string" ? payload.runtime_path : "";
      const options: string[] = Array.isArray(payload.options) ? payload.options : ["accept", "reject"];

      delegate.setHarnessActivateInteraction({
        interactionId,
        extensionName,
        runtimePath,
        options,
        pending: false,
      });

      // TUI is a log-viewing interface - auto-activate without user confirmation
      // Log activation info and send response directly
      appendEntry(delegate, {
        kind: "info",
        id: createId("harness-activate"),
        sessionId: activeSessionId,
        content: `扩展 **${extensionName}** 激活请求已收到，正在自动激活生效...`,
        icon: "i",
        meta: {
          title: `扩展激活: ${extensionName}`,
          items: [
            { label: "扩展名称", value: extensionName },
            { label: "运行路径", value: runtimePath },
          ],
        },
        at: new Date().toISOString(),
      });

      // Auto-activate extension (send activate_response with action="accept")
      delegate.autoActivateExtension(interactionId);
      return true;
    }

    default:
      return connectionChanged;
  }
}
