import { AlertTriangle, ArrowRight, Boxes, CheckCircle2, GitBranch, Loader2, Network, PackageCheck, RefreshCw, Sparkles, Users } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { webRequest } from '../../services/webClient';
import {
  layoutExpertGraph,
  isExpertTeamCandidateInstalled,
  memberName,
  normalizeExpertGraph,
  normalizeExpertTeamCandidates,
  normalizeExpertTeamMaterialization,
  type ExpertGraph,
  type ExpertGraphEdge,
  type ExpertGraphNode,
  type ExpertTeamCandidate,
} from '../../features/expertGraph';

type LoadPhase = 'loading' | 'ready' | 'unavailable' | 'error';
type MaterializeState = { status: 'running' | 'success' | 'error'; message: string };

function errorText(error: unknown): string {
  return error instanceof Error && error.message.trim() ? error.message : '请求失败，请稍后重试';
}

function shortText(value: string, maxLength: number): string {
  return value.length > maxLength ? `${value.slice(0, maxLength - 1)}…` : value;
}

function isUnavailableError(message: string): boolean {
  return /not found|not registered|unknown method|unsupported|未注册|不存在|不支持/i.test(message);
}

function edgeLabel(type: string): string {
  const labels: Record<string, string> = {
    can_feed: '可交接',
    needs_adapter: '需适配',
    can_delegate: '可委派',
    complements: '能力互补',
    overlaps: '能力重叠',
    conflicts: '存在冲突',
  };
  return labels[type] || type;
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    available: '可参与组团',
    ready: '可参与组团',
    installed: '已安装',
    candidate: '候选方案',
    needs_contract: '待补协作契约',
    external_dependency_hold: '外部依赖待确认',
    quarantined: '已隔离',
  };
  return labels[status] || status;
}

function NodeDetail({ node }: { node: ExpertGraphNode }) {
  return (
    <aside className="beta-expert-graph__node-detail">
      <div className="beta-expert-graph__detail-title">
        <span className="beta-expert-graph__node-icon">{node.name.slice(0, 1)}</span>
        <div>
          <strong>{node.name}</strong>
          <span>{statusLabel(node.status)}</span>
        </div>
      </div>
      {node.description && <p>{node.description}</p>}
      <dl>
        <div>
          <dt>来源</dt>
          <dd>{node.source || '本地已安装'}</dd>
        </div>
        <div>
          <dt>类型</dt>
          <dd>{node.type === 'team' ? '已有专家团' : '单专家'}</dd>
        </div>
        <div>
          <dt>复用状态</dt>
          <dd>{node.reusable ? '可参与挖掘' : '仅作参考'}</dd>
        </div>
      </dl>
      <div className="beta-expert-graph__detail-block">
        <strong>原始 Skills</strong>
        <div className="beta-expert-graph__chips">{node.skills.length > 0 ? node.skills.map(skill => <span key={skill}>{skill}</span>) : <em>未声明</em>}</div>
      </div>
      <div className="beta-expert-graph__ports">
        <div>
          <strong>输入</strong>
          <span>{node.inputs.length || '—'}</span>
        </div>
        <ArrowRight size={16} />
        <div>
          <strong>输出</strong>
          <span>{node.outputs.length || '—'}</span>
        </div>
      </div>
    </aside>
  );
}

function EdgeDetail({ edge, graph }: { edge: ExpertGraphEdge; graph: ExpertGraph }) {
  const source = graph.nodes.find(node => node.id === edge.source)?.name || edge.source;
  const target = graph.nodes.find(node => node.id === edge.target)?.name || edge.target;
  return (
    <aside className="beta-expert-graph__node-detail">
      <div className="beta-expert-graph__edge-detail-title">
        <GitBranch size={18} />
        <strong>{edgeLabel(edge.type)}</strong>
      </div>
      <p>
        {source} → {target}
      </p>
      <dl>
        <div>
          <dt>置信度</dt>
          <dd>{Math.round(edge.confidence * 100)}%</dd>
        </div>
      </dl>
      <div className="beta-expert-graph__detail-block">
        <strong>关系证据</strong>
        {edge.evidence.length > 0 ? (
          <ul>
            {edge.evidence.map((item, index) => (
              <li key={`${item}-${index}`}>{item}</li>
            ))}
          </ul>
        ) : (
          <em>暂无补充证据</em>
        )}
      </div>
    </aside>
  );
}

