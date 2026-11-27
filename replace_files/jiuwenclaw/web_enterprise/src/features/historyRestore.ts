import { Message, MessageRole, UsageSummary, WsEvent } from '../types';
import { webClient } from '../services/webClient';
import { normalizeFinalContent } from '../utils/finalContent';

export const HISTORY_GET_METHOD = 'history.get';
export const HISTORY_MESSAGE_EVENT = 'history.message';

/** 助手侧仅恢复这些事件；用户消息无 event_type，单独保留 */
const ALLOWED_ASSISTANT_EVENT_TYPES = new Set([
  'chat.final',
  'chat.tool_call',
  'chat.tool_result',
  'chat.usage_summary',
]);

/** 后端约定：最后一帧 `history.message` 使用 `payload.status: done`（兼容旧版 `payload.content: done`） */
const HISTORY_RESTORE_DONE_CONTENT = 'done';
/** 流式 chunk 之间的兜底：正常情况由 `done` / `page_complete` 等结束帧关闭；仅当缺少明确结束标记时使用 */
const HISTORY_RESTORE_IDLE_MS = 500;

export interface HistoryToolReplayItem {
  kind: 'tool_call' | 'tool_result';
  at: string;
  payload: Record<string, unknown>;
}

type HistoryTimelineEntry =
  | { kind: 'message'; message: Message }
  | { kind: 'tool_call'; at: string; payload: Record<string, unknown> }
  | { kind: 'tool_result'; at: string; payload: Record<string, unknown> }
  | { kind: 'usage_summary'; at: string; usage: UsageSummary };

interface BeginHistoryRestoreOptions {
  sessionId: string;
  onReady: (messages: Message[], totalPages: number | null) => void;
  /** 与消息同一时间线顺序，用于恢复 ToolGroupDisplay */
  onToolReplay?: (items: HistoryToolReplayItem[]) => void;
  /** 无消息且无工具回放时调用；`totalPages` 来自流中最后一帧（若有） */
  onEmpty?: (totalPages: number | null) => void;
  onError?: (message: string) => void;
}

export interface HistoryRestoreHandle {
  generation: number;
  dispose: () => void;
}

let restoreGeneration = 0;
let activeRestore: HistoryRestoreHandle | null = null;

/** 分页拉取与全量恢复互斥，避免 chunk 串台 */
let activePageFetchDispose: (() => void) | null = null;

