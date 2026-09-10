export type ExpertGraphPort = {
  id: string;
  label: string;
  mediaType: string;
  description: string;
};

export type ExpertGraphNode = {
  id: string;
  name: string;
  description: string;
  type: string;
  source: string;
  status: string;
  reusable: boolean;
  tags: string[];
  skills: string[];
  inputs: ExpertGraphPort[];
  outputs: ExpertGraphPort[];
};

export type ExpertGraphEdge = {
  id: string;
  source: string;
  target: string;
  type: string;
  confidence: number;
  evidence: string[];
};

export type ExpertGraph = {
  schemaVersion: string;
  graphId: string;
  createdAt: string;
  sourceHash: string;
  nodes: ExpertGraphNode[];
  edges: ExpertGraphEdge[];
};

export type ExpertTeamCandidate = {
  id: string;
  name: string;
  description: string;
  memberIds: string[];
  leaderId: string;
  workflow: string[];
  score: number;
  scoreBreakdown: Record<string, number>;
  quickPrompts: string[];
  deliverables: string[];
  status: string;
};

export type ExpertTeamMaterialization = {
  expertId: string;
  expertName: string;
  sourceRefreshed: boolean;
  packageName: string;
  warnings: string[];
  graph: ExpertGraph | null;
  candidates: ExpertTeamCandidate[];
  hasFreshWorkspace: boolean;
};

export type ExpertGraphLayoutNode = ExpertGraphNode & {
  x: number;
  y: number;
};

type RawRecord = Record<string, unknown>;

function asRecord(value: unknown): RawRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as RawRecord) : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function textList(value: unknown): string[] {
  return asArray(value)
    .map(item => text(item))
    .filter(Boolean);
}

