import { Message, MessageRole, UsageSummary, FileDownloadItem, MediaItem, WsEvent, ToolExecution } from '../types';
import { webClient } from '../services/webClient';
import { normalizeFinalContent } from '../utils/finalContent';
import { mergeFileDownloadItems } from '../utils/fileDownloadDedup';
import { parseTimestampToMs, timestampMsToIso } from '../utils/timestamp';
import { extractAutomation } from '../utils/heartbeatAutomation';
import { isA2UIClientEventContent } from './a2ui/a2uiContent';
import { normalizeToolCallPayload, normalizeToolResultPayload } from './tool-events/toolEventNormalizer';
import {
  buildGoalCompletedContent,
  isGoalCompletedContent,
} from '../components/GoalBar/goalCompletedMessage';
import { HistoryRecordReassembler } from './historyRecordReassembler';

export { HistoryRecordReassembler };

export const HISTORY_GET_METHOD = 'history.get';
export const HISTORY_MESSAGE_EVENT = 'history.message';

/**
 * 历史加载兜底超时（毫秒）。
 * faas 侧 history.get 流若因旧 session runtime 过 TTL 被回收而 init 超时
 * （60s timed out），后端不会发 done/batch_end 结束帧，也不会发匹配的
 * chat.error；前端若无限等待会让 isLoadingHistory 永久卡 true，进而吞掉
 * 后续所有 chat.processing_status(is_processing=false)，表现为「一直加载中」。
 * 到期强制 finalize，让调用方 setLoadingHistory(false) 恢复可交互。
 */
const HISTORY_RESTORE_TIMEOUT_MS = 30_000;

/** 助手侧仅恢复这些事件；用户消息无 event_type，单独保留 */
const ALLOWED_ASSISTANT_EVENT_TYPES = new Set([
  'chat.final',
  'chat.tool_call',
  'chat.tool_result',
  'chat.usage_summary',
  'chat.file',
  'chat.subtask_update',
  'team.message',
  'team.member',
  'team.task',
  'harness.message',
  'harness.stage_result',
  'harness.extension_ready',
  'context.compact_boundary'
]);

/** 后端约定：最后一帧 `history.message` 使用 `payload.status: done`（兼容旧版 `payload.content: done`） */
const HISTORY_RESTORE_DONE_CONTENT = 'done';

export interface HistoryToolReplayItem {
  kind: 'tool_call' | 'tool_result';
  at: string;
  payload: Record<string, unknown>;
}

/** Merge tool replay pages before pairing calls with their results. */
export function mergeHistoryToolReplayItems(
  existing: HistoryToolReplayItem[],
  incoming: HistoryToolReplayItem[],
): HistoryToolReplayItem[] {
  const merged = new Map<string, HistoryToolReplayItem>();
  for (const item of [...existing, ...incoming]) {
    merged.set(JSON.stringify(item), item);
  }
  return [...merged.values()];
}

export interface HistorySubagentRecovery {
  subagent: Record<string, unknown>;
  turns?: Array<{ task_id: string; task_description: string; started_at: number }>;
  result?: {
    subagent_id: string;
    content: string;
    output_file?: string;
  };
}

function readQuotedToken(input: string, start: number): { value: string; next: number } | null {
  const quote = input[start];
  if (quote !== "'" && quote !== '"') return null;
  let value = '';
  for (let index = start + 1; index < input.length; index += 1) {
    const char = input[index];
    if (char === '\\') {
      const escaped = input[index + 1];
      if (escaped === undefined) return null;
      value += escaped === 'n' ? '\n' : escaped === 'r' ? '\r' : escaped === 't' ? '\t' : escaped;
      index += 1;
      continue;
    }
    if (char === quote) return { value, next: index + 1 };
    value += char;
  }
  return null;
}

function findQuotedField(raw: string, field: string): string | null {
  const fieldPattern = new RegExp(`(?<![A-Za-z0-9_])(?:['"])?${field}(?:['"])?\\s*:`);
  const match = fieldPattern.exec(raw);
  if (!match) return null;
  let index = match.index + match[0].length;
  while (/\s/.test(raw[index] ?? '')) index += 1;
  return readQuotedToken(raw, index)?.value ?? null;
}

function findQuotedMapping(raw: string, field: string): Record<string, string> {
  const fieldPattern = new RegExp(`(?<![A-Za-z0-9_])(?:['"])?${field}(?:['"])?\\s*:\\s*\\{`);
  const match = fieldPattern.exec(raw);
  if (!match) return {};
  let index = match.index + match[0].length;
  const values: Record<string, string> = {};
  while (index < raw.length) {
    while (/\s|,/.test(raw[index] ?? '')) index += 1;
    if (raw[index] === '}') break;
    const key = readQuotedToken(raw, index);
    if (!key) break;
    index = key.next;
    while (/\s/.test(raw[index] ?? '')) index += 1;
    if (raw[index] !== ':') break;
    index += 1;
    while (/\s/.test(raw[index] ?? '')) index += 1;
    const value = readQuotedToken(raw, index);
    if (!value) break;
    values[key.value] = value.value;
    index = value.next;
  }
  return values;
}

function extractToolResultText(payload: Record<string, unknown>): string {
  const nested = isRecord(payload.tool_result) ? payload.tool_result : payload;
  return typeof nested.result === 'string' ? nested.result : '';
}

function extractHistorySubagentIds(payload: Record<string, unknown>): string[] {
  const raw = extractToolResultText(payload);
  const ids = new Set<string>();
  const direct = findQuotedField(raw, 'subagent_id');
  if (direct) ids.add(direct);
  for (const field of ['statuses', 'results', 'output_files']) {
    for (const id of Object.keys(findQuotedMapping(raw, field))) ids.add(id);
  }
  return [...ids];
}

function parseHistorySubagentWaitResult(payload: Record<string, unknown>): {
  statuses: Record<string, string>;
  results: Record<string, string>;
  outputFiles: Record<string, string>;
} {
  const nested = isRecord(payload.tool_result) ? payload.tool_result : payload;
  const structured = isRecord(nested.subagent_wait) ? nested.subagent_wait : null;
  if (structured) {
    const statuses = isRecord(structured.statuses) ? structured.statuses : {};
    const results = isRecord(structured.results) ? structured.results : {};
    const outputFiles = isRecord(structured.output_files) ? structured.output_files : {};
    return {
      statuses: Object.fromEntries(Object.entries(statuses).flatMap(([id, value]) => typeof value === 'string' ? [[id, value]] : [])),
      results: Object.fromEntries(Object.entries(results).flatMap(([id, value]) => typeof value === 'string' ? [[id, value]] : [])),
      outputFiles: Object.fromEntries(Object.entries(outputFiles).flatMap(([id, value]) => typeof value === 'string' ? [[id, value]] : [])),
    };
  }
  const raw = extractToolResultText(payload);
  return {
    statuses: findQuotedMapping(raw, 'statuses'),
    results: findQuotedMapping(raw, 'results'),
    outputFiles: findQuotedMapping(raw, 'output_files'),
  };
}

function normalizeRecoveredSubagentStatus(status: string): {
  status: 'running' | 'idle' | 'closed';
  turn_outcome: 'completed' | 'failed' | 'cancelled' | null;
  lifecycle: 'live' | 'closed';
  can_send_input: boolean;
  needs_resume: boolean;
  closed_reason: 'failed' | 'cancelled' | 'completed' | 'parent_ended' | 'manual' | 'evicted' | null;
} | null {
  const normalized = status.trim().toLowerCase();
  if (normalized === 'failed' || normalized === 'errored' || normalized === 'error') {
    return { status: 'idle', turn_outcome: 'failed', lifecycle: 'live', can_send_input: true, needs_resume: false, closed_reason: null };
  }
  if (normalized === 'cancelled' || normalized === 'canceled' || normalized === 'interrupted') {
    return { status: 'idle', turn_outcome: 'cancelled', lifecycle: 'live', can_send_input: true, needs_resume: false, closed_reason: null };
  }
  if (normalized === 'closed') {
    return { status: 'closed', turn_outcome: null, lifecycle: 'closed', can_send_input: false, needs_resume: true, closed_reason: 'manual' };
  }
  if (normalized === 'completed' || normalized === 'idle') {
    return { status: 'idle', turn_outcome: 'completed', lifecycle: 'live', can_send_input: true, needs_resume: false, closed_reason: null };
  }
  if (normalized === 'running' || normalized === 'pending' || normalized === 'pending_init') {
    return { status: 'running', turn_outcome: null, lifecycle: 'live', can_send_input: false, needs_resume: false, closed_reason: null };
  }
  return null;
}

