/**
 * SwarmFlow workflow 共享类型与工具函数。
 *
 * 从 TUI 的 workflows.ts 提取，供 web 端树视图渲染使用。
 * 包含 WorkflowRun → WorkflowPhase → WorkflowAgent 树结构定义、
 * 增量合并逻辑（mergeWorkflowRun）以及状态/格式化工具函数。
 */

export type WorkflowStatus =
  | 'planned'
  | 'pending'
  | 'running'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'stopped'
  | 'waiting_for_human';

export type WorkflowNodeType = 'agent' | 'agent_session' | 'human' | 'human_session';

export interface WorkflowAgentActivity {
  timestamp: string;
  type: 'tool_call' | 'tool_result';
  content: string;
}

export interface WorkflowBudget {
  total: number | null;
  spent: number;
  remaining: number | null;
  scope: 'leader' | 'session' | 'workflow';
  exhausted: boolean;
}

/**
 * Compact token-count label, same rounding as the TUI's formatTokenCount:
 * ``500`` stays ``500`` (no premature K-rounding — 500 must not read as 1K),
 * ``1250`` → ``1.3K``, ``220000`` → ``220K``.
 */
function compactTokenLabel(value: number): string {
  if (value < 1000) return `${Math.round(value)}`;
  if (value < 1_000_000) {
    const k = (value / 1000).toFixed(1);
    return `${k.endsWith('.0') ? k.slice(0, -2) : k}K`;
  }
  const m = (value / 1_000_000).toFixed(1);
  return `${m.endsWith('.0') ? m.slice(0, -2) : m}M`;
}

/**
 * Compact token-budget label, e.g. ``12K/50K`` or ``480/500``.
 * An unbounded ledger (``total == null``) shows spent only — the caller appends
 * the localized "unbounded" suffix (same wording as the TUI).
 */
export function formatBudgetK(budget: WorkflowBudget): string {
  const spent = compactTokenLabel(budget.spent ?? 0);
  if (budget.total == null) return spent;
  return `${spent}/${compactTokenLabel(budget.total)}`;
}

export interface WorkflowAgentPart {
  part_idx: number;
  total_parts: number;
  content: string;
}

export interface WorkflowAgent {
  id: string;
  name: string;
  status: WorkflowStatus;
  model?: string;
  prompt?: string;
  prompt_parts?: WorkflowAgentPart[];
  activity?: WorkflowAgentActivity[];
  outcome?: string;
  outcome_parts?: WorkflowAgentPart[];
  error?: string;
  error_parts?: WorkflowAgentPart[];
  started_at?: string;
  completed_at?: string;
  token_count?: number | null;
  duration_ms?: number | null;
  kind?: 'agent' | 'human';
  node_type?: WorkflowNodeType;
  correlation_id?: string;
  human_prompt?: string;
  human_prompt_parts?: WorkflowAgentPart[];
  human_reply?: string;
  human_reply_parts?: WorkflowAgentPart[];
  activity_parts?: WorkflowAgentPart[];
  /** True on get_phase summaries — full body fetched on demand via get_agent. */
  detail_pending?: boolean;
  /** ~200-char outcome stub carried on get_phase summaries (full text via get_agent). */
  outcome_preview?: string;
  /** ~200-char error stub carried on get_phase summaries (full text via get_agent). */
  error_preview?: string;
}

export interface WorkflowPhase {
  id: string;
  name: string;
  description?: string;
  status: WorkflowStatus;
  agent_count?: number;
  completed_agent_count?: number;
  /** Absent on phase summaries from ``action=get_workflow``. */
  agents?: WorkflowAgent[];
  phase_type?: 'child' | null;
  parent_phase?: string | null;
  iteration?: number | null;
  detail_pending?: boolean;
  agent_total?: number;
  has_more?: boolean;
}

