export type WorkflowStatus =
  | "planned"
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "stopped"
  | "paused"
  | "waiting_for_human";

export interface WorkflowAgentActivity {
  timestamp: string;
  type: "tool_call" | "tool_result";
  content: string;
}

export type WorkflowNodeType = "agent" | "agent_session" | "human" | "human_session";

export interface WorkflowBudget {
  total: number | null;
  spent: number;
  remaining: number | null;
  scope: "leader" | "session" | "workflow";
  exhausted: boolean;
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
  /** Reassembled from ``prompt_parts`` when the field exceeds the wire part limit. */
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
  kind?: "agent" | "human";
  /** Exact SwarmFlow primitive type when emitted by the backend. */
  node_type?: WorkflowNodeType;
  /** ``{phase}:{label}:{turn}`` on ``agent_session`` / ``human_session`` (and ``human()`` one-shots); absent on plain ``agent()``. */
  correlation_id?: string;
  human_prompt?: string;
  human_prompt_parts?: WorkflowAgentPart[];
  human_reply?: string;
  human_reply_parts?: WorkflowAgentPart[];
  activity_parts?: WorkflowAgentPart[];
  /** True on get_phase summaries — full body fetched on demand via get_agent. */
  detail_pending?: boolean;
}

export interface WorkflowPhase {
  id: string;
  name: string;
  description?: string;
  status: WorkflowStatus;
  agent_count?: number;
  completed_agent_count?: number;
  /** Absent on phase summaries from ``action=get_workflow`` (fetch via ``action=get_phase``). */
  agents?: WorkflowAgent[];
  /** "child" for sub-workflow cards, null/undefined for author phases. */
  phase_type?: "child" | null;
  /** Parent author phase name (set on child phase declarations). */
  parent_phase?: string | null;
  /** Phase summary only — full agents not yet fetched via ``action=get_phase``. */
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
  /** Leader-shared budget snapshot. This is deliberately not a per-run budget. */
  budget?: WorkflowBudget | null;
  /** Per-run ledger snapshot (META.workflow_token_limit); null when unset. */
  workflow_budget?: WorkflowBudget | null;
  /** Which ledger triggered a budget failure: "session" | "workflow". */
  budget_exhausted_scope?: "session" | "workflow" | null;
  /** "relaunch" = script-edit re-run (replace the phase tree); "resume" = normal pause→resume. */
  relaunch_kind?: "relaunch" | "resume" | null;
  /** Absent on list summaries from ``action=list`` (fetch via ``action=get_workflow``). */
  phases?: WorkflowPhase[];
  /** List summary only — full detail not yet fetched via ``action=get_workflow``. */
  detail_pending?: boolean;
  phase_total?: number;
  has_more?: boolean;
}

export interface WorkflowAgentLookup {
  workflow: WorkflowRun;
  phase: WorkflowPhase;
  agent: WorkflowAgent;
}

function trimCompactDecimal(value: string): string {
  return value.endsWith(".0") ? value.slice(0, -2) : value;
}

/** Compact a real token count for TUI rows without inventing missing usage. */
export function formatTokenCount(value?: number | null): string | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
  if (value < 1_000) return `${Math.round(value)}`;
  if (value < 1_000_000) return `${trimCompactDecimal((value / 1_000).toFixed(1))}k`;
  return `${trimCompactDecimal((value / 1_000_000).toFixed(1))}m`;
}

export function workflowBudgetUsedPercent(budget?: WorkflowBudget | null): number | null {
  if (!budget || typeof budget.total !== "number" || budget.total <= 0) return null;
  if (!Number.isFinite(budget.spent)) return null;
  return Math.round((budget.spent / budget.total) * 100);
}

export function isWorkflowBudgetLow(budget?: WorkflowBudget | null): boolean {
  return Boolean(
    budget &&
    budget.exhausted !== true &&
    typeof budget.total === "number" &&
    budget.total > 0 &&
    typeof budget.remaining === "number" &&
    budget.remaining / budget.total <= 0.2,
  );
}

export function isWorkflowBudgetExhausted(
  workflow: Pick<WorkflowRun, "status" | "budget" | "error" | "budget_exhausted_scope">,
): boolean {
  return workflowBudgetExhaustedScope(workflow) !== null;
}