/**
 * Recover subagent roster/result hints from the parent history's structured tool records.
 * The parent history endpoint currently filters chat.subtask_update, so this is only a
 * discovery bridge to the independent subagent history; it never treats tool-result text
 * as a transcript/result body except the explicit subagent_wait.results mapping.
 */
export function recoverSubagentToolHistory(
  items: HistoryToolReplayItem[],
  sessionId: string,
): HistorySubagentRecovery[] {
  type MutableRecovery = {
    id: string;
    subagentType: string;
    displayName: string;
    role: string;
    taskDescription: string;
    turns: Map<string, { task_id: string; task_description: string; started_at: number }>;
    status: NonNullable<ReturnType<typeof normalizeRecoveredSubagentStatus>>;
    createdAt: number;
    updatedAt: number;
    revision: number;
    result?: { content: string; output_file?: string };
  };

  const records = new Map<string, MutableRecovery>();
  const pendingSpawns: Array<{ subagentType: string; displayName: string; role: string; taskDescription: string; at: number }> = [];
  const pendingFollowUps = new Map<string, Array<{ taskDescription: string; at: number }>>();
  const ordered = [...items].sort((left, right) => parseTimestampToMs(left.at) - parseTimestampToMs(right.at));
  const ensure = (id: string, at: number, metadata?: { subagentType?: string; displayName?: string; role?: string; taskDescription?: string; taskId?: string }): MutableRecovery => {
    const existing = records.get(id);
    if (existing) {
      if (metadata?.subagentType) existing.subagentType = metadata.subagentType;
      if (metadata?.displayName) existing.displayName = metadata.displayName;
      if (metadata?.role) existing.role = metadata.role;
      if (metadata?.taskDescription) existing.taskDescription = metadata.taskDescription;
      if (metadata?.taskId && metadata.taskDescription) {
        existing.turns.set(metadata.taskId, {
          task_id: metadata.taskId,
          task_description: metadata.taskDescription,
          started_at: at,
        });
      }
      return existing;
    }
    const created = {
      id,
      subagentType: metadata?.subagentType || 'general-purpose',
      displayName: metadata?.displayName || metadata?.subagentType || 'general-purpose',
      role: metadata?.role || '',
      taskDescription: metadata?.taskDescription || '',
      turns: new Map<string, { task_id: string; task_description: string; started_at: number }>(),
      status: normalizeRecoveredSubagentStatus('running')!,
      createdAt: Number.isFinite(at) ? at : 0,
      updatedAt: Number.isFinite(at) ? at : 0,
      revision: 0,
    };
    if (metadata?.taskId && metadata.taskDescription) {
      created.turns.set(metadata.taskId, {
        task_id: metadata.taskId,
        task_description: metadata.taskDescription,
        started_at: at,
      });
    }
    records.set(id, created);
    return created;
  };
  const update = (record: MutableRecovery, status: string, at: number) => {
    const normalized = normalizeRecoveredSubagentStatus(status);
    if (!normalized) {
      records.delete(record.id);
      return;
    }
    record.status = normalized;
    record.updatedAt = Number.isFinite(at) ? at : record.updatedAt;
    record.revision += 1;
  };

  const toolResultSucceeded = (payload: Record<string, unknown>): boolean | null => {
    const nested = isRecord(payload.tool_result) ? payload.tool_result : payload;
    if (typeof nested.success === 'boolean') return nested.success;
    const raw = extractToolResultText(payload);
    const match = /\bsuccess\s*=\s*(True|False)\b/.exec(raw);
    if (match) return match[1] === 'True';
    const status = (typeof nested.status === 'string' ? nested.status : findQuotedField(raw, 'status'))?.trim().toLowerCase();
    if (!status) return null;
    if (['error', 'failed', 'failure', 'timeout', 'timed_out'].includes(status)) return false;
    if (['pending', 'queued', 'accepted', 'running', 'completed', 'success', 'succeeded'].includes(status)) return true;
    return null;
  };

  for (const item of ordered) {
    const at = parseTimestampToMs(item.at);
    if (item.kind === 'tool_call') {
      const call = normalizeToolCallPayload(item.payload);
      if (!call.name.startsWith('subagent_')) continue;
      const args = call.arguments;
      if (call.name === 'subagent_spawn') {
        pendingSpawns.push({
          subagentType: typeof args.subagent_type === 'string' ? args.subagent_type : 'general-purpose',
          displayName: typeof args.display_name === 'string' ? args.display_name.trim() : '',
          role: typeof args.role === 'string' ? args.role.trim() : '',
          taskDescription: typeof args.task_description === 'string' ? args.task_description : '',
          at,
        });
        continue;
      }
      if (call.name === 'subagent_send_input') {
        const subagentId = typeof args.subagent_id === 'string' ? args.subagent_id.trim() : '';
        const taskDescription = typeof args.query === 'string' ? args.query.trim() : '';
        if (subagentId && taskDescription) {
          pendingFollowUps.set(subagentId, [
            ...(pendingFollowUps.get(subagentId) ?? []),
            { taskDescription, at },
          ]);
        }
      }
      // Do not optimistically change a roster from a tool call. The bfb runtime
      // rejects close on a running subagent; only its successful tool result is
      // authoritative for the recovered status.
      continue;
    }

    const toolName = normalizeToolResultPayload(item.payload).toolName;
    if (!toolName.startsWith('subagent_')) continue;
    if (toolResultSucceeded(item.payload) !== true) continue;
    const rawResult = extractToolResultText(item.payload);
    if (toolName === 'subagent_spawn') {
      const id = findQuotedField(rawResult, 'subagent_id');
      if (!id) continue;
      const metadata = pendingSpawns.shift();
      const taskId = findQuotedField(rawResult, 'task_id') ?? undefined;
      ensure(id, at, metadata ? { ...metadata, taskId } : { taskId });
      continue;
    }
    if (toolName === 'subagent_wait') {
      const wait = parseHistorySubagentWaitResult(item.payload);
      for (const [id, status] of Object.entries(wait.statuses)) {
        const record = ensure(id, at);
        update(record, status, at);
        const content = wait.results[id]?.trim();
        if (content) {
          record.result = {
            content: wait.results[id],
            ...(wait.outputFiles[id]?.trim() ? { output_file: wait.outputFiles[id].trim() } : {}),
          };
        }
      }
      continue;
    }
    if (toolName === 'subagent_close') {
      const id = findQuotedField(rawResult, 'subagent_id') ?? extractHistorySubagentIds(item.payload)[0];
      if (id) update(ensure(id, at), 'closed', at);
      continue;
    }
    if (toolName === 'subagent_send_input' || toolName === 'subagent_resume') {
      const id = findQuotedField(rawResult, 'subagent_id') ?? extractHistorySubagentIds(item.payload)[0];
      const status = findQuotedField(rawResult, 'status');
      if (id && status) {
        const record = ensure(id, at);
        update(record, status, at);
        if (toolName === 'subagent_send_input') {
          const followUps = pendingFollowUps.get(id) ?? [];
          const followUp = followUps.shift();
          if (followUps.length > 0) pendingFollowUps.set(id, followUps);
          else pendingFollowUps.delete(id);
          if (followUp) {
            record.taskDescription = followUp.taskDescription;
            record.updatedAt = Math.max(record.updatedAt, followUp.at);
            const taskId = findQuotedField(rawResult, 'task_id');
            if (taskId) {
              record.turns.set(taskId, {
                task_id: taskId,
                task_description: followUp.taskDescription,
                started_at: followUp.at,
              });
            }
          }
        }
      }
    }
  }

  return [...records.values()].map((record) => ({
    subagent: {
      subagent_id: record.id,
      parent_session_id: sessionId,
      subagent_type: record.subagentType,
      display_name: record.displayName || record.subagentType,
      role: record.role,
      task_description: record.taskDescription,
      ...record.status,
      closed_at: record.status.status === 'closed' ? record.updatedAt : null,
      error: record.status.turn_outcome === 'failed' ? { code: 'SUBAGENT_ERROR', message: 'Subagent turn failed' } : null,
      created_at: record.createdAt,
      updated_at: record.updatedAt,
      revision: record.revision,
    },
    turns: [...record.turns.values()].sort((left, right) => left.started_at - right.started_at),
    ...(record.result ? { result: { subagent_id: record.id, ...record.result } } : {}),
  }));
}

