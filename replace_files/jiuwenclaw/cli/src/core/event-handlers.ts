import {
  parseHistoryFrame,
  createAttachmentInfoEntry,
  createSessionResultToolDisplay,
  extractMediaItems,
} from "./history-parser.js";
import { normalizeFinalContent } from "./final-content.js";
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

export interface PendingQuestion {
  requestId: string;
  source?: string;
  questions: PendingQuestionItem[];
}

export interface PendingQuestionItem {
  header: string;
  question: string;
  options: PendingQuestionOption[];
  multiSelect?: boolean;
}

export interface PendingQuestionOption {
  label: string;
  description?: string;
}

export interface UserAnswer {
  selected_options: string[];
  custom_input?: string;
}

export interface AppEventDelegate {
  getConnectionStatus(): ConnectionStatus;
  getSessionId(): string;
  setSessionId(sessionId: string): void;
  setMode(mode: "agent.plan" | "agent.fast" | "code.plan" | "code.normal" | "team"): void;
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
  setEvolutionStatus(status: "idle" | "running"): void;
  setContextCompression(stats: ContextCompressionStats | null): void;
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
  clearToolExecutionState(): void;
  /** 用户中断：将 running 的工具标为已结束，避免 TUI 继续转圈 */
  markRunningToolsInterrupted(): void;
  pushHistoryEntry(entry: HistoryItem): void;
  scheduleHistoryFlush(): void;
  safeRestoreHistory(sessionId: string): void;
}

function appendEntry(delegate: AppEventDelegate, entry: HistoryItem): void {
  delegate.setEntries([...delegate.getEntries(), entry]);
}

function appendThinkingChunk(
  delegate: AppEventDelegate,
  activeSessionId: string,
  content: string,
): void {
  const entries = delegate.getEntries();
  const lastEntry = entries[entries.length - 1];
  if (lastEntry && lastEntry.kind === "thinking" && lastEntry.sessionId === activeSessionId) {
    delegate.setEntries([
      ...entries.slice(0, -1),
      {
        ...lastEntry,
        content: `${lastEntry.content}${content}`,
      },
    ]);
    return;
  }

  appendEntry(delegate, {
    kind: "thinking",
    id: createId("reasoning"),
    sessionId: activeSessionId,
    content,
    at: new Date().toISOString(),
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
  // session_id is determined at construction time; connection.ack is only
  // used as a signal to restore history once connected.
  const sessionId = delegate.getSessionId();
  if (sessionId && delegate.getConnectionStatus() === "connected") {
    delegate.safeRestoreHistory(sessionId);
    delegate.safeFetchSessionTitle(sessionId);
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
      options: Array.isArray(item.options)
        ? item.options
            .filter((option): option is Record<string, unknown> =>
              Boolean(option && typeof option === "object"),
            )
            .map((option) => ({
              label: typeof option.label === "string" ? option.label : "",
              description: typeof option.description === "string" ? option.description : undefined,
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
  delegate.setEntries(
    streamingIndex !== -1
      ? [
          ...entries.filter(
            (entry, index) => !(index === streamingIndex && entry.kind === "assistant"),
          ),
          {
            ...(entries[streamingIndex] as Extract<HistoryItem, { kind: "assistant" }>),
            content:
              content ||
              (entries[streamingIndex]?.kind === "assistant"
                ? entries[streamingIndex].content
                : ""),
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
  const before = typeof payload.before_compressed === "number" ? payload.before_compressed : null;
  const after = typeof payload.after_compressed === "number" ? payload.after_compressed : null;
  delegate.setContextCompression({
    rate,
    beforeCompressed: before,
    afterCompressed: after,
  });
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

function handleTodoUpdated(delegate: AppEventDelegate, payload: Record<string, unknown>): boolean {
  const todos = Array.isArray(payload.todos) ? payload.todos : [];
  delegate.setTodos(
    todos
      .filter((item): item is TodoItem => Boolean(item && typeof item === "object"))
      .map((item) => ({
        id: typeof item.id === "string" ? item.id : "",
        content: typeof item.content === "string" ? item.content : "",
        activeForm: typeof item.activeForm === "string" ? item.activeForm : "",
        status: (item.status === "in_progress" || item.status === "completed"
          ? item.status
          : "pending") as TodoItem["status"],
        createdAt: typeof item.createdAt === "string" ? item.createdAt : new Date().toISOString(),
        updatedAt: typeof item.updatedAt === "string" ? item.updatedAt : new Date().toISOString(),
      }))
      .filter((item) => item.id.length > 0),
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
      delegate.addToolResultPayload(
        payload,
        activeSessionId,
        typeof payload.request_id === "string" ? payload.request_id : undefined,
      );
      return true;

    case "chat.processing_status":
      delegate.setStreamingState(
        payload.is_processing === true ? StreamingState.Responding : StreamingState.Idle,
      );
      if (payload.is_processing !== true) {
        delegate.getActiveSubtasks().clear();
        delegate.setEvolutionStatus("idle");
      }
      return true;

    case "chat.interrupt_result": {
      const intent = typeof payload.intent === "string" ? payload.intent : "cancel";
      if (intent === "cancel") {
        delegate.setStreamingState(StreamingState.Idle);
        delegate.getActiveSubtasks().clear();
        delegate.setEvolutionStatus("idle");
        delegate.markRunningToolsInterrupted();
      } else if (intent === "pause") {
        delegate.setStreamingState(StreamingState.Paused);
      } else {
        delegate.setStreamingState(StreamingState.Responding);
      }
      return true;
    }

    case "chat.ask_user_question": {
      const requestId = typeof payload.request_id === "string" ? payload.request_id : "";
      const questions = normalizePendingQuestion(payload);
      if (!requestId || questions.length === 0) {
        return connectionChanged;
      }
      delegate.setPendingQuestion({
        requestId,
        source: typeof payload.source === "string" ? payload.source : undefined,
        questions,
      });
      delegate.setStreamingState(StreamingState.WaitingForConfirmation);
      return true;
    }

    case "history.message": {
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

    case "context.compressed":
      return handleContextCompressed(delegate, payload);

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
      if (
        mode === "agent.plan" ||
        mode === "agent.fast" ||
        mode === "code.plan" ||
        mode === "code.normal" ||
        mode === "team"
      ) {
        delegate.setMode(mode as "agent.plan" | "agent.fast" | "code.plan" | "code.normal" | "team");
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

    default:
      return connectionChanged;
  }
}