function disposeActivePageFetch(): void {
  activePageFetchDispose?.();
  activePageFetchDispose = null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function pickFirstString(input: Record<string, unknown>, keys: string[]): string | undefined {
  for (const key of keys) {
    const value = input[key];
    if (typeof value === 'string') {
      const trimmed = value.trim();
      if (trimmed) {
        return trimmed;
      }
    }
  }
  return undefined;
}

function normalizeHistoryRole(rawRole: unknown): MessageRole {
  if (typeof rawRole !== 'string') return 'assistant';
  const role = rawRole.trim().toLowerCase();
  if (role === 'user' || role === 'human') return 'user';
  if (role === 'assistant' || role === 'ai' || role === 'bot') return 'assistant';
  if (role === 'system') return 'system';
  if (role === 'tool' || role === 'tool_call' || role === 'tool_result') return 'tool';
  return 'assistant';
}

function isHistoryRestoreDoneContent(rawContent: unknown): boolean {
  if (typeof rawContent !== 'string') {
    return false;
  }
  return rawContent.trim().toLowerCase() === HISTORY_RESTORE_DONE_CONTENT;
}

function isHistoryRestoreDonePayload(payload: Record<string, unknown>): boolean {
  const rawStatus = payload.status;
  if (typeof rawStatus === 'string' && rawStatus.trim().toLowerCase() === HISTORY_RESTORE_DONE_CONTENT) {
    return true;
  }
  return isHistoryRestoreDoneContent(payload.content);
}

function extractHistoryMessagePayload(payload: Record<string, unknown>): unknown {
  if ('message' in payload) {
    return payload.message;
  }
  return payload.content;
}

function normalizeHistoryContent(
  rawContent: unknown,
  onError?: (message: string) => void
): Record<string, unknown> | null {
  if (isHistoryRestoreDoneContent(rawContent)) {
    return null;
  }
  if (isRecord(rawContent)) {
    return rawContent;
  }
  if (typeof rawContent !== 'string') {
    return null;
  }
  try {
    const parsed = JSON.parse(rawContent);
    if (isRecord(parsed)) {
      return parsed;
    }
    return null;
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    onError?.(`history.message.content parse failed: ${detail}`);
    return null;
  }
}

function recordTimestampIso(record: Record<string, unknown>): string {
  const ts = record.timestamp;
  if (typeof ts === 'number' && Number.isFinite(ts)) {
    const millis = ts > 1_000_000_000_000 ? ts : ts * 1000;
    const d = new Date(millis);
    if (!Number.isNaN(d.getTime())) {
      return d.toISOString();
    }
  }
  if (typeof ts === 'string') {
    const parsed = Date.parse(ts);
    if (!Number.isNaN(parsed)) {
      return new Date(parsed).toISOString();
    }
  }
  return new Date().toISOString();
}

const _HISTORY_RECORD_META_KEYS = new Set([
  'id', 'role', 'request_id', 'channel_id', 'timestamp', 'event_type', 'event_payload',
]);

/** 合并 event_payload 与顶层 content，供 final / tool 解析 */
function buildEventPayloadForRecord(record: Record<string, unknown>): Record<string, unknown> {
  const ep = record.event_payload;
  const base = isRecord(ep) ? { ...ep } : {};

  // 无 event_payload 时：将顶层工具字段（extra 展平写入的字段）提升到 base
  if (!isRecord(ep)) {
    for (const [key, value] of Object.entries(record)) {
      if (!_HISTORY_RECORD_META_KEYS.has(key)) {
        base[key] = value;
      }
    }
  }

  if (typeof record.content === 'string' && typeof base.content !== 'string') {
    base.content = record.content;
  }
  return base;
}

function parseHistoryTimelineEntry(
  record: Record<string, unknown>,
  sessionId: string
): HistoryTimelineEntry | null {
  const role = normalizeHistoryRole(record.role);
  const at = recordTimestampIso(record);

  if (role === 'user') {
    const content = pickFirstString(record, ['content', 'text', 'body']) ?? '';
    if (!content.trim()) {
      return null;
    }
    const id =
      pickFirstString(record, ['id', 'message_id', 'msg_id']) ?? `hist-user-${sessionId}-${at}`;
    return {
      kind: 'message',
      message: { id, role: 'user', content, timestamp: at },
    };
  }

  if (role !== 'assistant') {
    return null;
  }

  let eventType = typeof record.event_type === 'string' ? record.event_type.trim() : '';

  if (!eventType) {
    const raw = String(record.content ?? '').trim();
    if (!raw) {
      return null;
    }
    eventType = 'chat.final';
  }

  if (!ALLOWED_ASSISTANT_EVENT_TYPES.has(eventType)) {
    return null;
  }

  const payload = buildEventPayloadForRecord(record);

  if (eventType === 'chat.final') {
    const content = normalizeFinalContent(payload);
    if (!content.trim()) {
      return null;
    }
    const id =
      pickFirstString(record, ['id', 'message_id', 'msg_id']) ?? `hist-final-${sessionId}-${at}`;
    return {
      kind: 'message',
      message: { id, role: 'assistant', content, timestamp: at },
    };
  }

  if (eventType === 'chat.tool_call') {
    return { kind: 'tool_call', at, payload };
  }

  if (eventType === 'chat.tool_result') {
    return { kind: 'tool_result', at, payload };
  }

  if (eventType === 'chat.usage_summary') {
    const rawUsage = payload.usage;
    if (isRecord(rawUsage)) {
      const usage: UsageSummary = {
        input_tokens: typeof rawUsage.input_tokens === 'number' ? rawUsage.input_tokens : 0,
        output_tokens: typeof rawUsage.output_tokens === 'number' ? rawUsage.output_tokens : 0,
        total_tokens: typeof rawUsage.total_tokens === 'number' ? rawUsage.total_tokens : 0,
      };
      if (typeof rawUsage.input_cost === 'number') usage.input_cost = rawUsage.input_cost;
      if (typeof rawUsage.output_cost === 'number') usage.output_cost = rawUsage.output_cost;
      if (typeof rawUsage.total_cost === 'number') usage.total_cost = rawUsage.total_cost;
      return { kind: 'usage_summary', at, usage };
    }
    return null;
  }

  return null;
}

/** 工作区 history.json 预览：最多展示条数（按消息时间取最近） */
export const HISTORY_FILE_PREVIEW_MAX_MESSAGES = 20;

/**
 * 将磁盘上的 history.json 解析结果（通常为记录数组）转为与历史恢复相同的筛选规则下的消息列表，
 * 并按时间升序仅保留时间上最近的 {@link HISTORY_FILE_PREVIEW_MAX_MESSAGES} 条用户/助手消息。
 */
export function parseHistoryJsonFileToPreviewMessages(
  parsed: unknown,
  sessionId: string
): Message[] {
  if (!Array.isArray(parsed)) {
    return [];
  }

  const messages: Message[] = [];
  for (const item of parsed) {
    if (!isRecord(item)) {
      continue;
    }
    const entry = parseHistoryTimelineEntry(item, sessionId);
    if (entry?.kind === 'message') {
      messages.push(entry.message);
    }
  }

  const sorted = [...messages].sort(
    (a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp)
  );
  return sorted.slice(-HISTORY_FILE_PREVIEW_MAX_MESSAGES);
}

function isHistoryBatchEnd(payload: Record<string, unknown>): boolean {
  const markers = [
    payload.done,
    payload.last,
    payload.is_last,
    payload.page_complete,
    payload.end,
  ];
  return markers.some((marker) => marker === true);
}

/**
 * 仅处理属于当前 `history.get` 会话的帧，避免多标签/乱序下的串台。
 * 无 `session_id` 时：丢弃数据行；仍接受明确的结束帧（兼容未注入 id 的旧链路）。
 */
function shouldProcessHistoryPayload(payload: Record<string, unknown>, expectedSessionId: string): boolean {
  const sid = typeof payload.session_id === 'string' ? payload.session_id.trim() : '';
  if (sid && sid !== expectedSessionId) {
    return false;
  }
  if (!sid) {
    return isHistoryRestoreDonePayload(payload) || isHistoryBatchEnd(payload);
  }
  return true;
}

export function beginHistoryRestore(options: BeginHistoryRestoreOptions): HistoryRestoreHandle {
  disposeActivePageFetch();
  activeRestore?.dispose();

  const generation = restoreGeneration + 1;
  restoreGeneration = generation;

  const entries: HistoryTimelineEntry[] = [];
  let totalPages: number | null = null;
  let idleTimer: number | null = null;
  let disposed = false;

  const clearIdleTimer = () => {
    if (idleTimer !== null) {
      window.clearTimeout(idleTimer);
      idleTimer = null;
    }
  };

  const unsubscribe = webClient.on(HISTORY_MESSAGE_EVENT, (event: WsEvent) => {
    if (disposed || generation !== restoreGeneration) {
      return;
    }

    const payload = event.payload;
    if (!shouldProcessHistoryPayload(payload, options.sessionId)) {
      return;
    }

    if (typeof payload.total_pages === 'number' && Number.isFinite(payload.total_pages)) {
      totalPages = payload.total_pages;
    }

    if (isHistoryRestoreDonePayload(payload)) {
      clearIdleTimer();
      finalize();
      return;
    }

    const raw = extractHistoryMessagePayload(payload);
    const record = normalizeHistoryContent(raw, options.onError);
    if (record) {
      const entry = parseHistoryTimelineEntry(record, options.sessionId);
      if (entry) {
        entries.unshift(entry);
      }
    }

    if (isHistoryBatchEnd(payload)) {
      clearIdleTimer();
      finalize();
      return;
    }

    clearIdleTimer();
    idleTimer = window.setTimeout(() => {
      finalize();
    }, HISTORY_RESTORE_IDLE_MS);
  });

  function dispose(): void {
    if (disposed) return;
    disposed = true;
    clearIdleTimer();
    unsubscribe();
    if (activeRestore?.generation === generation) {
      activeRestore = null;
    }
  }

  function finalize(): void {
    if (disposed) return;

    const messages: Message[] = [];
    const toolReplay: HistoryToolReplayItem[] = [];
    for (const e of entries) {
      if (e.kind === 'message') {
        messages.push(e.message);
      } else if (e.kind === 'usage_summary') {
        for (let i = messages.length - 1; i >= 0; i--) {
          if (messages[i].role === 'assistant') {
            messages[i] = { ...messages[i], usageSummary: e.usage };
            break;
          }
        }
      } else {
        toolReplay.push({ kind: e.kind, at: e.at, payload: e.payload });
      }
    }

    dispose();

    if (messages.length === 0 && toolReplay.length === 0) {
      options.onEmpty?.(totalPages);
      return;
    }
    options.onReady(messages, totalPages);
    if (toolReplay.length > 0) {
      options.onToolReplay?.(toolReplay);
    }
  }

  const handle: HistoryRestoreHandle = { generation, dispose };
  activeRestore = handle;
  return handle;
}

export interface FetchHistoryPageResult {
  messages: Message[];
  toolReplay: HistoryToolReplayItem[];
  totalPages: number | null;
}

export interface FetchHistoryPageOptions {
  sessionId: string;
  onReady: (result: FetchHistoryPageResult) => void;
  onEmpty?: (totalPages: number | null) => void;
  onError?: (message: string) => void;
}

/**
 * 拉取单页历史（用于「加载更早」），与 beginHistoryRestore 互斥。
 * 调用方需在订阅建立后再发 `history.get`（含对应 `page_idx`）。
 */
export function fetchHistoryPage(options: FetchHistoryPageOptions): HistoryRestoreHandle {
  disposeActivePageFetch();
  activeRestore?.dispose();

  const generation = restoreGeneration + 1;
  restoreGeneration = generation;

  const entries: HistoryTimelineEntry[] = [];
  let totalPages: number | null = null;
  let idleTimer: number | null = null;
  let disposed = false;

  const clearIdleTimer = () => {
    if (idleTimer !== null) {
      window.clearTimeout(idleTimer);
      idleTimer = null;
    }
  };

  const unsubscribe = webClient.on(HISTORY_MESSAGE_EVENT, (event: WsEvent) => {
    if (disposed || generation !== restoreGeneration) {
      return;
    }

    const payload = event.payload;
    if (!shouldProcessHistoryPayload(payload, options.sessionId)) {
      return;
    }

    if (typeof payload.total_pages === 'number' && Number.isFinite(payload.total_pages)) {
      totalPages = payload.total_pages;
    }

    if (isHistoryRestoreDonePayload(payload)) {
      clearIdleTimer();
      finalize();
      return;
    }

    const raw = extractHistoryMessagePayload(payload);
    const record = normalizeHistoryContent(raw, options.onError);
    if (record) {
      const entry = parseHistoryTimelineEntry(record, options.sessionId);
      if (entry) {
        entries.unshift(entry);
      }
    }

    if (isHistoryBatchEnd(payload)) {
      clearIdleTimer();
      finalize();
      return;
    }

    clearIdleTimer();
    idleTimer = window.setTimeout(() => {
      finalize();
    }, HISTORY_RESTORE_IDLE_MS);
  });

  function dispose(): void {
    if (disposed) return;
    disposed = true;
    clearIdleTimer();
    unsubscribe();
    activePageFetchDispose = null;
    if (activeRestore?.generation === generation) {
      activeRestore = null;
    }
  }

  function finalize(): void {
    if (disposed) return;

    const messages: Message[] = [];
    const toolReplay: HistoryToolReplayItem[] = [];
    for (const e of entries) {
      if (e.kind === 'message') {
        messages.push(e.message);
      } else if (e.kind === 'usage_summary') {
        for (let i = messages.length - 1; i >= 0; i--) {
          if (messages[i].role === 'assistant') {
            messages[i] = { ...messages[i], usageSummary: e.usage };
            break;
          }
        }
      } else {
        toolReplay.push({ kind: e.kind, at: e.at, payload: e.payload });
      }
    }

    dispose();

    if (messages.length === 0 && toolReplay.length === 0) {
      options.onEmpty?.(totalPages);
      return;
    }
    options.onReady({ messages, toolReplay, totalPages });
  }

  const handle: HistoryRestoreHandle = { generation, dispose };
  activeRestore = handle;
  activePageFetchDispose = dispose;
  return handle;
}