// RSI 纯展示函数：状态文案 / 节点状态色 / 分数格式化等。
// 与数据层解耦，便于组件复用与单测。

import type {
  RsiArtifactType,
  RsiNodeChange,
  RsiNodeType,
  RsiScenario,
  RsiTaskStatus,
  RsiTreeNode,
  RsiTreeGetResult,
} from './types';

// 节点类型 → 展示用状态色类名（对应 rsi.css 的 bar--* 与图例 dot）
export type NodeStatusKind = 'best-path' | 'evaluated' | 'pending' | 'failed' | 'pruned';

// 节点 type → 状态色映射（对齐样式概要：最优路径/已评测/待评测/已剪枝）
// adopted/root → best-path；rejected → evaluated；provisional → pending；pruned → pruned
export function nodeTypeToStatusKind(type: RsiNodeType): NodeStatusKind {
  switch (type) {
    case 'ROOT':
    case 'ADOPTED':
      return 'best-path';
    case 'REJECTED':
      return 'evaluated';
    case 'PROVISIONAL':
      return 'pending';
    case 'PRUNED':
      return 'pruned';
  }
}

export function statusKindClass(kind: NodeStatusKind): string {
  return `rsi-node__bar--${kind}`;
}

export function legendDotClass(kind: NodeStatusKind): string {
  return `rsi-legend__dot rsi-node__bar--${kind}`;
}

type JsonRecord = Record<string, unknown>;

function asRecord(value: unknown): JsonRecord | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as JsonRecord) : null;
}

function asText(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const text = value.trim();
  return text || null;
}

function asFiniteNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function extraRecord(node: RsiTreeNode, key: string): JsonRecord | null {
  return asRecord(node.extra?.[key]);
}

function firstNumber(...values: unknown[]): number | null {
  for (const value of values) {
    const number = asFiniteNumber(value);
    if (number != null) return number;
  }
  return null;
}

function clampText(value: string | null, max = 180): string | null {
  const text = value?.trim();
  if (!text) return null;
  return text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text;
}

const STAGE_LABELS: Record<string, string> = {
  plan: '规划中',
  planning: '规划中',
  research: '调研中',
  researching: '调研中',
  survey: '调研中',
  design: '设计中',
  designing: '设计中',
  implement: '实现中',
  implementation: '实现中',
  execute: '执行中',
  execution: '执行中',
  evaluate: '评测中',
  evaluation: '评测中',
  score: '评测中',
  report: '撰写中',
  reporting: '撰写中',
  write: '撰写中',
  writing: '撰写中',
  paper_writing: '撰写中',
  paper_generation: '撰写中',
  compile: '编译校验中',
  compiling: '编译校验中',
  code_generation: '实现中',
  program_generation: '实现中',
  validate: '校验中',
  validation: '校验中',
};

/** Parse the structured provider stage payload carried in node.extra.stage. */
interface RsiNodeStageSpec {
  id: string;
  status: string;
  name: string | null;
  failedCaseCount: number | null;
  caseIndex: number | null;
  totalCases: number | null;
  caseId: string | null;
  score: number | null;
  candidateIndex: number | null;
  totalCandidates: number | null;
  reusedCaseCount: number | null;
  evaluatedCaseCount: number | null;
}

export function nodeStageSpec(node: RsiTreeNode): RsiNodeStageSpec | null {
  const rawStage = node.extra?.stage;
  const stage = asRecord(rawStage);
  const id = asText(stage?.id ?? stage?.key ?? stage?.stage ?? rawStage)
    ?.toLowerCase()
    .replace(/[-\s]+/g, '_');
  if (!id) return null;
  const name = asText(stage?.name ?? stage?.label);
  const status = asText(stage?.status)?.toLowerCase() ?? '';
  return {
    id,
    status,
    name,
    failedCaseCount: firstNumber(stage?.failed_case_count, stage?.failedCaseCount),
    caseIndex: firstNumber(stage?.case_index, stage?.caseIndex),
    totalCases: firstNumber(stage?.total_cases, stage?.totalCases),
    caseId: asText(stage?.case_id ?? stage?.caseId),
    score: firstNumber(stage?.score),
    candidateIndex: firstNumber(stage?.candidate_index, stage?.candidateIndex),
    totalCandidates: firstNumber(stage?.total_candidates, stage?.totalCandidates),
    reusedCaseCount: firstNumber(stage?.reused_case_count),
    evaluatedCaseCount: firstNumber(stage?.evaluated_case_count),
  };
}

