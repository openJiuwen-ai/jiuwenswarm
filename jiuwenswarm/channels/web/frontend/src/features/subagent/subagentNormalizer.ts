import type {
  Subagent,
  SubagentActivity,
  SubagentActivityEvent,
  SubagentActivityKind,
  SubagentClosedReason,
  SubagentError,
  SubagentLifecycle,
  SubagentResult,
  SubagentStatus,
  SubagentTurnOutcome,
  SubagentUpdatedEvent,
} from '../../types/subagent';

type RecordValue = Record<string, unknown>;

const ACTIVITY_KINDS = new Set<SubagentActivityKind>([
  'tool_call',
  'tool_result',
  'thinking',
  'error',
  'truncated',
]);

function asRecord(value: unknown): RecordValue | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : null;
}

function asString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function asNonNegativeInteger(value: unknown): number | null {
  const number = asNumber(value);
  return number !== null && Number.isSafeInteger(number) && number >= 0 ? number : null;
}

function normalizeLifecycle(value: unknown): SubagentLifecycle | null {
  return value === 'live' || value === 'closed' ? value : null;
}

function normalizeTurnOutcome(value: unknown): SubagentTurnOutcome | null {
  if (value === 'completed' || value === 'failed' || value === 'parent_ended') return value;
  if (value === 'cancelled' || value === 'canceled') return 'cancelled';
  return null;
}

function normalizeStatus(
  value: string,
  lifecycle: SubagentLifecycle | null,
  canSendInput: boolean | null,
  needsResume: boolean | null,
): SubagentStatus | null {
  if (lifecycle === 'closed' || needsResume === true) return 'closed';
  if (value === 'running' || value === 'starting' || value === 'pending') return 'running';
  if (value === 'idle') return 'idle';
  if (value === 'closed') return 'closed';
  if (
    lifecycle === 'live'
    && canSendInput === true
    && ['completed', 'failed', 'error', 'cancelled', 'canceled'].includes(value)
  ) return 'idle';
  if (value === 'completed' || value === 'failed' || value === 'error' || value === 'cancelled' || value === 'canceled') return 'closed';
  return null;
}

function normalizeClosedReason(value: unknown, status: SubagentStatus, rawStatus: string, turnOutcome: SubagentTurnOutcome | null): SubagentClosedReason | null {
  if (status !== 'closed') return null;
  if (value === 'canceled') return 'cancelled';
  if (value === 'completed' || value === 'failed' || value === 'cancelled' || value === 'parent_ended' || value === 'manual' || value === 'evicted') {
    return value;
  }
  if (rawStatus === 'error' || rawStatus === 'failed') return 'failed';
  if (rawStatus === 'canceled' || rawStatus === 'cancelled') return 'cancelled';
  if (turnOutcome === 'failed') return 'failed';
  if (turnOutcome === 'cancelled') return 'cancelled';
  if (turnOutcome === 'parent_ended') return 'parent_ended';
  return 'completed';
}

function normalizeError(value: unknown, rawStatus: string, rawMessage: unknown, turnOutcome: SubagentTurnOutcome | null): SubagentError | null {
  const raw = asRecord(value);
  const message = asString(raw?.message) ?? (rawStatus === 'error' || rawStatus === 'failed' || turnOutcome === 'failed' ? asString(rawMessage) : null);
  if (!message) return null;
  return {
    code: asString(raw?.code) ?? 'SUBAGENT_ERROR',
    message,
  };
}

export function normalizeSubagent(value: unknown, sessionId?: string): Subagent | null {
  const raw = asRecord(value);
  if (!raw) return null;

  const subagentId = asString(raw.subagent_id ?? raw.subagentId);
  const parentSessionId = asString(raw.parent_session_id ?? raw.parentSessionId) ?? sessionId ?? null;
  if (sessionId && parentSessionId && parentSessionId !== sessionId) return null;
  const description = asString(raw.description);
  const displayName = asString(raw.display_name ?? raw.displayName) ?? description ?? subagentId;
  const rawStatus = (asString(raw.status) ?? '').toLowerCase();
  const turnOutcome = normalizeTurnOutcome(raw.turn_outcome ?? raw.turnOutcome);
  const lifecycle = normalizeLifecycle(raw.lifecycle);
  const canSendInput = typeof raw.can_send_input === 'boolean' ? raw.can_send_input : null;
  const needsResume = typeof raw.needs_resume === 'boolean' ? raw.needs_resume : null;
  const status = normalizeStatus(rawStatus, lifecycle, canSendInput, needsResume);
  const createdAt = asNumber(raw.created_at ?? raw.createdAt) ?? asNumber(raw.updated_at ?? raw.updatedAt) ?? 0;
  const updatedAt = asNumber(raw.updated_at ?? raw.updatedAt) ?? createdAt;
  const revision = asNonNegativeInteger(raw.revision) ?? 0;
  if (!subagentId || !parentSessionId || !displayName || !status) return null;

  return {
    subagent_id: subagentId,
    parent_session_id: parentSessionId,
    subagent_type: asString(raw.subagent_type ?? raw.subagentType) ?? '',
    display_name: displayName,
    role: asString(raw.role) ?? '',
    task_description: asString(raw.task_description ?? raw.taskDescription) ?? '',
    status,
    turn_outcome: turnOutcome,
    lifecycle,
    can_send_input: canSendInput,
    needs_resume: needsResume,
    closed_at: status === 'closed' ? asNumber(raw.closed_at ?? raw.closedAt) : null,
    closed_reason: normalizeClosedReason(raw.closed_reason ?? raw.closedReason, status, rawStatus, turnOutcome),
    error: normalizeError(raw.error, rawStatus, raw.message, turnOutcome),
    created_at: createdAt,
    updated_at: updatedAt,
    revision,
  };
}

