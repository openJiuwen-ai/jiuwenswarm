import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ReactFlow as ReactFlowComponent,
  type Node,
  type Edge,
  type NodeProps,
  Handle,
  Position,
  Background,
  Controls,
  useNodesState,
  useEdgesState,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import dagre from '@dagrejs/dagre';
import {
  CircleCheck,
  CircleX,
  CircleDot,
  Circle,
  Square,
  Smile,
  Loader2,
  Pause,
  RefreshCw,
} from 'lucide-react';
import {
  type WorkflowRun,
  type WorkflowPhase,
  type WorkflowAgent,
  type WorkflowStatus,
  type PhaseLoopGroup,
  type AgentLoopGroup,
  formatBudgetK,
  groupWorkflowAgentsByName,
  childPhasesOf,
  detectAgentLoops,
  detectPhaseLoops,
  sortPhasesByExecution,
  computeLoopStatus,
  findActiveIterationIndex,
} from './workflowTypes';
import { useChatStore } from '../../stores/chatStore';
import { useSessionStore } from '../../stores/sessionStore';
import { useTranslation } from 'react-i18next';
import type { AskUserQuestionPayload } from '../../types/websocket';
import {
  AgentDetailModal,
  buildDetailSections,
  type AgentModalState,
  type DetailSectionKey,
} from './AgentDetailModal';

// ── 状态图标 ──────────────────────────────────────────────

function StatusIcon({
  status,
  className = 'w-4 h-4',
}: {
  status: WorkflowStatus;
  className?: string;
}) {
  switch (status) {
    case 'completed':
      return <CircleCheck className={`${className} text-emerald-500`} />;
    case 'running':
      return <Loader2 className={`${className} text-blue-500 animate-spin`} />;
    case 'paused':
      return <Pause className={`${className} text-violet-500`} />;
    case 'failed':
      return <CircleX className={`${className} text-red-500`} />;
    case 'waiting_for_human':
      return <Smile className={`${className} text-amber-500 animate-pulse`} />;
    case 'pending':
      return <CircleDot className={`${className} text-gray-400`} />;
    case 'planned':
      return <Circle className={`${className} text-gray-400`} />;
    case 'stopped':
      return <Square className={`${className} text-gray-500`} />;
    default:
      return <Circle className={`${className} text-gray-400`} />;
  }
}

function statusBorder(status: WorkflowStatus): string {
  switch (status) {
    case 'running': return 'border-blue-500/60';
    case 'paused': return 'border-violet-500/60';
    case 'completed': return 'border-emerald-500/40';
    case 'failed': return 'border-red-500/60';
    case 'waiting_for_human': return 'border-amber-500/60';
    case 'pending':
    case 'planned': return 'border-gray-500/30';
    case 'stopped': return 'border-gray-600/40';
    default: return 'border-gray-500/30';
  }
}

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

// ── 自定义节点 ────────────────────────────────────────────