function GraphCanvas({ graph }: { graph: ExpertGraph }) {
  const layoutNodes = useMemo(() => layoutExpertGraph(graph), [graph]);
  const nodeById = useMemo(() => new Map(layoutNodes.map(node => [node.id, node])), [layoutNodes]);
  const canvasHeight = Math.max(420, ...layoutNodes.map(node => node.y + 58));
  const [selectedNodeId, setSelectedNodeId] = useState(layoutNodes[0]?.id || '');
  const [selectedEdgeId, setSelectedEdgeId] = useState('');
  const selectedNode = graph.nodes.find(node => node.id === selectedNodeId) || graph.nodes[0] || null;
  const selectedEdge = graph.edges.find(edge => edge.id === selectedEdgeId) || null;

  useEffect(() => {
    if (!graph.nodes.some(node => node.id === selectedNodeId)) setSelectedNodeId(graph.nodes[0]?.id || '');
  }, [graph, selectedNodeId]);

  return (
    <div className="beta-expert-graph__visual-layout">
      <div className="beta-expert-graph__canvas-wrap">
        <svg className="beta-expert-graph__canvas" viewBox={`0 0 920 ${canvasHeight}`} role="img" aria-label="专家能力协作图谱">
          <defs>
            <marker id="beta-expert-graph-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" />
            </marker>
          </defs>
          <g className="beta-expert-graph__edges">
            {graph.edges.map(edge => {
              const source = nodeById.get(edge.source);
              const target = nodeById.get(edge.target);
              if (!source || !target) return null;
              const active = edge.id === selectedEdgeId;
              return (
                <g
                  key={edge.id}
                  className={`beta-expert-graph__edge beta-expert-graph__edge--${edge.type}${active ? ' is-active' : ''}`}
                  role="button"
                  tabIndex={0}
                  onClick={() => {
                    setSelectedEdgeId(edge.id);
                    setSelectedNodeId('');
                  }}
                  onKeyDown={event => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      setSelectedEdgeId(edge.id);
                      setSelectedNodeId('');
                    }
                  }}
                >
                  <line x1={source.x + 74} y1={source.y} x2={target.x - 82} y2={target.y} markerEnd="url(#beta-expert-graph-arrow)" />
                  <line className="beta-expert-graph__edge-hit" x1={source.x + 74} y1={source.y} x2={target.x - 82} y2={target.y} />
                </g>
              );
            })}
          </g>
          <g>
            {layoutNodes.map(node => {
              const active = node.id === selectedNodeId;
              return (
                <g
                  key={node.id}
                  className={`beta-expert-graph__node${active ? ' is-active' : ''}${node.reusable ? '' : ' is-reference'}`}
                  role="button"
                  tabIndex={0}
                  transform={`translate(${node.x - 76} ${node.y - 32})`}
                  onClick={() => {
                    setSelectedNodeId(node.id);
                    setSelectedEdgeId('');
                  }}
                  onKeyDown={event => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      setSelectedNodeId(node.id);
                      setSelectedEdgeId('');
                    }
                  }}
                >
                  <rect width="152" height="64" rx="14" />
                  <circle cx="24" cy="24" r="12" />
                  <text className="beta-expert-graph__node-mark" x="24" y="28" textAnchor="middle">
                    {node.name.slice(0, 1)}
                  </text>
                  <text className="beta-expert-graph__node-name" x="44" y="27">
                    {shortText(node.name, 10)}
                  </text>
                  <text className="beta-expert-graph__node-meta" x="16" y="49">
                    {node.reusable ? `${node.skills.length} Skills` : '参考拓扑'}
                  </text>
                </g>
              );
            })}
          </g>
        </svg>
        <div className="beta-expert-graph__legend">
          {['can_feed', 'needs_adapter', 'complements'].map(type => (
            <span key={type} className={`beta-expert-graph__legend-item beta-expert-graph__legend-item--${type}`}>
              {edgeLabel(type)}
            </span>
          ))}
        </div>
      </div>
      {selectedEdge ? <EdgeDetail edge={selectedEdge} graph={graph} /> : selectedNode && <NodeDetail node={selectedNode} />}
    </div>
  );
}