/** Convert provider stage ids/names into a short, stable user-facing label. */
export function nodeStageLabel(node: RsiTreeNode): string | null {
  const spec = nodeStageSpec(node);
  const namedStage = spec?.name ?? null;
  const id = spec?.id ?? null;
  // Real single-harness progress stages carry an index; keep their exact
  // per-case/generation text instead of collapsing them to the generic label.
  if (
    namedStage &&
    id &&
    (id.startsWith('evaluate.case.') || id.startsWith('analyze.') || id === 'generate.candidate' || id === 'source.reuse')
  ) {
    return clampText(namedStage, 40);
  }
  const raw =
    namedStage ??
    asText(node.extra?.stage) ??
    id ??
    (isStageDescription(node.description) ? node.description : null);
  if (!raw) return null;
  if (id) {
    const normalizedId = id.replace(/_/g, '');
    const knownById = Object.entries(STAGE_LABELS).find(([key]) => key.replace(/_/g, '') === normalizedId);
    if (knownById) return knownById[1];
  }
  const lower = raw.toLowerCase();
  if (lower.includes('调研') || lower.includes('research') || lower.includes('survey')) return '调研中';
  if (lower.includes('设计') || lower.includes('design')) return '设计中';
  if (lower.includes('实现') || lower.includes('implement') || lower.includes('生成程序')) return '实现中';
  if (lower.includes('执行') || lower.includes('execute') || lower.includes('experiment')) return '执行中';
  if (lower.includes('评测') || lower.includes('评分') || lower.includes('evaluat') || lower.includes('score')) {
    return '评测中';
  }
  if (lower.includes('撰写') || lower.includes('报告') || lower.includes('report') || lower.includes('write'))
    return '撰写中';
  if (lower.includes('编译') || lower.includes('校验') || lower.includes('validat') || lower.includes('compil')) {
    return '编译校验中';
  }
  if (lower.includes('规划') || lower.includes('plan')) return '规划中';
  return clampText(raw, 24);
}

function isStageDescription(value: string | null): boolean {
  if (!value) return false;
  const lower = value.toLowerCase();
  return (
    lower.includes('正在') ||
    lower.includes('进行中') ||
    lower.includes('research') ||
    lower.includes('evaluat') ||
    lower.includes('implement') ||
    lower.includes('compile') ||
    lower.includes('validat') ||
    lower.includes('reporting')
  );
}

export type RsiI18nFunction = (
  key: string,
  options?: Record<string, unknown>,
) => string | null;

/** Localized stage label based on structured provider fields. */
export function nodeStageLocalizedLabel(
  node: RsiTreeNode,
  t: RsiI18nFunction | null | undefined,
): string | null {
  const spec = nodeStageSpec(node);
  if (!spec) return null;
  if (spec.id === 'source.reuse') {
    return t?.('rsi.stage.sourceReuse', {
      count: spec.reusedCaseCount ?? 0,
      total: spec.totalCases ?? 0,
      evaluated: spec.evaluatedCaseCount ?? 0,
      defaultValue: spec.name ?? '',
    }) || spec.name;
  }
  if (spec.id.startsWith('evaluate.case.')) {
    const statusMap: Record<string, string> = {
      passed: 'casePassed',
      failed: 'caseFailed',
      error: 'caseError',
      skipped: 'caseSkipped',
      running: 'caseRunning',
    };
    const key = statusMap[spec.status];
    if (!key) return spec.name ?? nodeStageLabel(node);
    const params: Record<string, unknown> = {
      index: spec.caseIndex ?? 0,
      total: spec.totalCases ?? 0,
    };
    const label = t?.(`rsi.stage.${key}`, { ...params, defaultValue: spec.name ?? '' });
    if (!label) return spec.name ?? nodeStageLabel(node);
    if (spec.score != null && (spec.status === 'passed' || spec.status === 'failed')) {
      const suffix = t?.('rsi.stage.scoreSuffix', {
        score: spec.score.toFixed(2),
        defaultValue: ` · 得分 ${spec.score.toFixed(2)}`,
      }) ?? ` · 得分 ${spec.score.toFixed(2)}`;
      return `${label}${suffix}`;
    }
    return label;
  }
  if (spec.id === 'generate.candidate') {
    const statusMap: Record<string, string> = {
      done: 'candidateDone',
      error: 'candidateError',
      running: 'candidateRunning',
    };
    const key = statusMap[spec.status];
    if (!key) return spec.name ?? nodeStageLabel(node);
    const params: Record<string, unknown> = {
      index: spec.candidateIndex ?? 0,
      total: spec.totalCandidates ?? 0,
    };
    const label = t?.(`rsi.stage.${key}`, { ...params, defaultValue: spec.name ?? '' });
    return label || spec.name || nodeStageLabel(node);
  }
  if (spec.id.startsWith('analyze.')) {
    const statusMap: Record<string, string> = {
      done: 'analysisDone',
      error: 'analysisError',
      running: 'analysisRunning',
    };
    const key = statusMap[spec.status] ?? 'analysisRunning';
    const params: Record<string, unknown> = {
      count: spec.failedCaseCount ?? 0,
    };
    const label = t?.(`rsi.stage.${key}`, { ...params, defaultValue: spec.name ?? '' });
    return label || spec.name || nodeStageLabel(node);
  }
  return null;
}