export interface WorkflowRun {
  id: string;
  name: string;
  summary: string;
  status: WorkflowStatus;
  agent_count?: number;
  completed_agent_count?: number;
  started_at?: string;
  completed_at?: string;
  script?: string;
  result?: string;
  error?: string;
  logs?: string[];
  logs_truncated?: boolean;
  token_count?: number | null;
  duration_ms?: number | null;
  estimated_token_count?: number | null;
  budget?: WorkflowBudget | null;
  /** Per-run ledger snapshot (META.workflow_token_limit); null when unset. */
  workflow_budget?: WorkflowBudget | null;
  /** Which ledger triggered a budget failure: 'session' | 'workflow'. */
  budget_exhausted_scope?: 'session' | 'workflow' | null;
  /** 'relaunch' = script-edit re-run (replace the phase tree); 'resume' = normal pause→resume. */
  relaunch_kind?: 'relaunch' | 'resume' | null;
  /** Absent on list summaries from ``action=list``. */
  phases?: WorkflowPhase[];
  detail_pending?: boolean;
  phase_total?: number;
  has_more?: boolean;
}

const SPLITTABLE_AGENT_FIELDS = [
  'prompt',
  'outcome',
  'human_prompt',
  'human_reply',
  'activity',
  'error',
] as const;

/** Reassemble ``{field}_parts`` arrays back into the base string field. */
export function reassembleAgentFieldParts(agent: WorkflowAgent): WorkflowAgent {
  let out = agent;
  for (const field of SPLITTABLE_AGENT_FIELDS) {
    const partsKey = `${field}_parts`;
    const parts = (out as unknown as Record<string, unknown>)[partsKey];
    if (!Array.isArray(parts) || parts.length === 0) continue;
    const sorted = [...parts].sort(
      (a, b) =>
        (a as WorkflowAgentPart).part_idx - (b as WorkflowAgentPart).part_idx,
    );
    const joined = sorted
      .map((p) => (p as WorkflowAgentPart).content ?? '')
      .join('');
    const next: WorkflowAgent = { ...out, [field]: joined } as WorkflowAgent;
    delete (next as unknown as Record<string, unknown>)[partsKey];
    out = next;
  }
  return out;
}

// ── 状态图标 ──────────────────────────────────────────────

export const WAITING_FOR_HUMAN_ICON = '\u263A';

export function workflowStatusIcon(status: WorkflowStatus): string {
  switch (status) {
    case 'planned':
      return '\u25C7';
    case 'completed':
      return '\u2713';
    case 'failed':
      return '\u00D7';
    case 'running':
      return '\u25D0';
    case 'paused':
      return '\u2016';
    case 'pending':
      return '\u25CB';
    case 'stopped':
      return '\u25A0';
    case 'waiting_for_human':
      return WAITING_FOR_HUMAN_ICON;
  }
}

// ── Session 节点工具 ──────────────────────────────────────

export function isSessionNode(agent: Pick<WorkflowAgent, 'node_type'>): boolean {
  return agent.node_type === 'agent_session' || agent.node_type === 'human_session';
}

export function sessionGroupKey(
  agent: Pick<WorkflowAgent, 'name' | 'node_type'>,
): string | null {
  if (!isSessionNode(agent)) return null;
  return `${agent.name}\0${agent.node_type}`;
}

export function parseTurnFromCorrelationId(correlationId?: string): number | null {
  if (!correlationId) return null;
  const parts = correlationId.split(':');
  const last = parts[parts.length - 1];
  if (last === undefined) return null;
  const turn = Number.parseInt(last, 10);
  return Number.isFinite(turn) ? turn : null;
}

export function sortWorkflowAgentsByTurn(agents: WorkflowAgent[]): WorkflowAgent[] {
  return [...agents].sort((a, b) => {
    const turnA = parseTurnFromCorrelationId(a.correlation_id);
    const turnB = parseTurnFromCorrelationId(b.correlation_id);
    if (turnA !== null && turnB !== null) return turnA - turnB;
    return (a.started_at ?? '').localeCompare(b.started_at ?? '');
  });
}