/**
 * Which ledger ran dry: "workflow" (per-run, retryable by revising the
 * workflow), "session" (team-wide, not retryable), or null when neither
 * ceiling was crossed. Not gated on the run status: a run whose ceiling was
 * crossed mid-flight can still finish "completed" (the rail force-finishes
 * in-flight agents and the engine's gate only blocks *new* agents), so the
 * ledgers themselves are the source of truth. Session wins when both are
 * dry (terminal, not retryable — same priority as the rail). Prefers the
 * structured ``budget_exhausted_scope`` field; falls back to the ledgers'
 * ``exhausted`` flags, then to the legacy error-text signal (older backends
 * only had the team-wide ledger).
 */
export function workflowBudgetExhaustedScope(
  workflow: Pick<
    WorkflowRun,
    "status" | "budget" | "workflow_budget" | "error" | "budget_exhausted_scope"
  >,
): "session" | "workflow" | null {
  if (workflow.budget_exhausted_scope === "workflow") return "workflow";
  if (workflow.budget_exhausted_scope === "session") return "session";
  if (workflow.budget?.exhausted === true) return "session";
  if (workflow.workflow_budget?.exhausted === true) return "workflow";
  // Legacy fallback (failed runs only — error text is the sole signal there).
  if (workflow.error && /budget exhausted/i.test(workflow.error)) return "session";
  return null;
}

export function formatWorkflowBudgetInline(budget?: WorkflowBudget | null): string | null {
  if (!budget) return null;
  const spent = formatTokenCount(budget.spent);
  if (!spent) return null;
  const total = formatTokenCount(budget.total);
  return total ? `team ${spent}/${total}` : `team spent ${spent} · unbounded`;
}

export function formatWorkflowBudgetDetail(budget?: WorkflowBudget | null): string | null {
  if (!budget) return null;
  const spent = formatTokenCount(budget.spent);
  if (!spent) return null;
  const total = formatTokenCount(budget.total);
  if (!total) return `Team budget spent ${spent} (unbounded)`;
  const percent = workflowBudgetUsedPercent(budget);
  return `Team budget ${spent}/${total}${percent === null ? "" : ` (${percent}%)`}`;
}

export function formatWorkflowRunBudgetInline(budget?: WorkflowBudget | null): string | null {
  if (!budget) return null;
  const spent = formatTokenCount(budget.spent);
  if (!spent) return null;
  const total = formatTokenCount(budget.total);
  return total ? `run ${spent}/${total}` : `run spent ${spent} · unbounded`;
}

export function formatWorkflowRunBudgetDetail(budget?: WorkflowBudget | null): string | null {
  if (!budget) return null;
  const spent = formatTokenCount(budget.spent);
  if (!spent) return null;
  const total = formatTokenCount(budget.total);
  if (!total) return `Run budget spent ${spent} (unbounded)`;
  const percent = workflowBudgetUsedPercent(budget);
  return `Run budget ${spent}/${total}${percent === null ? "" : ` (${percent}%)`}`;
}

/** Single-width “human waiting” marker (text symbol — not emoji 👤/🧑). */
export const WAITING_FOR_HUMAN_ICON = "☺";

/** Model/kind label for workflow agent rows — human nodes use ``human(model)`` form. */
export function formatWorkflowAgentKindLabel(agent: {
  kind?: WorkflowAgent["kind"];
  model?: string;
}): string {
  if (agent.kind === "human") {
    return agent.model ? `human(${agent.model})` : "human";
  }
  return agent.model ?? "";
}

/** Placeholder when a human turn completed via journal cache (no HUMAN_PROMPT / HUMAN_REPLIED). */
export const HUMAN_TURN_CACHED_QUESTION = "(cached, prompt not replayed)";
export const HUMAN_TURN_CACHED_ANSWER = "(cached, reply not replayed)";

/** Human turn replayed from journal — Q/A fields stay empty; show placeholders instead of faking history. */
export function isHumanTurnCached(agent: WorkflowAgent): boolean {
  return (
    agent.kind === "human" &&
    agent.status === "completed" &&
    !agent.human_prompt &&
    !agent.human_reply
  );
}

export function workflowStatusIcon(status: WorkflowStatus): string {
  switch (status) {
    case "planned":
      return "◇";
    case "completed":
      return "✓";
    case "failed":
      return "×";
    case "running":
      return "◐";
    case "pending":
      return "○";
    case "stopped":
      return "■";
    case "paused":
      return "‖";
    case "waiting_for_human":
      return WAITING_FOR_HUMAN_ICON;
  }
}

