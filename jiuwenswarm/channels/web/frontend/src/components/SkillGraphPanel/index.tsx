import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  AlertTriangle,
  CircleStop,
  Loader2,
  Plus,
  Search,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import {
  COMPONENT_CENTER_ATTRACTION_STRENGTH,
  computeConnectedComponents,
  seedPositions,
  stepSkillGraphLayout,
} from './skillGraphLayout';
import './SkillGraphPanel.css';

type RawRecord = Record<string, unknown>;

type BuildLogEntry = {
  ts?: string;
  stage?: string;
  label?: string;
  [key: string]: unknown;
};

type LLMTokenUsageTotals = {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
};

type LLMTokenUsageSummary = {
  total?: LLMTokenUsageTotals;
};

type BuildProgress = {
  stage?: string;
  label?: string;
  percent?: number;
  status?: 'idle' | 'running' | 'success' | 'error' | 'cancelled';
  current?: number;
  total?: number;
  ts?: string;
  llm_token_usage?: LLMTokenUsageSummary;
};

type SkillGraphPayload = {
  success?: boolean;
  detail?: string;
  graph_dir?: string;
  build_log?: BuildLogEntry[];
  build_progress?: BuildProgress;
  llm_token_usage?: LLMTokenUsageSummary;
  manifest?: RawRecord;
  graph_manifest?: RawRecord;
  orchestration_min_edge_confidence?: number;
  graph?: {
    nodes?: RawRecord[];
    edges?: RawRecord[];
    skills?: RawRecord[];
  };
  skills?: {
    skills?: RawRecord[];
  } | RawRecord[];
  diagnostics?: {
    diagnostics?: RawRecord[];
  };
};

type SkillGraphUpdate = {
  success?: boolean;
  detail?: string;
  background?: boolean;
  cancelled?: boolean;
  build_status?: 'idle' | 'running' | 'success' | 'error' | 'cancelled';
  graph_dir?: string;
  build_log?: BuildLogEntry[];
  build_progress?: BuildProgress;
  llm_token_usage?: LLMTokenUsageSummary;
};

type SkillGraphStatus = {
  success?: boolean;
  detail?: string;
  graph_dir?: string;
  build_log?: BuildLogEntry[];
  build_progress?: BuildProgress;
  llm_token_usage?: LLMTokenUsageSummary;
};

export type SkillGraphPanelHandle = {
  refresh: () => boolean;
  startIncrementalBuild: () => Promise<void>;
  cancelActiveBuild: () => Promise<void>;
};

type SkillGraphPanelProps = {
  onReadingChange?: (reading: boolean) => void;
  externalError?: string | null;
  onExternalErrorClear?: () => void;
};

type GraphNode = {
  id: string;
  type: string;
  label: string;
  properties: RawRecord;
  x: number;
  y: number;
  vx: number;
  vy: number;
  degree: number;
  inDegree: number;
  outDegree: number;
};

type GraphEdge = {
  source: string;
  target: string;
  type: string;
  confidence: number;
  runtimeWeight?: number;
  method: string;
  evidence: RawRecord;
};

type NormalizedGraph = {
  nodes: GraphNode[];
  edges: GraphEdge[];
};

type Transform = {
  x: number;
  y: number;
  scale: number;
};

type DetailListItem = {
  key: string;
  label: string;
  meta: string;
};

const GRAPH_NODE_DEFAULT = '#3f8ff3';
const GRAPH_NODE_HIGHLIGHT = '#0868f7';
const GRAPH_NODE_DIMMED = '#b2d1fb';
const GRAPH_EDGE_DEFAULT = '#c2c7ce';
const GRAPH_EDGE_HIGHLIGHT = '#1677ff';
const GRAPH_LABEL_DEFAULT = '#6b7280';
const GRAPH_LABEL_DIMMED = '#adb3bc';
const GRAPH_LABEL_ACTIVE = '#111827';

const DEFAULT_MIN_CONFIDENCE = 0.7;

type SymphonyBuildMode = 'incremental' | 'full';
type Translate = (key: string, options?: Record<string, unknown>) => string;

function FullBuildIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 2.5 20 7v10l-8 4.5L4 17V7l8-4.5Z" />
      <path d="M12 16v-6m0 0L9 8m3 2 3-2" />
    </svg>
  );
}

function ArrangeGraphIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="4" r="2" />
      <circle cx="5" cy="17" r="2" />
      <circle cx="19" cy="17" r="2" />
      <path d="M9.5 5.2a8.5 8.5 0 0 0-5.8 8.7M7.6 19.2a8.5 8.5 0 0 0 8.8 0m3.9-5.3a8.5 8.5 0 0 0-5.8-8.7" />
    </svg>
  );
}

const BUILD_STAGE_TRANSLATION_KEYS: Record<string, string> = {
  idle: 'idle',
  'update.start': 'updateStart',
  'update.cancel_requested': 'updateCancelRequested',
  'update.cancelled': 'updateCancelled',
  'scan.start': 'scanStart',
  'scan.done': 'scanDone',
  'diff.done': 'diffDone',
  'fingerprint.reuse': 'fingerprintReuse',
  'fingerprint.parse.start': 'fingerprintParseStart',
  'fingerprint.extract.start': 'fingerprintExtractStart',
  'fingerprint.normalize.start': 'fingerprintNormalizeStart',
  'fingerprint.done': 'fingerprintDone',
  'artifact.fingerprints.write.start': 'artifactFingerprintsWriteStart',
  'artifact.fingerprints.write.done': 'artifactFingerprintsWriteDone',
  'graph.build.start': 'graphBuildStart',
  'graph.registry.start': 'graphRegistryStart',
  'graph.registry.done': 'graphRegistryDone',
  'graph.candidates.start': 'graphCandidatesStart',
  'graph.candidates.done': 'graphCandidatesDone',
  'graph.resolve.start': 'graphResolveStart',
  'graph.resolve.progress': 'graphResolveProgress',
  'graph.resolve.done': 'graphResolveDone',
  'graph.materialize.start': 'graphMaterializeStart',
  'graph.materialize.done': 'graphMaterializeDone',
  'graph.lookup.start': 'graphLookupStart',
  'graph.lookup.done': 'graphLookupDone',
  'graph.build.done': 'graphBuildDone',
  'artifact.graph.write.start': 'artifactGraphWriteStart',
  'artifact.graph.write.done': 'artifactGraphWriteDone',
  'state.write.start': 'stateWriteStart',
  'state.write.done': 'stateWriteDone',
  'update.failed': 'updateFailed',
  'update.done': 'updateDone',
};

const SERVER_DETAIL_TRANSLATION_KEYS: Record<string, string> = {
  '当前没有正在运行的技能总谱构建。': 'skills.graph.serverDetails.noRunningBuild',
  '技能总谱后台构建已启动。': 'skills.graph.serverDetails.buildStarted',
  '技能总谱已在后台构建中。': 'skills.graph.serverDetails.buildRunning',
  '已取消技能总谱构建，已完成的缓存和 checkpoint 会保留。': 'skills.graph.serverDetails.cancelRequested',
  '已有技能总谱构建正在运行，请等待完成或先取消当前构建。': 'skills.graph.serverDetails.buildRunning',
  '技能总谱构建已取消，可再次执行增量构建继续。': 'skills.graph.serverDetails.buildCancelled',
  '技能总谱不存在或不完整，请先构建总谱。': 'skills.graph.serverDetails.graphMissing',
};

const SERVER_DETAIL_PREFIX_TRANSLATION_KEYS: Array<{ prefix: string; key: string }> = [
  { prefix: 'Symphony 总谱构建失败:', key: 'skills.graph.errors.buildFailedWithDetail' },
];

function asString(value: unknown, fallback = ''): string {
  if (value === undefined || value === null) return fallback;
  return String(value);
}

function asRecord(value: unknown): RawRecord {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as RawRecord)
    : {};
}

function confidenceValue(value: unknown, fallback: number): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(0, Math.min(1, parsed));
}

