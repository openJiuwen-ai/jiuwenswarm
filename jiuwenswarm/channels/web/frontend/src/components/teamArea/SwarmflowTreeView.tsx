/**
 * SwarmFlow 树视图组件。
 *
 * 将 WorkflowRun → Phase → Agent 树结构渲染为可折叠的缩进树或组织架构图。
 * 参考 TUI 的 workflows.ts 渲染逻辑，适配 web 端 Tailwind 样式。
 *
 * 功能：
 * - 缩进树 / 组织架构图 两种布局可切换
 * - 状态图标、进度条、模型信息
 * - Session 节点可展开显示各轮次（Turn 0/1/2...）
 * - 点击 waiting_for_human agent 重新打开 ask-user 对话框
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ChevronDown,
  ChevronRight,
  CircleCheck,
  CircleX,
  CircleDot,
  Circle,
  Square,
  Smile,
  Loader2,
  Pause,
  Play,
  RefreshCw,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  type WorkflowRun,
  type WorkflowPhase,
  type WorkflowAgent,
  type WorkflowStatus,
  formatBudgetK,
  groupWorkflowAgentsByName,
  childPhasesOf,
  findWorkflowAgent,
  countWaitingForHuman,
  parseTurnFromCorrelationId,
  detectAgentLoops,
  detectPhaseLoops,
  sortPhasesByExecution,
  computeLoopStatus,
  computeSessionStatus,
  findActiveIterationIndex,
} from './workflowTypes';
import { useChatStore } from '../../stores/chatStore';
import { useSessionStore } from '../../stores/sessionStore';
import { webRequest } from '../../services/webClient';
import type { AskUserQuestionPayload, WebError } from '../../types/websocket';
import {
  AgentDetailModal,
  buildDetailSections,
  formatCharCount,
  accentChipClass,
  type AgentModalState,
} from './AgentDetailModal';
import { Toast } from '../ConnectorMarket/Toast';

// ── 状态图标映射 ──────────────────────────────────────────

function StatusIcon({ status, className }: { status: WorkflowStatus; className?: string }) {
  const cls = className ?? 'w-4 h-4 shrink-0';
  switch (status) {
    case 'completed':
      return <CircleCheck className={`${cls} text-emerald-500`} />;
    case 'failed':
      return <CircleX className={`${cls} text-red-500`} />;
    case 'running':
      return <Loader2 className={`${cls} text-blue-500 animate-spin`} />;
    case 'paused':
      return <Pause className={`${cls} text-violet-500`} />;
    case 'pending':
    case 'planned':
      return <CircleDot className={`${cls} text-gray-400`} />;
    case 'stopped':
      return <Square className={`${cls} text-gray-400`} />;
    case 'waiting_for_human':
      return <Smile className={`${cls} text-amber-500`} />;
    default:
      return <Circle className={`${cls} text-gray-400`} />;
  }
}

// 控制 RPC 失败时服务端带回的权威 status → toast 文案 key 映射。
// 服务端在 controller miss 时会把 run 的真实状态（终态/暂停态）附在失败
// payload 里，前端据此提示并纠正陈旧卡片。
const CONTROL_STATUS_KEY: Record<string, string> = {
  completed: 'swarmflow.controlAlreadyCompleted',
  stopped: 'swarmflow.controlAlreadyStopped',
  failed: 'swarmflow.controlAlreadyFailed',
  paused: 'swarmflow.controlAlreadyPaused',
  running: 'swarmflow.controlUnavailable',
};

// ── 状态文本 ──────────────────────────────────────────────

function statusText(status: WorkflowStatus, t: (k: string) => string): string {
  const map: Record<WorkflowStatus, string> = {
    planned: t('swarmflow.statusPlanned'),
    pending: t('swarmflow.statusPending'),
    running: t('swarmflow.statusRunning'),
    paused: t('swarmflow.statusPaused'),
    completed: t('swarmflow.statusCompleted'),
    failed: t('swarmflow.statusFailed'),
    stopped: t('swarmflow.statusStopped'),
    waiting_for_human: t('swarmflow.statusWaitingForHuman'),
  };
  return map[status] ?? status;
}

// ── 进度条 ────────────────────────────────────────────────

function ProgressBar({ completed, total }: { completed: number; total: number }) {
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0;
  return (
    <div className="flex items-center gap-2 shrink-0">
      <div className="w-16 h-1.5 rounded-full bg-secondary overflow-hidden">
        <div
          className="h-full rounded-full bg-blue-500 transition-all"
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-xs text-text-muted tabular-nums">
        {completed}/{total} ({pct}%)
      </span>
    </div>
  );
}

// ── 迭代进度条 ────────────────────────────────────────────

function statusDotColor(status: WorkflowStatus): string {
  switch (status) {
    case 'completed': return 'bg-emerald-500';
    case 'running': return 'bg-blue-500 animate-pulse';
    case 'paused': return 'bg-violet-500';
    case 'failed': return 'bg-red-500';
    case 'waiting_for_human': return 'bg-amber-500';
    case 'pending':
    case 'planned': return 'bg-gray-400';
    case 'stopped': return 'bg-gray-600';
    default: return 'bg-gray-400';
  }
}

function IterationStrip<T extends { status: WorkflowStatus }>({
  members,
  selectedIndex,
  onSelect,
}: {
  members: T[];
  selectedIndex: number;
  onSelect: (index: number) => void;
}) {
  return (
    <div className="flex items-center gap-1 shrink-0">
      {members.map((member, i) => (
        <button
          key={i}
          type="button"
          className={`w-2.5 h-2.5 rounded-full transition-all hover:scale-125 ${statusDotColor(member.status)} ${
            i === selectedIndex ? 'ring-2 ring-blue-400 ring-offset-1 ring-offset-card' : ''
          }`}
          title={`第${i + 1}轮 · ${member.status}`}
          data-testid="team-area-swarmflow-iteration-dot"
          data-variant={i}
          onClick={(e) => {
            e.stopPropagation();
            onSelect(i);
          }}
        />
      ))}
    </div>
  );
}

// ── Agent 循环节点（场景 A：同名 agent 迭代） ─────────────

function AgentLoopNode({
  name,
  members,
  depth,
  runId,
  sessionId,
}: {
  name: string;
  members: WorkflowAgent[];
  depth: number;
  runId: string;
  sessionId: string;
}) {
  const [expanded, setExpanded] = useState(true);
  const [selectedIdx, setSelectedIdx] = useState(() => findActiveIterationIndex(members));
  const loopStatus = useMemo(() => computeLoopStatus(members), [members]);
  const completedCount = members.filter(
    (m) => m.status === 'completed' || m.status === 'failed' || m.status === 'stopped',
  ).length;

  return (
    <div>
      {/* 循环头部 */}
      <div
        className="flex items-center gap-2 px-2 py-1.5 rounded-md hover:bg-secondary/50 transition-colors cursor-pointer"
        style={{ paddingLeft: `${depth * 20 + 8}px` }}
        onClick={() => setExpanded((v) => !v)}
        data-testid="team-area-swarmflow-loop-header"
        data-variant={name}
      >
        {expanded ? (
          <ChevronDown className="w-4 h-4 text-text-muted shrink-0" />
        ) : (
          <ChevronRight className="w-4 h-4 text-text-muted shrink-0" />
        )}
        <RefreshCw className={`w-4 h-4 shrink-0 text-purple-500 ${loopStatus === 'running' ? 'animate-spin' : ''}`} />
        <span className="text-sm font-medium text-text break-words">{name}</span>
        <span className="text-xs text-text-muted shrink-0">×{members.length}轮</span>
        <span className="text-xs text-text-muted shrink-0">
          {completedCount}/{members.length}
        </span>
        <IterationStrip
          members={members}
          selectedIndex={selectedIdx}
          onSelect={setSelectedIdx}
        />
      </div>

      {/* 展开内容 */}
      {expanded && (
        <div>
          {members.map((member, i) =>
            i === selectedIdx ? (
              <AgentNode
                key={member.id}
                agent={member}
                phaseAgents={members}
                depth={depth + 1}
                runId={runId}
                sessionId={sessionId}
              />
            ) : (
              <div
                key={member.id}
                className="flex items-center gap-2 px-2 py-1 rounded-md hover:bg-secondary/30 transition-colors cursor-pointer"
                style={{ paddingLeft: `${(depth + 1) * 20 + 8}px` }}
                onClick={(e) => {
                  e.stopPropagation();
                  setSelectedIdx(i);
                }}
                data-testid="team-area-swarmflow-iteration-row"
                data-variant={i}
              >
                <div className="w-[18px] shrink-0" />
                <StatusIcon status={member.status} className="w-3.5 h-3.5" />
                <span className="text-xs text-text-muted">
                  第{i + 1}轮
                </span>
                {member.outcome && (
                  <span className="text-xs text-text-muted/70 truncate flex-1">
                    {member.outcome}
                  </span>
                )}
              </div>
            ),
          )}
        </div>
      )}
    </div>
  );
}

