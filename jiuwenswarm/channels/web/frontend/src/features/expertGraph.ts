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
  routingPolicy: ExpertTeamRoutingPolicy;
  memberProfiles: ExpertTeamMemberProfile[];
  routeExamples: ExpertTeamRouteExample[];
};

export type ExpertTeamRoutingPolicy = {
  mode: string;
  minSelected: number;
  maxSelected: number;
  selection: string;
  allowSingleMember: boolean;
  allowParallel: boolean;
  allowSerial: boolean;
};

export type ExpertTeamMemberProfile = {
  id: string;
  name: string;
  role: string;
  description: string;
  capabilities: string[];
  tags: string[];
  skills: string[];
  quickPrompts: string[];
  deliverables: string[];
  inputs: string[];
  outputs: string[];
};

export type ExpertTeamRouteExample = {
  id: string;
  type: string;
  title: string;
  intent: string;
  query: string;
  memberIds: string[];
  steps: string[];
  relationEdgeIds: string[];
  summary: string;
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

function booleanValue(value: unknown, fallback: boolean): boolean {
  return typeof value === 'boolean' ? value : fallback;
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

function normalizeMemberIds(value: unknown): string[] {
  return asArray(value)
    .map(item => {
      if (typeof item === 'string') return item.trim();
      const record = asRecord(item);
      return text(first(record, 'id', 'expertId', 'expert_id', 'memberId', 'member_id'));
    })
    .filter(Boolean);
}

function normalizeMemberProfiles(value: unknown): ExpertTeamMemberProfile[] {
  return asArray(value)
    .map((item): ExpertTeamMemberProfile | null => {
      const raw = asRecord(item);
      const id = text(first(raw, 'id', 'expertId', 'expert_id', 'memberId', 'member_id'));
      if (!id) return null;
      const tags = normalizeSkills(raw.tags);
      const skills = normalizeSkills(raw.skills);
      return {
        id,
        name: text(first(raw, 'name', 'displayName', 'display_name')) || id,
        role: text(raw.role) || 'member',
        description: text(first(raw, 'description', 'summary', 'responsibility')),
        capabilities: normalizeSkills(first(raw, 'capabilities', 'strengths'))
          .concat(tags, skills)
          .filter((item, index, all) => all.indexOf(item) === index),
        tags,
        skills,
        quickPrompts: textList(first(raw, 'quickPrompts', 'quick_prompts')),
        deliverables: textList(raw.deliverables),
        inputs: normalizePorts(raw.inputs).map(port => port.label),
        outputs: normalizePorts(raw.outputs).map(port => port.label),
      };
    })
    .filter((item): item is ExpertTeamMemberProfile => item !== null);
}

function normalizeRouteExamples(value: unknown): ExpertTeamRouteExample[] {
  return asArray(value)
    .map((item): ExpertTeamRouteExample | null => {
      if (typeof item === 'string') {
        const query = item.trim();
        return query ? { id: '', type: '', title: '', intent: '', query, memberIds: [], steps: [], relationEdgeIds: [], summary: '' } : null;
      }
      const raw = asRecord(item);
      const title = text(raw.title);
      const intent = text(raw.intent);
      const query = text(first(raw, 'query', 'prompt', 'input'));
      const memberIds = normalizeMemberIds(
        first(raw, 'memberIds', 'member_ids', 'selectedMemberIds', 'selected_member_ids', 'selectedMembers', 'selected_members', 'experts', 'route'),
      );
      const summary = text(first(raw, 'summary', 'reason', 'description', 'routingReason', 'routing_reason'));
      const steps = normalizeWorkflow(raw.steps);
      const relationEdgeIds = textList(first(raw, 'relationEdgeIds', 'relation_edge_ids'));
      if (!query && !title && !intent && memberIds.length === 0 && !summary && steps.length === 0) return null;
      return {
        id: text(raw.id),
        type: text(raw.type),
        title,
        intent,
        query,
        memberIds,
        steps,
        relationEdgeIds,
        summary,
      };
    })
    .filter((item): item is ExpertTeamRouteExample => item !== null);
}

function normalizeRoutingPolicy(value: unknown, memberCount: number): ExpertTeamRoutingPolicy {
  const raw = asRecord(value);
  const maximum = Math.max(1, memberCount);
  const maxSelected = Math.max(1, Math.min(maximum, numberValue(first(raw, 'maxSelected', 'max_selected'), maximum)));
  return {
    mode: text(raw.mode) || 'leader_selected',
    minSelected: Math.max(1, Math.min(maxSelected, numberValue(first(raw, 'minSelected', 'min_selected'), 1))),
    maxSelected,
    selection: text(raw.selection) || 'minimal_sufficient',
    allowSingleMember: booleanValue(first(raw, 'allowSingleMember', 'allow_single_member'), true),
    allowParallel: booleanValue(first(raw, 'allowParallel', 'allow_parallel'), true),
    allowSerial: booleanValue(first(raw, 'allowSerial', 'allow_serial'), true),
  };
}

export function normalizeExpertTeamCandidates(payload: unknown): ExpertTeamCandidate[] {
  const outer = asRecord(payload);
  const rawCandidates = first(outer, 'candidates', 'teamCandidates', 'team_candidates');
  return asArray(rawCandidates)
    .map((item): ExpertTeamCandidate | null => {
      const raw = asRecord(item);
      const id = text(raw.id);
      if (!id) return null;
      const memberIds = normalizeMemberIds(first(raw, 'memberIds', 'member_ids', 'members'));
      const memberProfiles = normalizeMemberProfiles(first(raw, 'memberProfiles', 'member_profiles'));
      return {
        id,
        name: text(raw.name) || id,
        description: text(raw.description),
        memberIds,
        leaderId: text(first(raw, 'leaderId', 'leader_id')),
        workflow: normalizeWorkflow(raw.workflow),
        score: Math.max(0, Math.min(100, numberValue(raw.score, 0))),
        scoreBreakdown: normalizeScoreBreakdown(first(raw, 'scoreBreakdown', 'score_breakdown')),
        quickPrompts: textList(first(raw, 'quickPrompts', 'quick_prompts')),
        deliverables: textList(raw.deliverables),
        status: text(raw.status) || 'candidate',
        routingPolicy: normalizeRoutingPolicy(first(raw, 'routingPolicy', 'routing_policy'), Math.max(memberIds.length, memberProfiles.length)),
        memberProfiles,
        routeExamples: normalizeRouteExamples(first(raw, 'routeExamples', 'route_examples')),
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

export function filterExpertGraph(graph: ExpertGraph, query: string, minConfidence: number): ExpertGraph {
  const normalizedQuery = query.trim().toLowerCase();
  const confidence = Math.max(0, Math.min(1, minConfidence));
  const confidenceEdges = graph.edges.filter(edge => edge.confidence >= confidence);
  if (!normalizedQuery) return { ...graph, edges: confidenceEdges };

  const matchingIds = new Set(
    graph.nodes
      .filter(node =>
        [node.name, node.description, node.type, node.source, node.status, ...node.tags, ...node.skills].join(' ').toLowerCase().includes(normalizedQuery),
      )
      .map(node => node.id),
  );
  const visibleIds = new Set(matchingIds);
  const edges = confidenceEdges.filter(edge => {
    const matches = matchingIds.has(edge.source) || matchingIds.has(edge.target);
    if (matches) {
      visibleIds.add(edge.source);
      visibleIds.add(edge.target);
    }
    return matches;
  });
  return {
    ...graph,
    nodes: graph.nodes.filter(node => visibleIds.has(node.id)),
    edges,
  };
}

export function memberName(memberId: string, graph: ExpertGraph): string {
  return graph.nodes.find(node => node.id === memberId)?.name || memberId;
}