export function normalizeSubagentActivity(value: unknown, sessionId?: string): SubagentActivity | null {
  const raw = asRecord(value);
  if (!raw) return null;

  const subagentId = asString(raw.subagent_id ?? raw.subagentId);
  const parentSessionId = asString(raw.parent_session_id ?? raw.parentSessionId) ?? sessionId ?? null;
  if (sessionId && parentSessionId && parentSessionId !== sessionId) return null;
  const taskId = asString(raw.task_id ?? raw.taskId) ?? subagentId;
  const sequence = asNonNegativeInteger(raw.seq ?? raw.sequence);
  const kind = ACTIVITY_KINDS.has(raw.kind as SubagentActivityKind) ? raw.kind as SubagentActivityKind : null;
  const atMs = asNumber(raw.at_ms ?? raw.atMs);
  if (!subagentId || !taskId || sequence === null || !kind || atMs === null) return null;

  const activityId = asString(raw.activity_id ?? raw.activityId) ?? `${subagentId}:${taskId}:${sequence}`;
  const normalized: SubagentActivity = {
    activity_id: activityId,
    subagent_id: subagentId,
    ...(parentSessionId ? { parent_session_id: parentSessionId } : {}),
    task_id: taskId,
    sequence,
    kind,
    summary: typeof raw.summary === 'string' ? raw.summary : '',
    at_ms: atMs,
  };
  const toolName = asString(raw.tool_name ?? raw.toolName);
  const toolCallId = asString(raw.tool_call_id ?? raw.toolCallId);
  const phaseId = asNonNegativeInteger(raw.phase_id ?? raw.phaseId);
  if (phaseId !== null && phaseId > 0) normalized.phase_id = phaseId;
  if (toolName) normalized.tool_name = toolName;
  if (toolCallId) normalized.tool_call_id = toolCallId;
  if (typeof raw.ok === 'boolean') normalized.ok = raw.ok;
  const dropped = asNonNegativeInteger(raw.dropped);
  if (dropped !== null) normalized.dropped = dropped;
  return normalized;
}

export function normalizeSubagentStatusEvent(value: unknown): SubagentUpdatedEvent | null {
  const raw = asRecord(value);
  if (!raw || (raw.event_type !== undefined && raw.event_type !== 'chat.subtask_update')) return null;
  const sessionId = asString(raw.session_id ?? raw.sessionId ?? raw.parent_session_id ?? raw.parentSessionId);
  if (!sessionId) return null;
  const subagent = normalizeSubagent(raw, sessionId);
  if (!subagent) return null;
  return {
    event_type: 'chat.subtask_update',
    session_id: sessionId,
    subagent,
  };
}

export function normalizeSubagentActivityEvent(value: unknown): SubagentActivityEvent | null {
  const raw = asRecord(value);
  if (!raw || (raw.event_type !== undefined && raw.event_type !== 'chat.subagent_activity')) return null;
  const sessionId = asString(raw.session_id ?? raw.sessionId ?? raw.parent_session_id ?? raw.parentSessionId);
  const activity = normalizeSubagentActivity(raw, sessionId ?? undefined);
  if (!sessionId || !activity) return null;
  return {
    event_type: 'chat.subagent_activity',
    session_id: sessionId,
    activity,
  };
}

export interface SubagentToolStatusUpdate {
  subagent_id: string;
  status: SubagentStatus;
  task_id?: string;
}

const STATUS_TOOL_NAMES = new Set(['subagent_close', 'subagent_list', 'subagent_resume', 'subagent_send_input', 'subagent_spawn']);

function normalizeToolStatus(value: unknown): SubagentStatus | null {
  const status = asString(value)?.toLowerCase();
  if (!status) return null;
  if (status === 'running' || status === 'starting' || status === 'pending' || status === 'pending_init') return 'running';
  if (status === 'idle' || status === 'completed' || status === 'failed' || status === 'error' || status === 'cancelled' || status === 'canceled' || status === 'interrupted') return 'idle';
  if (status === 'closed') return 'closed';
  return null;
}