/** 历史中随 chat.final / chat.tool_call 落盘的模型思考（reasoning_content），用于刷新后重建思考块。 */
export interface HistoryReasoningReplayItem {
  at: string;
  text: string;
  /** 末帧时刻（epoch ms）；异常结束时即使无收尾事件，耗时终点也能落在最后一个真实帧。 */
  updatedAt?: number;
}

export interface HistoryHarnessReplayItem {
  kind: 'harness_message' | 'harness_stage_result';
  at: string;
  payload: {
    content?: string;
    stage?: string;
    status?: string;
    error?: string;
    messages?: string[];
    metrics?: Record<string, unknown>;
  };
}

export interface HistoryTeamReplayItem {
  kind: 'team_member' | 'team_task';
  at: string;
  payload: {
    event: Record<string, unknown>;
  };
}

export interface HistorySubagentReplayItem {
  kind: 'updated' | 'message' | 'activity';
  at: string;
  payload: Record<string, unknown>;
}

type HistoryTimelineEntry =
  | { kind: 'message'; message: Message }
  | { kind: 'tool_call'; at: string; payload: Record<string, unknown> }
  | { kind: 'tool_result'; at: string; payload: Record<string, unknown> }
  | { kind: 'usage_summary'; at: string; usage: UsageSummary }
  | { kind: 'file_items'; at: string; files: FileDownloadItem[] }
  | { kind: 'team_member'; at: string; payload: { event: Record<string, unknown> } }
  | { kind: 'team_task'; at: string; payload: { event: Record<string, unknown> } }
  | { kind: 'harness_message'; at: string; content: string; stage?: string }
  | { kind: 'harness_stage_result'; at: string; stage: string; status: string; error: string; messages: string[]; metrics: Record<string, unknown> }
  | { kind: 'compaction'; at: string; summary: string }
  | { kind: 'reasoning'; at: string; text: string; updatedAt?: number }
  | { kind: 'subagent_update'; at: string; payload: Record<string, unknown> }
  | { kind: 'subagent_message'; at: string; payload: Record<string, unknown> }
  | { kind: 'subagent_activity'; at: string; payload: Record<string, unknown> };

/** 历史回放出的压缩汇总：boundary 记录计数，metadata 拼 tooltip 明细行 */
export interface HistoryCompactionReplay {
  count: number;
  summaries: string[];
}

interface BeginHistoryRestoreOptions {
  sessionId: string;
  subagentId?: string;
  onReady: (messages: Message[], totalPages: number | null) => void;
  /** 与消息同一时间线顺序，用于恢复 ToolGroupDisplay */
  onToolReplay?: (items: HistoryToolReplayItem[]) => void;
  /** 与消息同一时间线顺序，用于恢复 HarnessProgressBar */
  onHarnessReplay?: (items: HistoryHarnessReplayItem[]) => void;
  /** 与消息同一时间线顺序，用于恢复 Team 成员/任务状态 */
  onTeamReplay?: (items: HistoryTeamReplayItem[]) => void;
  /** 恢复独立 subagent history 中的 roster/status 与 transcript */
  onSubagentReplay?: (items: HistorySubagentReplayItem[]) => void;
  /** 与消息同一时间线顺序，用于恢复模型思考块（chat.reasoning） */
  onReasoningReplay?: (items: HistoryReasoningReplayItem[]) => void;
  /** 恢复上下文压缩汇总（context.compact_boundary），用于回显「本轮完成上下文压缩 N 次」 */
  onCompactionReplay?: (info: HistoryCompactionReplay) => void;
  /** 无消息且无工具回放时调用；`totalPages` 来自流中最后一帧（若有） */
  onEmpty?: (totalPages: number | null) => void;
  onError?: (message: string) => void;
}

export interface HistoryRestoreHandle {
  generation: number;
  dispose: () => void;
}

let restoreGeneration = 0;
const activeHistoryRequests = new Map<string, HistoryRestoreHandle>();

function makeHistoryRestoreKey(sessionId: string, subagentId?: string): string {
  return subagentId ? `${sessionId}:subagent:${subagentId}:restore` : `${sessionId}:restore`;
}

function makeHistoryPageKey(sessionId: string, pageIdx: number, subagentId?: string): string {
  return subagentId
    ? `${sessionId}:subagent:${subagentId}:page:${pageIdx}`
    : `${sessionId}:page:${pageIdx}`;
}