const CHANGE_AREA_LABELS: Record<string, string> = {
  paper: '论文内容',
  program: '程序逻辑',
  prompt: '提示词',
  skill: '技能',
  tool: '工具',
  rail: '护栏',
  experiments: '实验设计',
  experiment: '实验设计',
  reporting: '报告生成',
};

const OPERATION_LABELS: Record<string, string> = {
  add: '新增',
  create: '新增',
  generate: '生成',
  modify: '调整',
  update: '调整',
  remove: '移除',
  delete: '移除',
  replace: '替换',
};

function changeAreaLabel(change: RsiNodeChange): string | null {
  const raw = nodeChangeGroup(change).trim().toLowerCase();
  if (!raw) return null;
  return CHANGE_AREA_LABELS[raw] ?? raw;
}

/** A compact change sentence used by both the tree card and the detail drawer. */
export function nodeChangeDisplayLabel(change: RsiNodeChange): string {
  const summary = clampText(change.summary ?? change.reason ?? null, 150);
  if (summary) {
    if (summary === 'Generated a new paper version.') return '生成新的论文版本';
    if (summary === 'the starting program') return '基线程序';
    return summary;
  }
  const area = changeAreaLabel(change);
  const operation = OPERATION_LABELS[change.operation.toLowerCase()] ?? change.operation.toLowerCase();
  return [area, operation].filter(Boolean).join(' · ') || '产生结构化改动';
}

function nodeDescriptionSummary(node: RsiTreeNode): string | null {
  const description = asText(node.description);
  if (!description || isStageDescription(description)) return null;
  if (
    description === 'paper optimization root' ||
    description === 'No starting paper; first node writes from scratch.'
  ) {
    return '从基线开始生成论文';
  }
  if (description === 'Uploaded starting paper (ingestion not yet wired in).') return '已上传起始论文';
  if (description === 'the starting program') return '从基线程序开始优化';
  return clampText(description);
}

/** The one-line change/description used inside a tree card. */
export function nodeSummaryText(node: RsiTreeNode): string | null {
  return (node.changes ?? []).map(nodeChangeDisplayLabel).find(Boolean) ?? nodeDescriptionSummary(node);
}

function paperOutcome(node: RsiTreeNode): string | null {
  return asText(extraRecord(node, 'paper')?.outcome)?.toLowerCase() ?? null;
}

function hasRuntimeFailure(node: RsiTreeNode): boolean {
  if (paperOutcome(node) === 'failed') return true;
  const program = extraRecord(node, 'program');
  const evaluation = asRecord(program?.evaluation);
  if (asRecord(program?.error) || evaluation?.valid === false) return true;
  const failureClass = (node.failure_class ?? '').toLowerCase();
  const failureReason = (node.failure_reason ?? '').toLowerCase();
  return (
    (/fail|error|crash|terminat|invalid|exception|timeout/.test(failureClass) &&
      failureClass !== 'rejected_by_score') ||
    /manager decision failed|pipeline failed|generation failed|compile failed|execution timed out/.test(failureReason)
  );
}

function scoreForNode(node: RsiTreeNode): number | null {
  return firstNumber(node.score, extraRecord(node, 'paper')?.score_overall);
}

function siblingAttempt(node: RsiTreeNode, allNodes: RsiTreeNode[]): { index: number; total: number } | null {
  const siblings = allNodes
    .filter(
      (candidate) =>
        candidate.node_id !== node.node_id &&
        candidate.parent_id === node.parent_id &&
        candidate.iteration === node.iteration,
    )
    .concat(node)
    .sort((left, right) => left.node_id.localeCompare(right.node_id));
  if (siblings.length <= 1) return null;
  const index = Math.max(
    0,
    siblings.findIndex((candidate) => candidate.node_id === node.node_id),
  );
  return { index: index + 1, total: siblings.length };
}

function attemptInfo(
  node: RsiTreeNode,
  allNodes: RsiTreeNode[],
): { round: number; attempt: { index: number; total: number } | null } {
  const paper = extraRecord(node, 'paper');
  const program = extraRecord(node, 'program');
  const round = Math.max(1, firstNumber(paper?.round_index, program?.round_index, node.iteration) ?? 1);
  const explicitAttempt = firstNumber(paper?.attempt);
  const sibling = siblingAttempt(node, allNodes);
  if (explicitAttempt != null) {
    const total = Math.max(explicitAttempt, sibling?.total ?? 0);
    return { round, attempt: total > 1 ? { index: explicitAttempt, total } : null };
  }
  return { round, attempt: sibling };
}