function payloadManifest(payload: SkillGraphPayload | null | undefined): RawRecord {
  return asRecord(payload?.graph_manifest ?? payload?.manifest);
}

function graphConfidenceFloor(payload: SkillGraphPayload | null | undefined): number {
  const thresholds = asRecord(payloadManifest(payload).thresholds);
  return confidenceValue(thresholds.can_feed, 0);
}

function graphDefaultConfidence(payload: SkillGraphPayload | null | undefined): number {
  const floor = graphConfidenceFloor(payload);
  const defaultConfidence = confidenceValue(
    payload?.orchestration_min_edge_confidence,
    DEFAULT_MIN_CONFIDENCE,
  );
  return Math.max(floor, defaultConfidence);
}

function asArray(value: unknown): RawRecord[] {
  return Array.isArray(value) ? value.filter((item): item is RawRecord => Boolean(item && typeof item === 'object')) : [];
}

function asDetailItems(value: unknown, requiredLabel: string): DetailListItem[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item, index) => {
      const record = asRecord(item);
      const isRecord = Boolean(item && typeof item === 'object' && !Array.isArray(item));
      const label = asString(
        isRecord ? record.name ?? record.id ?? record.label ?? record.type : item,
      ).trim();
      if (!label) return null;

      const type = asString(record.type ?? record.kind ?? record.format).trim();
      const required = record.required === true ? requiredLabel : '';
      const description = asString(record.description).trim();
      const meta = [type, required, description].filter(Boolean).join(' / ');
      return {
        key: `${label}-${index}`,
        label,
        meta,
      };
    })
    .filter((item): item is DetailListItem => Boolean(item));
}

function typeFromId(id: string): string {
  const [prefix] = id.split(':');
  return prefix || 'skill';
}

function labelFromId(id: string): string {
  return id.replace(/^(skill:|slot:|input:|output:|artifact:|task:|type:)/, '');
}

function normalizeNode(raw: RawRecord, index: number, skillsById: Map<string, RawRecord>): GraphNode {
  const rawId = asString(raw.id ?? raw.node_id ?? raw.skill_id, `node:${index}`);
  const id = rawId.includes(':') ? rawId : `skill:${rawId}`;
  const skillId = id.replace(/^(?:skill|capability):/, '');
  const skill = skillsById.get(skillId);
  const properties = {
    ...asRecord(skill),
    ...asRecord(raw.properties),
  };
  return {
    id,
    type: asString(raw.type ?? raw.entity_type, typeFromId(id)),
    label: asString(raw.label ?? raw.name ?? skill?.name, labelFromId(id)),
    properties,
    x: 0,
    y: 0,
    vx: 0,
    vy: 0,
    degree: 0,
    inDegree: 0,
    outDegree: 0,
  };
}

function normalizeEdge(raw: RawRecord): GraphEdge | null {
  const rawSource = asString(raw.source ?? raw.source_id);
  const rawTarget = asString(raw.target ?? raw.target_id);
  if (!rawSource || !rawTarget) return null;
  const runtimeWeight = Number(raw.runtime_weight);
  return {
    source: rawSource.includes(':') ? rawSource : `skill:${rawSource}`,
    target: rawTarget.includes(':') ? rawTarget : `skill:${rawTarget}`,
    type: asString(raw.type ?? raw.relation_type, 'relates_to'),
    confidence: Number(raw.confidence ?? 1),
    runtimeWeight: Number.isFinite(runtimeWeight) ? runtimeWeight : undefined,
    method: asString(raw.method, 'deterministic'),
    evidence: asRecord(raw.evidence ?? raw.metadata),
  };
}

function normalizeGraph(payload: SkillGraphPayload): NormalizedGraph {
  const skillPayload = payload.skills;
  const skills = Array.isArray(skillPayload)
    ? asArray(skillPayload)
    : asArray((skillPayload as { skills?: unknown } | undefined)?.skills ?? payload.graph?.skills);
  const skillsById = new Map(skills.map((skill) => [asString(skill.id), skill]));
  const nodeMap = new Map<string, GraphNode>();

  asArray(payload.graph?.nodes).forEach((node, index) => {
    const normalized = normalizeNode(node, index, skillsById);
    nodeMap.set(normalized.id, normalized);
  });

  skills.forEach((skill) => {
    const skillId = asString(skill.id);
    if (!skillId) return;
    const id = `skill:${skillId}`;
    if (!nodeMap.has(id)) {
      nodeMap.set(
        id,
        normalizeNode({ id, type: 'skill', label: skill.name, properties: skill }, nodeMap.size, skillsById),
      );
    }
  });

  const edges = asArray(payload.graph?.edges)
    .map(normalizeEdge)
    .filter((edge): edge is GraphEdge => {
      if (!edge) return false;
      return nodeMap.has(edge.source) && nodeMap.has(edge.target);
    });

  const nodes = [...nodeMap.values()];
  const byId = new Map(nodes.map((node) => [node.id, node]));
  edges.forEach((edge) => {
    const source = byId.get(edge.source);
    const target = byId.get(edge.target);
    if (source) {
      source.degree += 1;
      source.outDegree += 1;
    }
    if (target) {
      target.degree += 1;
      target.inDegree += 1;
    }
  });
  seedPositions(nodes, 920, 620);
  return { nodes, edges };
}

function nodeSearchText(node: GraphNode): string {
  const props = node.properties || {};
  const values = [
    node.label,
    node.id,
    props.description,
    props.summary,
    ...(Array.isArray(props.tasks) ? props.tasks : []),
    ...(Array.isArray(props.skill_tags) ? props.skill_tags : []),
    ...(Array.isArray(props.data_tags) ? props.data_tags : []),
  ];
  return values.map((value) => String(value || '')).join(' ');
}

function isSkillNode(node: GraphNode): boolean {
  return node.type === 'skill' || node.id.startsWith('skill:');
}

function nodeRadius(node: GraphNode): number {
  const base = node.type === 'skill' ? 9 : 7;
  return Math.min(24, base + Math.sqrt(Math.max(0, node.degree)) * 2.1);
}

function truncate(value: string, limit: number): string {
  return value.length > limit ? `${value.slice(0, limit - 1)}...` : value;
}

function progressPercent(progress: BuildProgress | null): number {
  if (!progress || typeof progress.percent !== 'number') return 0;
  return Math.max(0, Math.min(100, Math.round(progress.percent)));
}

function isBuildRunningPayload(data: { build_progress?: BuildProgress }): boolean {
  return data.build_progress?.status === 'running';
}

function isTerminalBuildStatus(status: BuildProgress['status'] | undefined): boolean {
  return status === 'success' || status === 'error' || status === 'cancelled';
}

function buildStageLabel(stage: string, fallback: string, t: Translate): string {
  const key = BUILD_STAGE_TRANSLATION_KEYS[stage];
  if (!key) return fallback || stage || t('skills.graph.buildLogFallback');
  return t(`skills.graph.buildStages.${key}`, {
    defaultValue: fallback || stage || t('skills.graph.buildLogFallback'),
  });
}

function buildProgressLabel(progress: BuildProgress | null, updating: boolean, t: Translate): string {
  if (progress) {
    return buildStageLabel(
      asString(progress.stage),
      asString(progress.label, t('skills.graph.buildLogFallback')),
      t,
    );
  }
  return updating ? t('skills.graph.status.refreshing') : t('skills.graph.status.noBuildLogs');
}

function buildLogSummary(entry: BuildLogEntry, t: Translate): string {
  const label = buildStageLabel(
    asString(entry.stage),
    asString(entry.label || entry.stage, t('skills.graph.buildLogFallback')),
    t,
  );
  if (entry.stage === 'update.done') return label;
  const hasGlobalCandidateProgress = entry.stage === 'graph.resolve.progress'
    && entry.completed_candidate_count !== undefined
    && entry.total_candidate_count !== undefined;
  const countKeys: Array<[string, string?]> = hasGlobalCandidateProgress
    ? [['completed_candidate_count', 'total_candidate_count']]
    : [
      ['current', 'total'],
      ['skill_count', undefined],
      ['changed_count', undefined],
      ['removed_count', undefined],
      ['edge_count', undefined],
      ['diagnostics_count', undefined],
    ];
  const counts = countKeys
    .map(([key, totalKey]) => {
      const value = entry[key];
      if (value === undefined || value === null) return '';
      const total = totalKey ? entry[totalKey] : undefined;
      return total === undefined || total === null ? String(value) : formatBuildCount(value, total);
    })
    .filter(Boolean);
  return counts.length ? `${label} · ${counts.join(' · ')}` : label;
}