function numberValue(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function first(record: RawRecord, ...keys: string[]): unknown {
  for (const key of keys) {
    if (record[key] !== undefined && record[key] !== null) return record[key];
  }
  return undefined;
}

function normalizeSkills(value: unknown): string[] {
  return asArray(value)
    .map(item => {
      if (typeof item === 'string') return item.trim();
      const record = asRecord(item);
      return text(first(record, 'name', 'id', 'skill_name', 'skillName'));
    })
    .filter(Boolean);
}

function normalizePorts(value: unknown): ExpertGraphPort[] {
  return asArray(value)
    .map((item, index): ExpertGraphPort | null => {
      if (typeof item === 'string') {
        const label = item.trim();
        return label ? { id: label, label, mediaType: '', description: '' } : null;
      }
      const record = asRecord(item);
      const id = text(first(record, 'id', 'name', 'port_id', 'portId'));
      const label = text(first(record, 'label', 'name', 'id'));
      if (!id && !label) return null;
      return {
        id: id || `${label}-${index}`,
        label: label || id,
        mediaType: text(first(record, 'mediaType', 'media_type', 'type', 'format')),
        description: text(record.description),
      };
    })
    .filter((item): item is ExpertGraphPort => item !== null);
}

function normalizeEvidence(value: unknown): string[] {
  if (typeof value === 'string') return value.trim() ? [value.trim()] : [];
  if (Array.isArray(value)) {
    return value
      .map(item => {
        if (typeof item === 'string') return item.trim();
        const record = asRecord(item);
        return text(first(record, 'description', 'detail', 'label', 'reason'));
      })
      .filter(Boolean);
  }
  const record = asRecord(value);
  return Object.entries(record)
    .map(([key, item]) => {
      if (typeof item === 'string' || typeof item === 'number' || typeof item === 'boolean') {
        return `${key}: ${String(item)}`;
      }
      return '';
    })
    .filter(Boolean);
}

export function normalizeExpertGraph(payload: unknown): ExpertGraph | null {
  const outer = asRecord(payload);
  const rawGraph = asRecord(first(outer, 'graph', 'expertGraph', 'expert_graph'));
  if (Object.keys(rawGraph).length === 0) return null;

  const nodes = asArray(rawGraph.nodes)
    .map((item): ExpertGraphNode | null => {
      const raw = asRecord(item);
      const id = text(raw.id);
      if (!id) return null;
      return {
        id,
        name: text(raw.name) || id,
        description: text(raw.description),
        type: text(first(raw, 'type', 'packageType', 'package_type')) || 'expert',
        source: text(raw.source),
        status: text(raw.status) || 'ready',
        reusable: raw.reusable !== false,
        tags: textList(raw.tags),
        skills: normalizeSkills(raw.skills),
        inputs: normalizePorts(raw.inputs),
        outputs: normalizePorts(raw.outputs),
      };
    })
    .filter((item): item is ExpertGraphNode => item !== null);

  const nodeIds = new Set(nodes.map(node => node.id));
  const edges = asArray(rawGraph.edges)
    .map((item, index): ExpertGraphEdge | null => {
      const raw = asRecord(item);
      const source = text(first(raw, 'source', 'sourceId', 'source_id'));
      const target = text(first(raw, 'target', 'targetId', 'target_id'));
      if (!source || !target || !nodeIds.has(source) || !nodeIds.has(target)) return null;
      const type = text(raw.type) || 'related';
      return {
        id: text(raw.id) || `${source}-${target}-${type}-${index}`,
        source,
        target,
        type,
        confidence: Math.max(0, Math.min(1, numberValue(raw.confidence, 0))),
        evidence: normalizeEvidence(raw.evidence),
      };
    })
    .filter((item): item is ExpertGraphEdge => item !== null);

  return {
    schemaVersion: text(first(rawGraph, 'schemaVersion', 'schema_version')),
    graphId: text(first(rawGraph, 'graphId', 'graph_id')),
    createdAt: text(first(rawGraph, 'createdAt', 'created_at')),
    sourceHash: text(first(rawGraph, 'sourceHash', 'source_hash')),
    nodes,
    edges,
  };
}

function normalizeWorkflow(value: unknown): string[] {
  return asArray(value)
    .map(item => {
      if (typeof item === 'string') return item.trim();
      const record = asRecord(item);
      const direct = text(first(record, 'description', 'action', 'label', 'summary'));
      if (direct) return direct;
      const expertName = text(first(record, 'expertName', 'expert_name', 'expertId', 'expert_id'));
      const dependencies = textList(first(record, 'dependsOn', 'depends_on'));
      if (expertName) {
        return dependencies.length > 0 ? `${expertName} 接收上一步交付并继续处理` : `${expertName} 完成首步产出`;
      }
      const source = text(first(record, 'source', 'from', 'sourceId', 'source_id'));
      const target = text(first(record, 'target', 'to', 'targetId', 'target_id'));
      return source && target ? `${source} → ${target}` : '';
    })
    .filter(Boolean);
}

function normalizeScoreBreakdown(value: unknown): Record<string, number> {
  const result: Record<string, number> = {};
  Object.entries(asRecord(value)).forEach(([key, item]) => {
    const parsed = Number(item);
    if (Number.isFinite(parsed)) result[key] = parsed;
  });
  return result;
}

export function normalizeExpertTeamCandidates(payload: unknown): ExpertTeamCandidate[] {
  const outer = asRecord(payload);
  const rawCandidates = first(outer, 'candidates', 'teamCandidates', 'team_candidates');
  return asArray(rawCandidates)
    .map((item): ExpertTeamCandidate | null => {
      const raw = asRecord(item);
      const id = text(raw.id);
      if (!id) return null;
      return {
        id,
        name: text(raw.name) || id,
        description: text(raw.description),
        memberIds: textList(first(raw, 'memberIds', 'member_ids', 'members')),
        leaderId: text(first(raw, 'leaderId', 'leader_id')),
        workflow: normalizeWorkflow(raw.workflow),
        score: Math.max(0, Math.min(100, numberValue(raw.score, 0))),
        scoreBreakdown: normalizeScoreBreakdown(first(raw, 'scoreBreakdown', 'score_breakdown')),
        quickPrompts: textList(first(raw, 'quickPrompts', 'quick_prompts')),
        deliverables: textList(raw.deliverables),
        status: text(raw.status) || 'candidate',
      };
    })
    .filter((item): item is ExpertTeamCandidate => item !== null);
}

export function normalizeExpertTeamMaterialization(payload: unknown): ExpertTeamMaterialization {
  const outer = asRecord(payload);
  const expert = asRecord(outer.expert);
  const graph = normalizeExpertGraph(payload);
  const rawCandidates = first(outer, 'candidates', 'teamCandidates', 'team_candidates');
  return {
    expertId: text(expert.id),
    expertName: text(expert.name),
    sourceRefreshed: first(outer, 'source_refreshed', 'sourceRefreshed') !== false,
    packageName: text(first(outer, 'package_name', 'packageName')),
    warnings: textList(outer.warnings),
    graph,
    candidates: normalizeExpertTeamCandidates(payload),
    hasFreshWorkspace: graph !== null && Array.isArray(rawCandidates),
  };
}

export function isExpertTeamCandidateInstalled(candidate: ExpertTeamCandidate): boolean {
  return candidate.status.toLowerCase() === 'installed';
}

export function layoutExpertGraph(graph: ExpertGraph, width = 920, height = 420): ExpertGraphLayoutNode[] {
  if (graph.nodes.length === 0) return [];

  const nodeIds = new Set(graph.nodes.map(node => node.id));
  const directedTypes = new Set(['can_feed', 'needs_adapter', 'can_delegate']);
  const directedEdges = graph.edges.filter(edge => directedTypes.has(edge.type) && nodeIds.has(edge.source) && nodeIds.has(edge.target));
  const incoming = new Map(graph.nodes.map(node => [node.id, 0]));
  const outgoing = new Map(graph.nodes.map(node => [node.id, [] as string[]]));
  directedEdges.forEach(edge => {
    incoming.set(edge.target, (incoming.get(edge.target) || 0) + 1);
    outgoing.get(edge.source)?.push(edge.target);
  });

  const layer = new Map<string, number>();
  const queue = graph.nodes.filter(node => (incoming.get(node.id) || 0) === 0).map(node => node.id);
  queue.forEach(id => layer.set(id, 0));
  for (let index = 0; index < queue.length; index += 1) {
    const id = queue[index];
    const nextLayer = (layer.get(id) || 0) + 1;
    for (const target of outgoing.get(id) || []) {
      layer.set(target, Math.max(layer.get(target) || 0, nextLayer));
      incoming.set(target, (incoming.get(target) || 0) - 1);
      if ((incoming.get(target) || 0) === 0) queue.push(target);
    }
  }

  const fallbackLayer = Math.max(0, ...layer.values());
  graph.nodes.forEach(node => {
    if (!layer.has(node.id)) layer.set(node.id, fallbackLayer);
  });

  const groups = new Map<number, ExpertGraphNode[]>();
  graph.nodes.forEach(node => {
    const value = layer.get(node.id) || 0;
    groups.set(value, [...(groups.get(value) || []), node]);
  });
  const layers = [...groups.keys()].sort((a, b) => a - b);
  const horizontalPadding = 100;
  const verticalPadding = 58;
  const usableWidth = Math.max(1, width - horizontalPadding * 2);
  const maxLayerSize = Math.max(1, ...[...groups.values()].map(group => group.length));
  const effectiveHeight = Math.max(height, verticalPadding * 2 + (maxLayerSize - 1) * 78);
  const usableHeight = Math.max(1, effectiveHeight - verticalPadding * 2);

  return layers.flatMap((layerValue, layerIndex) => {
    const group = (groups.get(layerValue) || []).sort((a, b) => a.name.localeCompare(b.name));
    const x = layers.length === 1 ? width / 2 : horizontalPadding + (usableWidth * layerIndex) / (layers.length - 1);
    return group.map((node, index) => ({
      ...node,
      x,
      y: verticalPadding + (usableHeight * (index + 1)) / (group.length + 1),
    }));
  });
}

export function memberName(memberId: string, graph: ExpertGraph): string {
  return graph.nodes.find(node => node.id === memberId)?.name || memberId;
}