export type RsiNodeLifecycle =
  'baseline' | 'generating' | 'evaluating' | 'pending' | 'adopted' | 'rejected' | 'failed' | 'pruned';

export interface RsiNodePresentationContext {
  scenario: RsiScenario;
  artifactType: RsiArtifactType | null;
  allNodes?: RsiTreeNode[];
  taskRunning?: boolean;
}

export interface RsiNodePresentation {
  title: string;
  subtitle: string;
  lifecycle: RsiNodeLifecycle;
  statusKind: NodeStatusKind;
  runtimeKind: NodeRuntimeKind;
  runtimeLabel: string;
  runtimeIcon: NodeIconKind;
  stageLabel: string | null;
  summary: string | null;
  changeItems: string[];
  reasonLabel: string | null;
  reasonDetail: string | null;
  parentTitle: string | null;
  iteration: number;
  attempt: { index: number; total: number } | null;
  score: number | null;
  parentScore: number | null;
  scoreDelta: number | null;
  rawNodeId: string;
}

function artifactObjectLabel(scenario: RsiScenario, artifactType: RsiArtifactType | null): string {
  if (scenario === 'HARNESS') return 'Harness';
  return artifactType === 'PROGRAM' ? '程序' : '论文';
}

function lifecycleForNode(node: RsiTreeNode, taskRunning: boolean): RsiNodeLifecycle {
  if (node.type === 'ROOT') return 'baseline';
  if (node.type === 'PRUNED') return 'pruned';
  if (node.type === 'PROVISIONAL') {
    if (!taskRunning) return 'pending';
    const stage = nodeStageSpec(node);
    if (stage?.id.startsWith('evaluate.case.')) return 'evaluating';
    if (stage?.id === 'generate.candidate') return 'generating';
    return nodeStageLabel(node)?.includes('评测') ? 'evaluating' : 'generating';
  }
  if (hasRuntimeFailure(node)) return 'failed';
  return node.type === 'ADOPTED' || node.adopted ? 'adopted' : 'rejected';
}

function failureLabel(
  node: RsiTreeNode,
  lifecycle: RsiNodeLifecycle,
  score: number | null,
  parentScore: number | null,
): string | null {
  if (lifecycle === 'failed') {
    const failureClass = (node.failure_class ?? '').toLowerCase();
    const failureReason = (node.failure_reason ?? '').toLowerCase();
    if (
      failureClass.includes('manager') ||
      failureClass.includes('decision') ||
      failureReason.includes('manager decision')
    ) {
      return '管理器决策失败';
    }
    if (failureClass.includes('scor')) return '评分失败';
    if (failureClass.includes('compile') || failureClass.includes('valid')) return '编译或校验未通过';
    if (failureClass.includes('timeout')) return '执行超时';
    return '生成失败';
  }
  if (lifecycle === 'rejected') {
    if (node.extra?.iteration_unit === 'epoch') return '本轮未保留 Harness 改动';
    if (
      node.failure_class === 'rejected_by_score' &&
      score != null &&
      parentScore != null &&
      score <= parentScore
    ) return '得分未超过父节点';
    return '未达到采纳条件';
  }
  if (lifecycle === 'pruned') return '搜索空间已剪枝';
  return null;
}

function lifecycleStatusKind(lifecycle: RsiNodeLifecycle): NodeStatusKind {
  switch (lifecycle) {
    case 'baseline':
    case 'adopted':
      return 'best-path';
    case 'generating':
    case 'evaluating':
    case 'pending':
      return 'pending';
    case 'failed':
      return 'failed';
    case 'pruned':
      return 'pruned';
    case 'rejected':
      return 'evaluated';
  }
}

function lifecycleRuntimeKind(lifecycle: RsiNodeLifecycle): NodeRuntimeKind {
  switch (lifecycle) {
    case 'baseline':
    case 'adopted':
      return 'best-path';
    case 'generating':
      return 'evaluating';
    case 'evaluating':
      return 'evaluating';
    case 'pending':
      return 'pending';
    case 'failed':
      return 'failed';
    case 'rejected':
      return 'evaluated';
    case 'pruned':
      return 'pruned';
  }
}

/**
 * The runtime kind controls colors/layout, while the lifecycle carries the
 * user-facing phase. Keep these labels separate so a node that is still
 * generating an artifact is not presented as if evaluation had already
 * started.
 */