export function sessionMembersInPhase(
  phaseAgents: WorkflowAgent[],
  sessionLabel: string,
  nodeType?: WorkflowNodeType,
): WorkflowAgent[] {
  const members = phaseAgents.filter(
    (agent) =>
      agent.name === sessionLabel &&
      isSessionNode(agent) &&
      (nodeType === undefined || agent.node_type === nodeType),
  );
  // Deduplicate by turn (correlation_id): a session agent may emit multiple
  // agent_started events with different agent_id (e.g. a retry after a failed
  // structured-output attempt). Only the last agent for each turn survives —
  // earlier retries are stale and would render duplicate "turn 0" rows.
  const byTurn = new Map<string, WorkflowAgent>();
  for (const m of members) {
    const turn = m.correlation_id ?? m.id;
    byTurn.set(turn, m); // last wins (phaseAgents is in emission order)
  }
  return sortWorkflowAgentsByTurn([...byTurn.values()]);
}

export function phaseLocalTurnNumber(
  agent: WorkflowAgent,
  phaseAgents: WorkflowAgent[],
): number | null {
  if (!agent.correlation_id) return null;
  const members = sessionMembersInPhase(phaseAgents, agent.name, agent.node_type);
  const index = members.findIndex((member) => member.id === agent.id);
  return index >= 0 ? index : null;
}

export function shouldShowSessionTree(
  agent: WorkflowAgent,
  phaseAgents: WorkflowAgent[],
): boolean {
  if (!isSessionNode(agent)) return false;
  return sessionMembersInPhase(phaseAgents, agent.name, agent.node_type).length >= 1;
}

/**
 * 将 phase agents 分为 session 树与 one-shot 节点。
 * 每个 agent_session / human_session 组成为一棵树（即使只有一个 turn）。
 */
export function groupWorkflowAgentsByName(agents: WorkflowAgent[]): {
  sessions: Array<{ label: string; members: WorkflowAgent[] }>;
  oneShots: WorkflowAgent[];
} {
  const bySessionKey = new Map<string, WorkflowAgent[]>();
  const oneShots: WorkflowAgent[] = [];

  for (const agent of agents) {
    const key = sessionGroupKey(agent);
    if (!key) {
      oneShots.push(agent);
      continue;
    }
    const existing = bySessionKey.get(key) ?? [];
    existing.push(agent);
    bySessionKey.set(key, existing);
  }

  const sessions: Array<{ label: string; members: WorkflowAgent[] }> = [];
  for (const members of bySessionKey.values()) {
    const sorted = sortWorkflowAgentsByTurn(members);
    sessions.push({ label: sorted[0]?.name ?? 'session', members: sorted });
  }

  return { sessions, oneShots };
}

// ── 子工作流（child phase） ───────────────────────────────

export function childPhasesOf(workflow: WorkflowRun, parent: WorkflowPhase): WorkflowPhase[] {
  return (workflow.phases ?? []).filter(
    (phase) => phase.phase_type === 'child' && phase.parent_phase === parent.name,
  );
}

// ── 增量合并 ──────────────────────────────────────────────

function preferHumanPrompt(existing?: string, incoming?: string): string | undefined {
  const left = existing?.trim();
  const right = incoming?.trim();
  if (!left) return right || undefined;
  if (!right) return left;
  return right.length > left.length ? right : left;
}

function mergeWorkflowAgent(
  existing: WorkflowAgent | undefined,
  incoming: WorkflowAgent,
): WorkflowAgent {
  const reassembled = reassembleAgentFieldParts(incoming);
  // get_agent returns the full body (heavy text fields present) — clear
  // detail_pending so views know no further fetch is needed. get_phase returns
  // a summary (no heavy fields) — keep detail_pending:true.
  const incomingHasHeavy =
    reassembled.prompt !== undefined ||
    reassembled.outcome !== undefined ||
    reassembled.human_prompt !== undefined ||
    reassembled.human_reply !== undefined ||
    reassembled.error !== undefined ||
    reassembled.activity !== undefined;
  const detailPending = incomingHasHeavy ? false : reassembled.detail_pending ?? false;
  // Full body arrived — drop the now-stale summary previews.
  const clearPreviews = incomingHasHeavy;
  const result: WorkflowAgent = {
    ...existing,
    ...reassembled,
    activity: reassembled.activity ?? existing?.activity,
    human_prompt: preferHumanPrompt(existing?.human_prompt, reassembled.human_prompt),
    detail_pending: detailPending,
  };
  if (clearPreviews) {
    delete result.outcome_preview;
    delete result.error_preview;
  }
  return result;
}

