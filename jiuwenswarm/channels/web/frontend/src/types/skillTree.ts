/**
 * 技能树路径类型定义
 *
 * 由后端 symphony 技能检索工具在工具结果的 raw_output.skill_tree 中下发，
 * 前端用于在对话时间线内联回放「技能树路径流转」。
 */

export interface SkillTreeNamedId {
  id: string;
  label: string;
}

export type SkillTreeEventType =
  | 'fragment_built'
  | 'fragment_selected'
  | 'fragment_continue'
  | 'reduce_complete'
  | 'search_complete'
  | string;

export interface SkillTreeStep {
  order: number;
  event_type: SkillTreeEventType;
  node_id: string;
  label: string;
  depth: number;
  selectable_count?: number | null;
  selected: SkillTreeNamedId[];
  branches: SkillTreeNamedId[];
  leaves: SkillTreeNamedId[];
  candidate_count?: number | null;
}

export interface SkillTreeCandidate {
  rank: number;
  label: string;
  worker_id: string;
  description: string;
  path: string[];
  selected: boolean;
  source: string;
}

export interface SkillTreePath {
  query: string;
  elapsed_ms?: number | null;
  max_depth: number;
  candidate_count: number;
  steps: SkillTreeStep[];
  candidates: SkillTreeCandidate[];
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object') {
    return null;
  }
  return value as Record<string, unknown>;
}

/**
 * 从工具结果 payload 的 raw_output 中安全解析 skill_tree。
 * 既兼容已是对象的结构化下发，也兼容被序列化成 JSON 字符串的情况。
 */
export function parseSkillTreePath(rawOutput: unknown): SkillTreePath | undefined {
  let source: unknown = rawOutput;
  if (typeof source === 'string') {
    try {
      source = JSON.parse(source);
    } catch {
      return undefined;
    }
  }
  const record = asRecord(source);
  if (!record) {
    return undefined;
  }
  // 仅接受显式的 skill_tree 字段（agentic search 专用通道），避免误判其它工具的结构化输出
  const tree = asRecord(record.skill_tree);
  if (!tree) {
    return undefined;
  }
  const steps = Array.isArray(tree.steps) ? (tree.steps as SkillTreeStep[]) : [];
  const candidates = Array.isArray(tree.candidates)
    ? (tree.candidates as SkillTreeCandidate[])
    : [];
  if (steps.length === 0 && candidates.length === 0) {
    return undefined;
  }
  return {
    query: typeof tree.query === 'string' ? tree.query : '',
    elapsed_ms: typeof tree.elapsed_ms === 'number' ? tree.elapsed_ms : null,
    max_depth: typeof tree.max_depth === 'number' ? tree.max_depth : 0,
    candidate_count:
      typeof tree.candidate_count === 'number' ? tree.candidate_count : candidates.length,
    steps,
    candidates,
  };
}