function replaceActiveHistoryRequest(key: string): void {
  activeHistoryRequests.get(key)?.dispose();
  activeHistoryRequests.delete(key);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

/** 主动推荐消息的记录：source 落在 payload.source（或顶层 source）。 */
function isProactiveRecommendationRecord(record: Record<string, unknown>): boolean {
  const direct = typeof record.source === 'string' ? record.source : '';
  if (direct === 'proactive_recommendation') return true;
  const payload = record.payload;
  if (isRecord(payload)) {
    const nested = typeof payload.source === 'string' ? payload.source : '';
    if (nested === 'proactive_recommendation') return true;
  }
  return false;
}

/**
 * 从历史记录中提取随本步落盘的模型思考文本（reasoning_content 可能在顶层或 payload 内）。
 *
 * 主动推荐消息（主 agent 跑指令式 query 生成话术那轮）也会落盘 reasoning_content，
 * 但它不是用户这一轮的思考流——segment 无 messageId 绑定、按时间戳并入上一轮 turn，
 * 重建后会污染上一条用户消息的思考状态（"已完成" → "已完成 N 次思考"），故跳过。
 */
function extractHistoryReasoningText(record: Record<string, unknown>): string {
  if (isProactiveRecommendationRecord(record)) return '';
  const direct = record.reasoning_content;
  if (typeof direct === 'string' && direct.trim()) {
    return direct.trim();
  }
  const payload = record.payload;
  if (isRecord(payload)) {
    const nested = payload.reasoning_content;
    if (typeof nested === 'string' && nested.trim()) {
      return nested.trim();
    }
  }
  return '';
}

/** 提取 reasoning 末帧时刻（reasoning_updated_at 可能在顶层或 payload 内），非有效时刻返回 undefined。 */
function extractHistoryReasoningUpdatedAt(record: Record<string, unknown>): number | undefined {
  const direct = record.reasoning_updated_at;
  const raw = typeof direct === 'number' || (typeof direct === 'string' && direct.trim())
    ? direct
    : (() => {
        const payload = record.payload;
        if (!isRecord(payload)) {
          return undefined;
        }
        const nested = payload.reasoning_updated_at;
        return typeof nested === 'number' || (typeof nested === 'string' && nested.trim())
          ? nested
          : undefined;
      })();
  if (raw === undefined) {
    return undefined;
  }
  const ms = parseTimestampToMs(raw);
  return Number.isFinite(ms) && ms > 0 ? ms : undefined;
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

function recordTimestampIso(record: Record<string, unknown>): string | undefined {
  const ms = parseTimestampToMs(record.timestamp);
  // 缺时间戳时不要用 Date.now()（会撑爆「已完成」耗时），也不要回 ''（Date.parse('')=NaN 会让排序失效）。
  return timestampMsToIso(ms);
}

/** 消息排序用：无效时间戳落到 0，避免 NaN - NaN 导致顺序不确定。 */
function safeTimestampMs(value: unknown): number {
  const ms = parseTimestampToMs(value);
  return Number.isFinite(ms) ? ms : 0;
}

function isTeamModeRecord(record: Record<string, unknown>): boolean {
  return typeof record.mode === 'string' && record.mode.trim().toLowerCase() === 'team';
}

function isTeamTeammateMessageRecord(record: Record<string, unknown>): boolean {
  return typeof record.role === 'string' && record.role.trim().toLowerCase() === 'teammate';
}

function isHiddenTeamTeammateMessageRecord(record: Record<string, unknown>): boolean {
  return isTeamModeRecord(record) && isTeamTeammateMessageRecord(record);
}

const _HISTORY_RECORD_META_KEYS = new Set([
  'id', 'role', 'request_id', 'channel_id', 'timestamp', 'event_type', 'event_payload', 'mode',
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

function hasMismatchedHistoryBoundary(value: unknown, sessionId: string): boolean {
  if (Array.isArray(value)) {
    return value.some((item) => hasMismatchedHistoryBoundary(item, sessionId));
  }
  if (!isRecord(value)) return false;
  if (['session_id', 'parent_session_id', 'sessionId', 'parentSessionId'].some((key) => {
    const boundary = pickFirstString(value, [key]);
    return Boolean(boundary && boundary !== sessionId);
  })) return true;
  return Object.values(value).some((nested) => hasMismatchedHistoryBoundary(nested, sessionId));
}

export function parseSubagentHistoryReplay(
  record: Record<string, unknown>,
  sessionId: string,
  subagentId: string,
): HistorySubagentReplayItem | null {
  if (hasMismatchedHistoryBoundary(record, sessionId)) return null;

  const eventType = typeof record.event_type === 'string' ? record.event_type.trim() : '';
  if (normalizeHistoryRole(record.role) !== 'assistant' && eventType !== 'chat.subtask_update') return null;
  const at = recordTimestampIso(record) ?? '';
  const payload = buildEventPayloadForRecord(record);
  if (eventType === 'chat.subtask_update' && typeof record.role === 'string' && payload.role == null) {
    payload.role = record.role;
  }
  if (hasMismatchedHistoryBoundary(payload, sessionId)) return null;

  if (eventType === 'chat.final') {
    const finalParentSessionId = pickFirstString(payload, ['parent_session_id', 'parentSessionId']);
    if (finalParentSessionId && finalParentSessionId !== sessionId) return null;
    const content = typeof payload.content === 'string' ? payload.content : '';
    return content.trim() ? { kind: 'message', at, payload } : null;
  }

  if (eventType === 'chat.subtask_update') {
    const rosterSubagentId = pickFirstString(payload, ['subagent_id', 'subagentId']);
    return rosterSubagentId === subagentId ? { kind: 'updated', at, payload } : null;
  }

  if (eventType !== 'chat.subagent_activity') return null;
  const activity = isRecord(payload.subagent_activity) ? payload.subagent_activity : payload;
  if (hasMismatchedHistoryBoundary(activity, sessionId)) return null;
  const activitySubagentId = pickFirstString(activity, ['subagent_id', 'subagentId']);
  if (!activitySubagentId || activitySubagentId !== subagentId) return null;
  return { kind: 'activity', at, payload: activity };
}

function extractTeamEventRecord(record: Record<string, unknown>): Record<string, unknown> | null {
  if (isRecord(record.event)) {
    return record.event;
  }
  if (isRecord(record.event_payload)) {
    if (isRecord(record.event_payload.event)) {
      return record.event_payload.event;
    }
    return record.event_payload;
  }

  const payload: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(record)) {
    if (!_HISTORY_RECORD_META_KEYS.has(key)) {
      payload[key] = value;
    }
  }
  if (isRecord(payload.event)) {
    return payload.event;
  }
  return Object.keys(payload).length > 0 ? payload : null;
}

function filenameFromPath(path: string): string {
  const parts = path.split(/[\\/]+/).filter(Boolean);
  return parts[parts.length - 1] || 'image';
}

function normalizeHistoryMediaItem(value: unknown): MediaItem | null {
  if (!isRecord(value)) {
    return null;
  }

  const path = pickFirstString(value, ['path', 'url']);
  if (!path) {
    return null;
  }

  const mimeType = pickFirstString(value, ['mime_type', 'mimeType']) ?? 'application/octet-stream';
  const filename = pickFirstString(value, ['filename', 'name']) ?? filenameFromPath(path);
  const size = typeof value.size_bytes === 'number'
    ? value.size_bytes
    : typeof value.sizeBytes === 'number'
      ? value.sizeBytes
      : undefined;

  const rawType = typeof value.type === 'string' ? value.type.trim().toLowerCase() : '';
  let type: MediaItem['type'];
  if (rawType === 'image' || rawType === 'audio' || rawType === 'video' || rawType === 'document') {
    type = rawType;
  } else if (mimeType.startsWith('image/')) {
    type = 'image';
  } else if (mimeType.startsWith('audio/')) {
    type = 'audio';
  } else if (mimeType.startsWith('video/')) {
    type = 'video';
  } else {
    type = 'document';
  }

  // Keep legacy image-only filtering for ambiguous image records without type.
  if (type === 'image' && !mimeType.startsWith('image/') && rawType !== 'image') {
    return null;
  }

  return {
    type,
    filename,
    path,
    mime_type: mimeType,
    mimeType,
    ...(typeof size === 'number' ? { size_bytes: size, sizeBytes: size } : {}),
  };
}

function appendHistoryMediaItems(
  target: MediaItem[],
  seenKeys: Set<string>,
  value: unknown
): void {
  if (!Array.isArray(value)) {
    return;
  }
  for (const item of value) {
    const normalized = normalizeHistoryMediaItem(item);
    if (!normalized) {
      continue;
    }
    const key = normalized.path || `${normalized.filename}:${normalized.mimeType}`;
    if (seenKeys.has(key)) {
      continue;
    }
    seenKeys.add(key);
    target.push(normalized);
  }
}

function extractHistoryMediaItems(record: Record<string, unknown>): MediaItem[] {
  const mediaItems: MediaItem[] = [];
  const seenKeys = new Set<string>();

  appendHistoryMediaItems(mediaItems, seenKeys, record.media_items);
  appendHistoryMediaItems(mediaItems, seenKeys, record.mediaItems);

  if (isRecord(record.files)) {
    appendHistoryMediaItems(mediaItems, seenKeys, record.files.uploaded_images);
    appendHistoryMediaItems(mediaItems, seenKeys, record.files.uploaded_documents);
  }
  if (isRecord(record.event_payload)) {
    appendHistoryMediaItems(mediaItems, seenKeys, record.event_payload.media_items);
    if (isRecord(record.event_payload.files)) {
      appendHistoryMediaItems(mediaItems, seenKeys, record.event_payload.files.uploaded_images);
      appendHistoryMediaItems(mediaItems, seenKeys, record.event_payload.files.uploaded_documents);
    }
  }

  return mediaItems;
}

function isTruthyHistoryFlag(value: unknown): boolean {
  return value === true || value === 'true' || value === 1 || value === '1';
}

function compactTokenCount(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}m`;
  if (abs >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}

/** 从 boundary 记录的 compact_metadata 拼一行 tooltip 明细（兼容手动 compact 的 stats 结构） */
function formatCompactBoundarySummary(record: Record<string, unknown>): string {
  const meta = isRecord(record.compact_metadata) ? record.compact_metadata : null;
  if (!meta) return '';
  const before = isRecord(meta.before) ? meta.before : null;
  const after = isRecord(meta.after) ? meta.after : null;
  const saved = isRecord(meta.saved) ? meta.saved : null;
  const beforeTokens =
    typeof before?.tokens === 'number'
      ? before.tokens
      : typeof meta.raw_total_tokens === 'number'
        ? meta.raw_total_tokens
        : null;
  const afterTokens =
    typeof after?.tokens === 'number'
      ? after.tokens
      : typeof meta.total_tokens === 'number'
        ? meta.total_tokens
        : null;
  const percent =
    typeof saved?.percent === 'number'
      ? saved.percent
      : typeof meta.rate === 'number'
        ? meta.rate
        : null;
  const processor =
    typeof meta.processor === 'string' && meta.processor.trim() ? meta.processor.trim() : '';
  const parts: string[] = [];
  if (beforeTokens != null && afterTokens != null) {
    parts.push(`~${compactTokenCount(beforeTokens)} -> ~${compactTokenCount(afterTokens)} tokens`);
  }
  if (percent != null) {
    parts.push(`saved ${percent.toFixed(1)}%`);
  }
  const line = parts.join(', ');
  if (processor && line) return `${processor}: ${line}`;
  return processor || line;
}

function parseHistoryTimelineEntry(
  record: Record<string, unknown>,
  sessionId: string,
  subagentId?: string,
): HistoryTimelineEntry | null {
  const role = normalizeHistoryRole(record.role);
  // 无有效时间戳时用空串占位（勿用 Date.now()）；排序/工具构建侧已对空串做防护。
  const at = recordTimestampIso(record) ?? '';

  if (role === 'user') {
    const rawContent = record.content ?? record.text ?? record.body;
    if (isA2UIClientEventContent(rawContent)) {
      return null;
    }
    const content = typeof rawContent === 'string' ? rawContent : String(rawContent ?? '');
    const mediaItems = extractHistoryMediaItems(record);
    if (!content.trim() && mediaItems.length === 0) {
      return null;
    }
    const id =
      pickFirstString(record, ['id', 'message_id', 'msg_id']) ?? `hist-user-${sessionId}-${at}`;
    const isGoalObjectiveMessage =
      isTruthyHistoryFlag(record.is_goal_objective_message) ||
      isTruthyHistoryFlag(record.isGoalObjectiveMessage);
    const rawSkills = record.skills;
    const skills = Array.isArray(rawSkills)
      ? rawSkills.filter((s): s is string => typeof s === 'string' && s.trim().length > 0)
      : undefined;
    // §9：Heartbeat 自动轮的 user/assistant 消息后端会带 metadata.automation 落盘，
    // 历史恢复时读回同一个标记，保证刷新/切会话/后端重启后仍能识别 Heartbeat 轮。
    // 与实时链路（useWebSocket.ts）共用同一个 extractAutomation。
    const userAutomation = extractAutomation(record) ?? extractAutomation(buildEventPayloadForRecord(record));
    return {
      kind: 'message',
      message: {
        id,
        role: 'user',
        content,
        timestamp: at,
        ...(mediaItems.length > 0 ? { mediaItems } : {}),
        ...(isGoalObjectiveMessage ? { isGoalObjectiveMessage: true } : {}),
        ...(skills && skills.length > 0 ? { skills } : {}),
        ...(userAutomation ? { automation: userAutomation } : {}),
      },
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

  if (subagentId) {
    const replay = parseSubagentHistoryReplay(record, sessionId, subagentId);
    if (!replay) return null;
    const kind = replay.kind === 'updated'
      ? 'subagent_update'
      : replay.kind === 'activity'
        ? 'subagent_activity'
        : 'subagent_message';
    return { kind, at: replay.at, payload: replay.payload };
  }

  if (!ALLOWED_ASSISTANT_EVENT_TYPES.has(eventType)) {
    return null;
  }

  if (eventType === 'team.message') {
    const event = extractTeamEventRecord(record);
    if (!event) {
      return null;
    }
    const teamPayload = { event };
    const id = pickFirstString(event, ['message_id']) ?? `hist-team-message-${sessionId}-${at}`;
    return {
      kind: 'message',
      message: {
        id,
        role: 'system',
        content: `team.event:${JSON.stringify(teamPayload)}`,
        timestamp: at,
      },
    };
  }

  if (eventType === 'team.member' || eventType === 'team.task') {
    const event = extractTeamEventRecord(record);
    if (!event) {
      return null;
    }
    return {
      kind: eventType === 'team.member' ? 'team_member' : 'team_task',
      at,
      payload: { event },
    };
  }

  const payload = buildEventPayloadForRecord(record);

  if (eventType === 'chat.subtask_update') {
    return { kind: 'subagent_update', at, payload };
  }

  if (eventType === 'chat.final') {
    // Goal 完成卡片落盘的是 `goal.completed:` + JSON 信封，不是展示文本。
    // normalizeFinalContent 会把字面 `\n` 还原成真换行（GFM 表格要靠这个），信封里的
    // JSON 字符串于是带上非法控制符，parseGoalCompletedContent 解析失败 →
    // GoalCompletedCard 返回 null → 整张卡片在历史里凭空消失。信封原样透传。
    const rawContent = typeof payload.content === 'string' ? payload.content : '';
    let content = isGoalCompletedContent(rawContent)
      ? rawContent
      : normalizeFinalContent(payload);
    const isGoalCompletedMessage =
      isTruthyHistoryFlag(record.is_goal_completed_message) ||
      isTruthyHistoryFlag(record.isGoalCompletedMessage) ||
      isTruthyHistoryFlag(payload.is_goal_completed_message) ||
      isTruthyHistoryFlag(payload.isGoalCompletedMessage);
    if (isGoalCompletedMessage && !isGoalCompletedContent(content)) {
      const evidenceRaw =
        (typeof record.evidence === 'string' && record.evidence) ||
        (typeof payload.evidence === 'string' && payload.evidence) ||
        content;
      content = buildGoalCompletedContent({
        evidence: typeof evidenceRaw === 'string' ? evidenceRaw.trim() : '',
      });
    }
    if (!content.trim()) {
      return null;
    }
    const id =
      pickFirstString(record, ['id', 'message_id', 'msg_id']) ?? `hist-final-${sessionId}-${at}`;
    if (isTeamModeRecord(record)) {
      if (isHiddenTeamTeammateMessageRecord(record)) {
        return null;
      }
      return {
        kind: 'message',
        message: {
          id: `team-leader-${id}`,
          role: 'system',
          content: `team.leader:${JSON.stringify({
            content,
            timestamp: safeTimestampMs(at),
          })}`,
          timestamp: at,
        },
      };
    }
    // 主动推荐消息：从历史记录还原 source/proactive_type，使刷新后仍按
    // ProactiveRecommendationCard 渲染（否则会退化为普通白色气泡）。
    const histSource = typeof payload.source === 'string' ? payload.source : '';
    const isProactiveRecommendation = histSource === 'proactive_recommendation';
    const histProactiveType = typeof payload.proactive_type === 'string' ? payload.proactive_type : '';
    // completed_at：收尾时刻（耗时）；timestamp 已是气泡出现/首包时刻（排序）
    const completedAt =
      (typeof record.completed_at === 'number' || typeof record.completed_at === 'string'
        ? recordTimestampIso({ timestamp: record.completed_at })
        : undefined) ||
      (typeof payload.completed_at === 'number' || typeof payload.completed_at === 'string'
        ? recordTimestampIso({ timestamp: payload.completed_at })
        : undefined);
    return {
      kind: 'message',
      message: {
        id,
        role: 'assistant',
        content,
        timestamp: at,
        ...(completedAt ? { completedAt } : {}),
        ...(isProactiveRecommendation ? { isProactiveRecommendation } : {}),
        ...(isProactiveRecommendation && histProactiveType
          ? { proactiveType: histProactiveType as 'skill_recommend' | 'task_reminder' | 'need_exploration' }
          : {}),
        // §9：Heartbeat 自动轮的 assistant 消息同样带 metadata.automation 落盘，恢复时读回。
        // 优先读 payload（event_payload 已提升），再回退到 record 顶层。
        ...((extractAutomation(payload) ?? extractAutomation(record))
          ? { automation: (extractAutomation(payload) ?? extractAutomation(record))! }
          : {}),
      },
    };
  }

  if (eventType === 'chat.tool_call') {
    // 与实时一致：team 成员工具不进主聊天时间线（侧栏 teamHistoryPanelRestore 另有回放）。
    if (isHiddenTeamTeammateMessageRecord(record)) {
      return null;
    }
    // 把顶层 member/role 带进 payload，供 normalize 识别（即便未隐藏也能对齐展示）。
    if (typeof record.member_name === 'string' && record.member_name.trim() && payload.member_name == null) {
      payload.member_name = record.member_name;
    }
    if (typeof record.role === 'string' && payload.role == null) {
      payload.role = record.role;
    }
    return { kind: 'tool_call', at, payload };
  }

  if (eventType === 'chat.tool_result') {
    if (isHiddenTeamTeammateMessageRecord(record)) {
      return null;
    }
    if (typeof record.member_name === 'string' && record.member_name.trim() && payload.member_name == null) {
      payload.member_name = record.member_name;
    }
    if (typeof record.role === 'string' && payload.role == null) {
      payload.role = record.role;
    }
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

  if (eventType === 'chat.file') {
    const rawFiles = payload.files;
    if (!Array.isArray(rawFiles) || rawFiles.length === 0) {
      return null;
    }
    const files = rawFiles as FileDownloadItem[];
    return {
      kind: 'file_items',
      at,
      files,
    };
  }

  if (eventType === 'harness.message') {
    const content = typeof payload.content === 'string' ? payload.content : '';
    const stage = typeof payload.stage === 'string' ? payload.stage : undefined;
    if (!content.trim()) {
      return null;
    }
    return { kind: 'harness_message', at, content, stage };
  }

  if (eventType === 'context.compact_boundary') {
    return { kind: 'compaction', at, summary: formatCompactBoundarySummary(record) };
  }

  if (eventType === 'harness.stage_result') {
    const stage = typeof payload.stage === 'string' ? payload.stage : '';
    const status = typeof payload.status === 'string' ? payload.status : 'success';
    const error = typeof payload.error === 'string' ? payload.error : '';
    const messages = Array.isArray(payload.messages) ? payload.messages.filter((m) => typeof m === 'string') : [];
    const metrics = isRecord(payload.metrics) ? payload.metrics as Record<string, unknown> : {};
    if (!stage.trim()) {
      return null;
    }
    return { kind: 'harness_stage_result', at, stage, status, error, messages, metrics };
  }

  return null;
}

function isAssistantLikeHistoryMessage(message: Message): boolean {
  return (
    message.role === 'assistant' ||
    (message.role === 'system' && Boolean(message.id?.startsWith('team-leader-')))
  );
}

/**
 * 与实时 `addFileItems` 对齐：chat.file 挂到「已经出现的」最近一条助手消息。
 * 旧逻辑挂到下一条 final，会导致刷新后文件从总结上方跑到总结气泡内部下方。
 */
function attachFilesToPreviousAssistant(messages: Message[], files: FileDownloadItem[]): boolean {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (!isAssistantLikeHistoryMessage(messages[i])) {
      continue;
    }
    messages[i] = {
      ...messages[i],
      fileItems: mergeFileDownloadItems(messages[i].fileItems, files),
    };
    return true;
  }
  return false;
}

function createFileOnlyAssistantMessage(at: string, files: FileDownloadItem[]): Message {
  const timestamp = at.trim() || new Date().toISOString();
  return {
    id: `hist-file-${timestamp}`,
    role: 'assistant',
    content: '',
    timestamp,
    fileItems: files,
  };
}

interface MaterializedHistoryTimeline {
  messages: Message[];
  toolReplay: HistoryToolReplayItem[];
  harnessReplay: HistoryHarnessReplayItem[];
  teamReplay: HistoryTeamReplayItem[];
  subagentReplay: HistorySubagentReplayItem[];
  reasoningReplay: HistoryReasoningReplayItem[];
}

function entryTimestamp(entry: HistoryTimelineEntry): string {
  return entry.kind === 'message' ? entry.message.timestamp : entry.at;
}

/**
 * Goal 完成卡片沉到本轮末尾。
 *
 * 实时侧这张卡是「本轮内容都落地后」才插进对话流的（见 useWebSocket 的
 * scheduleAfterTurnSettles），历史里它的落盘时刻却可能早于同轮后续的收尾正文。
 * 不重新盖章，历史就会把完成卡片排到最后一句回答上面，和实时反过来。
 */
function sinkGoalCompletionCardsToTurnEnd(
  entries: HistoryTimelineEntry[]
): HistoryTimelineEntry[] {
  const out = [...entries];
  let changed = false;

  for (let i = 0; i < out.length; i += 1) {
    const entry = out[i];
    if (entry.kind !== 'message' || !isGoalCompletedContent(entry.message.content)) {
      continue;
    }
    const cardMs = safeTimestampMs(entryTimestamp(entry));
    let turnEndMs = cardMs;
    for (let j = i + 1; j < out.length; j += 1) {
      const next = out[j];
      if (next.kind === 'message' && next.message.role === 'user') {
        break;
      }
      turnEndMs = Math.max(turnEndMs, safeTimestampMs(entryTimestamp(next)));
    }
    if (turnEndMs <= cardMs) {
      continue;
    }
    const iso = timestampMsToIso(turnEndMs + 1);
    if (!iso) {
      continue;
    }
    out[i] = { ...entry, message: { ...entry.message, timestamp: iso } };
    changed = true;
  }

  if (!changed) {
    return entries;
  }
  return out.sort(
    (a, b) => safeTimestampMs(entryTimestamp(a)) - safeTimestampMs(entryTimestamp(b))
  );
}

/** 将 history 条目折叠成消息/工具/思考，供 restore / page / 文件预览共用。入口统一升序。 */
function materializeHistoryTimeline(
  rawEntries: HistoryTimelineEntry[]
): MaterializedHistoryTimeline {
  // restore 用 unshift 倒序入列；sink / 折叠依赖时间升序，这里统一排一次。
  const sortedEntries = [...rawEntries].sort(
    (a, b) => safeTimestampMs(entryTimestamp(a)) - safeTimestampMs(entryTimestamp(b))
  );
  const entries = sinkGoalCompletionCardsToTurnEnd(sortedEntries);
  const messages: Message[] = [];
  const toolReplay: HistoryToolReplayItem[] = [];
  const harnessReplay: HistoryHarnessReplayItem[] = [];
  const teamReplay: HistoryTeamReplayItem[] = [];
  const subagentReplay: HistorySubagentReplayItem[] = [];
  const reasoningReplay: HistoryReasoningReplayItem[] = [];

  for (const e of entries) {
    if (e.kind === 'message') {
      messages.push(e.message);
      continue;
    }
    if (e.kind === 'usage_summary') {
      for (let i = messages.length - 1; i >= 0; i -= 1) {
        if (messages[i].role === 'assistant') {
          messages[i] = { ...messages[i], usageSummary: e.usage };
          break;
        }
      }
      continue;
    }
    if (e.kind === 'harness_message') {
      harnessReplay.push({
        kind: 'harness_message',
        at: e.at,
        payload: { content: e.content, stage: e.stage },
      });
      messages.push({
        id: `harness-msg-${e.at}`,
        role: 'system',
        content: e.content,
        timestamp: e.at,
        isHarnessMessage: true,
      });
      continue;
    }
    if (e.kind === 'harness_stage_result') {
      harnessReplay.push({
        kind: 'harness_stage_result',
        at: e.at,
        payload: {
          stage: e.stage,
          status: e.status,
          error: e.error,
          messages: e.messages,
          metrics: e.metrics,
        },
      });
      continue;
    }
    if (e.kind === 'file_items') {
      if (!attachFilesToPreviousAssistant(messages, e.files)) {
        messages.push(createFileOnlyAssistantMessage(e.at, e.files));
      }
      continue;
    }
    if (e.kind === 'team_member' || e.kind === 'team_task') {
      teamReplay.push({ kind: e.kind, at: e.at, payload: e.payload });
      continue;
    }
    if (e.kind === 'subagent_update' || e.kind === 'subagent_message' || e.kind === 'subagent_activity') {
      subagentReplay.push({
        kind: e.kind === 'subagent_update'
          ? 'updated'
          : e.kind === 'subagent_activity'
            ? 'activity'
            : 'message',
        at: e.at,
        payload: e.payload,
      });
      continue;
    }
    if (e.kind === 'reasoning') {
      reasoningReplay.push({ at: e.at, text: e.text, updatedAt: e.updatedAt });
      continue;
    }
    if (e.kind === 'compaction') {
      // 压缩边界不进 toolReplay，由 beginHistoryRestore 在循环结束后统一汇总回放
      continue;
    }
    toolReplay.push({ kind: e.kind, at: e.at, payload: e.payload });
  }

  return { messages, toolReplay, harnessReplay, teamReplay, subagentReplay, reasoningReplay };
}

/**
 * 将磁盘上的 history.json 解析结果（通常为记录数组）转为与历史恢复相同的筛选规则下的消息列表，
 * 并按时间升序返回全部可展示的用户/助手消息。
 */
export function parseHistoryJsonFileToPreviewMessages(
  parsed: unknown,
  sessionId: string
): Message[] {
  return parseHistoryJsonFileToTimelinePreview(parsed, sessionId).messages;
}

export interface HistoryTimelinePreview {
  messages: Message[];
  executions: ToolExecution[];
  reasoningSegments: { id: string; text: string; startedAt: number; closed: true; updatedAt?: number; closedAt?: number }[];
  mode: 'team' | null;
}

/**
 * 历史文件完整时间线预览：消息 + 工具执行 + 思考段，供 ChatTimelineList 与会话页同一套折叠逻辑使用。
 */
export function parseHistoryJsonFileToTimelinePreview(
  parsed: unknown,
  sessionId: string
): HistoryTimelinePreview {
  if (!Array.isArray(parsed)) {
    return { messages: [], executions: [], reasoningSegments: [], mode: null };
  }

  const entries: HistoryTimelineEntry[] = [];
  let isTeam = false;

  for (const item of parsed) {
    if (!isRecord(item)) {
      continue;
    }
    if (isTeamModeRecord(item)) {
      isTeam = true;
    }
    const entry = parseHistoryTimelineEntry(item, sessionId);
    if (entry) {
      entries.push(entry);
    }
    const reasoningText = extractHistoryReasoningText(item);
    if (reasoningText) {
      entries.push({
        kind: 'reasoning',
        at: recordTimestampIso(item) ?? '',
        text: reasoningText,
        updatedAt: extractHistoryReasoningUpdatedAt(item),
      });
    }
  }

  // 文件预览按记录原始顺序；同戳时 sourceIndex 由后续 timeline 排序兜底。
  entries.sort((a, b) => {
    const aAt = a.kind === 'message' ? a.message.timestamp : a.at;
    const bAt = b.kind === 'message' ? b.message.timestamp : b.at;
    return safeTimestampMs(aAt) - safeTimestampMs(bAt);
  });

  const { messages, toolReplay, reasoningReplay } = materializeHistoryTimeline(entries);
  const executions = buildToolExecutionsFromReplay(toolReplay);
  const reasoningSegments = buildReasoningSegmentsFromReplay(sessionId, reasoningReplay);

  return {
    messages,
    executions,
    reasoningSegments,
    mode: isTeam ? 'team' : null,
  };
}

function buildReasoningSegmentsFromReplay(
  sessionId: string,
  items: HistoryReasoningReplayItem[]
): HistoryTimelinePreview['reasoningSegments'] {
  const segments: HistoryTimelinePreview['reasoningSegments'] = [];
  const seen = new Set<string>();
  items.forEach((item, index) => {
    const text = item.text?.trim();
    if (!text || seen.has(text)) {
      return;
    }
    seen.add(text);
    const parsed = parseTimestampToMs(item.at);
    // 解析失败时跳过该段，勿用 index 当 epoch（会让 startMs≈0，耗时爆炸）
    if (!Number.isFinite(parsed)) {
      return;
    }
    const startedAt = parsed - 1;
    // updatedAt 取落盘的 reasoning_updated_at（末帧时刻）；缺失/非法时回退 startedAt
    // （老历史无该字段，保持原有保守终点）。
    const replayUpdatedAt = parseTimestampToMs(item.updatedAt);
    const updatedAt =
      Number.isFinite(replayUpdatedAt) && replayUpdatedAt > 1_000_000_000_000
        ? replayUpdatedAt
        : startedAt;
    segments.push({
      id: `hist-preview-rsn-${sessionId}-${index}`,
      text,
      startedAt,
      closed: true,
      closedAt: startedAt,
      updatedAt,
    });
  });
  segments.sort((a, b) => a.startedAt - b.startedAt);
  return segments;
}

function buildToolExecutionsFromReplay(toolReplay: HistoryToolReplayItem[]): ToolExecution[] {
  const byId = new Map<string, ToolExecution>();
  const order: string[] = [];

  for (const item of toolReplay) {
    if (item.kind === 'tool_call') {
      const n = normalizeToolCallPayload(item.payload);
      if (!n.id || byId.has(n.id)) {
        continue;
      }
      // 与 buildReasoningSegmentsFromReplay 对齐：无效 at 跳过，避免空串写入 ToolExecution
      const parsed = parseTimestampToMs(item.at);
      if (!Number.isFinite(parsed)) {
        continue;
      }
      const startedAt = timestampMsToIso(parsed);
      if (!startedAt) {
        continue;
      }
      byId.set(n.id, {
        toolCallId: n.id,
        toolCall: {
          id: n.id,
          name: n.name,
          arguments: n.arguments,
          description: n.description,
          formatted_args: n.formatted_args,
          display_name: n.display_name,
          memberName: n.memberName,
        },
        // 与实时一致：先 pending，等 tool_result 再落终态；无 result 的孤儿在循环末尾结算。
        status: 'pending',
        startedAt,
        updatedAt: startedAt,
        timeoutAt: startedAt,
      });
      order.push(n.id);
      continue;
    }

    const n = normalizeToolResultPayload(item.payload);
    if (!n.toolCallId) {
      continue;
    }
    const existing = byId.get(n.toolCallId);
    const result = {
      toolName: n.toolName,
      result: n.result,
      success: n.success,
      ...(n.pending ? { pending: true as const } : {}),
      toolCallId: n.toolCallId,
      summary: n.summary,
      skillTree: n.skillTree,
      ...(n.timedOut ? { timedOut: true as const } : {}),
      ...(n.beamSearch ? { beamSearch: n.beamSearch } : {}),
    };
    const resultStatus: ToolExecution['status'] = n.pending
      ? 'pending'
      : n.timedOut
        ? 'timeout'
        : n.success
          ? 'completed'
          : 'error';
    const parsed = parseTimestampToMs(item.at);
    const atIso = Number.isFinite(parsed) ? timestampMsToIso(parsed) : undefined;
    if (!existing) {
      if (!atIso) {
        continue;
      }
      byId.set(n.toolCallId, {
        toolCallId: n.toolCallId,
        toolCall: {
          id: n.toolCallId,
          name: n.toolName || 'tool',
          arguments: {},
        },
        result,
        status: resultStatus,
        startedAt: atIso,
        updatedAt: atIso,
        timeoutAt: atIso,
      });
      order.push(n.toolCallId);
      continue;
    }
    byId.set(n.toolCallId, {
      ...existing,
      result,
      status: resultStatus,
      ...(atIso ? { updatedAt: atIso } : {}),
    });
  }

  // 与 chatStore.settleHistoricalToolExecutions 对齐：仅结算仍无结果的孤儿 call
  for (const id of order) {
    const execution = byId.get(id);
    if (!execution || execution.status !== 'pending' || execution.result) {
      continue;
    }
    byId.set(id, {
      ...execution,
      status: 'completed',
      updatedAt: execution.startedAt,
      result: {
        toolName: execution.toolCall.name,
        result: '',
        success: true,
        toolCallId: id,
      },
    });
  }

  return order.map((id) => byId.get(id)).filter((item): item is ToolExecution => Boolean(item));
}

export function parseHistoryJsonFilePreviewMode(parsed: unknown): 'team' | null {
  if (!Array.isArray(parsed)) {
    return null;
  }

  return parsed.some((item) => isRecord(item) && isTeamModeRecord(item)) ? 'team' : null;
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
export function shouldProcessHistoryPayload(
  payload: Record<string, unknown>,
  expectedSessionId: string,
  expectedPageIdx?: number,
  allowLegacyNoSession = false,
  expectedSubagentId?: string,
): boolean {
  if (hasMismatchedHistoryBoundary(payload, expectedSessionId)) return false;
  const sid = pickFirstString(payload, ['session_id', 'sessionId']) ?? '';
  if (sid && sid !== expectedSessionId) {
    return false;
  }
  const subagentId = pickFirstString(payload, ['subagent_id', 'subagentId']) ?? '';
  if (expectedSubagentId) {
    if (subagentId !== expectedSubagentId) {
      return false;
    }
  } else if (subagentId) {
    return false;
  }
  if (expectedPageIdx !== undefined && payload.page_idx !== expectedPageIdx) {
    return false;
  }
  if (!sid) {
    return allowLegacyNoSession && (isHistoryRestoreDonePayload(payload) || isHistoryBatchEnd(payload));
  }
  return true;
}

export function beginHistoryRestore(options: BeginHistoryRestoreOptions): HistoryRestoreHandle {
  const requestKey = makeHistoryRestoreKey(options.sessionId, options.subagentId);
  const releaseLiveEvents = webClient.suspendSessionEvents(options.sessionId);
  replaceActiveHistoryRequest(requestKey);

  const generation = restoreGeneration + 1;
  restoreGeneration = generation;

  const entries: HistoryTimelineEntry[] = [];
  let totalPages: number | null = null;
  let disposed = false;
  let finalized = false;
  let restoreTimer: ReturnType<typeof setTimeout> | null = null;
  const reassembler = new HistoryRecordReassembler();

  const unsubscribe = webClient.on(HISTORY_MESSAGE_EVENT, (event: WsEvent) => {
    if (disposed) {
      return;
    }

    const payload = event.payload;
    if (!shouldProcessHistoryPayload(
      payload,
      options.sessionId,
      undefined,
      activeHistoryRequests.size === 1,
      options.subagentId,
    )) {
      return;
    }

    if (typeof payload.total_pages === 'number' && Number.isFinite(payload.total_pages)) {
      totalPages = payload.total_pages;
    }

    if (isHistoryRestoreDonePayload(payload)) {
      finalize();
      return;
    }

    const raw = extractHistoryMessagePayload(payload);
    const record = normalizeHistoryContent(raw, options.onError);
    if (record) {
      const full = reassembler.feed(record);
      if (!full) {
        return;
      }
      const entry = parseHistoryTimelineEntry(full, options.sessionId, options.subagentId);
      if (entry) {
        entries.unshift(entry);
      }
      const reasoningText = extractHistoryReasoningText(full);
      if (reasoningText) {
        entries.unshift({
          kind: 'reasoning',
          at: recordTimestampIso(full) ?? '',
          text: reasoningText,
          updatedAt: extractHistoryReasoningUpdatedAt(full),
        });
      }
    }

    if (isHistoryBatchEnd(payload)) {
      finalize();
    }
  });

  function stopListening(): void {
    if (disposed) return;
    disposed = true;
    if (restoreTimer) {
      clearTimeout(restoreTimer);
      restoreTimer = null;
    }
    unsubscribe();
    if (activeHistoryRequests.get(requestKey)?.generation === generation) {
      activeHistoryRequests.delete(requestKey);
    }
  }

  function dispose(): void {
    stopListening();
    releaseLiveEvents();
  }

  function finalize(): void {
    if (disposed || finalized) return;
    finalized = true;
    reassembler.flush();

    const { messages, toolReplay, harnessReplay, teamReplay, subagentReplay, reasoningReplay } =
      materializeHistoryTimeline(entries);

    stopListening();

    try {
      if (messages.length === 0 && toolReplay.length === 0 && harnessReplay.length === 0 && teamReplay.length === 0 && subagentReplay.length === 0) {
        options.onEmpty?.(totalPages);
        return;
      }
      options.onReady(messages, totalPages);
      if (toolReplay.length > 0) {
        options.onToolReplay?.(toolReplay);
      }
      if (harnessReplay.length > 0) {
        options.onHarnessReplay?.(harnessReplay);
      }
      if (teamReplay.length > 0) {
        options.onTeamReplay?.(teamReplay);
      }
      if (subagentReplay.length > 0) {
      options.onSubagentReplay?.(subagentReplay);
    }
    if (reasoningReplay.length > 0) {
        options.onReasoningReplay?.(reasoningReplay);
      }
      const compactionCount = entries.reduce((n, e) => (e.kind === 'compaction' ? n + 1 : n), 0);
      if (compactionCount > 0) {
        const compactionSummaries = entries.flatMap((e) =>
          e.kind === 'compaction' && e.summary.trim() ? [e.summary] : []
        );
        options.onCompactionReplay?.({ count: compactionCount, summaries: compactionSummaries });
      }
    } finally {
      releaseLiveEvents();
    }
  }

  const handle: HistoryRestoreHandle = { generation, dispose };
  activeHistoryRequests.set(requestKey, handle);
  // 兜底：后端 history.get 流超时（faas 旧 session runtime 过 TTL 被
  // 回收、init 60s 超时）时不发结束帧，强制 finalize 恢复 isLoadingHistory，
  // 避免前端永久转圈、吞掉后续 chat.processing_status(is_processing=false)。
  restoreTimer = setTimeout(() => {
    if (disposed || finalized) return;
    finalize();
  }, HISTORY_RESTORE_TIMEOUT_MS);
  return handle;
}

export interface FetchHistoryPageResult {
  messages: Message[];
  toolReplay: HistoryToolReplayItem[];
  harnessReplay: HistoryHarnessReplayItem[];
  teamReplay: HistoryTeamReplayItem[];
  subagentReplay: HistorySubagentReplayItem[];
  reasoningReplay: HistoryReasoningReplayItem[];
  totalPages: number | null;
}

export interface FetchHistoryPageOptions {
  sessionId: string;
  pageIdx: number;
  subagentId?: string;
  onReady: (result: FetchHistoryPageResult) => void;
  onEmpty?: (totalPages: number | null) => void;
  onTimeout?: () => void;
  onError?: (message: string) => void;
}

/**
 * 拉取单页历史（用于「加载更早」）。
 * 调用方需在订阅建立后再发 `history.get`（含对应 `page_idx`）。
 */
export function fetchHistoryPage(options: FetchHistoryPageOptions): HistoryRestoreHandle {
  const requestKey = makeHistoryPageKey(options.sessionId, options.pageIdx, options.subagentId);
  replaceActiveHistoryRequest(requestKey);

  const generation = restoreGeneration + 1;
  restoreGeneration = generation;

  const entries: HistoryTimelineEntry[] = [];
  let totalPages: number | null = null;
  let disposed = false;
  let finalized = false;
  let timedOut = false;
  let restoreTimer: ReturnType<typeof setTimeout> | null = null;
  const reassembler = new HistoryRecordReassembler();

  const unsubscribe = webClient.on(HISTORY_MESSAGE_EVENT, (event: WsEvent) => {
    if (disposed) {
      return;
    }

    const payload = event.payload;
    if (!shouldProcessHistoryPayload(
      payload,
      options.sessionId,
      options.pageIdx,
      activeHistoryRequests.size === 1,
      options.subagentId,
    )) {
      return;
    }

    if (typeof payload.total_pages === 'number' && Number.isFinite(payload.total_pages)) {
      totalPages = payload.total_pages;
    }

    if (isHistoryRestoreDonePayload(payload)) {
      finalize();
      return;
    }

    const raw = extractHistoryMessagePayload(payload);
    const record = normalizeHistoryContent(raw, options.onError);
    if (record) {
      const full = reassembler.feed(record);
      if (!full) {
        return;
      }
      const entry = parseHistoryTimelineEntry(full, options.sessionId, options.subagentId);
      if (entry) {
        entries.unshift(entry);
      }
      const reasoningText = extractHistoryReasoningText(full);
      if (reasoningText) {
        entries.unshift({
          kind: 'reasoning',
          at: recordTimestampIso(full) ?? '',
          text: reasoningText,
          updatedAt: extractHistoryReasoningUpdatedAt(full),
        });
      }
    }

    if (isHistoryBatchEnd(payload)) {
      finalize();
    }
  });

  function dispose(): void {
    if (disposed) return;
    disposed = true;
    if (restoreTimer) {
      clearTimeout(restoreTimer);
      restoreTimer = null;
    }
    unsubscribe();
    if (activeHistoryRequests.get(requestKey)?.generation === generation) {
      activeHistoryRequests.delete(requestKey);
    }
  }

  function finalize(): void {
    if (disposed || finalized) return;
    finalized = true;
    reassembler.flush();

    if (timedOut) {
      dispose();
      options.onTimeout?.();
      return;
    }

    const { messages, toolReplay, harnessReplay, teamReplay, subagentReplay, reasoningReplay } =
      materializeHistoryTimeline(entries);

    dispose();

    if (messages.length === 0 && toolReplay.length === 0 && harnessReplay.length === 0 && teamReplay.length === 0 && subagentReplay.length === 0) {
      options.onEmpty?.(totalPages);
      return;
    }
    options.onReady({ messages, toolReplay, harnessReplay, teamReplay, subagentReplay, reasoningReplay, totalPages });
  }

  const handle: HistoryRestoreHandle = { generation, dispose };
  activeHistoryRequests.set(requestKey, handle);
  // 同 beginHistoryRestore：兜底超时，避免分页 history.get 流卡死。
  restoreTimer = setTimeout(() => {
    if (disposed || finalized) return;
    timedOut = true;
    finalize();
  }, HISTORY_RESTORE_TIMEOUT_MS);
  return handle;
}