function mergeWorkflowPhase(
  existing: WorkflowPhase | undefined,
  incoming: WorkflowPhase,
): WorkflowPhase {
  const incomingHasAgents = Object.prototype.hasOwnProperty.call(incoming, 'agents');
  if (!incomingHasAgents) {
    return { ...existing, ...incoming, agents: existing?.agents };
  }
  const existingAgents = existing?.agents ?? [];
  const mergedAgents = [...existingAgents];

  for (const incomingAgent of incoming.agents ?? []) {
    const index = mergedAgents.findIndex((agent) => agent.id === incomingAgent.id);
    const nextAgent = mergeWorkflowAgent(
      index === -1 ? undefined : mergedAgents[index],
      incomingAgent,
    );
    if (index === -1) {
      mergedAgents.push(nextAgent);
    } else {
      mergedAgents[index] = nextAgent;
    }
  }

  return { ...existing, ...incoming, agents: mergedAgents };
}

export function mergeWorkflowRun(
  existing: WorkflowRun | undefined,
  incoming: WorkflowRun,
): WorkflowRun {
  // Script-edit relaunch: the backend reset the phase/agent tree (stale cards
  // from the prior run were dropped). Replace the phases outright instead of
  // incrementally merging, or the old agents would survive.
  if (incoming.relaunch_kind === 'relaunch') {
    return {
      ...existing,
      ...incoming,
      phases: Array.isArray(incoming.phases) ? incoming.phases : [],
    };
  }
  const existingPhases = existing?.phases ?? [];
  const mergedPhases = [...existingPhases];
  const incomingLogs = Array.isArray(incoming.logs)
    ? incoming.logs.filter((log): log is string => typeof log === 'string')
    : undefined;

  const incomingPhases = Array.isArray(incoming.phases) ? incoming.phases : [];
  for (const incomingPhase of incomingPhases) {
    const index = mergedPhases.findIndex((phase) => phase.id === incomingPhase.id);
    const nextPhase = mergeWorkflowPhase(
      index === -1 ? undefined : mergedPhases[index],
      incomingPhase,
    );
    if (index === -1) {
      mergedPhases.push(nextPhase);
    } else {
      mergedPhases[index] = nextPhase;
    }
  }

  const merged: WorkflowRun = { ...existing, ...incoming, phases: mergedPhases };
  if (incomingLogs) {
    const incomingHasPhases = Object.prototype.hasOwnProperty.call(incoming, 'phases');
    merged.logs =
      existing && !incomingHasPhases ? [...(existing.logs ?? []), ...incomingLogs] : incomingLogs;
  } else if (existing?.logs && !Object.prototype.hasOwnProperty.call(incoming, 'logs')) {
    merged.logs = existing.logs;
  }
  if (Object.prototype.hasOwnProperty.call(incoming, 'detail_pending')) {
    merged.detail_pending = incoming.detail_pending;
  }
  if (Object.prototype.hasOwnProperty.call(incoming, 'has_more')) {
    merged.has_more = incoming.has_more;
  }
  if (Object.prototype.hasOwnProperty.call(incoming, 'phase_total')) {
    merged.phase_total = incoming.phase_total;
  }

  return merged;
}

export function normalizeWorkflowRun(workflow: WorkflowRun): WorkflowRun {
  return {
    ...workflow,
    logs: Array.isArray(workflow.logs)
      ? workflow.logs.filter((log): log is string => typeof log === 'string')
      : undefined,
    phases: Array.isArray(workflow.phases)
      ? workflow.phases.map((phase) => ({
          ...phase,
          agents: Array.isArray(phase.agents)
            ? phase.agents.map((agent) =>
                reassembleAgentFieldParts({
                  ...agent,
                  activity: Array.isArray(agent.activity)
                    ? agent.activity.filter(
                        (activity): activity is WorkflowAgentActivity =>
                          Boolean(
                            activity && typeof activity === 'object' && !Array.isArray(activity),
                          ),
                      )
                    : undefined,
                }),
              )
            : phase.agents,
        }))
      : workflow.phases,
  };
}