function CandidateCard({
  candidate,
  graph,
  rank,
  materializeState,
  onMaterialize,
}: {
  candidate: ExpertTeamCandidate;
  graph: ExpertGraph;
  rank: number;
  materializeState?: MaterializeState;
  onMaterialize: (candidate: ExpertTeamCandidate) => void;
}) {
  const materializing = materializeState?.status === 'running';
  const installed = isExpertTeamCandidateInstalled(candidate) || materializeState?.status === 'success';
  return (
    <article className="beta-expert-graph__candidate">
      <div className="beta-expert-graph__candidate-top">
        <span className="beta-expert-graph__rank">推荐 {rank}</span>
        <span className="beta-expert-graph__score">匹配度 {Math.round(candidate.score)}%</span>
      </div>
      <h3>{candidate.name}</h3>
      <p>{candidate.description}</p>
      <div className="beta-expert-graph__member-chain" aria-label="专家协作顺序">
        {candidate.memberIds.map((memberId, index) => (
          <div key={memberId} className="beta-expert-graph__member">
            <span>{memberName(memberId, graph)}</span>
            {index < candidate.memberIds.length - 1 && <ArrowRight size={15} />}
          </div>
        ))}
      </div>
      {candidate.workflow.length > 0 && (
        <ol className="beta-expert-graph__workflow">
          {candidate.workflow.map((step, index) => (
            <li key={`${step}-${index}`}>
              <span>{index + 1}</span>
              {step}
            </li>
          ))}
        </ol>
      )}
      <div className="beta-expert-graph__candidate-foot">
        <span>
          <PackageCheck size={15} />
          {candidate.deliverables[0] || '协作交付成品'}
        </span>
        <span>
          <CheckCircle2 size={15} />
          {statusLabel(candidate.status)}
        </span>
      </div>
      {candidate.quickPrompts[0] && (
        <div className="beta-expert-graph__sample-query">
          <strong>推荐 Query</strong>
          <span>{candidate.quickPrompts[0]}</span>
        </div>
      )}
      <button
        className={`beta-expert-graph__materialize${installed ? ' is-installed' : ''}`}
        type="button"
        disabled={materializing || installed}
        onClick={() => onMaterialize(candidate)}
      >
        {materializing ? <Loader2 className="is-spinning" size={16} /> : installed ? <CheckCircle2 size={16} /> : <Boxes size={16} />}
        {materializing ? '正在生成并安装…' : installed ? '专家团已安装' : '生成并安装专家团'}
      </button>
      {materializeState && materializeState.status !== 'running' && (
        <div className={`beta-expert-graph__materialize-result beta-expert-graph__materialize-result--${materializeState.status}`} role="status">
          {materializeState.message}
        </div>
      )}
    </article>
  );
}