function lifecycleRuntimeLabel(lifecycle: RsiNodeLifecycle): string {
  switch (lifecycle) {
    case 'baseline':
    case 'adopted':
      return '当前最优';
    case 'generating':
      return '生成中';
    case 'evaluating':
      return '评测中';
    case 'pending':
      return '待处理';
    case 'rejected':
      return '未采用';
    case 'failed':
      return '失败';
    case 'pruned':
      return '已剪枝';
  }
}

function titleForNode(
  context: RsiNodePresentationContext,
  lifecycle: RsiNodeLifecycle,
  round: number,
  attempt: { index: number; total: number } | null,
): string {
  const objectLabel = artifactObjectLabel(context.scenario, context.artifactType);
  if (lifecycle === 'baseline') return `基线${objectLabel}`;
  if (lifecycle === 'adopted') return `${objectLabel}版本 ${round} · 当前最优`;
  const suffix = lifecycle === 'failed' ? '尝试' : lifecycle === 'pruned' ? '候选' : '候选';
  const attemptLabel = attempt ? ` ${attempt.index}/${attempt.total}` : '';
  return `第 ${round} 轮 · ${objectLabel}${suffix}${attemptLabel}`;
}

export function presentRsiNode(node: RsiTreeNode, context: RsiNodePresentationContext): RsiNodePresentation {
  const allNodes = context.allNodes ?? [node];
  const taskRunning = Boolean(context.taskRunning);
  const lifecycle = lifecycleForNode(node, taskRunning);
  const { round, attempt } = attemptInfo(node, allNodes);
  const parent = node.parent_id ? allNodes.find((candidate) => candidate.node_id === node.parent_id) : null;
  const score = scoreForNode(node);
  const parentScore = parent ? scoreForNode(parent) : null;
  const changeItems = (node.changes ?? []).map(nodeChangeDisplayLabel).filter(Boolean);
  const summary = changeItems[0] ?? nodeDescriptionSummary(node);
  const reasonLabel = failureLabel(node, lifecycle, score, parentScore);
  return {
    title: titleForNode(context, lifecycle, round, attempt),
    subtitle:
      lifecycle === 'baseline'
        ? '优化起点'
        : attempt
          ? `第 ${round} 轮 · 尝试 ${attempt.index}/${attempt.total}`
          : `第 ${round} 轮`,
    lifecycle,
    statusKind: lifecycleStatusKind(lifecycle),
    runtimeKind: lifecycleRuntimeKind(lifecycle),
    runtimeLabel: lifecycleRuntimeLabel(lifecycle),
    runtimeIcon: runtimeIconKind(lifecycleRuntimeKind(lifecycle)),
    stageLabel: nodeStageLabel(node),
    summary,
    changeItems,
    reasonLabel,
    reasonDetail: clampText(asText(node.failure_reason), 500),
    parentTitle: parent ? presentRsiNode(parent, { ...context, allNodes }).title : null,
    iteration: round,
    attempt,
    score,
    parentScore,
    scoreDelta: score != null && parentScore != null ? score - parentScore : null,
    rawNodeId: node.node_id,
  };
}

export function nodeChangeGroup(change: RsiNodeChange): string {
  return change.group ?? change.element ?? change.function ?? change.target ?? '';
}

export function nodeChangeSummary(change: RsiNodeChange): string {
  return change.summary ?? change.reason ?? change.operation ?? '';
}

// 任务状态 → 中文标签 key（i18n key 后缀）
const STATUS_LABEL_KEY: Record<RsiTaskStatus, string> = {
  CREATED: 'statusCreated',
  QUEUED: 'statusQueued',
  RUNNING: 'statusRunning',
  COMPLETED: 'statusCompleted',
  FAILED: 'statusFailed',
  PAUSED: 'statusPaused',
  TERMINATED: 'statusTerminated',
};

export function statusLabelKey(status: RsiTaskStatus): string {
  return STATUS_LABEL_KEY[status] ?? 'statusCreated';
}

// 运行态操作按钮映射：根据当前状态返回可执行动作集
export type RsiActionKind = 'config' | 'delete' | 'pause' | 'resume' | 'stop' | 'install' | 'download';

export function actionsForStatus(
  status: RsiTaskStatus,
  scenario: RsiScenario,
  installed = false,
  tree: RsiTreeGetResult | null = null,
): RsiActionKind[] {
  const actions: RsiActionKind[] = ['config', 'delete'];
  switch (status) {
    case 'QUEUED':
    case 'CREATED':
      actions.push('pause');
      break;
    case 'RUNNING':
      // Harness 引擎不支持运行中暂停，改用停止任务（后端 terminate）。
      actions.push(scenario === 'HARNESS' ? 'stop' : 'pause');
      break;
    case 'PAUSED':
      actions.push('resume');
      break;
    case 'COMPLETED':
      if (!installed) {
        if (scenario === 'HARNESS') {
          // 仅有基线节点（没有真正展开优化）时，尚未生成可安装的 Harness 插件包。
          const hasOptimizedNodes = (tree?.nodes.length ?? 0) > 1;
          if (hasOptimizedNodes) actions.push('install');
        } else {
          actions.push('download');
        }
      }
      break;
    case 'FAILED':
    case 'TERMINATED':
      break;
  }
  return actions;
}