function formatBuildCount(value: unknown, total: unknown): string {
  const parsedValue = Number(value);
  const parsedTotal = Number(total);
  if (!Number.isFinite(parsedValue) || !Number.isFinite(parsedTotal) || parsedTotal <= 0) {
    return `${String(value)}/${String(total)}`;
  }
  return `${Math.max(0, Math.min(Math.round(parsedValue), Math.round(parsedTotal)))}/${Math.round(parsedTotal)}`;
}

function localizedServerDetail(detail: unknown, fallbackKey: string, t: Translate): string {
  const text = asString(detail).trim();
  if (!text) return t(fallbackKey);
  const exactKey = SERVER_DETAIL_TRANSLATION_KEYS[text];
  if (exactKey) return t(exactKey);
  const prefixMatch = SERVER_DETAIL_PREFIX_TRANSLATION_KEYS.find(({ prefix }) => text.startsWith(prefix));
  if (prefixMatch) {
    return t(prefixMatch.key, { detail: text.slice(prefixMatch.prefix.length).trim() });
  }
  return text;
}

function normalizeTokenCount(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.round(parsed) : 0;
}

function tokenUsageTotal(usage: LLMTokenUsageSummary | null): LLMTokenUsageTotals | null {
  const total = usage?.total;
  if (!total || typeof total !== 'object') return null;
  const totalTokens = normalizeTokenCount(total.total_tokens);
  if (totalTokens <= 0) return null;
  return {
    prompt_tokens: normalizeTokenCount(total.prompt_tokens),
    completion_tokens: normalizeTokenCount(total.completion_tokens),
    total_tokens: totalTokens,
  };
}

function formatTokenUsage(usage: LLMTokenUsageSummary | null, t: Translate): string {
  const total = tokenUsageTotal(usage);
  if (!total) return '';
  return t('skills.graph.tokenUsage', {
    prompt: (total.prompt_tokens || 0).toLocaleString(),
    completion: (total.completion_tokens || 0).toLocaleString(),
    total: (total.total_tokens || 0).toLocaleString(),
  });
}

function parseBuildLogTime(entry: BuildLogEntry | undefined): number {
  const ts = asString(entry?.ts);
  if (!ts) return 0;
  const parsed = new Date(ts).getTime();
  return Number.isFinite(parsed) ? parsed : 0;
}

function formatElapsedTime(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const paddedMinutes = String(minutes).padStart(2, '0');
  const paddedSeconds = String(seconds).padStart(2, '0');
  return hours > 0
    ? `${hours}:${paddedMinutes}:${paddedSeconds}`
    : `${minutes}:${paddedSeconds}`;
}

function buildElapsedText(
  entries: BuildLogEntry[],
  progress: BuildProgress | null,
  now: number,
  startTime: number | null,
): string {
  const start = startTime || parseBuildLogTime(entries[0]);
  if (!start) return '';
  const latest = parseBuildLogTime(entries[entries.length - 1]);
  const end = progress?.status === 'running' ? now : latest || now;
  return formatElapsedTime(end - start);
}

function compactBuildLog(entries: BuildLogEntry[]): BuildLogEntry[] {
  const compacted: BuildLogEntry[] = [];
  const indexByGroup = new Map<string, number>();
  entries.forEach((entry) => {
    const key = buildLogGroupKey(entry);
    if (!key) {
      compacted.push(entry);
      return;
    }
    const existingIndex = indexByGroup.get(key);
    if (existingIndex === undefined) {
      indexByGroup.set(key, compacted.length);
      compacted.push(entry);
      return;
    }
    compacted[existingIndex] = entry;
  });
  const activeGroups = new Set(
    compacted
      .filter((entry) => isActiveBuildLogEntry(entry))
      .map(buildLogGroupKey)
      .filter(Boolean),
  );
  return compacted.filter((entry, index) => {
    if (isCompletedBuildLogEntry(entry)) return false;
    if (isSupersededBuildStart(entry, index, compacted)) return false;
    if (activeGroups.size > 0 && !isActiveBuildLogEntry(entry) && !isTerminalBuildLogEntry(entry)) {
      return false;
    }
    const key = buildLogGroupKey(entry);
    return !key || !activeGroups.has(key) || isActiveBuildLogEntry(entry);
  });
}

function buildLogGroupKey(entry: BuildLogEntry): string {
  const stage = asString(entry.stage);
  if (stage.startsWith('fingerprint.')) {
    return stage;
  }
  if (stage === 'graph.resolve.progress') {
    return 'graph.resolve';
  }
  return '';
}

function isActiveBuildLogEntry(entry: BuildLogEntry): boolean {
  const stage = asString(entry.stage);
  return stage === 'graph.resolve.progress' || (
    stage.startsWith('fingerprint.')
    && stage !== 'fingerprint.done'
    && stage !== 'fingerprint.reuse'
  );
}

function isCompletedBuildLogEntry(entry: BuildLogEntry): boolean {
  const stage = asString(entry.stage);
  return stage === 'scan.done'
    || stage === 'diff.done'
    || stage === 'fingerprint.done'
    || stage === 'fingerprint.reuse'
    || stage === 'graph.registry.done'
    || stage === 'graph.candidates.done'
    || stage === 'graph.resolve.done'
    || stage === 'graph.materialize.done'
    || stage === 'graph.lookup.done'
    || stage === 'graph.build.done'
    || stage === 'artifact.fingerprints.write.done'
    || stage === 'artifact.graph.write.done'
    || stage === 'state.write.done';
}

function isSupersededBuildStart(entry: BuildLogEntry, index: number, entries: BuildLogEntry[]): boolean {
  const stage = asString(entry.stage);
  if (stage === 'update.start' && entries.length > index + 1) {
    return true;
  }
  const doneStageByStart: Record<string, string> = {
    'scan.start': 'scan.done',
    'artifact.fingerprints.write.start': 'artifact.fingerprints.write.done',
    'graph.registry.start': 'graph.registry.done',
    'graph.candidates.start': 'graph.candidates.done',
    'graph.resolve.start': 'graph.resolve.done',
    'graph.materialize.start': 'graph.materialize.done',
    'graph.lookup.start': 'graph.lookup.done',
    'graph.build.start': 'graph.build.done',
    'artifact.graph.write.start': 'artifact.graph.write.done',
    'state.write.start': 'state.write.done',
  };
  const doneStage = doneStageByStart[stage];
  return Boolean(doneStage && entries.slice(index + 1).some((item) => asString(item.stage) === doneStage));
}

function isTerminalBuildLogEntry(entry: BuildLogEntry): boolean {
  const stage = asString(entry.stage);
  return stage === 'update.done' || stage === 'update.failed' || stage === 'update.cancelled';
}

function buildLogTime(entry: BuildLogEntry): string {
  const ts = asString(entry.ts);
  if (!ts) return '';
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString();
}

function buildLogSignature(entries?: BuildLogEntry[]): string {
  if (!Array.isArray(entries) || entries.length === 0) return '';
  const latest = entries[entries.length - 1];
  return [
    latest.ts,
    latest.stage,
    latest.current,
    latest.total,
    latest.label,
  ].map((item) => asString(item)).join('|');
}