// ── Phase 循环节点（场景 B：前缀+数字后缀 phase） ──────────

function PhaseLoopNode({
  baseName,
  members,
  workflow,
  depth,
  runId,
  sessionId,
}: {
  baseName: string;
  members: WorkflowPhase[];
  workflow: WorkflowRun;
  depth: number;
  runId: string;
  sessionId: string;
}) {
  const [expanded, setExpanded] = useState(true);
  const [selectedIdx, setSelectedIdx] = useState(() => findActiveIterationIndex(members));
  const loopStatus = useMemo(() => computeLoopStatus(members), [members]);
  const completedCount = members.filter(
    (m) => m.status === 'completed' || m.status === 'failed' || m.status === 'stopped',
  ).length;

  return (
    <div>
      <div
        className="flex items-center gap-2 px-2 py-1.5 rounded-md hover:bg-secondary/50 transition-colors cursor-pointer"
        style={{ paddingLeft: `${depth * 20 + 8}px` }}
        onClick={() => setExpanded((v) => !v)}
        data-testid="team-area-swarmflow-loop-header"
        data-variant={baseName}
      >
        {expanded ? (
          <ChevronDown className="w-4 h-4 text-text-muted shrink-0" />
        ) : (
          <ChevronRight className="w-4 h-4 text-text-muted shrink-0" />
        )}
        <RefreshCw className={`w-4 h-4 shrink-0 text-purple-500 ${loopStatus === 'running' ? 'animate-spin' : ''}`} />
        <span className="text-sm font-medium text-text break-words">{baseName}</span>
        <span className="text-xs text-text-muted shrink-0">×{members.length}轮</span>
        <span className="text-xs text-text-muted shrink-0">
          {completedCount}/{members.length}
        </span>
        <IterationStrip
          members={members}
          selectedIndex={selectedIdx}
          onSelect={setSelectedIdx}
        />
      </div>

      {expanded && (
        <div>
          {members.map((phase, i) =>
            i === selectedIdx ? (
              <PhaseNode
                key={phase.id}
                phase={phase}
                workflow={workflow}
                depth={depth + 1}
                runId={runId}
                sessionId={sessionId}
              />
            ) : (
              <div
                key={phase.id}
                className="flex items-center gap-2 px-2 py-1 rounded-md hover:bg-secondary/30 transition-colors cursor-pointer"
                style={{ paddingLeft: `${(depth + 1) * 20 + 8}px` }}
                onClick={(e) => {
                  e.stopPropagation();
                  setSelectedIdx(i);
                }}
                data-testid="team-area-swarmflow-iteration-row"
                data-variant={i}
              >
                <div className="w-[18px] shrink-0" />
                <StatusIcon status={phase.status} className="w-3.5 h-3.5" />
                <span className="text-xs text-text-muted">
                  第{phase.iteration ?? i + 1}轮
                </span>
                <span className="text-xs text-text-muted/70 shrink-0">
                  {phase.completed_agent_count ??
                    (phase.agents ?? []).filter(
                      (a) =>
                        a.status === 'completed' ||
                        a.status === 'failed' ||
                        a.status === 'stopped',
                    ).length}/
                  {phase.agent_count ?? (phase.agents ?? []).length}
                </span>
              </div>
            ),
          )}
        </div>
      )}
    </div>
  );
}