export function applyWorkflowUpdate(
  workflows: WorkflowRun[],
  incoming: WorkflowRun,
): WorkflowRun[] {
  const index = workflows.findIndex((workflow) => workflow.id === incoming.id);
  if (index === -1) {
    return [normalizeWorkflowRun(incoming), ...workflows];
  }
  return workflows.map((workflow, itemIndex) =>
    itemIndex === index ? normalizeWorkflowRun(mergeWorkflowRun(workflow, incoming)) : workflow,
  );
}

// ── 统计工具 ──────────────────────────────────────────────

export function countWaitingForHuman(workflow: WorkflowRun): number {
  let n = 0;
  for (const phase of workflow.phases ?? []) {
    for (const agent of phase.agents ?? []) {
      if (agent.status === 'waiting_for_human') n += 1;
    }
  }
  return n;
}

export function collectWaitingForHuman(workflow: WorkflowRun): {
  phase: WorkflowPhase;
  agent: WorkflowAgent;
}[] {
  const out: { phase: WorkflowPhase; agent: WorkflowAgent }[] = [];
  for (const phase of workflow.phases ?? []) {
    for (const agent of phase.agents ?? []) {
      if (agent.status === 'waiting_for_human') out.push({ phase, agent });
    }
  }
  return out;
}

export function findWorkflowAgent(
  workflows: WorkflowRun[],
  workflowId: string,
  agentId: string,
): { workflow: WorkflowRun; phase: WorkflowPhase; agent: WorkflowAgent } | null {
  const workflow = workflows.find((item) => item.id === workflowId);
  if (!workflow) return null;
  for (const phase of workflow.phases ?? []) {
    const agent = (phase.agents ?? []).find((item) => item.id === agentId);
    if (agent) return { workflow, phase, agent };
  }
  return null;
}

// ── 循环检测工具 ──────────────────────────────────────────

export interface AgentLoopGroup {
  /** agent 名称（循环内同名） */
  name: string;
  /** 各迭代的 agent，按时间排序 */
  members: WorkflowAgent[];
}

export interface PhaseLoopGroup {
  /** phase 基础名（去掉数字后缀） */
  baseName: string;
  /** 各迭代的 phase，按序号排序 */
  members: WorkflowPhase[];
}

/**
 * 场景 A：检测同名 agent 循环。
 *
 * 当一个 phase 内有多个同名（非 session）agent 时，
 * 它们来自 for 循环的不同迭代。
 */
export function detectAgentLoops(agents: WorkflowAgent[]): {
  loops: AgentLoopGroup[];
  unique: WorkflowAgent[];
} {
  const byName = new Map<string, WorkflowAgent[]>();
  for (const agent of agents) {
    if (isSessionNode(agent)) continue;
    const existing = byName.get(agent.name) ?? [];
    existing.push(agent);
    byName.set(agent.name, existing);
  }
  const loops: AgentLoopGroup[] = [];
  const unique: WorkflowAgent[] = [];
  for (const [name, members] of byName) {
    if (members.length > 1) {
      loops.push({ name, members: sortWorkflowAgentsByTurn(members) });
    } else {
      unique.push(members[0]);
    }
  }
  return { loops, unique };
}

const PHASE_NUM_SUFFIX = /^(.+?)[_\-](\d+)$/;

/** Extract the loop base name: strip ``_N`` / ``-N`` suffix, or use the name as-is. */
function phaseBaseName(phase: WorkflowPhase): string {
  const match = phase.name.match(PHASE_NUM_SUFFIX);
  return match ? match[1] : phase.name;
}

/** Sort key for loop members: prefer ``iteration`` field, fall back to suffix number. */
function phaseIterationKey(phase: WorkflowPhase): number {
  if (phase.iteration != null) return phase.iteration;
  const m = phase.name.match(PHASE_NUM_SUFFIX);
  return m ? parseInt(m[2], 10) : 0;
}

/**
 * 检测 phase 循环（loop-aware）。
 *
 * 两种模式均支持：
 * 1. **iteration 字段**（新）：同名 phase 带 ``iteration`` 字段（1, 2, 3…），
 *    后端按 ``(title, iteration)`` 不合并，前端按 name 分组。
 * 2. **数字后缀**（旧兼容）：phase 名形如 ``review_0`` / ``review_1``。
 */