// 分数格式化：保留 1 位小数，null 显示 —
export function formatScore(score: number | null, digits = 1): string {
  if (score == null || Number.isNaN(score)) return '--';
  return score.toFixed(digits);
}

// 提升百分比：↑ 5.2% / ↓ 2.1%，null 显示空串
export function formatGain(gain: number | null): { text: string; kind: 'up' | 'down' | 'none' } {
  if (gain == null || Number.isNaN(gain)) return { text: '', kind: 'none' };
  const pct = gain * 100;
  if (pct >= 0) return { text: `${pct.toFixed(1)}% ↑`, kind: 'up' };
  return { text: `${Math.abs(pct).toFixed(1)}% ↓`, kind: 'down' };
}

// token 用量格式化：万 tokens
export function formatTokens(tokens: { input: number; output: number; cache_hit: number }): string {
  const total = tokens.input + tokens.output;
  const wan = total / 10000;
  if (wan >= 1) return `${wan.toFixed(1)} 万tokens`;
  return `${total}tokens`;
}

// token 用量格式化（K 单位）：示例 123K tokens（对齐样式概要）
export function formatTokensK(tokens: { input: number; output: number; cache_hit: number }): string {
  // cache_hit 是 input 的子集，不应重复计入总量。
  const total = tokens.input + tokens.output;
  if (total >= 1000) return Math.round(total / 1000) + 'K tokens';
  return total + ' tokens';
}

// 费用格式化：元
export function formatCost(cost: number | null): string {
  if (cost == null) return '--';
  return cost.toFixed(2);
}

// 日期时间格式化：ISO 字符串 → 本地 'YYYY-MM-DD HH:mm:ss'（对齐样式概要示例）
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '--';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    [d.getFullYear(), pad(d.getMonth() + 1), pad(d.getDate())].join('-') +
    ' ' +
    [pad(d.getHours()), pad(d.getMinutes()), pad(d.getSeconds())].join(':')
  );
}

// 场景显示名：面向用户的名称不再使用“产物优化|论文”这种技术分隔符。
export function scenarioLabel(scenario: 'HARNESS' | 'ARTIFACT'): string {
  return scenario === 'HARNESS' ? 'Harness优化' : '产物优化';
}
export function artifactTypeLabel(type: 'PAPER' | 'PROGRAM'): string {
  return type === 'PAPER' ? '论文' : '程序';
}
// 完整类型标签：Harness优化 / 论文优化 / 程序优化
export function typeDisplayLabel(scenario: 'HARNESS' | 'ARTIFACT', artifactType: 'PAPER' | 'PROGRAM' | null): string {
  if (scenario === 'HARNESS') return 'Harness优化';
  return artifactType === 'PROGRAM' ? '程序优化' : '论文优化';
}

// 状态徽章信息：i18n 标签 key + 图标类型
export type StatusBadgeKind = 'queued' | 'running' | 'paused' | 'completed' | 'installed' | 'failed';

export interface StatusBadgeInfo {
  labelKey: string;
  kind: StatusBadgeKind | null;
}

export function statusBadgeInfo(status: RsiTaskStatus, installed = false): StatusBadgeInfo {
  switch (status) {
    case 'QUEUED':
    case 'CREATED':
      return { labelKey: 'statusQueued', kind: 'queued' };
    case 'RUNNING':
      return { labelKey: 'statusRunning', kind: 'running' };
    case 'PAUSED':
      return { labelKey: 'statusPaused', kind: 'paused' };
    case 'COMPLETED':
      return installed
        ? { labelKey: 'statusInstalled', kind: 'installed' }
        : { labelKey: 'statusCompleted', kind: 'completed' };
    case 'FAILED':
    case 'TERMINATED':
      return { labelKey: statusLabelKey(status), kind: 'failed' };
  }
}