// ── Agent 节点 ────────────────────────────────────────────

function AgentNode({
  agent,
  phaseAgents,
  depth,
  runId,
  sessionId,
}: {
  agent: WorkflowAgent;
  phaseAgents: WorkflowAgent[];
  depth: number;
  runId: string;
  sessionId: string;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(agent.status === 'running');
  const [showDetail, setShowDetail] = useState(false);
  const [modalState, setModalState] = useState<AgentModalState | null>(null);

  // 汇总当前 agent 可用的详情 section（标签与弹窗共用同一数据源）
  const detailSections = useMemo(() => buildDetailSections(agent), [agent]);

  const isSession = agent.node_type === 'agent_session' || agent.node_type === 'human_session';
  const sessionMembers = useMemo(
    () =>
      isSession
        ? groupWorkflowAgentsByName(phaseAgents).sessions.find(
            (s) => s.label === agent.name,
          )?.members ?? []
        : [],
    [isSession, agent.name, phaseAgents],
  );
  const hasSessionTree = isSession && sessionMembers.length >= 1;

  // detail_pending=true 表示该 agent 仅含摘要（get_phase 下发），prompt/outcome 等
  // 大文本字段尚未拉取——展开详情或弹窗时按需调 get_agent 补全完整体（通用，不限 human）。
  const agentDetailPending = agent.detail_pending === true;
  const ensureAgentDetail = useCallback(async () => {
    if (!agentDetailPending) return;
    const store = useSessionStore.getState();
    const lookup = findWorkflowAgent(
      store.runtimes[sessionId]?.workflowRuns ?? [],
      runId,
      agent.id,
    );
    if (lookup) {
      await store
        .loadAgentDetail(sessionId, runId, lookup.phase.id, agent.id)
        .catch(() => undefined);
    }
  }, [agentDetailPending, agent.id, runId, sessionId]);

  // 详情展开时懒拉完整 agent 主体（get_agent）。
  useEffect(() => {
    if (!showDetail || !agentDetailPending) return;
    void ensureAgentDetail();
  }, [showDetail, agentDetailPending, ensureAgentDetail]);

  const handleAgentClick = useCallback(() => {
    if (agent.status !== 'waiting_for_human') return;
    void (async () => {
      let question = agent.human_prompt?.trim();
      if (!question) {
        // Phase summary 可能未携带 prompt——按需拉单个 agent 完整体（get_agent）。
        await ensureAgentDetail();
        const refreshed = findWorkflowAgent(
          useSessionStore.getState().runtimes[sessionId]?.workflowRuns ?? [],
          runId,
          agent.id,
        );
        question = refreshed?.agent.human_prompt?.trim();
      }
      const corr = agent.correlation_id ?? agent.id;
      const payload: AskUserQuestionPayload = {
        request_id: `swarmflow:${runId}:${corr}`,
        source: 'swarmflow_human',
        questions: [
          {
            question: question || '(SwarmFlow is waiting for your input)',
            header: agent.name,
            options: [],
            multi_select: false,
          },
        ],
        swarmflowMeta: {
          run_id: runId,
          correlation_id: corr,
          agent_id: agent.id,
          agent_name: agent.name,
        },
      };
      useChatStore.getState().enqueuePendingQuestion(sessionId, payload);
    })();
  }, [agent, runId, sessionId, ensureAgentDetail]);

  // 刷新页面等场景会清空 pendingQuestions（后端 chat.ask_user_question 只在状态
  // 首次跳转时推一次，无快照回放）——节点挂载时若已处于 waiting_for_human，按
  // 当前状态补一次弹窗，语义等同用户手动点击该行。
  useEffect(() => {
    handleAgentClick();
  }, [agent.status]);

  return (
    <div>
      <div
        className="flex items-center gap-2 px-2 py-1 rounded-md hover:bg-secondary/50 transition-colors"
        style={{ paddingLeft: `${depth * 20 + 8}px` }}
        onClick={handleAgentClick}
        role={agent.status === 'waiting_for_human' ? 'button' : undefined}
        title={agent.status === 'waiting_for_human' ? t('swarmflow.clickToReply') : undefined}
        data-testid="team-area-swarmflow-agent-row"
        data-variant={agent.id}
      >
        {hasSessionTree && (
          <button
            type="button"
            className="shrink-0 p-0.5 rounded hover:bg-secondary"
            onClick={(e) => {
              e.stopPropagation();
              setExpanded((v) => !v);
            }}
          >
            {expanded ? (
              <ChevronDown className="w-3.5 h-3.5 text-text-muted" />
            ) : (
              <ChevronRight className="w-3.5 h-3.5 text-text-muted" />
            )}
          </button>
        )}
        {!hasSessionTree && <div className="w-[18px] shrink-0" />}
        <StatusIcon status={agent.status} />
        <span className="text-sm text-text break-words flex-1" data-testid="team-area-swarmflow-agent-name" data-variant={agent.id}>{agent.name}</span>
        {agent.model && (
          <span className="text-xs text-text-muted shrink-0">{agent.model}</span>
        )}
        {/* 展开 input/output 详情：完整体已就绪（有 prompt/outcome/error）或仅为摘要待拉取时均显示 */}
        {(agent.prompt || agent.outcome || agent.error || agentDetailPending) && (
          <button
            type="button"
            className="shrink-0 p-0.5 rounded hover:bg-secondary"
            data-testid="team-area-swarmflow-agent-detail-toggle"
            data-variant={agent.id}
            onClick={(e) => {
              e.stopPropagation();
              setShowDetail((v) => !v);
            }}
          >
            {showDetail ? (
              <ChevronDown className="w-3 h-3 text-text-muted" />
            ) : (
              <ChevronRight className="w-3 h-3 text-text-muted" />
            )}
          </button>
        )}
        {agent.status === 'waiting_for_human' && (
          <span className="text-xs text-amber-500 shrink-0 animate-pulse" data-testid="team-area-swarmflow-agent-waiting-hint">
            {t('swarmflow.clickToReply')}
          </span>
        )}
      </div>

      {/* Input / Output 详情：子标题标签，点击弹窗查看 */}
      {showDetail && detailSections.length > 0 && (
        <div
          className="mx-2 mb-1 flex flex-wrap items-center gap-1.5 rounded-lg border border-border/50 bg-card/50 px-2 py-1.5"
          style={{ marginLeft: `${depth * 20 + 28}px` }}
          data-testid="team-area-swarmflow-agent-detail-chips"
          data-variant={agent.id}
        >
          {/* Meta 信息 */}
          {(agent.model || agent.token_count != null || agent.duration_ms != null || agent.started_at) && (
            <div className="flex items-center gap-2 text-[10px] text-text-muted/70 mr-auto pr-2">
              {agent.model && <span className="font-mono">{agent.model}</span>}
              {agent.token_count != null && <span>{agent.token_count} tok</span>}
              {agent.duration_ms != null && <span>{(agent.duration_ms / 1000).toFixed(1)}s</span>}
              {agent.started_at && <span>{new Date(agent.started_at).toLocaleTimeString()}</span>}
            </div>
          )}

          {detailSections.map((sec) => (
            <button
              key={sec.key}
              type="button"
              aria-label={`${sec.label} (${formatCharCount(sec.content)} 字符)`}
              data-testid="team-area-swarmflow-agent-detail-chip"
              data-variant={sec.key}
              onClick={(e) => {
                e.stopPropagation();
                setModalState({ sections: detailSections, activeKey: sec.key });
              }}
              className={`shrink-0 inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] font-medium transition-colors ${accentChipClass[sec.accent]}`}
            >
              <span>{sec.icon} {sec.label}</span>
              <span className="ml-0.5 px-1 rounded bg-black/10 dark:bg-white/10 text-[9px] tabular-nums opacity-80">
                {formatCharCount(sec.content)}
              </span>
            </button>
          ))}
        </div>
      )}

      {/* 内容弹窗：标题含 Agent 名 + Tab 切换不同 section（共享组件） */}
      <AgentDetailModal
        state={modalState}
        agentName={agent.name}
        onClose={() => setModalState(null)}
        onTabChange={(key) => setModalState((prev) => (prev ? { ...prev, activeKey: key } : prev))}
      />

      {/* Outcome 副文本（详情展开时隐藏） */}
      {/* Outcome 副文本：完整体未就绪时用摘要预览兜底（恢复路径），展开详情时隐藏 */}
      {((agent.outcome || agent.outcome_preview) && !showDetail) && (
        <div
          className="flex items-start gap-1.5 px-2 pb-1 text-xs text-text-muted/60"
          style={{ paddingLeft: `${depth * 20 + 28}px` }}
          data-testid="team-area-swarmflow-agent-outcome-preview"
        >
          <span className="shrink-0 text-text-muted/40">└</span>
          <span className="min-w-0 flex-1 truncate">{agent.outcome ?? agent.outcome_preview}</span>
        </div>
      )}
      {((agent.error || agent.error_preview) && !showDetail) && (
        <div
          className="flex items-start gap-1.5 px-2 pb-1 text-xs text-red-400/80"
          style={{ paddingLeft: `${depth * 20 + 28}px` }}
          data-testid="team-area-swarmflow-agent-error-preview"
        >
          <span className="shrink-0 text-red-400/40">└</span>
          <span className="min-w-0 flex-1 truncate">{agent.error ?? agent.error_preview}</span>
        </div>
      )}

      {/* Session turns */}
      {hasSessionTree && expanded && (
        <div
          className="border-l border-border/30 ml-4"
          style={{ marginLeft: `${depth * 20 + 16}px` }}
          data-testid="team-area-swarmflow-agent-turns"
          data-variant={agent.id}
        >
          {sessionMembers.map((member) => {
            const turn = parseTurnFromCorrelationId(member.correlation_id);
            return (
              <div key={member.id} className="relative pl-4">
                <div className="absolute left-0 top-1/2 -translate-y-1/2 w-3 border-t border-border/30" />
                <div className="flex items-center gap-2 px-2 py-1 rounded-md hover:bg-secondary/50" data-testid="team-area-swarmflow-agent-turn" data-variant={member.id}>
                  <StatusIcon status={member.status} className="w-3.5 h-3.5" />
                  <span className="text-xs text-text-muted">
                    Turn {turn ?? '?'}
                  </span>
                  {member.outcome && (
                    <span className="text-xs text-text-muted/60 truncate flex-1">
                      {member.outcome}
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── Phase 节点 ────────────────────────────────────────────

function PhaseNode({
  phase,
  workflow,
  depth,
  runId,
  sessionId,
}: {
  phase: WorkflowPhase;
  workflow: WorkflowRun;
  depth: number;
  runId: string;
  sessionId: string;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(true);
  const { sessions, oneShots } = useMemo(
    () => groupWorkflowAgentsByName(phase.agents ?? []),
    [phase.agents],
  );
  // 检测同名 agent 循环（场景 A）
  const { loops: agentLoops, unique: uniqueAgents } = useMemo(
    () => detectAgentLoops(oneShots),
    [oneShots],
  );
  const childPhases = useMemo(
    () => childPhasesOf(workflow, phase),
    [workflow, phase],
  );
  // Prefer backend aggregate counters: a parent phase's agent_count already
  // includes its child phases' agents (same behavior as the TUI); fall back
  // to counting direct agents when the fields are absent.
  const completedCount =
    phase.completed_agent_count ??
    (phase.agents ?? []).filter(
      (a) => a.status === 'completed' || a.status === 'failed' || a.status === 'stopped',
    ).length;
  const totalCount = phase.agent_count ?? phase.agents?.length ?? 0;
  const hasChildren =
    sessions.length > 0 || uniqueAgents.length > 0 || agentLoops.length > 0 || childPhases.length > 0;

  // 历史恢复后 get_workflow 只给 phase 骨架（无 agents）——展开时按需拉完整 agents。
  const needsAgents = phase.agents === undefined && (phase.agent_count ?? 0) > 0;
  useEffect(() => {
    if (!expanded || !needsAgents) return;
    void useSessionStore
      .getState()
      .loadPhaseAgents(sessionId, runId, phase.id)
      .catch(() => undefined);
  }, [expanded, needsAgents, sessionId, runId, phase.id]);

  return (
    <div>
      <div
        className="flex items-center gap-2 px-2 py-1.5 rounded-md hover:bg-secondary/50 transition-colors cursor-pointer"
        style={{ paddingLeft: `${depth * 20 + 8}px` }}
        onClick={() => hasChildren && setExpanded((v) => !v)}
        data-testid="team-area-swarmflow-phase-header"
        data-variant={phase.id}
      >
        {hasChildren ? (
          expanded ? (
            <ChevronDown className="w-4 h-4 text-text-muted shrink-0" />
          ) : (
            <ChevronRight className="w-4 h-4 text-text-muted shrink-0" />
          )
        ) : (
          <div className="w-4 shrink-0" />
        )}
        <StatusIcon status={phase.status} />
        <span className="text-sm font-medium text-text break-words flex-1" data-testid="team-area-swarmflow-phase-name" data-variant={phase.id}>
          {phase.name}
        </span>
        {phase.phase_type === 'child' && (
          <span className="text-xs px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-500 shrink-0" data-testid="team-area-swarmflow-phase-sub-workflow-badge">
            {t('swarmflow.subWorkflow')}
          </span>
        )}
        <ProgressBar completed={completedCount} total={totalCount} />
      </div>

      {expanded && hasChildren && (
        <div
          className="border-l border-border/30 ml-4"
          style={{ marginLeft: `${depth * 20 + 16}px` }}
        >
          {/* 唯一 agent（非循环） */}
          {uniqueAgents.map((agent) => (
            <div key={agent.id} className="relative pl-4">
              <div className="absolute left-0 top-1/2 -translate-y-1/2 w-3 border-t border-border/30" />
              <AgentNode
                agent={agent}
                phaseAgents={phase.agents ?? []}
                depth={0}
                runId={runId}
                sessionId={sessionId}
              />
            </div>
          ))}
          {/* 同名 agent 循环（场景 A） */}
          {agentLoops.map((loop) => (
            <div key={`loop-${loop.name}`} className="relative pl-4">
              <div className="absolute left-0 top-1/2 -translate-y-1/2 w-3 border-t border-border/30" />
              <AgentLoopNode
                name={loop.name}
                members={loop.members}
                depth={0}
                runId={runId}
                sessionId={sessionId}
              />
            </div>
          ))}
          {/* Session groups — render one representative node that expands to show turns */}
          {sessions.map((session) => {
            const representative = session.members[0];
            if (!representative) return null;
            // 代表节点状态按 members 聚合：Turn0 完成不代表整张卡完成。
            const status = computeSessionStatus(session.members);
            const node =
              status === representative.status ? representative : { ...representative, status };
            return (
              <div key={representative.id} className="relative pl-4">
                <div className="absolute left-0 top-1/2 -translate-y-1/2 w-3 border-t border-border/30" />
                <AgentNode
                  agent={node}
                  phaseAgents={phase.agents ?? []}
                  depth={0}
                  runId={runId}
                  sessionId={sessionId}
                />
              </div>
            );
          })}
          {/* Child phases (sub-workflows) */}
          {childPhases.map((child) => (
            <div key={child.id} className="relative pl-4">
              <div className="absolute left-0 top-1/2 -translate-y-1/2 w-3 border-t border-border/30" />
              <PhaseNode
                phase={child}
                workflow={workflow}
                depth={0}
                runId={runId}
                sessionId={sessionId}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Run 根节点 ────────────────────────────────────────────

function RunNode({
  run,
  sessionId,
}: {
  run: WorkflowRun;
  sessionId: string;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(true);
  const [runDetail, setRunDetail] = useState<AgentModalState | null>(null);
  // Control request in-flight flag: clicking pause/resume/stop immediately
  // spins and disables all three buttons until run.status flips to the target
  // state (progress event arrives). Backend abort can take seconds; a blocked
  // control channel longer (09-08 measured 80s before res returned, but the
  // event arrived with it). Disabling prevents repeat clicks (19:18:16: pause
  // then resume 4ms later, then 6 more resume clicks all ok=False). No timeout
  // fallback: buttons honor run.status only — if the event never arrives the
  // spinner stays. State follows events.
  const [pendingControl, setPendingControl] = useState<
    'pause' | 'resume' | 'stop' | null
  >(null);
  useEffect(() => {
    if (!pendingControl) return;
    if (pendingControl === 'pause' && run.status !== 'running') {
      setPendingControl(null);
    } else if (pendingControl === 'resume' && run.status === 'running') {
      setPendingControl(null);
    } else if (
      pendingControl === 'stop' &&
      (run.status === 'stopped' ||
        run.status === 'completed' ||
        run.status === 'failed')
    ) {
      setPendingControl(null);
    }
  }, [pendingControl, run.status]);
  const [controlError, setControlError] = useState<string | null>(null);
  // 控制 RPC 失败统一处理：服务端在 controller miss 时把权威 status 附在
  // 失败 payload 里带回，据此纠正停在 running/paused 的陈旧卡片并用 Toast
  // 明确提示——此前只 console.error，用户连点没反应还不知道原因
  // （17:06-17:07 对已完成 run 反复点暂停、前端只报 not found 的先例）。
  const applyControlFailure = useCallback(
    (err: unknown): boolean => {
      console.error('[swarmflow] control failed:', err);
      const webErr = err as WebError | undefined;
      const payload = webErr?.payload as
        | { status?: unknown }
        | undefined;
      const status = typeof payload?.status === 'string' ? payload.status : null;
      if (status && status !== run.status) {
        // 最小 delta：mergeWorkflowRun 只覆盖 incoming 携带的字段，
        // name/summary 等缺席字段保留 store 现值，不会用旧渲染快照回退。
        useSessionStore.getState().applyWorkflowUpdate(sessionId, {
          id: run.id,
          status: status as WorkflowStatus,
        } as WorkflowRun);
      }
      // 传输层错误（超时/断连，code 非空）没有权威 payload：如实显示其自带
      // 文案（如"请求超时"），不误报"未找到工作流运行"（16:0x 断连窗口点
      // resume 弹 controlNotFound 的先例）。返回是否服务端权威失败，供 pause
      // 在途态决定复位——传输层错误时转圈保持到事件到达，状态以事件为准。
      const transport = webErr?.code !== undefined;
      setControlError(
        status
          ? t(CONTROL_STATUS_KEY[status] || 'swarmflow.controlNotFound')
          : transport && webErr?.message
            ? webErr.message
            : t('swarmflow.controlNotFound'),
      );
      return !transport;
    },
    [run.id, run.status, sessionId, t],
  );
  const completedCount =
    run.completed_agent_count ??
    (run.phases ?? []).reduce(
      (sum, p) =>
        sum +
        (p.agents ?? []).filter(
          (a) => a.status === 'completed' || a.status === 'failed' || a.status === 'stopped',
        ).length,
      0,
    );
  const totalCount =
    run.agent_count ??
    (run.phases ?? []).reduce((sum, p) => sum + (p.agents ?? []).length, 0);
  const waitingCount = countWaitingForHuman(run);

  // 检测 phase 循环（场景 B：前缀+数字后缀）
  const { loops: phaseLoops, unique: uniquePhases } = useMemo(() => {
    const sorted = sortPhasesByExecution(run.phases ?? []);
    const topLevel = sorted.filter((p) => p.phase_type !== 'child');
    return detectPhaseLoops(topLevel);
  }, [run.phases]);

  // Exhaustion hint source: explicit failure scope first, then the ledgers.
  // A completed run can still finish over budget — its in-flight calls were
  // settled after the limit was crossed — so the pill must not be gated on
  // status === 'failed'. Session exhaustion is surfaced only when a team
  // ceiling is configured (run.budget.total != null).
  const budgetExhaustedScope =
    run.budget_exhausted_scope ??
    (run.workflow_budget?.exhausted
      ? 'workflow'
      : run.budget?.total != null && run.budget?.exhausted
        ? 'session'
        : null);
  // 机械按钮只在 team 活着（方块亮）时可用：灰飞机 = 无 leader harness 可宿主 run；
  // recovered = 冷启动后 controller 无票据。两种情况恢复都只能由 Leader 经 ask_user 裁决。
  const teamRunning = useChatStore((s) => s.runtimes[sessionId]?.isProcessing ?? false);
  const controlsDisabled = !teamRunning || run.recovered === true;
  // Two reasons to disable: team asleep / cold start (grey, not-allowed) or a
  // control request in flight (spinner, wait cursor). The in-flight look wins
  // while a request is pending.
  const controlBtnClass =
    `flex items-center justify-center w-7 h-7 rounded text-text-muted hover:bg-secondary transition-colors disabled:hover:bg-transparent disabled:hover:text-text-muted ${
      pendingControl !== null ? 'disabled:opacity-70 disabled:cursor-wait' : 'disabled:opacity-40 disabled:cursor-not-allowed'
    }`;

  return (
    <div className="border border-border rounded-lg overflow-hidden bg-card/50" data-testid="team-area-swarmflow-run" data-variant={run.id}>
      <div
        className="flex items-center gap-2 px-3 py-2.5 bg-secondary/30 cursor-pointer hover:bg-secondary/50 transition-colors"
        onClick={() => setExpanded((v) => !v)}
        data-testid="team-area-swarmflow-run-header"
        data-variant={run.id}
      >
        {expanded ? (
          <ChevronDown className="w-4 h-4 text-text-muted shrink-0" />
        ) : (
          <ChevronRight className="w-4 h-4 text-text-muted shrink-0" />
        )}
        <StatusIcon status={run.status} className="w-4 h-4 shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-text-strong truncate" data-testid="team-area-swarmflow-run-name" data-variant={run.id}>
              {run.name}
            </span>
            <span className="text-xs text-text-muted shrink-0" data-testid="team-area-swarmflow-run-status" data-variant={run.id}>
              {statusText(run.status, t)}
            </span>
          </div>
          {run.summary && (
            <p className="text-xs text-text-muted truncate mt-0.5" data-testid="team-area-swarmflow-run-summary" data-variant={run.id}>{run.summary}</p>
          )}
        </div>
        {waitingCount > 0 && (
          <span className="text-xs px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-500 shrink-0 animate-pulse" data-testid="team-area-swarmflow-run-waiting-badge" data-variant={run.id}>
            {t('swarmflow.waitingForHuman', { count: waitingCount })}
          </span>
        )}
        <ProgressBar completed={completedCount} total={totalCount} />
        {run.budget && (
          <span
            className={`text-xs shrink-0 tabular-nums px-1.5 py-0.5 rounded-full ${
              run.budget.exhausted
                ? 'bg-red-500/10 text-red-500'
                : 'bg-blue-500/10 text-blue-500'
            }`}
            title={t('swarmflow.sessionBudget')}
            data-testid="team-area-swarmflow-run-budget"
            data-variant="session"
          >
            {t('swarmflow.sessionBudgetShort')} {formatBudgetK(run.budget)}
            {run.budget.total == null && ` · ${t('swarmflow.budgetUnlimited')}`}
          </span>
        )}
        {run.workflow_budget && (
          <span
            className={`text-xs shrink-0 tabular-nums px-1.5 py-0.5 rounded-full ${
              run.workflow_budget.exhausted
                ? 'bg-red-500/10 text-red-500'
                : 'bg-green-500/10 text-green-500'
            }`}
            title={t('swarmflow.runBudget')}
          >
            {t('swarmflow.runBudgetShort')} {formatBudgetK(run.workflow_budget)}
            {run.workflow_budget.total == null && ` · ${t('swarmflow.budgetUnlimited')}`}
          </span>
        )}
        {budgetExhaustedScope && (
          <span className="text-xs px-2 py-0.5 rounded-full bg-red-500/10 text-red-500 shrink-0">
            {t(
              budgetExhaustedScope === 'session'
                ? 'swarmflow.budgetExhaustedSession'
                : 'swarmflow.budgetExhaustedWorkflow',
            )}
          </span>
        )}
        {(run.status === 'running' || run.status === 'paused') && (
          <div
            className="flex items-center gap-1 shrink-0"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              type="button"
              title={t(controlsDisabled ? 'swarmflow.controlsDisabledHint' : 'swarmflow.pauseResumeHint')}
              className={`${controlBtnClass} hover:text-amber-500`}
              disabled={controlsDisabled || pendingControl !== null}
              data-testid="team-area-swarmflow-run-pause-btn"
              data-variant={run.status === 'running' ? 'pause' : 'resume'}
              onClick={() => {
                if (run.status !== 'running') {
                  setPendingControl('resume');
                  void webRequest('swarmflow.resume', {
                    session_id: sessionId,
                    run_id: run.id,
                  }).catch((err) => {
                    if (applyControlFailure(err)) {
                      setPendingControl(null);
                    }
                  });
                  return;
                }
                setPendingControl('pause');
                void webRequest('swarmflow.pause', {
                  session_id: sessionId,
                  run_id: run.id,
                }).catch((err) => {
                  if (applyControlFailure(err)) {
                    setPendingControl(null);
                  }
                });
              }}
            >
              {pendingControl === 'pause' || pendingControl === 'resume' ? (
                <Loader2 className="w-4 h-4 animate-spin text-amber-500" />
              ) : run.status === 'running' ? (
                <Pause className="w-4 h-4" />
              ) : (
                <Play className="w-4 h-4" />
              )}
            </button>
            <button
              type="button"
              title={t(controlsDisabled ? 'swarmflow.controlsDisabledHint' : 'swarmflow.stopHint')}
              className={`${controlBtnClass} hover:text-red-500`}
              disabled={controlsDisabled || pendingControl !== null}
              data-testid="team-area-swarmflow-run-stop-btn"
              onClick={() => {
                setPendingControl('stop');
                void webRequest('swarmflow.stop', { session_id: sessionId, run_id: run.id }).catch(
                  (err) => {
                    if (applyControlFailure(err)) {
                      setPendingControl(null);
                    }
                  },
                );
              }}
            >
              {pendingControl === 'stop' ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin text-red-500" />
              ) : (
                <Square className="w-3.5 h-3.5" />
              )}
            </button>
          </div>
        )}
      </div>

      {controlError && (
        <Toast message={controlError} onClose={() => setControlError(null)} variant="error" />
      )}
      {run.error && (
        <button
          type="button"
          onClick={() =>
            setRunDetail({
              sections: [
                { key: 'error', label: '错误', icon: '✕', content: run.error!, accent: 'red' },
              ],
              activeKey: 'error',
            })
          }
          className="flex w-full items-start gap-1.5 px-3 py-1.5 text-left text-xs text-red-400/90 hover:bg-red-500/5"
          data-testid="team-area-swarmflow-run-error"
        >
          <CircleX className="w-3.5 h-3.5 shrink-0 mt-0.5 text-red-400/70" />
          <span className="flex-1 min-w-0 truncate">{run.error}</span>
        </button>
      )}
      {run.result && (
        <button
          type="button"
          onClick={() =>
            setRunDetail({
              sections: [
                { key: 'result', label: '结果', icon: '✓', content: run.result!, accent: 'emerald' },
              ],
              activeKey: 'result',
            })
          }
          className="flex w-full items-start gap-1.5 px-3 py-1.5 text-left text-xs text-text-muted hover:bg-emerald-500/5"
          data-testid="team-area-swarmflow-run-result"
        >
          <CircleCheck className="w-3.5 h-3.5 shrink-0 mt-0.5 text-emerald-500/70" />
          <span className="flex-1 min-w-0 truncate">{run.result}</span>
        </button>
      )}

      {expanded && (
        <div className="py-1 border-l border-border/30 ml-4">
          {/* 唯一 phase（非循环） */}
          {uniquePhases.map((phase) => (
            <div key={phase.id} className="relative pl-4">
              <div className="absolute left-0 top-1/2 -translate-y-1/2 w-3 border-t border-border/30" />
              <PhaseNode
                phase={phase}
                workflow={run}
                depth={0}
                runId={run.id}
                sessionId={sessionId}
              />
            </div>
          ))}
          {/* Phase 循环（场景 B：前缀+数字后缀） */}
          {phaseLoops.map((loop) => (
            <div key={`loop-${loop.baseName}`} className="relative pl-4">
              <div className="absolute left-0 top-1/2 -translate-y-1/2 w-3 border-t border-border/30" />
              <PhaseLoopNode
                baseName={loop.baseName}
                members={loop.members}
                workflow={run}
                depth={0}
                runId={run.id}
                sessionId={sessionId}
              />
            </div>
          ))}
        </div>
      )}

      <AgentDetailModal
        state={runDetail}
        agentName={run.name}
        onClose={() => setRunDetail(null)}
        onTabChange={(key) => setRunDetail((prev) => (prev ? { ...prev, activeKey: key } : prev))}
      />
    </div>
  );
}

// ── 主组件 ────────────────────────────────────────────────

export interface SwarmflowTreeViewProps {
  runs: WorkflowRun[];
  sessionId: string;
}

export function SwarmflowTreeView({ runs, sessionId }: SwarmflowTreeViewProps) {
  const { t } = useTranslation();

  if (runs.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-sm text-text-muted" data-testid="team-area-swarmflow-tree-empty">
        {t('swarmflow.noWorkflow')}
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full" data-testid="team-area-swarmflow-tree-view">
      <div className="flex-1 overflow-auto">
        <div className="flex flex-col gap-2 p-2">
          {runs.map((run) => (
            <RunNode key={run.id} run={run} sessionId={sessionId} />
          ))}
        </div>
      </div>
    </div>
  );
}