const RunGraphNode = ({ data }: NodeProps) => {
  const { t } = useTranslation();
  const run = data.run as WorkflowRun;
  const completed =
    run.completed_agent_count ??
    (run.phases ?? []).reduce(
      (s, p) =>
        s +
        (p.agents ?? []).filter(
          (a) => a.status === 'completed' || a.status === 'failed' || a.status === 'stopped',
        ).length,
      0,
    );
  const total =
    run.agent_count ??
    (run.phases ?? []).reduce((s, p) => s + (p.agents ?? []).length, 0);
  const showBudget = Boolean(run.workflow_budget || (run.budget && run.budget.total != null));
  // Exhaustion hint source: explicit failure scope first, then the ledgers.
  // Session exhaustion is surfaced only when a team ceiling is configured
  // (run.budget.total != null).
  const budgetExhaustedScope =
    run.budget_exhausted_scope ??
    (run.workflow_budget?.exhausted
      ? 'workflow'
      : run.budget?.total != null && run.budget?.exhausted
        ? 'session'
        : null);

  return (
    <div className={`px-3 py-2 rounded-lg border-2 bg-card shadow-md min-w-[140px] ${statusBorder(run.status)}`}>
      <Handle type="source" position={Position.Right} className="!bg-border" />
      <div className="flex items-start gap-1.5">
        <StatusIcon status={run.status} className="w-4 h-4 shrink-0 mt-0.5" />
        <span className="text-xs font-semibold text-text-strong break-words min-w-0">{run.name}</span>
      </div>
      <div className="flex items-center gap-1.5 mt-1">
        <div className="flex-1 h-1 rounded-full bg-secondary overflow-hidden">
          <div className="h-full bg-blue-500 rounded-full" style={{ width: `${total > 0 ? (completed / total) * 100 : 0}%` }} />
        </div>
        <span className="text-[10px] text-text-muted tabular-nums shrink-0">{completed}/{total}</span>
      </div>
      {showBudget && (
        <div className="flex flex-wrap items-center gap-1 mt-0.5 text-[10px] tabular-nums">
          {run.budget && run.budget.total != null && (
            <span
              className={`px-1.5 rounded ${
                run.budget.exhausted
                  ? 'bg-red-500/10 text-red-500'
                  : 'bg-blue-500/10 text-blue-500'
              }`}
              title={t('swarmflow.sessionBudget')}
            >
              {t('swarmflow.sessionBudgetShort')} {formatBudgetK(run.budget)}
            </span>
          )}
          {run.workflow_budget && (
            <span
              className={`px-1.5 rounded ${
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
        </div>
      )}
      {budgetExhaustedScope && (
        <div className="mt-0.5 text-[10px] text-red-500">
          {t(
            budgetExhaustedScope === 'session'
              ? 'swarmflow.budgetExhaustedSession'
              : 'swarmflow.budgetExhaustedWorkflow',
          )}
        </div>
      )}
    </div>
  );
};

const PhaseGraphNode = ({ data }: NodeProps) => {
  const phase = data.phase as WorkflowPhase;
  // Prefer backend aggregate counters: a parent phase's agent_count already
  // includes its child phases' agents (same behavior as the TUI); fall back
  // to counting direct agents when the fields are absent.
  const completed =
    phase.completed_agent_count ??
    (phase.agents ?? []).filter(
      (a) => a.status === 'completed' || a.status === 'failed' || a.status === 'stopped',
    ).length;
  const total = phase.agent_count ?? phase.agents?.length ?? 0;

  return (
    <div className={`px-2.5 py-1.5 rounded-md border bg-card shadow-sm min-w-[110px] ${statusBorder(phase.status)}`}>
      <Handle type="target" position={Position.Left} className="!bg-border" />
      <Handle type="source" position={Position.Right} className="!bg-border" />
      <div className="flex items-start gap-1.5">
        <StatusIcon status={phase.status} className="w-3.5 h-3.5 shrink-0 mt-0.5" />
        <span className="text-xs font-medium text-text break-words min-w-0">{phase.name}</span>
      </div>
      <span className="text-[10px] text-text-muted tabular-nums">{completed}/{total}</span>
    </div>
  );
};

const AgentGraphNode = ({ data }: NodeProps) => {
  const agent = data.agent as WorkflowAgent;
  const hasDetail = !!(agent.prompt || agent.human_prompt || agent.human_reply || agent.outcome || agent.error);
  const interactive = hasDetail || agent.status === 'waiting_for_human';

  return (
    <div
      className={`px-2.5 py-1 rounded-md border bg-card shadow-sm min-w-[90px] ${statusBorder(agent.status)} ${interactive ? 'cursor-pointer' : ''} ${agent.status === 'waiting_for_human' ? 'ring-1 ring-amber-500/40' : ''}`}
      title={interactive ? '点击查看详情' : undefined}
    >
      <Handle type="target" position={Position.Left} className="!bg-border" />
      {agent.node_type !== 'human' && agent.node_type !== 'human_session' && (
        <Handle type="source" position={Position.Right} className="!bg-border" />
      )}
      <div className="flex items-start gap-1">
        <StatusIcon status={agent.status} className="w-3.5 h-3.5 shrink-0 mt-0.5" />
        <span className="text-xs text-text break-words min-w-0">{agent.name}</span>
      </div>
    </div>
  );
};

const LoopGraphNode = ({ data }: NodeProps) => {
  const loop = data.loop as PhaseLoopGroup;
  const activeIdx = findActiveIterationIndex(loop.members);
  const loopStatus = computeLoopStatus(loop.members);

  return (
    <div className="px-2.5 py-1.5 rounded-md border-2 border-purple-500/40 bg-purple-500/5 shadow-sm min-w-[100px]">
      <Handle type="target" position={Position.Left} className="!bg-border" />
      <Handle type="source" position={Position.Right} className="!bg-border" />
      <div className="flex items-start gap-1">
        <RefreshCw className={`w-3 h-3 text-purple-500 shrink-0 mt-0.5 ${loopStatus === 'running' ? 'animate-spin' : ''}`} />
        <span className="text-xs font-medium text-text break-words min-w-0">{loop.baseName}</span>
        <span className="text-[10px] text-text-muted shrink-0">×{loop.members.length}轮</span>
      </div>
      <div className="flex items-center gap-0.5 mt-0.5">
        {loop.members.map((m, i) => (
          <div
            key={i}
            className={`w-1.5 h-1.5 rounded-full ${statusDotColor(m.status)} ${i === activeIdx ? 'ring-1 ring-blue-400' : ''}`}
          />
        ))}
      </div>
    </div>
  );
};

const SessionGraphNode = ({ data }: NodeProps) => {
  const agent = data.agent as WorkflowAgent;
  const members = data.members as WorkflowAgent[];

  return (
    <div className="px-2.5 py-1 rounded-md border-2 border-indigo-500/40 bg-indigo-500/5 shadow-sm min-w-[90px]">
      <Handle type="target" position={Position.Left} className="!bg-border" />
      <Handle type="source" position={Position.Right} className="!bg-border" />
      <div className="flex items-start gap-1">
        <StatusIcon status={agent.status} className="w-3.5 h-3.5 shrink-0 mt-0.5" />
        <span className="text-xs font-medium text-text break-words min-w-0">{agent.name}</span>
      </div>
      <span className="text-[10px] text-text-muted">T{members.length}</span>
    </div>
  );
};

const nodeTypes = {
  runNode: RunGraphNode,
  phaseNode: PhaseGraphNode,
  agentNode: AgentGraphNode,
  loopNode: LoopGraphNode,
  sessionNode: SessionGraphNode,
};

// ── 数据转换：WorkflowRun[] → React Flow nodes/edges ──────

function workflowToGraph(
  runs: WorkflowRun[],
  sessionId: string,
): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  for (const run of runs) {
    // Run 节点
    nodes.push({
      id: run.id,
      type: 'runNode',
      data: { run, sessionId },
      position: { x: 0, y: 0 },
    });

    const topLevel = sortPhasesByExecution(run.phases ?? []).filter(
      (p) => p.phase_type !== 'child',
    );
    const { loops: phaseLoops } = detectPhaseLoops(topLevel);

    // 建立 phase.id → 所属 loop 的映射，便于按原始顺序判断该 phase 是否属于循环
    const phaseLoopByMemberId = new Map<string, PhaseLoopGroup>();
    for (const loop of phaseLoops) {
      for (const m of loop.members) {
        phaseLoopByMemberId.set(m.id, loop);
      }
    }
    const drawnPhaseLoops = new Set<string>();

    // 按原始 topLevel 顺序渲染：遇到 loop 成员时画 loop 节点（去重），否则画普通 phase 节点
    for (const phase of topLevel) {
      const loop = phaseLoopByMemberId.get(phase.id);
      if (loop) {
        if (drawnPhaseLoops.has(loop.baseName)) continue;
        drawnPhaseLoops.add(loop.baseName);

        const loopId = `${run.id}:loop:${loop.baseName}`;
        nodes.push({
          id: loopId,
          type: 'loopNode',
          data: { loop, run, sessionId },
          position: { x: 0, y: 0 },
        });
        edges.push({
          id: `e:${run.id}:${loopId}`,
          source: run.id,
          target: loopId,
          type: 'default',
        });

        // 当前迭代的 phase
        const activeIdx = findActiveIterationIndex(loop.members);
        const activePhase = loop.members[activeIdx];
        if (activePhase) {
          const phaseId = `${run.id}:${activePhase.id}`;
          nodes.push({
            id: phaseId,
            type: 'phaseNode',
            data: { phase: activePhase, run, sessionId },
            position: { x: 0, y: 0 },
          });
          edges.push({
            id: `e:${loopId}:${phaseId}`,
            source: loopId,
            target: phaseId,
            type: 'default',
            animated: activePhase.status === 'running',
          });
          addPhaseChildren(nodes, edges, activePhase, phaseId, run, sessionId);
        }
        continue;
      }

      // 普通 phase
      const phaseId = `${run.id}:${phase.id}`;
      nodes.push({
        id: phaseId,
        type: 'phaseNode',
        data: { phase, run, sessionId },
        position: { x: 0, y: 0 },
      });
      edges.push({
        id: `e:${run.id}:${phaseId}`,
        source: run.id,
        target: phaseId,
        type: 'default',
      });
      addPhaseChildren(nodes, edges, phase, phaseId, run, sessionId);
    }
  }

  return { nodes, edges };
}

function addPhaseChildren(
  nodes: Node[],
  edges: Edge[],
  phase: WorkflowPhase,
  phaseId: string,
  run: WorkflowRun,
  sessionId: string,
) {
  const { sessions } = groupWorkflowAgentsByName(phase.agents ?? []);
  const { loops: agentLoops } = detectAgentLoops(phase.agents ?? []);
  const childPhases = childPhasesOf(run, phase);

  // 建立 agent.id → 所属 agent loop 的映射
  const agentLoopByMemberId = new Map<string, AgentLoopGroup>();
  for (const loop of agentLoops) {
    for (const m of loop.members) agentLoopByMemberId.set(m.id, loop);
  }
  // 建立 agent.id → 所属 session group 的映射
  const sessionByMemberId = new Map<string, { label: string; members: WorkflowAgent[] }>();
  for (const session of sessions) {
    for (const m of session.members) sessionByMemberId.set(m.id, session);
  }
  const drawnAgentLoops = new Set<string>();
  const drawnSessions = new Set<string>();

  // 按原始 phase.agents 顺序渲染：session 成员画 session 节点（去重），loop 成员画 loop 节点（去重），否则画普通 agent 节点
  for (const agent of phase.agents ?? []) {
    // session 成员
    const session = sessionByMemberId.get(agent.id);
    if (session) {
      if (drawnSessions.has(session.label)) continue;
      drawnSessions.add(session.label);
      const representative = session.members[0];
      if (!representative) continue;
      const nodeId = `${phaseId}:${representative.id}`;
      nodes.push({
        id: nodeId,
        type: 'sessionNode',
        data: { agent: representative, members: session.members, run, sessionId },
        position: { x: 0, y: 0 },
      });
      edges.push({
        id: `e:${phaseId}:${nodeId}`,
        source: phaseId,
        target: nodeId,
        type: 'default',
        animated: representative.status === 'running',
      });
      continue;
    }

    // loop 成员
    const loop = agentLoopByMemberId.get(agent.id);
    if (loop) {
      if (drawnAgentLoops.has(loop.name)) continue;
      drawnAgentLoops.add(loop.name);
      const loopId = `${phaseId}:loop:${loop.name}`;
      const loopStatus = computeLoopStatus(loop.members);
      nodes.push({
        id: loopId,
        type: 'loopNode',
        data: { loop: { baseName: loop.name, members: loop.members }, run, sessionId },
        position: { x: 0, y: 0 },
      });
      edges.push({
        id: `e:${phaseId}:${loopId}`,
        source: phaseId,
        target: loopId,
        type: 'default',
        animated: loopStatus === 'running',
      });
      const activeIdx = findActiveIterationIndex(loop.members);
      const activeAgent = loop.members[activeIdx];
      if (activeAgent) {
        const agentId = `${loopId}:${activeAgent.id}`;
        nodes.push({
          id: agentId,
          type: 'agentNode',
          data: { agent: activeAgent, runId: run.id, sessionId },
          position: { x: 0, y: 0 },
        });
        edges.push({
          id: `e:${loopId}:${agentId}`,
          source: loopId,
          target: agentId,
          type: 'default',
          animated: activeAgent.status === 'running',
        });
      }
      continue;
    }

    // 普通 agent
    const agentId = `${phaseId}:${agent.id}`;
    nodes.push({
      id: agentId,
      type: 'agentNode',
      data: { agent, runId: run.id, sessionId },
      position: { x: 0, y: 0 },
    });
    edges.push({
      id: `e:${phaseId}:${agentId}`,
      source: phaseId,
      target: agentId,
      type: 'default',
      animated: agent.status === 'running',
    });
  }

  // 子 phase（子工作流）
  for (const child of childPhases) {
    const childId = `${phaseId}:${child.id}`;
    nodes.push({
      id: childId,
      type: 'phaseNode',
      data: { phase: child, run, sessionId },
      position: { x: 0, y: 0 },
    });
    edges.push({
      id: `e:${phaseId}:${childId}`,
      source: phaseId,
      target: childId,
      type: 'default',
      style: { strokeDasharray: '5 3' },
    });
    addPhaseChildren(nodes, edges, child, childId, run, sessionId);
  }
}

// ── Dagre 自动布局 ────────────────────────────────────────

function layoutWithDagre(nodes: Node[], edges: Edge[]): Node[] {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({
    rankdir: 'LR',
    nodesep: 80,
    ranksep: 140,
    marginx: 20,
    marginy: 20,
  });

  const nodeWidthMap: Record<string, number> = {
    runNode: 200,
    phaseNode: 180,
    agentNode: 170,
    loopNode: 170,
    sessionNode: 170,
  };

  for (const node of nodes) {
    const w = nodeWidthMap[node.type ?? 'agentNode'] ?? 180;
    g.setNode(node.id, { width: w, height: 80 });
  }
  for (const edge of edges) {
    g.setEdge(edge.source, edge.target);
  }

  dagre.layout(g);

  // dagre 同 rank 内会按"最小边交叉"重排节点，打乱定义顺序。
  // 这里按 nodes 原始顺序（即定义顺序）重排同 rank 的 y 坐标，保证从上到下符合脚本定义。
  const originalIndex = new Map<string, number>();
  nodes.forEach((n, i) => originalIndex.set(n.id, i));

  const rankBuckets = new Map<number, string[]>();
  for (const node of nodes) {
    const pos = g.node(node.id);
    if (!pos || pos.rank == null) continue;
    const r = pos.rank as number;
    if (!rankBuckets.has(r)) rankBuckets.set(r, []);
    rankBuckets.get(r)!.push(node.id);
  }

  for (const ids of rankBuckets.values()) {
    if (ids.length <= 1) continue;
    // 按 nodes 原始顺序排序
    ids.sort((a, b) => (originalIndex.get(a) ?? 0) - (originalIndex.get(b) ?? 0));
    const positions = ids.map((id) => g.node(id));
    const ys = positions.map((p) => p.y);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const count = ids.length;
    const step = count > 1 ? (maxY - minY) / (count - 1) : 0;
    ids.forEach((id, i) => {
      const pos = g.node(id);
      if (pos) pos.y = minY + step * i;
    });
  }

  return nodes.map((node) => {
    const pos = g.node(node.id);
    if (pos) {
      const w = nodeWidthMap[node.type ?? 'agentNode'] ?? 180;
      return {
        ...node,
        position: {
          x: pos.x - w / 2,
          y: pos.y - 40,
        },
      };
    }
    return node;
  });
}

// ── 主组件 ────────────────────────────────────────────────

export function SwarmflowGraphView({
  runs,
  sessionId,
}: {
  runs: WorkflowRun[];
  sessionId: string;
}) {
  const { nodes: rawNodes, edges: rawEdges } = useMemo(
    () => workflowToGraph(runs, sessionId),
    [runs, sessionId],
  );

  const laidNodes = useMemo(() => layoutWithDagre(rawNodes, rawEdges), [rawNodes, rawEdges]);

  const [nodes, setNodes, onNodesChange] = useNodesState(laidNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(rawEdges);

  // workflow 数据变化时同步到 React Flow state（useNodesState 只在挂载时取初始值）
  useEffect(() => {
    setNodes(laidNodes);
  }, [laidNodes, setNodes]);

  useEffect(() => {
    setEdges(rawEdges);
  }, [rawEdges, setEdges]);

  // 历史恢复后 get_workflow 只给 phase 骨架（无 agents）——进入图视图时补齐缺失的 agents。
  useEffect(() => {
    for (const run of runs) {
      for (const phase of run.phases ?? []) {
        if (phase.agents === undefined && (phase.agent_count ?? 0) > 0) {
          void useSessionStore
            .getState()
            .loadPhaseAgents(sessionId, run.id, phase.id)
            .catch(() => undefined);
        }
      }
    }
  }, [runs, sessionId]);

  const [modalState, setModalState] = useState<AgentModalState | null>(null);
  const [modalAgentName, setModalAgentName] = useState('');

  // 统一处理节点点击：agent/session 节点 → waiting_for_human 打开 ask-user，否则打开详情弹窗
  const onNodeClick = useCallback((_evt: React.MouseEvent, node: Node) => {
    if (node.type !== 'agentNode' && node.type !== 'sessionNode') return;
    const agent = node.data?.agent as WorkflowAgent | undefined;
    if (!agent) return;

    // waiting_for_human：转发到 ask-user
    if (agent.status === 'waiting_for_human') {
      const runId = (node.data?.runId as string) ?? '';
      const corr = agent.correlation_id ?? agent.id;
      const payload: AskUserQuestionPayload = {
        request_id: `swarmflow:${runId}:${corr}`,
        source: 'swarmflow_human',
        questions: [{
          question: agent.human_prompt || '(SwarmFlow is waiting for your input)',
          header: agent.name,
          options: [],
          multi_select: false,
        }],
        swarmflowMeta: {
          run_id: runId,
          correlation_id: corr,
          agent_id: agent.id,
          agent_name: agent.name,
        },
      };
      useChatStore.getState().setPendingQuestion(sessionId, payload);
      return;
    }

    // 其它状态：若有输入/输出等内容则打开详情弹窗
    const sections = buildDetailSections(agent);
    if (sections.length > 0) {
      setModalAgentName(agent.name);
      setModalState({ sections, activeKey: sections[0].key });
    }
  }, [sessionId]);

  return (
    <div className="w-full h-full min-h-[400px] relative">
      <ReactFlowComponent
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={onNodeClick}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.3}
        maxZoom={1.5}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="hsl(var(--border) / 0.3)" gap={20} />
        <Controls showInteractive={false} />
      </ReactFlowComponent>

      {/* Agent 详情弹窗（与缩进树共用同一组件） */}
      <AgentDetailModal
        state={modalState}
        agentName={modalAgentName}
        onClose={() => setModalState(null)}
        onTabChange={(key: DetailSectionKey) =>
          setModalState((prev) => (prev ? { ...prev, activeKey: key } : prev))
        }
      />
    </div>
  );
}
