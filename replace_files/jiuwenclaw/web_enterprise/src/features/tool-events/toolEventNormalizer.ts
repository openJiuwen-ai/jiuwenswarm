type UnknownPayload = Record<string, unknown>;

function asRecord(value: unknown): UnknownPayload | null {
  if (!value || typeof value !== 'object') {
    return null;
  }
  return value as UnknownPayload;
}

function parseArguments(raw: unknown): Record<string, unknown> {
  if (raw && typeof raw === 'object') {
    return raw as Record<string, unknown>;
  }
  if (typeof raw === 'string') {
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') {
        return parsed as Record<string, unknown>;
      }
    } catch {
      // ignore: 非 JSON 字符串时保持空对象
    }
  }
  return {};
}

function resolveToolCallId(payload: UnknownPayload, fallback?: UnknownPayload): string | undefined {
  const candidates = [
    payload.id,
    payload.tool_call_id,
    payload.toolCallId,
    fallback?.tool_call_id,
    fallback?.toolCallId,
  ];
  for (const item of candidates) {
    if (typeof item === 'string' && item) {
      return item;
    }
  }
  return undefined;
}

export interface NormalizedToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  description?: string;
  formatted_args?: string;
  memberId?: string;
  memberName?: string;
}

function resolveMemberInfo(
  payload: UnknownPayload,
  inner: UnknownPayload,
): { memberId?: string; memberName?: string } {
  const pick = (v: unknown) =>
    typeof v === 'string' && v.trim() ? v.trim() : undefined;
  return {
    memberId: pick(payload.member_id) ?? pick(inner.member_id),
    memberName: pick(payload.member_name) ?? pick(inner.member_name),
  };
}

export interface NormalizedToolResult {
  toolName: string;
  toolCallId?: string;
  result: string;
  success: boolean;
  summary?: string;
}

export function normalizeToolCallPayload(payload: UnknownPayload): NormalizedToolCall {
  const toolCallPayload = asRecord(payload.tool_call) ?? payload;
  const id = resolveToolCallId(toolCallPayload, payload) || `tool-${Date.now()}`;
  const name =
    (typeof toolCallPayload.name === 'string' && toolCallPayload.name) ||
    (typeof payload.tool_name === 'string' && payload.tool_name) ||
    'unknown';
  const description =
    typeof toolCallPayload.description === 'string'
      ? toolCallPayload.description
      : undefined;
  const formatted_args =
    typeof toolCallPayload.formatted_args === 'string'
      ? toolCallPayload.formatted_args
      : undefined;

  const { memberId, memberName } = resolveMemberInfo(payload, toolCallPayload);

  return {
    id,
    name,
    arguments: parseArguments(toolCallPayload.arguments),
    description,
    formatted_args,
    memberId,
    memberName,
  };
}

interface DeepResearchAsyncResultBlob {
  task_id: string;
  status: string;
  result?: unknown;
  query?: string;
}

function isDeepResearchAsyncResultBlob(value: unknown): value is DeepResearchAsyncResultBlob {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return false;
  }
  const o = value as UnknownPayload;
  return typeof o.task_id === 'string' && !!o.task_id && typeof o.status === 'string';
}

export function normalizeToolResultPayload(payload: UnknownPayload): NormalizedToolResult {
  const toolResultPayload = asRecord(payload.tool_result) ?? payload;
  let rawResultUnknown: unknown = toolResultPayload.result;

  if (isDeepResearchAsyncResultBlob(rawResultUnknown)) {
    const inner = rawResultUnknown.result;
    if (typeof inner === 'string' && inner) {
      rawResultUnknown = inner;
    } else if (inner != null && typeof inner !== 'string') {
      rawResultUnknown = JSON.stringify(inner);
    } else {
      rawResultUnknown =
        typeof rawResultUnknown.query === 'string' ? rawResultUnknown.query : '';
    }
  }

  const result =
    (typeof rawResultUnknown === 'string' && rawResultUnknown) ||
    (toolResultPayload.data != null ? String(toolResultPayload.data) : '') ||
    (typeof toolResultPayload.error === 'string'
      ? toolResultPayload.error
      : '');
  const status =
    typeof toolResultPayload.status === 'string'
      ? toolResultPayload.status
      : '';
  const maybeDrBlob = toolResultPayload.result;
  const nestedStatus = isDeepResearchAsyncResultBlob(maybeDrBlob) ? maybeDrBlob.status : '';
  const success =
    typeof toolResultPayload.success === 'boolean'
      ? toolResultPayload.success
      : (nestedStatus || status)
        ? (nestedStatus || status) !== 'error'
        : true;
  const toolName =
    (typeof toolResultPayload.tool_name === 'string' &&
      toolResultPayload.tool_name) ||
    (typeof toolResultPayload.name === 'string' &&
      toolResultPayload.name) ||
    'unknown';
  const toolCallId = resolveToolCallId(toolResultPayload, payload);
  const summary =
    typeof toolResultPayload.summary === 'string'
      ? toolResultPayload.summary
      : success ? undefined : '❌';

  return {
    toolName,
    toolCallId,
    result,
    success,
    summary,
  };
}

/**
 * DeepResearch 网关异步推送（tool_name=deepresearch + result 为 task 对象）：作为独立气泡展示，不走工具结果合并。
 */
export function tryDeepResearchStandaloneAssistantTurn(
  payload: UnknownPayload,
): { messageId: string; content: string } | null {
  const toolResultPayload = asRecord(payload.tool_result) ?? payload;
  if (
    typeof toolResultPayload.tool_name !== 'string' ||
    toolResultPayload.tool_name !== 'deepresearch'
  ) {
    return null;
  }
  const raw = toolResultPayload.result;
  if (!isDeepResearchAsyncResultBlob(raw)) {
    return null;
  }
  const n = normalizeToolResultPayload(payload);
  const title =
    raw.status === 'completed'
      ? '深度研究已完成'
      : raw.status === 'cancelled'
        ? '深度研究已取消'
        : raw.status === 'error'
          ? '深度研究失败'
          : `深度研究（${raw.status}）`;
  const parts: string[] = [`### ${title}`, '', `**任务 ID：** \`${raw.task_id}\``];
  if (typeof raw.query === 'string' && raw.query.trim()) {
    parts.push(`**主题：** ${raw.query.trim()}`);
  }
  const body = (n.result || '').trim() || (n.success ? '' : '无输出');
  parts.push('', body);
  return {
    messageId: `assistant-deepresearch-${raw.task_id}`,
    content: parts.join('\n'),
  };
}
