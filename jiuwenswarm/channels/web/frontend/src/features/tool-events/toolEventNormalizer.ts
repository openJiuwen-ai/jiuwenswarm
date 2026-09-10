import { parseSkillTreePath, type SkillTreePath } from '../../types/skillTree';
import {
  parseBeamSearchProgress,
  type BeamSearchProgress,
} from '../../types/beamSearch';

type UnknownPayload = Record<string, unknown>;

const MERMAID_DIRECT_ID_PATTERN = /^[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*$/;
const UNICODE_CAPABILITY_ID_PATTERN = /^[\p{L}\p{M}\p{N}_-]+$/u;
const MERMAID_RESERVED_IDS = new Set([
  'acc_descr',
  'acc_descr_multiline',
  'acc_title',
  'alt',
  'and',
  'architecture-beta',
  'block-beta',
  'c4context',
  'class',
  'classdef',
  'click',
  'default',
  'direction',
  'else',
  'end',
  'flowchart',
  'gantt',
  'gitgraph',
  'graph',
  'journey',
  'linkstyle',
  'loop',
  'mindmap',
  'opt',
  'par',
  'participant',
  'pie',
  'rect',
  'requirementdiagram',
  'sankey-beta',
  'sequencediagram',
  'style',
  'state',
  'subgraph',
  'timeline',
  'xychart-beta',
]);
const PLANNED_GRAPH_NODE_RADIUS = 8;
const PLANNED_GRAPH_FONT_FAMILY = 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Monaco, Consolas, monospace';
const PLANNED_GRAPH_FONT_SIZE = '11px';

function asRecord(value: unknown): UnknownPayload | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
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

function resolveMemberName(payload: UnknownPayload, fallback?: UnknownPayload): string | undefined {
  const candidates = [
    payload.member_name,
    fallback?.member_name,
  ];
  for (const item of candidates) {
    if (typeof item === 'string' && item.trim()) {
      return item.trim();
    }
  }

  let role = '';
  if (typeof payload.role === 'string') {
    role = payload.role;
  } else if (typeof fallback?.role === 'string') {
    role = fallback.role;
  }
  return role.trim().toLowerCase() === 'teammate' ? 'teammate' : undefined;
}

function isValidCapabilityId(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    UNICODE_CAPABILITY_ID_PATTERN.test(value) &&
    !MERMAID_RESERVED_IDS.has(value.toLowerCase())
  );
}

function isDirectMermaidId(value: string): boolean {
  const normalized = value.toLowerCase();
  return (
    MERMAID_DIRECT_ID_PATTERN.test(value) &&
    !Array.from(MERMAID_RESERVED_IDS).some((keyword) => normalized.startsWith(keyword))
  );
}

function mermaidNodeIds(nodeIds: string[]): Map<string, string> {
  const usedIds = new Set(nodeIds.filter(isDirectMermaidId));
  const renderedIds = new Map<string, string>();
  let aliasIndex = 0;

  for (const nodeId of nodeIds) {
    if (isDirectMermaidId(nodeId)) {
      renderedIds.set(nodeId, nodeId);
      continue;
    }

    let alias = `capability_${aliasIndex++}`;
    while (usedIds.has(alias)) {
      alias = `capability_${aliasIndex++}`;
    }
    usedIds.add(alias);
    renderedIds.set(nodeId, alias);
  }

  return renderedIds;
}

function compareStable(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

/** 将合法的 planned_graph JGF 投影成 Mermaid，必要时用安全别名保留原始标签。 */
export function plannedGraphToMermaid(rawOutput: unknown): string | undefined {
  const output = asRecord(rawOutput);
  const plannedGraph = asRecord(output?.planned_graph);
  const graph = asRecord(plannedGraph?.graph);
  const nodes = asRecord(graph?.nodes);
  const edges = graph?.edges;
  if (!graph || !nodes || !Array.isArray(edges)) {
    return undefined;
  }

  const nodeIds = Object.keys(nodes);
  if (
    nodeIds.length === 0 ||
    nodeIds.some((nodeId) => !isValidCapabilityId(nodeId) || !asRecord(nodes[nodeId]))
  ) {
    return undefined;
  }

  const nodeIdSet = new Set(nodeIds);
  const normalizedEdges: Array<{ source: string; target: string }> = [];
  for (const edge of edges) {
    const edgeRecord = asRecord(edge);
    const source = edgeRecord?.source;
    const target = edgeRecord?.target;
    if (
      !edgeRecord ||
      edgeRecord.relation !== 'can_feed' ||
      !isValidCapabilityId(source) ||
      !isValidCapabilityId(target) ||
      !nodeIdSet.has(source) ||
      !nodeIdSet.has(target)
    ) {
      return undefined;
    }
    normalizedEdges.push({ source, target });
  }

  normalizedEdges.sort((left, right) =>
    compareStable(left.source, right.source) || compareStable(left.target, right.target)
  );
  nodeIds.sort(compareStable);
  const renderedNodeIds = mermaidNodeIds(nodeIds);

  return [
    `%%{init: ${JSON.stringify({
      fontFamily: PLANNED_GRAPH_FONT_FAMILY,
      themeVariables: {
        fontSize: PLANNED_GRAPH_FONT_SIZE,
        radius: PLANNED_GRAPH_NODE_RADIUS,
      },
    })}}%%`,
    'flowchart LR',
    ...nodeIds.map((nodeId) =>
      `${renderedNodeIds.get(nodeId)}(${JSON.stringify(String(asRecord(nodes[nodeId])?.label || nodeId))})`
    ),
    ...normalizedEdges.map(
      ({ source, target }) => `${renderedNodeIds.get(source)} --> ${renderedNodeIds.get(target)}`,
    ),
  ].join('\n');
}

export interface NormalizedToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  description?: string;
  formatted_args?: string;
  /** 后端下发的可读展示名（部分工具带），前端优先直接展示，省去本地推断。 */
  display_name?: string;
  memberName?: string;
}