export function detectPhaseLoops(phases: WorkflowPhase[]): {
  loops: PhaseLoopGroup[];
  unique: WorkflowPhase[];
} {
  const byBase = new Map<string, WorkflowPhase[]>();
  for (const phase of phases) {
    const base = phaseBaseName(phase);
    const existing = byBase.get(base) ?? [];
    existing.push(phase);
    byBase.set(base, existing);
  }
  const loops: PhaseLoopGroup[] = [];
  const loopedIds = new Set<string>();
  for (const [baseName, members] of byBase) {
    if (members.length > 1) {
      members.sort((a, b) => phaseIterationKey(a) - phaseIterationKey(b));
      loops.push({ baseName, members });
      members.forEach((m) => loopedIds.add(m.id));
    }
  }
  const unique = phases.filter((p) => !loopedIds.has(p.id));
  return { loops, unique };
}

/**
 * 按"实际执行顺序"重排 phases。
 *
 * 后端在 ``_on_workflow_started`` 按 META phases 列表 pre-create planned
 * 卡片（占了数组前 N 位），运行时新建的 phase（不在 META 里，如
 * ``对抗验证``、``回归验收-held-in``）被 append 到末尾，导致显示顺序
 * 与执行顺序不一致。
 *
 * 修复策略：有 agent 的 phase 按第一个 agent 的 ``started_at`` 排序，
 * 空 planned 卡片（无 agent）排到末尾。child phases 紧跟其 parent。
 */
export function sortPhasesByExecution(phases: WorkflowPhase[]): WorkflowPhase[] {
  const topLevel = phases.filter((p) => p.phase_type !== 'child');
  const children = phases.filter((p) => p.phase_type === 'child');

  const withAgents = topLevel.filter((p) => p.agents && p.agents.length > 0);
  const withoutAgents = topLevel.filter((p) => !p.agents || p.agents.length === 0);

  withAgents.sort((a, b) => {
    const aTime = a.agents?.[0]?.started_at ?? '';
    const bTime = b.agents?.[0]?.started_at ?? '';
    return aTime.localeCompare(bTime);
  });

  const sortedTop = [...withAgents, ...withoutAgents];

  // 把 child phases 插回各自 parent 之后
  if (children.length === 0) return sortedTop;
  const result = [...sortedTop];
  for (const child of children) {
    const parentIdx = result.findIndex(
      (p) => p.name === child.parent_phase || p.id === child.parent_phase,
    );
    if (parentIdx >= 0) {
      result.splice(parentIdx + 1, 0, child);
    } else {
      result.push(child);
    }
  }
  return result;
}

/**
 * 计算循环迭代的整体状态。
 * 优先级：running > waiting_for_human > failed > pending > planned > completed > stopped
 */
export function computeLoopStatus<T extends { status: WorkflowStatus }>(
  members: T[],
): WorkflowStatus {
  if (members.length === 0) return 'planned';
  const statuses = members.map((m) => m.status);
  if (statuses.includes('running')) return 'running';
  if (statuses.includes('paused')) return 'paused';
  if (statuses.includes('waiting_for_human')) return 'waiting_for_human';
  if (statuses.includes('failed')) return 'failed';
  if (statuses.includes('pending')) return 'pending';
  if (statuses.includes('planned')) return 'planned';
  if (statuses.every((s) => s === 'completed')) return 'completed';
  if (statuses.every((s) => s === 'stopped')) return 'stopped';
  return 'running';
}

/**
 * 找到当前应展开的迭代索引（最新的非 completed 迭代，或最后一个）。
 */
export function findActiveIterationIndex<T extends { status: WorkflowStatus }>(
  members: T[],
): number {
  for (let i = members.length - 1; i >= 0; i--) {
    const s = members[i].status;
    if (s === 'running' || s === 'paused' || s === 'waiting_for_human' || s === 'pending' || s === 'planned') {
      return i;
    }
  }
  return members.length - 1;
}