/** Fixed user-facing status lines — avoid showing raw engine narration (e.g. result payload). */
export const WORKFLOW_STATUS_BANNER: Partial<Record<WorkflowStatus, string>> = {
  running: "Workflow running",
  paused: "Workflow paused",
  completed: "Workflow completed",
  stopped: "Workflow stopped",
};

export function runningWorkflowsBannerText(count: number): string {
  if (count <= 0) return "";
  return count === 1 ? "1 workflow running" : `${count} workflows running`;
}

export function pausedWorkflowsBannerText(count: number): string {
  if (count <= 0) return "";
  return count === 1 ? "1 workflow paused" : `${count} workflows paused`;
}

/** Format an ISO timestamp for workflow started-at display (local time). */
export function formatWorkflowLocalTime(iso?: string): string {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return "—";
  const date = new Date(ms);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  const seconds = String(date.getSeconds()).padStart(2, "0");
  return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
}

export function formatWorkflowStartedText(workflow: WorkflowRun): string {
  return `started ${formatWorkflowLocalTime(workflow.started_at)}`;
}

function formatDurationMs(durationMs: number): string {
  if (durationMs < 1000) {
    return `${Math.round(durationMs)}ms`;
  }
  const totalSeconds = Math.floor(durationMs / 1000);
  if (totalSeconds >= 60) {
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes}m ${seconds}s`;
  }
  return `${(durationMs / 1000).toFixed(1)}s`;
}

/** Elapsed or total runtime — never a completed-at timestamp. */
export function formatWorkflowRunningTime(workflow: WorkflowRun, now = Date.now()): string {
  if (
    typeof workflow.duration_ms === "number" &&
    Number.isFinite(workflow.duration_ms) &&
    workflow.duration_ms >= 0 &&
    workflow.status !== "running" &&
    workflow.status !== "pending" &&
    workflow.status !== "planned"
  ) {
    return formatDurationMs(workflow.duration_ms);
  }
  const startedMs = Date.parse(workflow.started_at ?? "");
  if (!Number.isFinite(startedMs)) return "—";
  if (workflow.completed_at && workflow.status !== "running") {
    const completedMs = Date.parse(workflow.completed_at);
    if (Number.isFinite(completedMs)) {
      return formatDurationMs(Math.max(0, completedMs - startedMs));
    }
  }
  return formatDurationMs(Math.max(0, now - startedMs));
}

export function formatWorkflowDurationLabel(status: WorkflowStatus): string {
  switch (status) {
    case "completed":
      return "completed";
    case "failed":
      return "failed";
    case "stopped":
      return "stopped";
    case "paused":
      return "paused";
    case "running":
    case "pending":
    case "planned":
    default:
      return "running";
  }
}

export function formatWorkflowRunningText(workflow: WorkflowRun, now = Date.now()): string {
  return `${formatWorkflowDurationLabel(workflow.status)} ${formatWorkflowRunningTime(workflow, now)}`;
}

export function formatWorkflowTimingText(workflow: WorkflowRun, now = Date.now()): string {
  return `${formatWorkflowStartedText(workflow)} · ${formatWorkflowRunningText(workflow, now)}`;
}

export function workflowStatusBannerText(status: WorkflowStatus): string | null {
  return WORKFLOW_STATUS_BANNER[status] ?? null;
}

/** Child sub-workflow cards belonging to an author phase. */
export function childPhasesOf(workflow: WorkflowRun, parent: WorkflowPhase): WorkflowPhase[] {
  return (workflow.phases ?? []).filter(
    (phase) => phase.phase_type === "child" && phase.parent_phase === parent.name,
  );
}

/** Flat phase list for ↑/↓ selection — parent rows then indented child rows. */
export interface WorkflowPhaseSelectEntry {
  phaseId: string;
  name: string;
  status: WorkflowStatus;
  completed: number;
  total: number;
  isChild: boolean;
}

export function workflowPhaseSelectEntries(workflow: WorkflowRun): WorkflowPhaseSelectEntry[] {
  const childrenByParent = new Map<string, WorkflowPhase[]>();
  const orderedParents: WorkflowPhase[] = [];
  for (const phase of workflow.phases ?? []) {
    if (phase.phase_type === "child") {
      const parentName = phase.parent_phase || "";
      if (!childrenByParent.has(parentName)) childrenByParent.set(parentName, []);
      childrenByParent.get(parentName)!.push(phase);
    } else {
      orderedParents.push(phase);
    }
  }

  const entries: WorkflowPhaseSelectEntry[] = [];
  const appendPhase = (phase: WorkflowPhase, isChild: boolean) => {
    entries.push({
      phaseId: phase.id,
      name: phase.name,
      status: phase.status,
      completed: phase.completed_agent_count ?? 0,
      total: phase.agent_count ?? 0,
      isChild,
    });
  };

  for (const parent of orderedParents) {
    appendPhase(parent, false);
    for (const child of childrenByParent.get(parent.name) ?? []) {
      appendPhase(child, true);
    }
  }

  for (const [parentName, children] of childrenByParent) {
    if (orderedParents.some((parent) => parent.name === parentName)) continue;
    for (const child of children) {
      appendPhase(child, true);
    }
  }

  return entries;
}

export function findWorkflowAgent(
  workflows: WorkflowRun[],
  workflowId: string,
  agentId: string,
): WorkflowAgentLookup | null {
  const workflow = workflows.find((item) => item.id === workflowId);
  if (!workflow) return null;
  for (const phase of workflow.phases ?? []) {
    const agent = (phase.agents ?? []).find((item) => item.id === agentId);
    if (agent) return { workflow, phase, agent };
  }
  return null;
}

export function normalizeWorkflowRun(workflow: WorkflowRun): WorkflowRun {
  return {
    ...workflow,
    logs: Array.isArray(workflow.logs)
      ? workflow.logs.filter((log): log is string => typeof log === "string")
      : undefined,
    phases: Array.isArray(workflow.phases)
      ? workflow.phases.map((phase) => ({
          ...phase,
          agents: Array.isArray(phase.agents)
            ? phase.agents.map((agent) => reassembleAgentFieldParts({
                ...agent,
                activity: Array.isArray(agent.activity)
                  ? agent.activity.filter(
                      (activity): activity is WorkflowAgentActivity =>
                        Boolean(
                          activity && typeof activity === "object" && !Array.isArray(activity),
                        ),
                    )
                  : undefined,
              }))
            : phase.agents,
        }))
      : workflow.phases,
  };
}

const SPLITTABLE_AGENT_FIELDS = [
  "prompt",
  "outcome",
  "human_prompt",
  "human_reply",
  "activity",
  "error",
] as const;

/** Reassemble ``{field}_parts`` arrays back into the base string field. */
export function reassembleAgentFieldParts(agent: WorkflowAgent): WorkflowAgent {
  let out = agent;
  for (const field of SPLITTABLE_AGENT_FIELDS) {
    const partsKey = `${field}_parts`;
    const parts = (out as unknown as Record<string, unknown>)[partsKey];
    if (!Array.isArray(parts) || parts.length === 0) continue;
    const sorted = [...parts].sort((a, b) => {
      const ai = (a as WorkflowAgentPart).part_idx;
      const bi = (b as WorkflowAgentPart).part_idx;
      return ai - bi;
    });
    const joined = sorted
      .map((p) => (p as WorkflowAgentPart).content ?? "")
      .join("");
    const next: WorkflowAgent = { ...out, [field]: joined } as WorkflowAgent;
    delete (next as unknown as Record<string, unknown>)[partsKey];
    out = next;
  }
  return out;
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
  return {
    ...existing,
    ...reassembled,
    activity: reassembled.activity ?? existing?.activity,
    human_prompt: preferHumanPrompt(existing?.human_prompt, reassembled.human_prompt),
    detail_pending: detailPending,
  };
}

export function mergeHumanPromptText(existing?: string, incoming?: string): string {
  return preferHumanPrompt(existing, incoming) ?? "";
}

function preferHumanPrompt(existing?: string, incoming?: string): string | undefined {
  const left = existing?.trim();
  const right = incoming?.trim();
  if (!left) return right || undefined;
  if (!right) return left;
  return right.length > left.length ? right : left;
}

function mergeWorkflowPhase(
  existing: WorkflowPhase | undefined,
  incoming: WorkflowPhase,
): WorkflowPhase {
  const incomingHasAgents = Object.prototype.hasOwnProperty.call(incoming, "agents");
  if (!incomingHasAgents) {
    // Phase summary (action=get_workflow) — keep existing agents, update meta only.
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

  return {
    ...existing,
    ...incoming,
    agents: mergedAgents,
  };
}

export function mergeWorkflowRun(
  existing: WorkflowRun | undefined,
  incoming: WorkflowRun,
): WorkflowRun {
  // Script-edit relaunch: the backend reset the phase/agent tree (stale cards
  // from the prior run were dropped). Replace the phases outright instead of
  // incrementally merging, or the old agents would survive.
  if (incoming.relaunch_kind === "relaunch") {
    return {
      ...existing,
      ...incoming,
      phases: Array.isArray(incoming.phases) ? incoming.phases : [],
    };
  }
  const existingPhases = existing?.phases ?? [];
  const mergedPhases = [...existingPhases];
  const incomingHasPhases = Object.prototype.hasOwnProperty.call(incoming, "phases");
  const incomingLogs = Array.isArray(incoming.logs)
    ? incoming.logs.filter((log): log is string => typeof log === "string")
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

  const merged: WorkflowRun = {
    ...existing,
    ...incoming,
    phases: mergedPhases,
  };
  if (incomingLogs) {
    merged.logs =
      existing && !incomingHasPhases ? [...(existing.logs ?? []), ...incomingLogs] : incomingLogs;
  } else if (existing?.logs && !Object.prototype.hasOwnProperty.call(incoming, "logs")) {
    merged.logs = existing.logs;
  }

  const detailLoaded = workflowHasAgentDetails(merged);
  if (detailLoaded) {
    delete merged.detail_pending;
  }
  if (Object.prototype.hasOwnProperty.call(incoming, "detail_pending")) {
    merged.detail_pending = incoming.detail_pending;
  }
  if (Object.prototype.hasOwnProperty.call(incoming, "has_more")) {
    merged.has_more = incoming.has_more;
  }
  if (Object.prototype.hasOwnProperty.call(incoming, "phase_total")) {
    merged.phase_total = incoming.phase_total;
  }

  return merged;
}

function workflowHasAgentDetails(workflow: WorkflowRun): boolean {
  for (const phase of workflow.phases ?? []) {
    if ((phase.agents?.length ?? 0) > 0) return true;
  }
  return false;
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

/** Whether the node was created by ``agent_session()`` or ``human_session()``. */
export function isSessionNode(agent: Pick<WorkflowAgent, "node_type">): boolean {
  return agent.node_type === "agent_session" || agent.node_type === "human_session";
}

/** Session History (``s``) is available only for explicit session primitives. */
export function canOpenSessionHistory(agent: Pick<WorkflowAgent, "node_type">): boolean {
  return isSessionNode(agent);
}

/** Group key for session nodes — label plus exact session primitive type. */
export function sessionGroupKey(agent: Pick<WorkflowAgent, "name" | "node_type">): string | null {
  if (!isSessionNode(agent)) return null;
  return `${agent.name}\0${agent.node_type}`;
}

/**
 * Reverse-parse the global turn index from a session correlation id
 * ``{phase}:{label}:{turn}`` (emitted on ``AGENT_STARTED`` for ``agent_session`` /
 * ``human_session``, and for ``human()`` one-shots). Returns null when the id is
 * absent or malformed (plain ``agent()`` nodes have no correlation id).
 */
export function parseTurnFromCorrelationId(correlationId?: string): number | null {
  if (!correlationId) return null;
  const parts = correlationId.split(":");
  const last = parts[parts.length - 1];
  if (last === undefined) return null;
  const turn = Number.parseInt(last, 10);
  return Number.isFinite(turn) ? turn : null;
}

/** Same-name session turns within one phase, filtered by session primitive type. */
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
  return sortWorkflowAgentsByTurn(members);
}

/**
 * Phase-local turn index (0-based) for session UI.
 *
 * ``correlation_id`` encodes a global session turn index (backend history length
 * across the whole workflow run). The TUI groups by name inside one phase, so
 * display uses the sorted index within that phase — not the global turn counter.
 */
export function phaseLocalTurnNumber(
  agent: WorkflowAgent,
  phaseAgents: WorkflowAgent[],
): number | null {
  if (!agent.correlation_id) return null;
  const members = sessionMembersInPhase(phaseAgents, agent.name, agent.node_type);
  const index = members.findIndex((member) => member.id === agent.id);
  return index >= 0 ? index : null;
}

/**
 * Whether the agents list should render a session parent row and turn child rows.
 *
 * Any ``agent_session`` / ``human_session`` node shows a tree (including a single
 * ``turn 0`` child) so the list is visually distinct from plain ``agent()`` /
 * ``human()``. Plain one-shots never form a tree.
 */
export function shouldShowSessionTree(
  agent: WorkflowAgent,
  phaseAgents: WorkflowAgent[],
): boolean {
  if (!isSessionNode(agent)) return false;
  return sessionMembersInPhase(phaseAgents, agent.name, agent.node_type).length >= 1;
}

/**
 * Whether detail titles, reply banners, and similar chrome should include turn.
 *
 * Plain ``agent()`` / ``human()`` never do. ``agent_session`` / ``human_session``
 * do — even when the current phase has only one turn (SF-TURN-02: still ``turn 0``).
 */
export function shouldShowTurnInDetailOrReply(agent: WorkflowAgent): boolean {
  return isSessionNode(agent);
}

/** Phase-local turn index for detail/reply/session history, or null for one-shots. */
export function sessionTurnLabelNumber(
  agent: WorkflowAgent,
  phaseAgents: WorkflowAgent[],
): number | null {
  if (!shouldShowTurnInDetailOrReply(agent)) return null;
  return phaseLocalTurnNumber(agent, phaseAgents);
}

/** @deprecated Use {@link shouldShowSessionTree} or {@link shouldShowTurnInDetailOrReply}. */
export function shouldShowAgentTurnLabel(
  agent: WorkflowAgent,
  phaseAgents: WorkflowAgent[],
): boolean {
  return shouldShowSessionTree(agent, phaseAgents);
}

/** @deprecated Use {@link sessionTurnLabelNumber}. */
export function agentTurnLabelNumber(
  agent: WorkflowAgent,
  phaseAgents: WorkflowAgent[],
): number | null {
  return sessionTurnLabelNumber(agent, phaseAgents);
}

/** Sort session nodes by global ``correlation_id`` turn, then ``started_at``. */
export function sortWorkflowAgentsByTurn(agents: WorkflowAgent[]): WorkflowAgent[] {
  return [...agents].sort((a, b) => {
    const turnA = parseTurnFromCorrelationId(a.correlation_id);
    const turnB = parseTurnFromCorrelationId(b.correlation_id);
    if (turnA !== null && turnB !== null) return turnA - turnB;
    return (a.started_at ?? "").localeCompare(b.started_at ?? "");
  });
}

/**
 * Split phase agents into session trees vs one-shot nodes.
 *
 * Every ``agent_session`` / ``human_session`` group becomes a tree (even with a
 * single turn) so lists stay visually distinct from plain ``agent()`` /
 * ``human()``. Plain one-shots never aggregate — even when labels repeat or a
 * ``correlation_id`` exists.
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
    sessions.push({ label: sorted[0]?.name ?? "session", members: sorted });
  }

  return { sessions, oneShots };
}

/** Pending-input banner: "M inputs waiting" (empty string when count <= 0). */
export function pendingInputsBannerText(count: number): string {
  if (count <= 0) return "";
  return count === 1 ? "1 input waiting" : `${count} inputs waiting`;
}

/** Main-chat hint when human nodes are waiting for reply. */
export function pendingHumanViewHint(key: string | null = "alt+h"): string {
  return key ? `${key} to view human inputs` : "use /swarmflows to view human inputs";
}

/** Count agents in a workflow run that are currently waiting for a human reply. */
export function countWaitingForHuman(workflow: WorkflowRun): number {
  let n = 0;
  for (const phase of workflow.phases ?? []) {
    for (const agent of phase.agents ?? []) {
      if (agent.status === "waiting_for_human") n += 1;
    }
  }
  return n;
}

/** Collect all waiting-for-human agents across a run, in phase+agent order. */
export function collectWaitingForHuman(workflow: WorkflowRun): {
  phase: WorkflowPhase;
  agent: WorkflowAgent;
}[] {
  const out: { phase: WorkflowPhase; agent: WorkflowAgent }[] = [];
  for (const phase of workflow.phases ?? []) {
    for (const agent of phase.agents ?? []) {
      if (agent.status === "waiting_for_human") out.push({ phase, agent });
    }
  }
  return out;
}
