import {
  AlertTriangle,
  ArrowRight,
  Boxes,
  CheckCircle2,
  Crown,
  GitBranch,
  Loader2,
  Minus,
  Network,
  PackageCheck,
  Plus,
  RefreshCw,
  Route,
  ScanSearch,
  Search,
  Sparkles,
  Users,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { InteractiveRelationshipGraph, type InteractiveRelationshipGraphHandle } from '../RelationshipGraph/InteractiveRelationshipGraph';
import { webRequest } from '../../services/webClient';
import {
  filterExpertGraph,
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
  const graphControllerRef = useRef<InteractiveRelationshipGraphHandle>(null);
  const [query, setQuery] = useState('');
  const [minConfidence, setMinConfidence] = useState(0.5);
  const [selectedNodeId, setSelectedNodeId] = useState(graph.nodes[0]?.id || '');
  const [selectedEdgeId, setSelectedEdgeId] = useState('');
  const visibleGraph = useMemo(() => filterExpertGraph(graph, query, minConfidence), [graph, minConfidence, query]);
  const relationshipNodes = useMemo(
    () =>
      visibleGraph.nodes.map(node => ({
        id: node.id,
        label: node.name,
        kind: node.type === 'team' ? 'team' : 'expert',
        reference: !node.reusable,
      })),
    [visibleGraph.nodes],
  );
  const relationshipEdges = useMemo(
    () =>
      visibleGraph.edges.map(edge => ({
        ...edge,
        directed: !['complements', 'overlaps', 'conflicts'].includes(edge.type),
      })),
    [visibleGraph.edges],
  );
  const degreeById = useMemo(() => {
    const counts = new Map(visibleGraph.nodes.map(node => [node.id, 0]));
    visibleGraph.edges.forEach(edge => {
      counts.set(edge.source, (counts.get(edge.source) || 0) + 1);
      counts.set(edge.target, (counts.get(edge.target) || 0) + 1);
    });
    return counts;
  }, [visibleGraph]);
  const selectedNode = visibleGraph.nodes.find(node => node.id === selectedNodeId) || null;
  const selectedEdge = visibleGraph.edges.find(edge => edge.id === selectedEdgeId) || null;

  useEffect(() => {
    if (visibleGraph.edges.some(edge => edge.id === selectedEdgeId)) return;
    if (selectedEdgeId) setSelectedEdgeId('');
    if (visibleGraph.nodes.some(node => node.id === selectedNodeId)) return;
    setSelectedNodeId(visibleGraph.nodes[0]?.id || '');
  }, [selectedEdgeId, selectedNodeId, visibleGraph.edges, visibleGraph.nodes]);

  return (
    <div className="beta-expert-graph__visual-layout">
      <aside className="beta-expert-graph__graph-controls">
        <div className="beta-expert-graph__graph-counts">
          <div>
            <strong>{visibleGraph.nodes.length}</strong>
            <span>专家</span>
          </div>
          <div>
            <strong>{visibleGraph.edges.length}</strong>
            <span>关系</span>
          </div>
        </div>
        <label className="beta-expert-graph__graph-search">
          <Search size={15} />
          <input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索专家、Skill 或标签" />
        </label>
        <label className="beta-expert-graph__confidence-filter">
          <span>
            <strong>最小关联强度</strong>
            <em>{Math.round(minConfidence * 100)}%</em>
          </span>
          <input
            type="range"
            min="0"
            max="100"
            step="5"
            value={Math.round(minConfidence * 100)}
            onChange={event => setMinConfidence(Number(event.target.value) / 100)}
          />
        </label>
        <div className="beta-expert-graph__node-list" aria-label="图谱中的专家">
          {visibleGraph.nodes.map(node => (
            <button
              key={node.id}
              className={selectedNodeId === node.id ? 'is-active' : ''}
              type="button"
              aria-pressed={selectedNodeId === node.id}
              onClick={() => {
                setSelectedNodeId(node.id);
                setSelectedEdgeId('');
                graphControllerRef.current?.focusNode(node.id);
              }}
            >
              <span>{node.name}</span>
              <em>{degreeById.get(node.id) || 0}</em>
            </button>
          ))}
          {visibleGraph.nodes.length === 0 && <p>没有符合筛选条件的专家</p>}
        </div>
      </aside>
      <div className="beta-expert-graph__canvas-wrap">
        <InteractiveRelationshipGraph
          ref={graphControllerRef}
          nodes={relationshipNodes}
          edges={relationshipEdges}
          selectedNodeId={selectedNodeId}
          selectedEdgeId={selectedEdgeId}
          ariaLabel="专家能力协作图谱，可拖动节点或画布并使用滚轮缩放"
          onSelectNode={nodeId => {
            setSelectedNodeId(nodeId || '');
            if (nodeId) setSelectedEdgeId('');
          }}
          onSelectEdge={edgeId => {
            setSelectedEdgeId(edgeId || '');
            if (edgeId) setSelectedNodeId('');
          }}
        />
        <div className="beta-expert-graph__canvas-tools" aria-label="图谱视图控制">
          <button type="button" aria-label="缩小图谱" title="缩小" onClick={() => graphControllerRef.current?.zoomOut()}>
            <Minus size={15} />
          </button>
          <button type="button" aria-label="放大图谱" title="放大" onClick={() => graphControllerRef.current?.zoomIn()}>
            <Plus size={15} />
          </button>
          <button type="button" aria-label="适配图谱视图" title="适配视图" onClick={() => graphControllerRef.current?.fitView()}>
            <ScanSearch size={15} />
          </button>
        </div>
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
  const providedProfileById = new Map(candidate.memberProfiles.map(profile => [profile.id, profile]));
  const memberProfiles = [
    ...candidate.memberIds.map(
      id =>
        providedProfileById.get(id) || {
          id,
          name: memberName(id, graph),
          role: id === candidate.leaderId ? 'lead' : 'member',
          description: '',
          capabilities: [],
          tags: [],
          skills: [],
          quickPrompts: [],
          deliverables: [],
          inputs: [],
          outputs: [],
        },
    ),
    ...candidate.memberProfiles.filter(profile => !candidate.memberIds.includes(profile.id)),
  ];
  const leader = memberProfiles.find(profile => profile.id === candidate.leaderId || profile.role === 'lead');
  const members = memberProfiles.filter(profile => profile.id !== leader?.id);
  const routeExamples =
    candidate.routeExamples.length > 0
      ? candidate.routeExamples
      : candidate.quickPrompts.slice(0, 2).map((query, index) => ({
          id: '',
          type: '',
          title: '',
          intent: '',
          query,
          memberIds: [],
          steps: [],
          relationEdgeIds: [],
          summary: candidate.workflow[index] || '主理人会根据任务选择最少且足够的成员执行',
        }));
  const selectionRange =
    candidate.routingPolicy.minSelected === candidate.routingPolicy.maxSelected
      ? `${candidate.routingPolicy.minSelected} 位`
      : `${candidate.routingPolicy.minSelected}–${candidate.routingPolicy.maxSelected} 位`;
  return (
    <article className="beta-expert-graph__candidate">
      <div className="beta-expert-graph__candidate-top">
        <span className="beta-expert-graph__rank">推荐 {rank}</span>
        <span className="beta-expert-graph__score">匹配度 {Math.round(candidate.score)}%</span>
      </div>
      <h3>{candidate.name}</h3>
      <p>{candidate.description}</p>
      <div className="beta-expert-graph__dispatch-summary">
        <div>
          <Crown size={16} />
          <span>主理人</span>
          <strong>{leader?.name || memberName(candidate.leaderId, graph) || '团队主理人'}</strong>
        </div>
        <div>
          <Route size={16} />
          <span>按需调度 {selectionRange}</span>
          <small>
            {[
              candidate.routingPolicy.allowSingleMember ? '可单人' : '',
              candidate.routingPolicy.allowParallel ? '可并行' : '',
              candidate.routingPolicy.allowSerial ? '可串行' : '',
            ]
              .filter(Boolean)
              .join(' · ')}
          </small>
        </div>
      </div>
      <div className="beta-expert-graph__member-pool">
        <strong>可调度成员</strong>
        <div>
          {members.map(profile => (
            <span key={profile.id} title={profile.description || undefined}>
              {profile.name}
              {profile.capabilities[0] && <em>{profile.capabilities[0]}</em>}
            </span>
          ))}
          {members.length === 0 && <em>主理人可独立完成简单任务</em>}
        </div>
      </div>
      {routeExamples.length > 0 && (
        <div className="beta-expert-graph__route-examples">
          <strong>示例路由</strong>
          {routeExamples.map((example, index) => (
            <div key={example.id || `${example.title || example.query}-${index}`}>
              <span>{example.title || example.query || example.intent || `协作场景 ${index + 1}`}</span>
              <p>
                {example.memberIds.length > 0
                  ? example.memberIds.map(id => memberName(id, graph)).join(' + ')
                  : example.summary || '由主理人按任务动态选择成员'}
              </p>
              {example.memberIds.length > 0 && (example.summary || example.intent) && <small>{example.summary || example.intent}</small>}
              {example.steps.length > 0 && <small>{example.steps.join(' → ')}</small>}
            </div>
          ))}
        </div>
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
      const payload = await webRequest('experts.teams.mine', { graph_id: nextGraph.graphId || undefined, limit: 12 });
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
          <p>从专家能力关系中发现可组合团队，由主理人针对每次任务动态选择成员。</p>
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
                <p>主理人理解你的任务后，只调度完成本次需求所需的成员；候选不会自动安装。</p>
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
              <div className="beta-expert-graph__empty-candidates">当前还没有发现合适的专家组合。可安装更多不同能力的专家后重新构建图谱。</div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