export interface NormalizedToolResult {
  toolName: string;
  toolCallId?: string;
  result: string;
  success: boolean;
  /** status=pending 表示后台任务已接受，尚未进入终态。 */
  pending?: boolean;
  /** status=timeout / timed_out 时为 true，供 store 落成 timeout */
  timedOut?: boolean;
  summary?: string;
  skillTree?: SkillTreePath;
  beamSearch?: BeamSearchProgress;
  /** 仅 symphony_compose_graph 的合法 planned_graph 前端展示投影。 */
  mermaid?: string;
}

export interface NormalizedToolUpdate {
  toolName: string;
  toolCallId?: string;
  beamSearch?: BeamSearchProgress;
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
  const displayNameRaw =
    (typeof toolCallPayload.display_name === 'string' && toolCallPayload.display_name) ||
    (typeof toolCallPayload.displayName === 'string' && toolCallPayload.displayName) ||
    '';
  const display_name = displayNameRaw.trim() || undefined;
  const memberName = resolveMemberName(toolCallPayload, payload);

  return {
    id,
    name,
    arguments: parseArguments(toolCallPayload.arguments),
    description,
    formatted_args,
    display_name,
    memberName,
  };
}

export function normalizeToolResultPayload(payload: UnknownPayload): NormalizedToolResult {
  const toolResultPayload = asRecord(payload.tool_result) ?? payload;
  const rawOutputRecord =
    asRecord(toolResultPayload.raw_output) ?? asRecord(toolResultPayload.rawOutput);
  const rawOutputData = asRecord(rawOutputRecord?.data);
  const rawOutputResult =
    typeof rawOutputRecord?.result === 'string'
      ? rawOutputRecord.result
      : undefined;
  const nestedDataResult =
    typeof rawOutputData?.result === 'string'
      ? rawOutputData.result
      : typeof rawOutputData?.message === 'string'
        ? rawOutputData.message
        : undefined;
  const directDataRecord = asRecord(toolResultPayload.data);
  const directDataResult =
    typeof directDataRecord?.result === 'string'
      ? directDataRecord.result
      : typeof directDataRecord?.message === 'string'
        ? directDataRecord.message
        : undefined;
  const rawOutputFallback =
    typeof rawOutputRecord?.output === 'string'
      ? rawOutputRecord.output
      : undefined;
  const result =
    rawOutputResult ||
    nestedDataResult ||
    (typeof toolResultPayload.result === 'string' &&
      toolResultPayload.result) ||
    directDataResult ||
    rawOutputFallback ||
    (typeof toolResultPayload.data === 'string' ? toolResultPayload.data : '') ||
    (typeof toolResultPayload.error === 'string'
      ? toolResultPayload.error
      : '');
  const status =
    typeof toolResultPayload.status === 'string'
      ? toolResultPayload.status.trim().toLowerCase()
      : typeof rawOutputRecord?.status === 'string'
        ? rawOutputRecord.status.trim().toLowerCase()
        : typeof rawOutputData?.status === 'string'
          ? rawOutputData.status.trim().toLowerCase()
          : '';
  const pending = status === 'pending';
  const timedOut = status === 'timeout' || status === 'timed_out';
  const statusFailed =
    !pending &&
    (timedOut || status === 'error' || status === 'failed' || status === 'failure');
  const success =
    pending
      ? true
      : typeof toolResultPayload.success === 'boolean'
        ? toolResultPayload.success && !timedOut
        : status
          ? !statusFailed
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
  const skillTree =
    parseSkillTreePath(toolResultPayload.raw_output) ??
    parseSkillTreePath(toolResultPayload.rawOutput);
  const beamSearch =
    parseBeamSearchProgress(rawOutputRecord?.beam_search);
  const mermaid =
    toolName === 'symphony_compose_graph' &&
    success &&
    !pending &&
    !timedOut
      ? plannedGraphToMermaid(rawOutputRecord)
      : undefined;

  return {
    toolName,
    toolCallId,
    result,
    success,
    ...(pending ? { pending: true } : {}),
    ...(timedOut ? { timedOut: true } : {}),
    summary,
    skillTree,
    beamSearch,
    ...(mermaid ? { mermaid } : {}),
  };
}

export function normalizeToolUpdatePayload(payload: UnknownPayload): NormalizedToolUpdate {
  const update = asRecord(payload.tool_update) ?? payload;
  return {
    toolName:
      (typeof update.tool_name === 'string' && update.tool_name) || 'unknown',
    toolCallId: resolveToolCallId(update, payload),
    beamSearch: parseBeamSearchProgress(update.beam_search_event),
  };
}