export function ExpertGraphWorkspace({ onExpertInstalled }: { onExpertInstalled?: () => void | Promise<void> }) {
  const [phase, setPhase] = useState<LoadPhase>('loading');
  const [graph, setGraph] = useState<ExpertGraph | null>(null);
  const [candidates, setCandidates] = useState<ExpertTeamCandidate[]>([]);
  const [error, setError] = useState('');
  const [candidateError, setCandidateError] = useState('');
  const [building, setBuilding] = useState(false);
  const [materializeStates, setMaterializeStates] = useState<Record<string, MaterializeState>>({});
  const materializingIds = useRef(new Set<string>());

  const mineCandidates = useCallback(async (nextGraph: ExpertGraph) => {
    setCandidateError('');
    try {
      const payload = await webRequest('experts.teams.mine', { graph_id: nextGraph.graphId || undefined, limit: 6 });
      setCandidates(normalizeExpertTeamCandidates(payload));
    } catch (nextError) {
      setCandidates([]);
      setCandidateError(errorText(nextError));
    }
  }, []);

  const buildGraph = useCallback(
    async (force: boolean) => {
      setBuilding(true);
      setError('');
      try {
        const payload = await webRequest('experts.graph.build', { force });
        const nextGraph = normalizeExpertGraph(payload);
        if (!nextGraph) throw new Error('后端未返回可用的专家图谱');
        setGraph(nextGraph);
        setPhase('ready');
        await mineCandidates(nextGraph);
      } catch (nextError) {
        const message = errorText(nextError);
        setPhase(isUnavailableError(message) ? 'unavailable' : 'error');
        setError(message);
      } finally {
        setBuilding(false);
      }
    },
    [mineCandidates],
  );

  const loadGraph = useCallback(async () => {
    setPhase('loading');
    setError('');
    try {
      const payload = await webRequest('experts.graph.get', {});
      const existingGraph = normalizeExpertGraph(payload);
      if (!existingGraph) {
        await buildGraph(false);
        return;
      }
      setGraph(existingGraph);
      setPhase('ready');
      await mineCandidates(existingGraph);
    } catch (nextError) {
      const message = errorText(nextError);
      setPhase(isUnavailableError(message) ? 'unavailable' : 'error');
      setError(message);
    }
  }, [buildGraph, mineCandidates]);

  const materializeCandidate = useCallback(
    async (candidate: ExpertTeamCandidate) => {
      if (isExpertTeamCandidateInstalled(candidate) || materializingIds.current.has(candidate.id) || materializeStates[candidate.id]?.status === 'success')
        return;
      materializingIds.current.add(candidate.id);
      setMaterializeStates(current => ({ ...current, [candidate.id]: { status: 'running', message: '' } }));
      try {
        const payload = await webRequest('experts.teams.materialize', {
          candidate_id: candidate.id,
          graph_id: graph?.graphId,
        });
        const result = normalizeExpertTeamMaterialization(payload);
        const message = result.sourceRefreshed
          ? `${result.expertName || candidate.name}已安装，可返回专家广场直接使用。`
          : result.warnings[0] || '专家团包已生成，但当前专家源尚未刷新，请刷新环境后使用。';
        setMaterializeStates(current => ({ ...current, [candidate.id]: { status: 'success', message } }));

        if (result.hasFreshWorkspace && result.graph) {
          setGraph(result.graph);
          setCandidates(result.candidates);
          setCandidateError('');
          setPhase('ready');
        } else {
          // Older servers invalidate their graph after materialization without
          // returning the replacement snapshot. Never leave another card bound
          // to that stale graph id: rebuild and mine before accepting new clicks.
          setGraph(null);
          setCandidates([]);
          await buildGraph(true);
        }
        if (result.sourceRefreshed) await onExpertInstalled?.();
      } catch (nextError) {
        setMaterializeStates(current => ({ ...current, [candidate.id]: { status: 'error', message: errorText(nextError) } }));
      } finally {
        materializingIds.current.delete(candidate.id);
      }
    },
    [buildGraph, graph?.graphId, materializeStates, onExpertInstalled],
  );

  useEffect(() => {
    void loadGraph();
  }, [loadGraph]);

  return (
    <div className="beta-expert-graph">
      <header className="beta-expert-graph__header">
        <div>
          <div className="beta-expert-graph__title">
            <Network size={24} />
            <h1>专家协作图谱</h1>
          </div>
          <p>从已安装专家的能力和交付契约中，发现可以接力完成真实任务的专家团。</p>
        </div>
        <button type="button" disabled={building || phase === 'loading'} onClick={() => void buildGraph(true)}>
          {building ? <Loader2 className="is-spinning" size={17} /> : <RefreshCw size={17} />}
          {building ? '正在构建…' : '重新构建'}
        </button>
      </header>

      {phase === 'loading' && (
        <div className="beta-expert-graph__state">
          <Loader2 className="is-spinning" size={22} />
          <span>正在读取专家与协作关系…</span>
        </div>
      )}
      {phase === 'unavailable' && (
        <div className="beta-expert-graph__state beta-expert-graph__state--notice">
          <Boxes size={24} />
          <div>
            <strong>当前环境暂未启用专家图谱</strong>
            <p>专家广场和单专家使用不受影响。后端能力接入后即可在这里查看协作候选。</p>
          </div>
          <button type="button" onClick={() => void loadGraph()}>
            重试
          </button>
        </div>
      )}
      {phase === 'error' && (
        <div className="beta-expert-graph__state beta-expert-graph__state--error">
          <AlertTriangle size={24} />
          <div>
            <strong>专家图谱加载失败</strong>
            <p>{error}</p>
          </div>
          <button type="button" onClick={() => void loadGraph()}>
            重新加载
          </button>
        </div>
      )}

      {phase === 'ready' && graph && (
        <>
          <div className="beta-expert-graph__stats">
            <div>
              <Users size={18} />
              <strong>{graph.nodes.length}</strong>
              <span>个专家节点</span>
            </div>
            <div>
              <GitBranch size={18} />
              <strong>{graph.edges.length}</strong>
              <span>条协作关系</span>
            </div>
            <div>
              <Sparkles size={18} />
              <strong>{candidates.length}</strong>
              <span>个专家团候选</span>
            </div>
          </div>

          {graph.nodes.length > 0 ? <GraphCanvas graph={graph} /> : <div className="beta-expert-graph__state">暂无可构图的专家，请先安装专家包。</div>}

          <section className="beta-expert-graph__candidates">
            <div className="beta-expert-graph__section-title">
              <div>
                <h2>推荐专家团</h2>
                <p>只展示具有明确协作关系和主交付物的组合，候选不会自动安装。</p>
              </div>
              {candidateError && <span title={candidateError}>候选挖掘暂不可用</span>}
            </div>
            {candidates.length > 0 ? (
              <div className="beta-expert-graph__candidate-grid">
                {candidates.map((candidate, index) => (
                  <CandidateCard
                    key={candidate.id}
                    candidate={candidate}
                    graph={graph}
                    rank={index + 1}
                    materializeState={materializeStates[candidate.id]}
                    onMaterialize={materializeCandidate}
                  />
                ))}
              </div>
            ) : (
              <div className="beta-expert-graph__empty-candidates">
                当前还没有满足可靠交接条件的专家组合。单专家仍可正常使用，也不会为了凑数量生成低质量专家团。
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