export const SkillGraphPanel = forwardRef<SkillGraphPanelHandle, SkillGraphPanelProps>(function SkillGraphPanel(
  { onReadingChange, externalError, onExternalErrorClear },
  ref,
) {
  const { t } = useTranslation();
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const graphRef = useRef<NormalizedGraph>({ nodes: [], edges: [] });
  const visibleRef = useRef<NormalizedGraph>({ nodes: [], edges: [] });
  const layoutComponentsRef = useRef<ReturnType<typeof computeConnectedComponents>>([]);
  const transformRef = useRef<Transform>({ x: 0, y: 0, scale: 1 });
  const selectedRef = useRef<GraphNode | null>(null);
  const hoveredRef = useRef<GraphNode | null>(null);
  const externalBuildRunningRef = useRef(false);
  const observedBuildLogSignatureRef = useRef<string | null>(null);
  const autoFitRequestRef = useRef(0);
  const canvasSizeRef = useRef({ width: 0, height: 0 });
  const minConfidenceTouchedRef = useRef(false);
  const dragRef = useRef<{ active: boolean; moved: boolean; x: number; y: number }>({
    active: false,
    moved: false,
    x: 0,
    y: 0,
  });

  const [graph, setGraph] = useState<NormalizedGraph>({ nodes: [], edges: [] });
  const [payload, setPayload] = useState<SkillGraphPayload | null>(null);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [query, setQuery] = useState('');
  const [minConfidence, setMinConfidence] = useState(DEFAULT_MIN_CONFIDENCE);
  const [loading, setLoading] = useState(false);
  const [updating, setUpdating] = useState(false);
  const [buildMode, setBuildMode] = useState<SymphonyBuildMode | null>(null);
  const [cancellingBuild, setCancellingBuild] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [buildLog, setBuildLog] = useState<BuildLogEntry[]>([]);
  const [buildProgress, setBuildProgress] = useState<BuildProgress | null>(null);
  const [tokenUsage, setTokenUsage] = useState<LLMTokenUsageSummary | null>(null);
  const [showBuildLogPanel, setShowBuildLogPanel] = useState(false);
  const [buildElapsedNow, setBuildElapsedNow] = useState(() => Date.now());
  const [buildElapsedStart, setBuildElapsedStart] = useState<number | null>(null);
  const buildProgressStatusRef = useRef<BuildProgress['status'] | undefined>(undefined);
  const [autoFitRequest, setAutoFitRequest] = useState(0);

  const applyBuildLog = useCallback((data: { build_log?: BuildLogEntry[]; build_progress?: BuildProgress; llm_token_usage?: LLMTokenUsageSummary }) => {
    const nextStatus = data.build_progress?.status;
    const resetElapsedStart = nextStatus === 'running' && buildProgressStatusRef.current !== 'running';
    if (Array.isArray(data.build_log)) {
      const nextBuildLog = data.build_log;
      const nextStart = parseBuildLogTime(nextBuildLog[0]);
      setBuildElapsedStart((current) => {
        if (!nextBuildLog.length) return null;
        if (!nextStart) return resetElapsedStart ? null : current;
        if (resetElapsedStart || current === null || nextStart < current) return nextStart;
        return current;
      });
      setBuildLog(nextBuildLog);
      observedBuildLogSignatureRef.current = buildLogSignature(nextBuildLog);
    }
    if (data.build_progress) {
      setBuildProgress(data.build_progress);
      buildProgressStatusRef.current = data.build_progress.status;
    }
    const nextTokenUsage = data.llm_token_usage || data.build_progress?.llm_token_usage;
    if (tokenUsageTotal(nextTokenUsage || null)) {
      setTokenUsage(nextTokenUsage || null);
    }
  }, []);

  const resetBuildUiOnTerminalStatus = useCallback((data: { detail?: string; cancelled?: boolean; build_progress?: BuildProgress }): boolean => {
    const status = data.build_progress?.status ?? (data.cancelled ? 'cancelled' : undefined);
    if (!isTerminalBuildStatus(status)) return false;
    externalBuildRunningRef.current = false;
    setUpdating(false);
    setBuildMode(null);
    setLoading(false);
    if (status === 'error') {
      setError(data.detail || data.build_progress?.label || t('skills.graph.errors.refreshFailed'));
    }
    return true;
  }, []);

  useEffect(() => {
    graphRef.current = graph;
  }, [graph]);

  useEffect(() => {
    if (buildProgress?.status !== 'running') return undefined;
    setBuildElapsedNow(Date.now());
    const timer = window.setInterval(() => {
      setBuildElapsedNow(Date.now());
    }, 1000);
    return () => window.clearInterval(timer);
  }, [buildProgress?.status]);

  const visible = useMemo(() => {
    const text = query.trim().toLowerCase();
    const nodeById = new Map(graph.nodes.map((node) => [node.id, node]));
    const previousById = new Map(visibleRef.current.nodes.map((node) => [node.id, node]));
    const matchedSkillIds = new Set(
      graph.nodes
        .filter(isSkillNode)
        .filter((node) => !text || nodeSearchText(node).toLowerCase().includes(text))
        .map((node) => node.id),
    );
    let edges = graph.edges.filter((edge) => {
      if (edge.confidence < minConfidence) return false;
      const source = nodeById.get(edge.source);
      const target = nodeById.get(edge.target);
      if (!source || !target) return false;
      if (!text) return true;
      return matchedSkillIds.has(edge.source) || matchedSkillIds.has(edge.target);
    });

    const linkedIds = new Set<string>();
    edges.forEach((edge) => {
      linkedIds.add(edge.source);
      linkedIds.add(edge.target);
    });

    const nodes = graph.nodes.filter((node) => {
      if (!text) return linkedIds.has(node.id) || graph.edges.length === 0;
      return linkedIds.has(node.id) || matchedSkillIds.has(node.id);
    }).map((node) => {
      const previous = previousById.get(node.id);
      return {
        ...node,
        x: previous?.x ?? node.x,
        y: previous?.y ?? node.y,
        vx: previous?.vx ?? node.vx,
        vy: previous?.vy ?? node.vy,
        degree: 0,
        inDegree: 0,
        outDegree: 0,
      };
    });

    const visibleIds = new Set(nodes.map((node) => node.id));
    edges = edges.filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target));
    const visibleById = new Map(nodes.map((node) => [node.id, node]));
    edges.forEach((edge) => {
      const source = visibleById.get(edge.source);
      const target = visibleById.get(edge.target);
      if (source) {
        source.degree += 1;
        source.outDegree += 1;
      }
      if (target) {
        target.degree += 1;
        target.inDegree += 1;
      }
    });
    return { nodes, edges };
  }, [graph, minConfidence, query]);

  useEffect(() => {
    visibleRef.current = visible;
    layoutComponentsRef.current = computeConnectedComponents(visible.nodes, visible.edges);
    if (selectedRef.current) {
      const visibleSelected = visible.nodes.find((node) => node.id === selectedRef.current?.id);
      if (visibleSelected) {
        selectedRef.current = visibleSelected;
        setSelectedNode(visibleSelected);
      } else {
        selectedRef.current = null;
        setSelectedNode(null);
      }
    }
  }, [visible]);

  const fitView = useCallback(() => {
    const canvas = canvasRef.current;
    const nodes = visibleRef.current.nodes;
    if (!canvas || nodes.length === 0) return;
    const rect = canvas.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const xs = nodes.map((node) => node.x);
    const ys = nodes.map((node) => node.y);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const graphW = Math.max(1, maxX - minX);
    const graphH = Math.max(1, maxY - minY);
    const horizontalPadding = Math.min(80, rect.width * 0.2);
    const verticalPadding = Math.min(80, rect.height * 0.2);
    const scale = Math.max(
      0.18,
      Math.min(2.2, Math.min(
        Math.max(1, rect.width - horizontalPadding) / graphW,
        Math.max(1, rect.height - verticalPadding) / graphH,
      )),
    );
    transformRef.current = {
      scale,
      x: rect.width / 2 - ((minX + maxX) / 2) * scale,
      y: rect.height / 2 - ((minY + maxY) / 2) * scale,
    };
  }, []);

  const requestAutoFit = useCallback(() => {
    autoFitRequestRef.current += 1;
    setAutoFitRequest(autoFitRequestRef.current);
  }, []);

  useEffect(() => {
    if (autoFitRequest === 0 || visible.nodes.length === 0) return undefined;
    let firstFrame = 0;
    let secondFrame = 0;
    let settleTimer = 0;
    let finalTimer = 0;
    firstFrame = window.requestAnimationFrame(() => {
      secondFrame = window.requestAnimationFrame(() => {
        fitView();
      });
    });
    settleTimer = window.setTimeout(() => {
      fitView();
    }, 320);
    finalTimer = window.setTimeout(() => {
      fitView();
    }, 900);
    return () => {
      window.cancelAnimationFrame(firstFrame);
      window.cancelAnimationFrame(secondFrame);
      window.clearTimeout(settleTimer);
      window.clearTimeout(finalTimer);
    };
  }, [autoFitRequest, fitView, visible.nodes.length, visible.edges.length]);

  const loadGraph = useCallback(async (clearExternalError = false) => {
    if (clearExternalError) {
      onExternalErrorClear?.();
    }
    setLoading(true);
    let keepLoading = false;
    try {
      const data = await webRequest<SkillGraphPayload>('skills.graph.get', {}, { timeoutMs: 60_000 });
      applyBuildLog(data);
      if (!data.success) {
        if (isBuildRunningPayload(data)) {
          setShowBuildLogPanel(true);
          keepLoading = true;
          return;
        }
        throw new Error(localizedServerDetail(data.detail, 'skills.graph.errors.readFailed', t));
      }
      const normalized = normalizeGraph(data);
      setPayload(data);
      setGraph(normalized);
      setMinConfidence((current) => {
        if (!minConfidenceTouchedRef.current) {
          return graphDefaultConfidence(data);
        }
        return Math.max(graphConfidenceFloor(data), confidenceValue(current, 1));
      });
      selectedRef.current = null;
      setSelectedNode(null);
      setError(null);
      requestAutoFit();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPayload(null);
      setGraph({ nodes: [], edges: [] });
    } finally {
      if (!keepLoading) {
        setLoading(false);
      }
    }
  }, [applyBuildLog, onExternalErrorClear, requestAutoFit, t]);

  const restoreBuildStatus = useCallback(async (): Promise<boolean> => {
    const data = await webRequest<SkillGraphStatus>(
      'skills.graph.status',
      {},
      { timeoutMs: 60_000 },
    );
    applyBuildLog(data);
    const isRunning = isBuildRunningPayload(data);
    externalBuildRunningRef.current = isRunning;
    if (isRunning) {
      setShowBuildLogPanel(true);
      setLoading(true);
      return true;
    }
    return false;
  }, [applyBuildLog]);

  const rebuildGraph = useCallback(async (mode: SymphonyBuildMode) => {
    const force = mode === 'full';
    setBuildElapsedStart(null);
    setUpdating(true);
    setBuildMode(mode);
    setShowBuildLogPanel(true);
    setError(null);
    onExternalErrorClear?.();
    setTokenUsage(null);
    setBuildProgress({
      stage: 'update.start',
      label: force ? t('skills.graph.status.prepareFull') : t('skills.graph.status.prepareIncremental'),
      percent: 3,
      status: 'running',
    });
    try {
      const data = await webRequest<SkillGraphUpdate>(
        'skills.graph.build',
        { force },
        { timeoutMs: 60_000 },
      );
      applyBuildLog(data);
      if (!data.success) {
        throw new Error(localizedServerDetail(data.detail, 'skills.graph.errors.refreshFailed', t));
      }
      externalBuildRunningRef.current = true;
    } catch (err) {
      const error = err instanceof Error ? err : new Error(String(err));
      externalBuildRunningRef.current = false;
      setError(error.message);
      setUpdating(false);
      setBuildMode(null);
      setBuildProgress((current) => ({
        ...current,
        label: error.message,
        status: 'error',
      }));
      throw error;
    }
  }, [applyBuildLog, onExternalErrorClear, t]);

  const cancelBuild = useCallback(async () => {
    setCancellingBuild(true);
    setShowBuildLogPanel(true);
    setError(null);
    onExternalErrorClear?.();
    try {
      const data = await webRequest<SkillGraphUpdate>(
        'skills.graph.cancel',
        {},
        { timeoutMs: 60_000 },
      );
      applyBuildLog(data);
      if (resetBuildUiOnTerminalStatus(data)) {
        return;
      }
      if (!data.success && data.build_status !== 'idle') {
        throw new Error(localizedServerDetail(data.detail, 'skills.graph.errors.cancelFailed', t));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCancellingBuild(false);
    }
  }, [applyBuildLog, onExternalErrorClear, resetBuildUiOnTerminalStatus, t]);

  const cancelActiveBuild = useCallback(async () => {
    setCancellingBuild(true);
    setError(null);
    try {
      const data = await webRequest<SkillGraphUpdate>(
        'skills.graph.cancel',
        {},
        { timeoutMs: 60_000 },
      );
      if (data.build_status === 'idle') {
        return;
      }
      applyBuildLog(data);
      if (resetBuildUiOnTerminalStatus(data)) {
        return;
      }
      if (!data.success) {
        throw new Error(localizedServerDetail(data.detail, 'skills.graph.errors.cancelFailed', t));
      }
    } catch (err) {
      const error = err instanceof Error ? err : new Error(String(err));
      setError(error.message);
      throw error;
    } finally {
      setCancellingBuild(false);
    }
  }, [applyBuildLog, resetBuildUiOnTerminalStatus, t]);

  useEffect(() => {
    let stopped = false;
    const restore = async () => {
      try {
        const isRunning = await restoreBuildStatus();
        if (!stopped && !isRunning) {
          await loadGraph();
        }
      } catch {
        if (!stopped) {
          await loadGraph();
        }
      }
    };

    void restore();
    return () => {
      stopped = true;
    };
  }, [loadGraph, restoreBuildStatus]);

  useEffect(() => {
    if (!updating) return undefined;

    let stopped = false;
    let timer: number | null = null;
    const poll = async () => {
      try {
        const data = await webRequest<SkillGraphStatus>(
          'skills.graph.status',
          {},
          { timeoutMs: 60_000 },
        );
        if (!stopped) {
          setShowBuildLogPanel(true);
          applyBuildLog(data);
          const status = data.build_progress?.status;
          if (resetBuildUiOnTerminalStatus(data)) {
            if (status === 'success') {
              void loadGraph();
            }
            return;
          }
        }
      } catch {
        // 轮询只用于补充进度日志；失败不覆盖主更新请求的错误处理。
      }
      if (!stopped) {
        timer = window.setTimeout(() => {
          void poll();
        }, 1500);
      }
    };

    void poll();
    return () => {
      stopped = true;
      if (timer !== null) {
        window.clearTimeout(timer);
      }
    };
  }, [applyBuildLog, loadGraph, resetBuildUiOnTerminalStatus, updating]);

  useEffect(() => {
    if (updating) return undefined;

    let stopped = false;
    let timer: number | null = null;
    const poll = async () => {
      let nextDelay = 3000;
      try {
        const data = await webRequest<SkillGraphStatus>(
          'skills.graph.status',
          {},
          { timeoutMs: 60_000 },
        );
        if (!stopped) {
          const status = data.build_progress?.status;
          const wasRunning = externalBuildRunningRef.current;
          if (status === 'running') {
            setShowBuildLogPanel(true);
            setLoading(true);
            nextDelay = 1000;
          }
          applyBuildLog(data);
          externalBuildRunningRef.current = status === 'running';
          if (wasRunning && status === 'success') {
            setLoading(false);
            void loadGraph();
          } else if (status !== 'running') {
            setLoading(false);
          }
        }
      } catch {
        // 被动轮询只用于同步对话侧触发的图谱进度，不影响当前图谱交互。
      }
      if (!stopped) {
        timer = window.setTimeout(() => {
          void poll();
        }, nextDelay);
      }
    };

    void poll();
    return () => {
      stopped = true;
      if (timer !== null) {
        window.clearTimeout(timer);
      }
    };
  }, [applyBuildLog, loadGraph, updating]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const resizeCanvas = () => {
      const rect = canvas.getBoundingClientRect();
      const previousSize = canvasSizeRef.current;
      const becameVisible = (previousSize.width <= 0 || previousSize.height <= 0) && rect.width > 0 && rect.height > 0;
      const resized = Math.abs(previousSize.width - rect.width) > 2 || Math.abs(previousSize.height - rect.height) > 2;
      canvasSizeRef.current = { width: rect.width, height: rect.height };
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(rect.width * dpr));
      canvas.height = Math.max(1, Math.floor(rect.height * dpr));
      const ctx = canvas.getContext('2d');
      ctx?.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (transformRef.current.x === 0 && transformRef.current.y === 0) {
        transformRef.current = { x: rect.width / 2, y: rect.height / 2, scale: 1 };
      }
      if ((becameVisible || resized) && visibleRef.current.nodes.length > 0) {
        requestAutoFit();
      }
    };

    resizeCanvas();
    const observer = new ResizeObserver(resizeCanvas);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [requestAutoFit]);

  useEffect(() => {
    let frame = 0;
    let mounted = true;

    const stepSimulation = () => {
      const nodes = visibleRef.current.nodes;
      const edges = visibleRef.current.edges;
      const canvas = canvasRef.current;
      if (!canvas || nodes.length === 0) return;
      const width = canvas.clientWidth || 900;
      const height = canvas.clientHeight || 620;
      stepSkillGraphLayout(
        nodes,
        edges,
        width,
        height,
        layoutComponentsRef.current,
        COMPONENT_CENTER_ATTRACTION_STRENGTH,
      );
    };

    const draw = () => {
      const canvas = canvasRef.current;
      const ctx = canvas?.getContext('2d');
      if (!canvas || !ctx) return;
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      const pixelRatioX = canvas.width / Math.max(1, width);
      const pixelRatioY = canvas.height / Math.max(1, height);
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.setTransform(pixelRatioX, 0, 0, pixelRatioY, 0, 0);
      ctx.save();
      ctx.translate(transformRef.current.x, transformRef.current.y);
      ctx.scale(transformRef.current.scale, transformRef.current.scale);

      const nodeById = new Map(visibleRef.current.nodes.map((node) => [node.id, node]));
      const drawableNodeIds = new Set(
        visibleRef.current.nodes
          .filter((node) => {
            const radius = nodeRadius(node) * transformRef.current.scale + 2;
            const screenX = transformRef.current.x + node.x * transformRef.current.scale;
            const screenY = transformRef.current.y + node.y * transformRef.current.scale;
            return screenX - radius >= 0
              && screenX + radius <= width
              && screenY - radius >= 0
              && screenY + radius <= height;
          })
          .map((node) => node.id),
      );
      const selectedId = selectedRef.current?.id;
      const focusId = selectedId || hoveredRef.current?.id;
      const relatedNodeIds = new Set<string>();
      if (focusId) {
        visibleRef.current.edges.forEach((edge) => {
          if (edge.source === focusId) relatedNodeIds.add(edge.target);
          if (edge.target === focusId) relatedNodeIds.add(edge.source);
        });
      }
      visibleRef.current.edges.forEach((edge) => {
        if (!drawableNodeIds.has(edge.source) || !drawableNodeIds.has(edge.target)) return;
        const source = nodeById.get(edge.source);
        const target = nodeById.get(edge.target);
        if (!source || !target) return;
        const active = Boolean(focusId && (edge.source === focusId || edge.target === focusId));
        ctx.strokeStyle = active ? GRAPH_EDGE_HIGHLIGHT : GRAPH_EDGE_DEFAULT;
        ctx.globalAlpha = active ? 0.9 : focusId ? 0.5 : 0.72;
        ctx.lineWidth = active ? 1.8 : 1;
        ctx.beginPath();
        ctx.moveTo(source.x, source.y);
        ctx.lineTo(target.x, target.y);
        ctx.stroke();
        ctx.globalAlpha = 1;

        const angle = Math.atan2(target.y - source.y, target.x - source.x);
        const radius = nodeRadius(target);
        const x = target.x - Math.cos(angle) * radius;
        const y = target.y - Math.sin(angle) * radius;
        ctx.globalAlpha = active ? 0.92 : focusId ? 0.48 : 0.68;
        ctx.fillStyle = ctx.strokeStyle;
        ctx.beginPath();
        ctx.moveTo(x, y);
        ctx.lineTo(x - Math.cos(angle - 0.5) * 8, y - Math.sin(angle - 0.5) * 8);
        ctx.lineTo(x - Math.cos(angle + 0.5) * 8, y - Math.sin(angle + 0.5) * 8);
        ctx.closePath();
        ctx.fill();
        ctx.globalAlpha = 1;
      });

      visibleRef.current.nodes.forEach((node) => {
        if (!drawableNodeIds.has(node.id)) return;
        const selected = selectedId === node.id;
        const hovered = hoveredRef.current?.id === node.id;
        const radius = nodeRadius(node);
        const focused = focusId === node.id;
        const highlighted = Boolean(focusId && (focused || relatedNodeIds.has(node.id)) && !selected);
        const dimmed = Boolean(focusId && !focused && !relatedNodeIds.has(node.id));
        const displayRadius = selected ? radius + 2 : radius;
        ctx.save();
        if (selected) {
          const fill = ctx.createRadialGradient(
            node.x - displayRadius * 0.35,
            node.y - displayRadius * 0.4,
            displayRadius * 0.08,
            node.x,
            node.y,
            displayRadius * 1.15,
          );
          fill.addColorStop(0, '#78b5ff');
          fill.addColorStop(0.52, '#2b8cff');
          fill.addColorStop(1, '#0668f7');
          ctx.fillStyle = fill;
          ctx.strokeStyle = '#ffffff';
          ctx.lineWidth = 2.6;
          ctx.shadowColor = 'rgba(22, 119, 255, 0.32)';
          ctx.shadowBlur = 16;
        } else {
          ctx.fillStyle = dimmed
            ? GRAPH_NODE_DIMMED
            : highlighted || hovered
              ? GRAPH_NODE_HIGHLIGHT
              : GRAPH_NODE_DEFAULT;
          ctx.strokeStyle = 'rgba(255, 255, 255, 0.72)';
          ctx.lineWidth = 1;
        }
        ctx.beginPath();
        ctx.arc(node.x, node.y, displayRadius, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
        ctx.restore();

        if (transformRef.current.scale > 0.42 || selected || hovered) {
          ctx.font = `${selected ? 700 : highlighted || hovered ? 600 : 400} ${selected ? 13 : 12}px Inter, system-ui, sans-serif`;
          ctx.fillStyle = dimmed
            ? GRAPH_LABEL_DIMMED
            : selected || highlighted || hovered
              ? GRAPH_LABEL_ACTIVE
              : GRAPH_LABEL_DEFAULT;
          ctx.textAlign = 'center';
          ctx.textBaseline = 'top';
          ctx.fillText(truncate(node.label, 26), node.x, node.y + displayRadius + 5);
        }
      });

      ctx.restore();
    };

    const tick = () => {
      if (!mounted) return;
      stepSimulation();
      draw();
      frame = window.requestAnimationFrame(tick);
    };
    tick();
    return () => {
      mounted = false;
      window.cancelAnimationFrame(frame);
    };
  }, []);

  const screenToWorld = useCallback((x: number, y: number) => ({
    x: (x - transformRef.current.x) / transformRef.current.scale,
    y: (y - transformRef.current.y) / transformRef.current.scale,
  }), []);

  const findNodeAt = useCallback((clientX: number, clientY: number) => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    const point = screenToWorld(clientX - rect.left, clientY - rect.top);
    for (let i = visibleRef.current.nodes.length - 1; i >= 0; i -= 1) {
      const node = visibleRef.current.nodes[i];
      const hit = nodeRadius(node) + 5 / transformRef.current.scale;
      if (Math.hypot(node.x - point.x, node.y - point.y) <= hit) return node;
    }
    return null;
  }, [screenToWorld]);

  const selectNode = useCallback((node: GraphNode | null) => {
    selectedRef.current = node;
    setSelectedNode(node);
  }, []);

  const zoomAt = useCallback((factor: number, clientX?: number, clientY?: number) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const cx = clientX === undefined ? rect.width / 2 : clientX - rect.left;
    const cy = clientY === undefined ? rect.height / 2 : clientY - rect.top;
    const before = screenToWorld(cx, cy);
    const scale = Math.max(0.12, Math.min(4, transformRef.current.scale * factor));
    transformRef.current = {
      scale,
      x: cx - before.x * scale,
      y: cy - before.y * scale,
    };
  }, [screenToWorld]);

  const relatedEdges = useMemo(() => {
    if (!selectedNode) return [];
    return visible.edges.filter(
      (edge) => edge.source === selectedNode.id || edge.target === selectedNode.id,
    );
  }, [selectedNode, visible.edges]);

  const visibleSkillNodes = useMemo(
    () => visible.nodes.filter(isSkillNode),
    [visible.nodes],
  );

  const isGraphBuildRunning = buildProgress?.status === 'running';
  const isGraphBuildCancelled = buildProgress?.status === 'cancelled';
  const isBusy = loading || updating;
  const canCancelBuild = (updating || isGraphBuildRunning) && !cancellingBuild;
  const isIncrementalBuild = updating && buildMode === 'incremental';
  const isFullBuild = updating && buildMode === 'full';
  const manifest = payloadManifest(payload);
  const graphMinConfidence = graphConfidenceFloor(payload);
  const createdAt = asString(manifest.created_at);
  const graphUpdatedAt = createdAt ? new Date(createdAt).toLocaleString() : '';
  const currentProgressPercent = progressPercent(buildProgress);
  const progressLabel = buildProgressLabel(buildProgress, updating, t);
  const progressTitle = isGraphBuildRunning
    ? t('skills.graph.status.refreshing')
    : isGraphBuildCancelled
    ? t('skills.graph.status.cancelled')
    : progressLabel;
  const recentBuildLog = compactBuildLog(buildLog).slice(-8);
  const tokenUsageText = formatTokenUsage(tokenUsage, t);
  const elapsedText = buildElapsedText(buildLog, buildProgress, buildElapsedNow, buildElapsedStart);
  const buildMetricsText = [tokenUsageText, elapsedText].filter(Boolean).join(' · ');

  const detailTasks = selectedNode ? asDetailItems(selectedNode.properties.tasks, t('skills.graph.required')) : [];
  const visibleError = externalError || error;

  useImperativeHandle(ref, () => ({
    refresh: () => {
      if (isBusy) {
        return false;
      }
      void loadGraph(true);
      return true;
    },
    startIncrementalBuild: () => rebuildGraph('incremental'),
    cancelActiveBuild,
  }), [cancelActiveBuild, isBusy, loadGraph, rebuildGraph]);

  useEffect(() => {
    onReadingChange?.(loading);
  }, [loading, onReadingChange]);

  useEffect(() => () => {
    onReadingChange?.(false);
  }, [onReadingChange]);

  return (
    <div data-testid="skill-graph-panel" className="skill-graph-panel">
      <aside data-testid="skill-graph-panel-sidebar" className="skill-graph-panel__sidebar">
        <div data-testid="skill-graph-panel-stats" className="skill-graph-panel__stats skill-graph-panel__stats--compact">
          <span data-testid="skill-graph-panel-stats-skill-count"><strong>{visibleSkillNodes.length}</strong>{t('skills.graph.stats.skillsSuffix')}</span>
          <span data-testid="skill-graph-panel-stats-edge-count"><strong>{visible.edges.length}</strong>{t('skills.graph.stats.edgesSuffix')}</span>
        </div>

        <label data-testid="skill-graph-panel-search" className="skill-graph-panel__search">
          <Search size={16} aria-hidden="true" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            data-testid="skill-graph-panel-search-input"
            placeholder={t('skills.graph.searchPlaceholder')}
          />
        </label>

        <div data-testid="skill-graph-panel-filters" className="skill-graph-panel__filters">
          <label>
            <span>{t('skills.graph.minConfidence', { percent: Math.round(minConfidence * 100) })}</span>
            <input
              type="range"
              min={graphMinConfidence}
              max={1}
              step={0.05}
              value={minConfidence}
              data-testid="skill-graph-panel-min-confidence-slider"
              onChange={(event) => {
                minConfidenceTouchedRef.current = true;
                setMinConfidence(Number(event.target.value));
              }}
            />
            <small data-testid="skill-graph-panel-min-confidence-help" className="skill-graph-panel__filter-help">
              {t('skills.graph.minConfidenceHelp')}
            </small>
          </label>
        </div>

        <div data-testid="skill-graph-panel-actions" className="skill-graph-panel__actions">
          <button
            type="button"
            onClick={() => void rebuildGraph('incremental').catch(() => undefined)}
            disabled={isBusy}
            data-testid="skill-graph-panel-action-incremental-build"
            title={t('skills.graph.actions.incrementalBuild')}
          >
            {isIncrementalBuild ? <Loader2 size={16} className="skill-graph-panel__spin" aria-hidden="true" /> : <Plus size={16} aria-hidden="true" />}
            <span>{t('skills.graph.actions.incrementalBuild')}</span>
          </button>
          <button
            type="button"
            onClick={cancelBuild}
            disabled={!canCancelBuild}
            data-testid="skill-graph-panel-action-cancel-build"
            title={t('skills.graph.actions.cancelBuild')}
          >
            {cancellingBuild ? <Loader2 size={16} className="skill-graph-panel__spin" aria-hidden="true" /> : <CircleStop size={16} aria-hidden="true" />}
            <span>{t('skills.graph.actions.cancelBuild')}</span>
          </button>
          <button
            type="button"
            onClick={() => void rebuildGraph('full').catch(() => undefined)}
            disabled={isBusy}
            data-testid="skill-graph-panel-action-full-rebuild"
            title={t('skills.graph.actions.fullRebuild')}
          >
            {isFullBuild ? <Loader2 size={16} className="skill-graph-panel__spin" aria-hidden="true" /> : <FullBuildIcon />}
            <span>{t('skills.graph.actions.fullRebuild')}</span>
          </button>
          <button type="button" onClick={fitView} disabled={!visible.nodes.length} data-testid="skill-graph-panel-action-fit-view" title={t('skills.graph.actions.fitView')}>
            <ArrangeGraphIcon />
            <span>{t('skills.graph.actions.fitView')}</span>
          </button>
        </div>
        <section data-testid="skill-graph-panel-actions-help" className="skill-graph-panel__actions-help">
          <ul>
            <li data-testid="skill-graph-panel-actions-help-item-incremental">{t('skills.graph.actionHelp.incrementalBuild')}</li>
            <li data-testid="skill-graph-panel-actions-help-item-cancel">{t('skills.graph.actionHelp.cancelBuild')}</li>
            <li data-testid="skill-graph-panel-actions-help-item-full">{t('skills.graph.actionHelp.fullRebuild')}</li>
            <li data-testid="skill-graph-panel-actions-help-item-fit">{t('skills.graph.actionHelp.fitView')}</li>
          </ul>
        </section>

        {(updating || showBuildLogPanel) ? (
          <div data-testid="skill-graph-panel-build-log" className="skill-graph-panel__build-log">
            <div data-testid="skill-graph-panel-progress-head" className="skill-graph-panel__progress-head">
              <span data-testid="skill-graph-panel-progress-title">{progressTitle}</span>
              <strong data-testid="skill-graph-panel-progress-percent">{currentProgressPercent}%</strong>
            </div>
            <div data-testid="skill-graph-panel-progress-track" className="skill-graph-panel__progress-track" aria-hidden="true">
              <span style={{ width: `${currentProgressPercent}%` }} />
            </div>
            {buildMetricsText ? (
              <div data-testid="skill-graph-panel-build-metrics" className="skill-graph-panel__build-metrics">
                <span>{buildMetricsText}</span>
              </div>
            ) : null}
            <div data-testid="skill-graph-panel-log-list" className="skill-graph-panel__log-list">
              {recentBuildLog.length === 0 ? (
                <div data-testid="skill-graph-panel-log-list-empty" className="skill-graph-panel__empty skill-graph-panel__empty--compact">{t('skills.graph.status.waitingBuildLogs')}</div>
              ) : (
                recentBuildLog.map((entry, index) => (
                  <div data-testid="skill-graph-panel-log-row" data-variant={entry.ts} className="skill-graph-panel__log-row" key={`${entry.ts || 'log'}-${entry.stage || index}-${index}`}>
                    <span>{buildLogTime(entry)}</span>
                    <strong>{buildLogSummary(entry, t)}</strong>
                  </div>
                ))
              )}
            </div>
          </div>
        ) : null}

        {visibleError ? (
          <div data-testid="skill-graph-panel-error" className="skill-graph-panel__error">
            <AlertTriangle size={16} aria-hidden="true" />
            <span data-testid="skill-graph-panel-error-text">{visibleError}</span>
          </div>
        ) : null}

        <section data-testid="skill-graph-panel-node-list" className="skill-graph-panel__node-list">
          <h3 data-testid="skill-graph-panel-node-list-title">{t('skills.graph.skillList')}</h3>
          {visibleSkillNodes.length === 0 ? (
            <div data-testid="skill-graph-panel-node-list-empty" className="skill-graph-panel__empty">{t('skills.graph.noVisibleSkills')}</div>
          ) : (
            [...visibleSkillNodes]
              .sort((a, b) => b.degree - a.degree)
              .slice(0, 80)
              .map((node) => (
                <button
                  type="button"
                  key={node.id}
                  data-testid="skill-graph-panel-node" data-variant={node.id}
                  className={selectedNode?.id === node.id ? 'is-active' : ''}
                  onClick={() => selectNode(node)}
                >
                  <span>{node.label}</span>
                </button>
              ))
          )}
        </section>
      </aside>

      <section data-testid="skill-graph-panel-canvas-wrap" className="skill-graph-panel__canvas-wrap">
        {graphUpdatedAt ? (
          <div data-testid="skill-graph-panel-graph-meta" className="skill-graph-panel__graph-meta">
            {t('skills.graph.updatedAt', { time: graphUpdatedAt })}
          </div>
        ) : null}
        <canvas
          ref={canvasRef}
          data-testid="skill-graph-panel-canvas"
          onPointerDown={(event) => {
            dragRef.current = { active: true, moved: false, x: event.clientX, y: event.clientY };
            event.currentTarget.setPointerCapture(event.pointerId);
          }}
          onPointerMove={(event) => {
            const drag = dragRef.current;
            hoveredRef.current = findNodeAt(event.clientX, event.clientY);
            if (drag.active) {
              const dx = event.clientX - drag.x;
              const dy = event.clientY - drag.y;
              if (Math.abs(dx) + Math.abs(dy) > 2) drag.moved = true;
              transformRef.current.x += dx;
              transformRef.current.y += dy;
              drag.x = event.clientX;
              drag.y = event.clientY;
            }
          }}
          onPointerUp={(event) => {
            const drag = dragRef.current;
            if (!drag.moved) {
              selectNode(findNodeAt(event.clientX, event.clientY));
            }
            dragRef.current = { active: false, moved: false, x: 0, y: 0 };
            event.currentTarget.releasePointerCapture(event.pointerId);
          }}
          onPointerLeave={() => {
            hoveredRef.current = null;
            dragRef.current.active = false;
          }}
          onWheel={(event) => {
            event.preventDefault();
            zoomAt(event.deltaY > 0 ? 0.9 : 1.1, event.clientX, event.clientY);
          }}
        />
        {isBusy ? (
          <div data-testid="skill-graph-panel-loading" className={`skill-graph-panel__loading${graphUpdatedAt ? ' skill-graph-panel__loading--below-meta' : ''}`}>
            <Loader2 size={18} className="skill-graph-panel__spin" aria-hidden="true" />
            <span data-testid="skill-graph-panel-loading-text">{isGraphBuildRunning ? `${progressTitle} · ${currentProgressPercent}%` : t('skills.graph.status.reading')}</span>
          </div>
        ) : null}
      </section>

      <aside data-testid="skill-graph-panel-detail" className="skill-graph-panel__detail">
        {selectedNode ? (
          <>
            <div data-testid="skill-graph-panel-detail-head">
              <h3 data-testid="skill-graph-panel-detail-title">{selectedNode.label}</h3>
              <p data-testid="skill-graph-panel-detail-id">{selectedNode.id}</p>
            </div>
            <div data-testid="skill-graph-panel-detail-grid" className="skill-graph-panel__detail-grid">
              <span data-testid="skill-graph-panel-detail-in-degree">{t('skills.graph.inDegree')}<strong>{selectedNode.inDegree}</strong></span>
              <span data-testid="skill-graph-panel-detail-out-degree">{t('skills.graph.outDegree')}<strong>{selectedNode.outDegree}</strong></span>
            </div>
            {asString(selectedNode.properties.description) ? (
              <section data-testid="skill-graph-panel-detail-description" className="skill-graph-panel__description">
                <h4 className="skill-graph-panel__detail-section-title">{t('skills.graph.description')}</h4>
                <p data-testid="skill-graph-panel-detail-description-content" className="skill-graph-panel__description-content">
                  {asString(selectedNode.properties.description)}
                </p>
              </section>
            ) : null}
            <div data-testid="skill-graph-panel-io-sections" className="skill-graph-panel__io-sections">
              {detailTasks.length > 0 ? (
                <section data-testid="skill-graph-panel-io-section-task" className="skill-graph-panel__io-section skill-graph-panel__io-section--task">
                  <h4 data-testid="skill-graph-panel-io-section-task-title">{t('skills.graph.tasks')}</h4>
                  <div data-testid="skill-graph-panel-io-section-task-tags" className="skill-graph-panel__tags">
                    {detailTasks.slice(0, 18).map((item) => (
                      <span key={item.key} data-testid="skill-graph-panel-io-section-task-tag" data-variant={item.key} title={item.meta || item.label}>
                        {item.label}
                        {item.meta ? <small>{item.meta}</small> : null}
                      </span>
                    ))}
                  </div>
                </section>
              ) : null}
            </div>
            <div data-testid="skill-graph-panel-related" className="skill-graph-panel__related">
              <h4 data-testid="skill-graph-panel-related-title" className="skill-graph-panel__detail-section-title">
                {t('skills.graph.relatedEdges')}
              </h4>
              {relatedEdges.length === 0 ? (
                <div data-testid="skill-graph-panel-related-empty" className="skill-graph-panel__empty">{t('skills.graph.noRelatedEdges')}</div>
              ) : (
                relatedEdges.slice(0, 80).map((edge, index) => {
                  const otherId = edge.source === selectedNode.id ? edge.target : edge.source;
                  const other = graph.nodes.find((node) => node.id === otherId);
                  return (
                    <button
                      type="button"
                      key={`${edge.source}-${edge.target}-${index}`}
                      data-testid="skill-graph-panel-related-edge" data-variant={`${edge.source}-${edge.target}`}
                      onClick={() => {
                        if (other) selectNode(other);
                      }}
                    >
                      <span>{edge.source === selectedNode.id ? '→' : '←'} {other?.label || labelFromId(otherId)}</span>
                      <small>
                        {t('skills.graph.linkStrength', { percent: Math.round(edge.confidence * 100) })}
                        {edge.runtimeWeight === undefined
                          ? ''
                          : ` · runtime_weight ${edge.runtimeWeight.toFixed(2)}`}
                      </small>
                    </button>
                  );
                })
              )}
            </div>
          </>
        ) : (
          <div data-testid="skill-graph-panel-detail-empty" className="skill-graph-panel__empty skill-graph-panel__detail-empty">{t('skills.graph.selectSkillHint')}</div>
        )}
      </aside>
    </div>
  );
});