// Parses the compatibility `result` string (str() of the structured tool
// result) for subagent ids and statuses; removed once the UI reads structured
// data instead.
function extractToolResultText(value: RecordValue): string {
  const nested = asRecord(value.tool_result) ?? value;
  const rawOutput = asRecord(nested.raw_output ?? nested.rawOutput);
  const candidates = [
    nested.result,
    rawOutput?.result,
    nested.data,
    rawOutput?.data,
  ];
  return candidates.find((candidate): candidate is string => typeof candidate === 'string') ?? '';
}

function toolResultSucceeded(value: RecordValue, resultText: string): boolean {
  const nested = asRecord(value.tool_result) ?? value;
  if (typeof value.success === 'boolean') return value.success;
  if (typeof nested.success === 'boolean') return nested.success;
  const rawSuccess = /\bsuccess\s*=\s*(True|False)\b/i.exec(resultText)?.[1];
  if (rawSuccess) return rawSuccess.toLowerCase() === 'true';
  const jsonSuccess = /["']success["']\s*:\s*(true|false)\b/i.exec(resultText)?.[1];
  return jsonSuccess?.toLowerCase() === 'true';
}

function collectStructuredToolStatuses(value: unknown, updates: Map<string, SubagentToolStatusUpdate>): void {
  if (Array.isArray(value)) {
    value.forEach(item => collectStructuredToolStatuses(item, updates));
    return;
  }
  const record = asRecord(value);
  if (!record) return;

  const subagentId = asString(record.subagent_id ?? record.subagentId);
  const status = normalizeToolStatus(record.status);
  if (subagentId && status) {
    const taskId = asString(record.task_id ?? record.taskId);
    updates.set(subagentId, { subagent_id: subagentId, status, ...(taskId ? { task_id: taskId } : {}) });
  }
  Object.values(record).forEach(item => collectStructuredToolStatuses(item, updates));
}

function collectTextToolStatuses(resultText: string, updates: Map<string, SubagentToolStatusUpdate>): void {
  const idPattern = /["'](?:subagent_id|subagentId)["']\s*:\s*(["'])([^"']+)\1/g;
  const matches = [...resultText.matchAll(idPattern)];
  matches.forEach((match, index) => {
    const start = match.index ?? 0;
    const end = matches[index + 1]?.index ?? resultText.length;
    const statusMatch = /["']status["']\s*:\s*(["'])([^"']+)\1/.exec(resultText.slice(start, end));
    const taskIdMatch = /["']task_id["']\s*:\s*(["'])([^"']+)\1/.exec(resultText.slice(start, end));
    const subagentId = match[2]?.trim();
    const status = normalizeToolStatus(statusMatch?.[2]);
    if (subagentId && status) {
      const taskId = taskIdMatch?.[2]?.trim();
      updates.set(subagentId, { subagent_id: subagentId, status, ...(taskId ? { task_id: taskId } : {}) });
    }
  });
}

/** Read explicit successful subagent control/list tool results for live roster convergence. */
export function normalizeSubagentToolStatusUpdates(value: unknown): SubagentToolStatusUpdate[] {
  const raw = asRecord(value);
  if (!raw) return [];
  const nested = asRecord(raw.tool_result) ?? raw;
  const toolName = asString(nested.tool_name ?? nested.toolName ?? nested.name ?? raw.tool_name ?? raw.toolName ?? raw.name)?.toLowerCase();
  if (!toolName || !STATUS_TOOL_NAMES.has(toolName)) return [];

  const resultText = extractToolResultText(raw);
  if (!toolResultSucceeded(raw, resultText)) return [];

  if (toolName === 'subagent_close') {
    const subagentId = asString(nested.subagent_id ?? nested.subagentId ?? raw.subagent_id ?? raw.subagentId)
      ?? /["']?subagent_id["']?\s*:\s*["']([^"']+)["']/.exec(resultText)?.[1]?.trim();
    return subagentId ? [{ subagent_id: subagentId, status: 'closed' }] : [];
  }

  const updates = new Map<string, SubagentToolStatusUpdate>();
  collectStructuredToolStatuses(nested, updates);
  collectTextToolStatuses(resultText, updates);
  return [...updates.values()];
}

export function normalizeSubagentWaitResults(value: unknown): SubagentResult[] {
  const raw = asRecord(value);
  const toolResult = asRecord(raw?.tool_result) ?? raw;
  const waitResult = asRecord(toolResult?.subagent_wait ?? toolResult?.subagentWait);
  const results = asRecord(waitResult?.results);
  if (!results) return [];
  const outputFiles = asRecord(waitResult?.output_files ?? waitResult?.outputFiles);

  return Object.entries(results).flatMap(([subagentId, content]) => {
    if (!subagentId || typeof content !== 'string' || !content.trim()) return [];
    const outputFile = typeof outputFiles?.[subagentId] === 'string' && outputFiles[subagentId].trim()
      ? outputFiles[subagentId].trim()
      : undefined;
    return [{
      subagent_id: subagentId,
      content,
      ...(outputFile ? { output_file: outputFile } : {}),
    }];
  });
}