// 节点显示名称：保留旧调用点的兼容包装。新 UI 使用 presentRsiNode，避免把 UUID 直接展示给用户。
export function nodeDisplayName(
  type: RsiNodeType,
  nodeId: string,
  scenario: RsiScenario,
  artifactType: RsiArtifactType | null,
): string {
  if (type === 'ROOT') {
    if (scenario === 'HARNESS') return '基线Harness';
    if (artifactType === 'PAPER') return '基线论文';
    return '基线程序';
  }
  const objectLabel = artifactObjectLabel(scenario, artifactType);
  const match = nodeId.match(/(?:attempt|node|N|:)(\d+)$/i);
  const ordinal = match?.[1] ?? '候选';
  return type === 'ADOPTED' ? `${objectLabel}版本 ${ordinal}` : `${objectLabel}候选 ${ordinal}`;
}

// 节点状态标签（兼容旧调用点）；新 UI 使用 presentRsiNode.runtimeLabel。
export function nodeStatusLabel(type: RsiNodeType): string {
  switch (type) {
    case 'ROOT':
      return '基线';
    case 'ADOPTED':
      return '当前最优';
    case 'REJECTED':
      return '未采用';
    case 'PROVISIONAL':
      return '进行中';
    case 'PRUNED':
      return '已剪枝';
  }
}

// ── 运行态展示：区分「评测中」与「待评测」（同色 #FCE7AE，图标/文案不同）──
// provisional + 任务运行中 → 评测中；provisional + 任务非运行 → 待评测。
// 后端若给节点增加独立评测状态字段，可在 nodeRuntimeKind 内替换该启发式。
export type NodeRuntimeKind = 'best-path' | 'evaluated' | 'evaluating' | 'pending' | 'failed' | 'pruned';

export type NodeIconKind = 'crown' | 'check' | 'chevron-double' | 'clock' | 'minus';

// 节点 type + 任务运行态 → 运行态 kind（决定图标与右侧标签）
export function nodeRuntimeKind(type: RsiNodeType, taskRunning: boolean): NodeRuntimeKind {
  switch (type) {
    case 'ROOT':
    case 'ADOPTED':
      return 'best-path';
    case 'REJECTED':
      return 'evaluated';
    case 'PROVISIONAL':
      return taskRunning ? 'evaluating' : 'pending';
    case 'PRUNED':
      return 'pruned';
  }
}

/** Node-aware runtime mapping. Unlike the legacy type-only helper, this distinguishes failed attempts. */
export function nodeRuntimeKindForNode(node: RsiTreeNode, taskRunning: boolean): NodeRuntimeKind {
  const lifecycle = lifecycleForNode(node, taskRunning);
  return lifecycleRuntimeKind(lifecycle);
}

// 运行态 kind → 上层背景色 class（评测中复用 pending 色板）
export function runtimeKindColorClass(kind: NodeRuntimeKind): string {
  const color = kind === 'evaluating' ? 'pending' : kind;
  return `rsi-node__bar--${color}`;
}

// 运行态 kind → 上层左侧黑色徽章内的图标
export function runtimeIconKind(kind: NodeRuntimeKind): NodeIconKind {
  switch (kind) {
    case 'best-path':
      return 'crown';
    case 'evaluated':
      return 'check';
    case 'evaluating':
      return 'chevron-double';
    case 'failed':
      return 'minus';
    case 'pending':
      return 'clock';
    case 'pruned':
      return 'minus';
  }
}

// 运行态 kind → 上层右侧状态标签
export function nodeRuntimeLabel(kind: NodeRuntimeKind): string {
  switch (kind) {
    case 'best-path':
      return '当前最优';
    case 'evaluated':
      return '未采用';
    case 'evaluating':
      return '评测中';
    case 'pending':
      return '待处理';
    case 'failed':
      return '失败';
    case 'pruned':
      return '已剪枝';
  }
}

// 节点下层分数行：score(分数) + extra.potential_score(潜力分) + extra.other_score(其他分) + 其余 *_score
// 数据驱动：当前仅有 score 时只显示 1 行；后端在 extra 补充分数后自动多行并触发展开。
export interface NodeScoreLine {
  value: string;
  label: string;
}

export function nodeScoreLines(node: RsiTreeNode): NodeScoreLine[] {
  const lines: NodeScoreLine[] = [];
  const score = scoreForNode(node);
  if (score != null) lines.push({ value: formatScore(score), label: '分数' });
  const extra = node.extra;
  if (extra && typeof extra === 'object') {
    const potential = extra['potential_score'];
    if (typeof potential === 'number') lines.push({ value: formatScore(potential), label: '潜力分' });
    const other = extra['other_score'];
    if (typeof other === 'number') lines.push({ value: formatScore(other), label: '其他分' });
    for (const [k, v] of Object.entries(extra)) {
      if (k === 'potential_score' || k === 'other_score') continue;
      if (k.endsWith('_score') && typeof v === 'number') {
        lines.push({ value: formatScore(v), label: scoreLabel(k.replace(/_score$/, '')) });
      }
    }
  }
  return lines;
}

function scoreLabel(value: string): string {
  const labels: Record<string, string> = {
    overall: '总分',
    quality: '质量分',
    correctness: '正确性',
    gate: '门槛分',
    reward: '奖励分',
    rollout: '执行分',
  };
  return labels[value.toLowerCase()] ?? value.replace(/[_-]+/g, ' ');
}

// 节点尺寸估算（布局与组件共用，保证布局高度=渲染高度）
// 上层高度固定 32；下层高度随内容行数变化；评测中宽度最大 280。
export const RSI_NODE_BAR_H = 32;
export const RSI_SCORE_LINE_H = 26; // 分数行行高
export const RSI_EVAL_LINE_H = 24; // 评测中/文本行高
export const RSI_SUMMARY_LINE_H = 20; // 节点摘要行高
export const RSI_STAGE_LINE_H = 16; // 根节点评测阶段提示行高
export const RSI_BODY_PAD = 8; // 下层上下 padding 合计
export const RSI_BODY_MIN_H = 42; // 下层最小高度（与节点高保真 32 + 42 对齐）
export const RSI_NODE_MIN_W = 180;
export const RSI_NODE_MAX_W = 280;
export const RSI_SCORE_TOGGLE_H = 20; // 展开按钮(分隔符+行)高度

export interface NodeMetrics {
  width: number;
  barH: number;
  bodyH: number;
}

// 评测中文本行数估算（按估算宽度每行可容纳字符数，最多 4 行）
function evalTextRows(text: string, width: number): number {
  const charsPerLine = Math.max(1, Math.floor((width - 24) / 14));
  return Math.min(4, Math.max(1, Math.ceil(text.length / charsPerLine)));
}

function summaryRows(text: string, width: number): number {
  const charsPerLine = Math.max(1, Math.floor((width - 20) / 8));
  return Math.min(2, Math.max(1, Math.ceil(text.length / charsPerLine)));
}

// 节点宽高估算：barH(32) + bodyH(随内容)；评测中宽度自适应(<=280)。
export function nodeMetrics(node: RsiTreeNode, kind: NodeRuntimeKind, scoreExpanded: boolean): NodeMetrics {
  const barH = RSI_NODE_BAR_H;
  let width = RSI_NODE_MIN_W;
  let bodyH = RSI_BODY_PAD;
  const summary = nodeSummaryText(node);
  const stage = nodeStageLabel(node);
  const rootStageRunning = node.type === 'ROOT' && nodeStageSpec(node)?.status === 'running';
  if (kind === 'evaluating') {
    const text = stage ?? summary ?? '正在处理';
    const secondary = stage && summary ? summary : null;
    width = Math.min(RSI_NODE_MAX_W, Math.max(RSI_NODE_MIN_W, text.length * 8 + 32));
    bodyH = Math.max(
      RSI_BODY_MIN_H,
      RSI_BODY_PAD + evalTextRows(text, width) * RSI_EVAL_LINE_H + (secondary ? RSI_SUMMARY_LINE_H : 0),
    );
  } else if (kind === 'best-path' || kind === 'evaluated') {
    const linesArr = nodeScoreLines(node);
    const shown = scoreExpanded ? Math.min(linesArr.length, 5) : Math.min(linesArr.length, 3);
    const summaryH = summary ? summaryRows(summary, RSI_NODE_MIN_W) * RSI_SUMMARY_LINE_H : 0;
    const stageH = rootStageRunning ? RSI_STAGE_LINE_H : 0;
    bodyH = Math.max(RSI_BODY_MIN_H, RSI_BODY_PAD + stageH + summaryH + shown * RSI_SCORE_LINE_H);
    if (linesArr.length > 3) bodyH += RSI_SCORE_TOGGLE_H;
  } else if (kind === 'pending') {
    bodyH = Math.max(RSI_BODY_MIN_H, RSI_BODY_PAD + RSI_SUMMARY_LINE_H);
  } else if (kind === 'failed') {
    bodyH = Math.max(RSI_BODY_MIN_H, RSI_BODY_PAD + RSI_EVAL_LINE_H + (node.failure_reason ? RSI_SUMMARY_LINE_H : 0));
  } else if (kind === 'pruned') {
    bodyH = Math.max(
      RSI_BODY_MIN_H,
      RSI_BODY_PAD + (node.score == null && !summary ? RSI_EVAL_LINE_H : RSI_SCORE_LINE_H),
    );
  }
  return { width, barH, bodyH };
}

// 节点总高度
export function nodeHeight(node: RsiTreeNode, kind: NodeRuntimeKind, scoreExpanded: boolean): number {
  const m = nodeMetrics(node, kind, scoreExpanded);
  return m.barH + m.bodyH;
}
