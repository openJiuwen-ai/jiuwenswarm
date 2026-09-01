/**
 * PersonalContextGraphPanel — 上下文图谱子页。
 *
 * 复用 SkillGraphPanel 的布局内核（skillGraphLayout.ts 的纯函数），
 * 交互与绘制对齐 SkillGraphPanel：DPR 适配、拖拽平移、滚轮缩放、自动 fitView、
 * 节点渐变/高亮/暗化、边箭头、标签。
 * 数据走流式 personal_context.context.stream_graph / 非流式 search_pages / get_node。
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, Loader2, Plus, RefreshCw, Search, X } from 'lucide-react';
import {
  computeConnectedComponents,
  seedPositions,
  stepSkillGraphLayout,
  COMPONENT_CENTER_ATTRACTION_STRENGTH,
  type LayoutEdge,
  type LayoutNode,
} from '../SkillGraphPanel/skillGraphLayout';
import { usePersonalContextStore } from '../../stores';
import {
  type ContextEdge,
  type ContextNode,
  type ContextSearchResultItem,
  type ContextSourceDetail,
  PROVIDER_LABEL_KEYS,
  pcApi,
} from '../../services/personalContextApi';
import { MarkdownRenderer } from '../MarkdownRenderer';
import './GraphPanel.css';

interface PersonalContextGraphPanelProps {
  isConnected: boolean;
  isActive: boolean;
  onNavigateServices: () => void;
}

type Transform = { x: number; y: number; scale: number };

// 连线按类型分色：归属（contains，蓝色）更重要；提及（黄色）
const GRAPH_EDGE_BELONG = '#1476FF';
const GRAPH_EDGE_MENTION = '#F5A623';
const GRAPH_EDGE_BELONG_ACTIVE = '#0F58D6';
const GRAPH_EDGE_MENTION_ACTIVE = '#D48806';
const GRAPH_LABEL_DEFAULT = '#808080';
const GRAPH_LABEL_DIMMED = '#bdbdbd';
const GRAPH_LABEL_ACTIVE = '#191919';
// 高保真：未聚焦元素透明度 40%
const DIM_ALPHA = 0.4;

// 高保真节点多层光晕调色板（CSS background 多层 radial-gradient → Canvas 叠加）
type GlowLayer = { r: number; g: number; b: number; stops: Array<[number, number]> };
type NodePalette = { base: [number, number, number]; layers: GlowLayer[] };
// 一级节点（根）：紫色系
const PALETTE_ROOT: NodePalette = {
  base: [165, 172, 255],
  layers: [
    { r: 125, g: 133, b: 234, stops: [[0, 0], [0.48, 0.1], [0.74, 0.35], [1, 1]] },
    { r: 125, g: 133, b: 234, stops: [[0, 0], [0.69, 0], [0.89, 0.23], [1, 0.07]] },
    { r: 125, g: 133, b: 234, stops: [[0, 0], [0.69, 0], [0.89, 0.46], [1, 1]] },
  ],
};
// 文件夹节点：橙色系
const PALETTE_FOLDER: NodePalette = {
  base: [255, 150, 0],
  layers: [
    { r: 255, g: 211, b: 85, stops: [[0, 0], [0.48, 0.1], [0.75, 1], [1, 1]] },
    { r: 255, g: 211, b: 85, stops: [[0, 0], [0.69, 0], [0.89, 0.24], [1, 0.07]] },
    { r: 244, g: 194, b: 49, stops: [[0, 0], [0.70, 0], [0.89, 0.46], [1, 1]] },
  ],
};
// 文档节点：灰色系
const PALETTE_DOC: NodePalette = {
  base: [204, 204, 204],
  layers: [
    { r: 174, g: 174, b: 174, stops: [[0, 0], [0.48, 0.1], [0.74, 0.35], [1, 1]] },
    { r: 174, g: 174, b: 174, stops: [[0, 0], [0.69, 0], [0.89, 0.23], [1, 0.07]] },
    { r: 174, g: 174, b: 174, stops: [[0, 0], [0.69, 0], [0.89, 0.46], [1, 1]] },
  ],
};
function rgbaOf(layer: GlowLayer, alpha: number): string {
  return `rgba(${layer.r}, ${layer.g}, ${layer.b}, ${alpha})`;
}
/** 按高保真多层 radial-gradient 叠加绘制节点圆体。绘制顺序：base 实色 → layer3 → layer2 → layer1（顶）。 */
function paintGlowNode(ctx: CanvasRenderingContext2D, x: number, y: number, r: number, palette: NodePalette) {
  const [br, bg, bb] = palette.base;
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.fillStyle = `rgba(${br}, ${bg}, ${bb}, 1)`;
  ctx.fill();
  for (let i = palette.layers.length - 1; i >= 0; i -= 1) {
    const layer = palette.layers[i];
    const grad = ctx.createRadialGradient(x, y, 0, x, y, r);
    for (const [off, a] of layer.stops) grad.addColorStop(off, rgbaOf(layer, a));
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = grad;
    ctx.fill();
  }
}

function truncate(value: string, limit: number): string {
  return value.length > limit ? `${value.slice(0, limit - 1)}...` : value;
}

/** 取节点的文件名（path/label 的最后一段）；取不到返回空串，调用方据此决定是否显示。 */
function nodeFileName(node: { label?: string; path?: string }): string {
  const raw = (node.path && node.path.trim()) || (node.label && node.label.trim()) || '';
  if (!raw) return '';
  const segs = raw.split(/[\\/]+/).map((s) => s.trim()).filter(Boolean);
  return segs.length ? segs[segs.length - 1] : '';
}

/** 按层级递减的节点半径：根(depth=1)最大，每深一层缩小 3，下限 12。 */
function nodeRadius(depth: number): number {
  return Math.max(12, 25 - depth * 3);
}

/** 由 contains 关系计算每个节点层级：根(depth=1)无父 contains 入边，子节点 = 父+1。 */
function computeDepthMap(nodes: LayoutNode[], edges: LayoutEdge[]): Map<string, number> {
  const childrenOf = new Map<string, string[]>();
  const hasParent = new Set<string>();
  edges.forEach((e) => {
    if (e.type === 'contains') {
      const arr = childrenOf.get(e.source) || [];
      arr.push(e.target);
      childrenOf.set(e.source, arr);
      hasParent.add(e.target);
    }
  });
  const depth = new Map<string, number>();
  // 根节点：无父 contains 入边
  const queue: string[] = [];
  nodes.forEach((n) => { if (!hasParent.has(n.id)) { depth.set(n.id, 1); queue.push(n.id); } });
  while (queue.length > 0) {
    const id = queue.shift()!;
    const d = depth.get(id) || 1;
    const kids = childrenOf.get(id);
    if (kids) {
      kids.forEach((c) => {
        if (!depth.has(c)) { depth.set(c, d + 1); queue.push(c); }
        else if ((depth.get(c) || 1) < d + 1) { depth.set(c, d + 1); queue.push(c); }
      });
    }
  }
  // 兜底：未覆盖节点默认 depth=1
  nodes.forEach((n) => { if (!depth.has(n.id)) depth.set(n.id, 1); });
  return depth;
}

/** 把 ContextNode/Edge 适配为布局内核需要的 LayoutNode/LayoutEdge。 */
function adaptGraph(nodes: ContextNode[], edges: ContextEdge[]) {
  const layoutNodes: LayoutNode[] = nodes.map((n) => ({
    id: n.id,
    x: 0,
    y: 0,
    vx: 0,
    vy: 0,
  }));
  const layoutEdges: LayoutEdge[] = edges.map((e) => ({
    source: e.source,
    target: e.target,
    type: e.kind,
  }));
  return { layoutNodes, layoutEdges };
}

/** 由 directory/document 节点的 path 构建文件树。 */
type TreeNode = {
  name: string;
  path: string;
  node?: ContextNode;
  children: TreeNode[];
};

function buildFileTree(nodes: ContextNode[]): TreeNode[] {
  const roots: TreeNode[] = [];
  const dirMap = new Map<string, TreeNode>();

  const ensureDir = (segs: string[]): TreeNode => {
    let cur = roots;
    let acc = '';
    let parent: TreeNode | null = null;
    for (let i = 0; i < segs.length; i++) {
      acc = i === 0 ? segs[i] : `${acc}/${segs[i]}`;
      let node = dirMap.get(acc);
      if (!node) {
        node = { name: segs[i], path: acc, children: [] };
        dirMap.set(acc, node);
        (parent ? parent.children : cur).push(node);
      }
      parent = node;
    }
    return parent as TreeNode;
  };

  // 收录 document 与 source 节点（source 节点 path 形如 src_xxx.md，也平铺在根）
  const leaves = nodes.filter((n) => n.kind === 'document' || n.kind === 'source');
  leaves
    .slice()
    .sort((a, b) => a.path.localeCompare(b.path))
    .forEach((n) => {
      const segs = n.path.split('/');
      const fileName = segs.pop()!;
      const parent = segs.length ? ensureDir(segs) : null;
      const leaf: TreeNode = { name: fileName, path: n.path, node: n, children: [] };
      (parent ? parent.children : roots).push(leaf);
    });

  return roots;
}

/** 高亮 snippet 中的查询关键字，返回分段 + 命中次数。 */
function highlightSnippet(
  snippet: string,
  query: string,
): { segments: Array<{ text: string; match: boolean }>; count: number } {
  if (!query) return { segments: [{ text: snippet, match: false }], count: 0 };
  const lower = snippet.toLowerCase();
  const q = query.toLowerCase();
  const segments: Array<{ text: string; match: boolean }> = [];
  let count = 0;
  let lastIdx = 0;
  let idx = lower.indexOf(q);
  while (idx !== -1) {
    if (idx > lastIdx) segments.push({ text: snippet.slice(lastIdx, idx), match: false });
    segments.push({ text: snippet.slice(idx, idx + q.length), match: true });
    count += 1;
    lastIdx = idx + q.length;
    idx = lower.indexOf(q, lastIdx);
  }
  if (lastIdx < snippet.length) segments.push({ text: snippet.slice(lastIdx), match: false });
  return { segments, count };
}

export function PersonalContextGraphPanel({
  isConnected,
  isActive,
  onNavigateServices,
}: PersonalContextGraphPanelProps) {
  const { t } = useTranslation();
  const { graph, loadingGraph, status, config, loadGraph, loadStatus } = usePersonalContextStore();
  const [query, setQuery] = useState('');
  const [searchResults, setSearchResults] = useState<ContextSearchResultItem[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [expandedResults, setExpandedResults] = useState<Set<string>>(new Set());
  const [searchHits, setSearchHits] = useState<Set<string>>(new Set());
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [nodeDetail, setNodeDetail] = useState<{ markdown: string; title: string } | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [nodeSources, setNodeSources] = useState<string[]>([]);
  const [sourceCard, setSourceCard] = useState<ContextSourceDetail | null>(null);
  const [sourceCardLoading, setSourceCardLoading] = useState(false);
  const [collapsedDirs, setCollapsedDirs] = useState<Set<string>>(new Set());
  const collapsedDirsRef = useRef<Set<string>>(new Set());

  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const nodesRef = useRef<LayoutNode[]>([]);
  const layoutEdgesRef = useRef<LayoutEdge[]>([]);
  const nodeByIdRef = useRef<Map<string, ContextNode>>(new Map());
  const transformRef = useRef<Transform>({ x: 0, y: 0, scale: 1 });
  const canvasSizeRef = useRef({ width: 0, height: 0 });
  const rafRef = useRef<number | null>(null);
  const dragRef = useRef({ active: false, moved: false, x: 0, y: 0 });
  const hoveredRef = useRef<string | null>(null);
  const autoFitRequestRef = useRef(0);
  const autoFitCancelledRef = useRef(false);
  const layoutTicksRemainingRef = useRef(0);
  const transformInitializedRef = useRef(false);
  const [autoFitRequest, setAutoFitRequest] = useState(0);
  // 请求序号：丢弃过期请求的结果，避免快速切换时旧响应覆盖新状态（竞态）。
  const searchReqRef = useRef(0);
  const detailReqRef = useRef(0);
  const sourceReqRef = useRef(0);
  const searchTimerRef = useRef<number | null>(null);

  // 同步 collapsedDirs → ref（draw 循环读取 ref 避免重建 RAF）
  useEffect(() => {
    collapsedDirsRef.current = collapsedDirs;
  }, [collapsedDirs]);

  // 上下文是否就绪
  const contextReady = status?.context_ready === true || (graph?.context_ready ?? false);
  const runtimeState = status?.state ?? 'CREATED';
  const lastErrorText = status?.last_error?.message ?? null;
  const nodeCount = graph?.nodes.length ?? 0;
  const edgeCount = graph?.edges.length ?? 0;

  // 空态引导
  const hasRunningFetch = useMemo(() => {
    const states = status?.fetch_service_states ?? {};
    return Object.values(states).some((st) => st === 'STARTING' || st === 'RUNNING' || st === 'STOPPING');
  }, [status]);
  const hasEnabledService = config.fetch_services.some((s) => s.enabled);
  const hasFetchServices = config.fetch_services.length > 0;
  const collectionEnabled = status?.collection_enabled === true;
  const emptyHintKey = hasRunningFetch
    ? 'personalContext.info.collecting'
    : !hasEnabledService
      ? 'personalContext.info.serviceDisabled'
      : !collectionEnabled
        ? 'personalContext.info.fetchingDisabled'
        : !hasFetchServices
          ? 'personalContext.info.noServices'
          : 'personalContext.info.noGraph';

  // 拉图
  const refresh = useCallback(() => {
    if (!isConnected) return;
    void loadGraph();
    void loadStatus();
  }, [isConnected, loadGraph, loadStatus]);

  // 进页拉一次 + 轮询：图未就绪时轮询。RUNNING 时 3s；FAILED 时降频 30s。
  const pollIntervalMs = contextReady
    ? null
    : runtimeState === 'FAILED' ? 30000 : 3000;
  useEffect(() => {
    if (!isConnected || !isActive) return;
    void refresh();
    const interval = pollIntervalMs == null ? null : window.setInterval(() => void refresh(), pollIntervalMs);
    return () => {
      if (interval != null) window.clearInterval(interval);
    };
  }, [isConnected, isActive, refresh, pollIntervalMs]);

  // 图数据变化 → 初始化布局
  useEffect(() => {
    if (!graph || graph.nodes.length === 0) {
      nodesRef.current = [];
      layoutEdgesRef.current = [];
      nodeByIdRef.current = new Map();
      return;
    }
    const { layoutNodes, layoutEdges } = adaptGraph(graph.nodes, graph.edges);
    nodesRef.current = layoutNodes;
    layoutEdgesRef.current = layoutEdges;
    nodeByIdRef.current = new Map(graph.nodes.map((n) => [n.id, n]));
    const canvas = canvasRef.current;
    if (canvas) {
      const rect = canvas.getBoundingClientRect();
      seedPositions(layoutNodes, rect.width || 900, rect.height || 600);
    }
    layoutTicksRemainingRef.current = 180;
    autoFitCancelledRef.current = false;
    autoFitRequestRef.current += 1;
    setAutoFitRequest(autoFitRequestRef.current);
  }, [graph]);

  // DPR + ResizeObserver 适配 canvas 尺寸（对齐 SkillGraphPanel）
  useEffect(() => {
    if (!isActive) return;
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
      if (!transformInitializedRef.current) {
        transformRef.current = { x: rect.width * 0.35, y: rect.height / 2, scale: 1 };
        transformInitializedRef.current = true;
      }
      if ((becameVisible || resized) && nodesRef.current.length > 0) {
        autoFitCancelledRef.current = false;
        autoFitRequestRef.current += 1;
        setAutoFitRequest(autoFitRequestRef.current);
      }
    };

    resizeCanvas();
    const observer = new ResizeObserver(resizeCanvas);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [isActive]);

  const screenToWorld = useCallback((x: number, y: number) => ({
    x: (x - transformRef.current.x) / transformRef.current.scale,
    y: (y - transformRef.current.y) / transformRef.current.scale,
  }), []);

  const findNodeAt = useCallback((clientX: number, clientY: number) => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    const point = screenToWorld(clientX - rect.left, clientY - rect.top);
    const nodes = nodesRef.current;
    const depthMap = computeDepthMap(nodes, layoutEdgesRef.current);
    for (let i = nodes.length - 1; i >= 0; i -= 1) {
      const n = nodes[i];
      const ctxNode = nodeByIdRef.current.get(n.id);
      if (!ctxNode) continue;
      const hit = nodeRadius(depthMap.get(n.id) || 1) + 5 / transformRef.current.scale;
      if (Math.hypot(n.x - point.x, n.y - point.y) <= hit) return n;
    }
    return null;
  }, [screenToWorld]);

  const fitView = useCallback(() => {
    const canvas = canvasRef.current;
    const nodes = nodesRef.current;
    if (!canvas || nodes.length === 0) return;
    const rect = canvas.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const xs = nodes.map((n) => n.x);
    const ys = nodes.map((n) => n.y);
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
      x: rect.width * 0.35 - ((minX + maxX) / 2) * scale,
      y: rect.height / 2 - ((minY + maxY) / 2) * scale,
    };
  }, []);

  // autoFit 延迟触发（等布局稳定）
  useEffect(() => {
    if (autoFitRequest === 0 || nodesRef.current.length === 0) return undefined;
    let firstFrame = 0;
    let settleTimer = 0;
    let finalTimer = 0;
    firstFrame = window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        if (!autoFitCancelledRef.current) fitView();
      });
    });
    settleTimer = window.setTimeout(() => {
      if (!autoFitCancelledRef.current) fitView();
    }, 320);
    finalTimer = window.setTimeout(() => {
      if (!autoFitCancelledRef.current) fitView();
    }, 900);
    return () => {
      if (firstFrame) window.cancelAnimationFrame(firstFrame);
      if (settleTimer) window.clearTimeout(settleTimer);
      if (finalTimer) window.clearTimeout(finalTimer);
    };
  }, [autoFitRequest, fitView]);

  // 详情面板开/关时画布宽度变化 → 重新居中（防抖 200ms）
  useEffect(() => {
    if (!isActive || nodesRef.current.length === 0) return undefined;
    const timer = window.setTimeout(() => {
      autoFitCancelledRef.current = false;
      fitView();
    }, 200);
    return () => window.clearTimeout(timer);
  }, [selectedNodeId, isActive, fitView]);

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

  // 布局 + 绘制循环
  useEffect(() => {
    if (!isActive) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const stepSimulation = () => {
      const nodes = nodesRef.current;
      const edges = layoutEdgesRef.current;
      if (nodes.length > 0 && layoutTicksRemainingRef.current > 0) {
        const width = canvas.clientWidth || 900;
        const height = canvas.clientHeight || 600;
        const components = computeConnectedComponents(nodes, edges);
        stepSkillGraphLayout(
          nodes,
          edges,
          width,
          height,
          components,
          COMPONENT_CENTER_ATTRACTION_STRENGTH,
        );
        layoutTicksRemainingRef.current -= 1;
      }
    };

    const draw = () => {
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      const pixelRatioX = canvas.width / Math.max(1, width);
      const pixelRatioY = canvas.height / Math.max(1, height);
      const transform = { ...transformRef.current };
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.setTransform(pixelRatioX, 0, 0, pixelRatioY, 0, 0);
      ctx.save();
      ctx.translate(transform.x, transform.y);
      ctx.scale(transform.scale, transform.scale);

      const nodes = nodesRef.current;
      const edges = layoutEdgesRef.current;
      const nodeMap = new Map(nodes.map((n) => [n.id, n]));
      const depthMap = computeDepthMap(nodes, edges);
      // 计算可见节点：收起的 directory 的子节点不绘制
      const collapsed = collapsedDirsRef.current;
      const containsMap = new Map<string, string[]>();
      edges.forEach((edge) => {
        if (edge.type === 'contains') {
          const arr = containsMap.get(edge.source) || [];
          arr.push(edge.target);
          containsMap.set(edge.source, arr);
        }
      });
      const allChildren = new Set<string>();
      containsMap.forEach((arr) => arr.forEach((id) => allChildren.add(id)));
      const visibleNodeIds = new Set<string>();
      const visVisited = new Set<string>();
      const visQueue: string[] = nodes.filter((n) => !allChildren.has(n.id)).map((n) => n.id);
      while (visQueue.length > 0) {
        const id = visQueue.shift()!;
        if (visVisited.has(id)) continue;
        visVisited.add(id);
        visibleNodeIds.add(id);
        const children = containsMap.get(id);
        if (children && !collapsed.has(id)) {
          children.forEach((c) => { if (!visVisited.has(c)) visQueue.push(c); });
        }
      }
      const drawableNodeIds = new Set(
        nodes.filter((n) => {
          if (!visibleNodeIds.has(n.id)) return false;
          const ctxNode = nodeByIdRef.current.get(n.id);
          if (!ctxNode) return false;
          const radius = nodeRadius(depthMap.get(n.id) || 1) * transform.scale + 2;
          const screenX = transform.x + n.x * transform.scale;
          const screenY = transform.y + n.y * transform.scale;
          return screenX - radius >= 0
            && screenX + radius <= width
            && screenY - radius >= 0
            && screenY + radius <= height;
        }).map((n) => n.id),
      );
      const selectedId = selectedNodeId;
      const hoveredId = hoveredRef.current;
      const focusId = selectedId || hoveredId;
      const relatedNodeIds = new Set<string>();
      if (focusId) {
        edges.forEach((edge) => {
          if (edge.source === focusId) relatedNodeIds.add(edge.target);
          if (edge.target === focusId) relatedNodeIds.add(edge.source);
        });
      }

      // 边 + 箭头
      edges.forEach((edge) => {
        if (!drawableNodeIds.has(edge.source) || !drawableNodeIds.has(edge.target)) return;
        const source = nodeMap.get(edge.source);
        const target = nodeMap.get(edge.target);
        if (!source || !target) return;
        const active = Boolean(focusId && (edge.source === focusId || edge.target === focusId));
        const isBelong = edge.type === 'contains';
        const edgeColor = isBelong ? GRAPH_EDGE_BELONG : GRAPH_EDGE_MENTION;
        ctx.strokeStyle = active ? (isBelong ? GRAPH_EDGE_BELONG_ACTIVE : GRAPH_EDGE_MENTION_ACTIVE) : edgeColor;
        ctx.globalAlpha = active ? 0.9 : focusId ? DIM_ALPHA : 0.72;
        ctx.lineWidth = active ? 1.8 : 1;
        ctx.beginPath();
        ctx.moveTo(source.x, source.y);
        ctx.lineTo(target.x, target.y);
        ctx.stroke();
        ctx.globalAlpha = 1;

        const angle = Math.atan2(target.y - source.y, target.x - source.x);
        const ctxTarget = nodeByIdRef.current.get(edge.target);
        const radius = ctxTarget ? nodeRadius(depthMap.get(edge.target) || 1) : 7;
        const x = target.x - Math.cos(angle) * radius;
        const y = target.y - Math.sin(angle) * radius;
        ctx.globalAlpha = active ? 0.92 : focusId ? DIM_ALPHA : 0.68;
        ctx.fillStyle = ctx.strokeStyle;
        ctx.beginPath();
        ctx.moveTo(x, y);
        ctx.lineTo(x - Math.cos(angle - 0.5) * 8, y - Math.sin(angle - 0.5) * 8);
        ctx.lineTo(x - Math.cos(angle + 0.5) * 8, y - Math.sin(angle + 0.5) * 8);
        ctx.closePath();
        ctx.fill();
        ctx.globalAlpha = 1;
      });

      // 节点文件名（中间层）：先收集标签并绘制，再在其上绘制节点圆，确保图层顺序为 连线(底)→文件名(中)→节点(顶)
      const labels: Array<{ text: string; x: number; y: number; font: string; fillStyle: string; dimmed: boolean }> = [];
      nodes.forEach((n) => {
        if (!drawableNodeIds.has(n.id)) return;
        const ctxNode = nodeByIdRef.current.get(n.id);
        if (!ctxNode) return;
        const selected = selectedId === n.id;
        const hovered = hoveredId === n.id;
        const radius = nodeRadius(depthMap.get(n.id) || 1);
        const focused = focusId === n.id;
        const highlighted = Boolean(focusId && (focused || relatedNodeIds.has(n.id)) && !selected);
        const dimmed = Boolean(focusId && !focused && !relatedNodeIds.has(n.id));
        const displayRadius = selected ? radius + 2 : radius;
        // 只显示文件名（path/label 最后一段）；子节点取不到文件名则不显示标签
        const fileName = nodeFileName(ctxNode);
        if (fileName) {
          labels.push({
            text: truncate(fileName, 26),
            x: transform.x + n.x * transform.scale,
            y: transform.y + (n.y + displayRadius) * transform.scale + 5,
            font: `${selected ? 700 : highlighted || hovered ? 600 : 400} ${selected ? 13 : 12}px Inter, system-ui, sans-serif`,
            fillStyle: dimmed
              ? GRAPH_LABEL_DIMMED
              : selected || highlighted || hovered
                ? GRAPH_LABEL_ACTIVE
                : GRAPH_LABEL_DEFAULT,
            dimmed,
          });
        }
      });

      // 标签（屏幕坐标，不缩放）— 中间层
      ctx.setTransform(pixelRatioX, 0, 0, pixelRatioY, 0, 0);
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      labels.forEach((label) => {
        ctx.globalAlpha = label.dimmed ? DIM_ALPHA : 1;
        ctx.font = label.font;
        ctx.fillStyle = label.fillStyle;
        ctx.fillText(label.text, label.x, label.y);
      });
      ctx.globalAlpha = 1;

      // 节点圆 + 展开收起标记（最上层）— 重新进入变换坐标系
      ctx.setTransform(pixelRatioX, 0, 0, pixelRatioY, 0, 0);
      ctx.translate(transform.x, transform.y);
      ctx.scale(transform.scale, transform.scale);
      nodes.forEach((n) => {
        if (!drawableNodeIds.has(n.id)) return;
        const ctxNode = nodeByIdRef.current.get(n.id);
        if (!ctxNode) return;
        const selected = selectedId === n.id;
        const hovered = hoveredId === n.id;
        const radius = nodeRadius(depthMap.get(n.id) || 1);
        const focused = focusId === n.id;
        const highlighted = Boolean(focusId && (focused || relatedNodeIds.has(n.id)) && !selected);
        const dimmed = Boolean(focusId && !focused && !relatedNodeIds.has(n.id));
        const displayRadius = selected ? radius + 2 : radius;
        // 按节点角色选取高保真多层光晕调色板：根节点=紫，文件夹=橙，文档=灰
        const isRoot = !allChildren.has(n.id);
        const palette = isRoot
          ? PALETTE_ROOT
          : ctxNode.kind === 'directory'
            ? PALETTE_FOLDER
            : PALETTE_DOC;
        ctx.save();
        ctx.globalAlpha = dimmed ? DIM_ALPHA : 1;
        paintGlowNode(ctx, n.x, n.y, displayRadius, palette);
        if (selected) {
          ctx.strokeStyle = '#ffffff';
          ctx.lineWidth = 2.6;
          ctx.shadowColor = 'rgba(255, 136, 33, 0.32)';
          ctx.shadowBlur = 16;
          ctx.beginPath();
          ctx.arc(n.x, n.y, displayRadius, 0, Math.PI * 2);
          ctx.stroke();
        } else if (highlighted || hovered) {
          ctx.strokeStyle = '#ff8821';
          ctx.lineWidth = 1.8;
          ctx.beginPath();
          ctx.arc(n.x, n.y, displayRadius, 0, Math.PI * 2);
          ctx.stroke();
        } else {
          ctx.strokeStyle = 'rgba(255, 255, 255, 0.72)';
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.arc(n.x, n.y, displayRadius, 0, Math.PI * 2);
          ctx.stroke();
        }
        ctx.restore();
        ctx.globalAlpha = 1;
        // 绘制 +/- 展开收起标记
        if (ctxNode.kind === 'directory' && ctxNode.has_children) {
          const isCollapsed = collapsed.has(n.id);
          const badgeR = Math.max(4, displayRadius * 0.42);
          const bx = n.x + displayRadius + badgeR + 2;
          const by = n.y;
          ctx.save();
          ctx.fillStyle = '#000000';
          ctx.strokeStyle = isCollapsed ? '#ff8821' : '#33bcf2';
          ctx.lineWidth = 1.5;
          ctx.beginPath();
          ctx.arc(bx, by, badgeR, 0, Math.PI * 2);
          ctx.fill();
          ctx.stroke();
          ctx.strokeStyle = isCollapsed ? '#ff8821' : '#33bcf2';
          ctx.lineWidth = 1.6;
          ctx.lineCap = 'round';
          const half = badgeR * 0.55;
          ctx.beginPath();
          ctx.moveTo(bx - half, by);
          ctx.lineTo(bx + half, by);
          ctx.stroke();
          if (isCollapsed) {
            ctx.beginPath();
            ctx.moveTo(bx, by - half);
            ctx.lineTo(bx, by + half);
            ctx.stroke();
          }
          ctx.restore();
        }
      });
      ctx.restore();
    };

    const tick = () => {
      stepSimulation();
      draw();
      rafRef.current = window.requestAnimationFrame(tick);
    };
    tick();
    return () => {
      if (rafRef.current != null) window.cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
  }, [isActive, selectedNodeId, collapsedDirs]);

  // 搜索：实际执行
  const doSearch = useCallback((q: string) => {
    if (!q) {
      setSearchResults([]);
      setSearchHits(new Set());
      setSearchLoading(false);
      return;
    }
    const reqId = ++searchReqRef.current;
    setSearchLoading(true);
    void pcApi.searchPages(q).then((res) => {
      if (reqId !== searchReqRef.current) return; // 已被更新的请求取代
      setSearchResults(res.results);
      setSearchHits(new Set(res.results.map((r) => r.node_id)));
      // 自动展开首个结果
      if (res.results.length > 0) {
        setExpandedResults(new Set([res.results[0].node_id]));
      }
    }).catch(() => {
      if (reqId !== searchReqRef.current) return;
      setSearchResults([]);
      setSearchHits(new Set());
    }).finally(() => {
      if (reqId !== searchReqRef.current) return;
      setSearchLoading(false);
    });
  }, []);

  // 输入变化 → 防抖自动触发
  useEffect(() => {
    const q = query.trim();
    if (searchTimerRef.current != null) window.clearTimeout(searchTimerRef.current);
    if (!q) {
      setSearchResults([]);
      setSearchHits(new Set());
      return;
    }
    searchTimerRef.current = window.setTimeout(() => doSearch(q), 300);
    return () => {
      if (searchTimerRef.current != null) window.clearTimeout(searchTimerRef.current);
    };
  }, [query, doSearch]);

  // 回车立即搜索
  const handleSearch = useCallback(() => {
    if (searchTimerRef.current != null) window.clearTimeout(searchTimerRef.current);
    doSearch(query.trim());
  }, [query, doSearch]);

  // 详情面板 markdown 链接点击 → 解析相对 href 为节点路径，定位文件树 + 图谱节点
  const handleDetailLinkClick = useCallback((href: string, event: React.MouseEvent<HTMLAnchorElement>): boolean => {
    if (!selectedNodeId || href.startsWith('#') || /^https?:/i.test(href)) {
      return false; // 锚点或外部 http 链接，走默认行为
    }

    // source 链接形如 ../source-meta/src_xxx.md → 拉取来源详情卡片
    const srcMatch = href.match(/source-meta\/?(src_[a-f0-9]+)\.md/);
    if (srcMatch) {
      event.preventDefault();
      const sourceId = srcMatch[1];
      const reqId = ++sourceReqRef.current;
      setSourceCardLoading(true);
      setSourceCard(null);
      void pcApi.getSource(sourceId)
        .then((sd) => {
          if (reqId !== sourceReqRef.current) return;
          setSourceCard(sd);
        })
        .catch(() => {
          if (reqId !== sourceReqRef.current) return;
          setSourceCard(null);
        })
        .finally(() => {
          if (reqId !== sourceReqRef.current) return;
          setSourceCardLoading(false);
        });
      return true;
    }

    const currentNode = nodeByIdRef.current.get(selectedNodeId);
    if (!currentNode) return false;

    // 解析相对路径：currentNode.path 是相对 context_root 的 posix 路径，
    // href 是相对当前页面所在目录的 markdown 相对路径（可能含 ../）。
    const currentDir = currentNode.path.includes('/')
      ? currentNode.path.slice(0, currentNode.path.lastIndexOf('/'))
      : '';
    const segments = (currentDir ? `${currentDir}/${href}` : href).split('/');
    const resolved: string[] = [];
    for (const seg of segments) {
      if (seg === '' || seg === '.') continue;
      if (seg === '..') { resolved.pop(); continue; }
      resolved.push(seg);
    }
    const targetPath = resolved.join('/');

    // 在所有节点里按 path 匹配
    let matched: string | null = null;
    for (const node of nodeByIdRef.current.values()) {
      if (node.path === targetPath) {
        matched = node.id;
        break;
      }
    }
    if (!matched) return false;

    event.preventDefault();
    autoFitCancelledRef.current = true;
    setSelectedNodeId(matched);
    return true;
  }, [selectedNodeId]);

  // 选中节点 → 拉详情 + 解析采集来源
  useEffect(() => {
    if (!selectedNodeId) {
      setNodeDetail(null);
      setNodeSources([]);
      setSourceCard(null);
      return;
    }
    const reqId = ++detailReqRef.current;
    setDetailLoading(true);
    setNodeSources([]);
    void pcApi.getNode(selectedNodeId)
      .then((d) => {
        if (reqId !== detailReqRef.current) return;
        setNodeDetail({ markdown: d.markdown, title: d.title });
        // 解析 markdown 中的 [来源N](../source-meta/src_xxx.md) 链接，拉取来源详情
        const sourceIds: string[] = [];
        const re = /\]\(\.\.\/source-meta\/(src_[a-f0-9]+)\.md\)/g;
        let m;
        while ((m = re.exec(d.markdown)) !== null) {
          if (!sourceIds.includes(m[1])) sourceIds.push(m[1]);
        }
        if (sourceIds.length === 0) return;
        // 并发拉取前 8 个来源的 provider
        Promise.all(sourceIds.slice(0, 8).map((sid) => pcApi.getSource(sid).catch(() => null)))
          .then((details) => {
            if (reqId !== detailReqRef.current) return;
            const labels = new Set<string>();
            details.forEach((sd) => {
              if (!sd?.provider) return;
              const key = PROVIDER_LABEL_KEYS[sd.provider as keyof typeof PROVIDER_LABEL_KEYS];
              if (key) labels.add(t(key));
            });
            setNodeSources(Array.from(labels));
          });
      })
      .catch(() => {
        if (reqId !== detailReqRef.current) return;
        setNodeDetail(null);
      })
      .finally(() => {
        if (reqId !== detailReqRef.current) return;
        setDetailLoading(false);
      });
  }, [selectedNodeId, t]);

  const fileTree = useMemo(() => {
    if (!graph) return [];
    return buildFileTree(graph.nodes);
  }, [graph]);



  // 切换目录节点展开/收起
  const toggleDirCollapse = useCallback((nodeId: string) => {
    setCollapsedDirs((prev) => {
      const next = new Set(prev);
      if (next.has(nodeId)) next.delete(nodeId);
      else next.add(nodeId);
      return next;
    });
  }, []);

  // canvas 交互
  const handlePointerDown = useCallback((event: React.PointerEvent<HTMLCanvasElement>) => {
    dragRef.current = { active: true, moved: false, x: event.clientX, y: event.clientY };
    event.currentTarget.setPointerCapture(event.pointerId);
  }, []);

  const handlePointerMove = useCallback((event: React.PointerEvent<HTMLCanvasElement>) => {
    const drag = dragRef.current;
    const found = findNodeAt(event.clientX, event.clientY);
    hoveredRef.current = found ? found.id : null;
    if (drag.active) {
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 2) drag.moved = true;
      transformRef.current.x += dx;
      transformRef.current.y += dy;
      drag.x = event.clientX;
      drag.y = event.clientY;
    }
  }, [findNodeAt]);

  const handlePointerUp = useCallback((event: React.PointerEvent<HTMLCanvasElement>) => {
    const drag = dragRef.current;
    if (!drag.moved) {
      const found = findNodeAt(event.clientX, event.clientY);
      if (found) {
        const ctxNode = nodeByIdRef.current.get(found.id);
        // directory 节点且有子节点 → 切换展开/收起
        if (ctxNode?.kind === 'directory' && ctxNode.has_children) {
          toggleDirCollapse(found.id);
        }
        autoFitCancelledRef.current = true;
        setSelectedNodeId(found.id);
      } else {
        setSelectedNodeId(null);
      }
    }
    dragRef.current = { active: false, moved: false, x: 0, y: 0 };
    event.currentTarget.releasePointerCapture(event.pointerId);
  }, [findNodeAt, toggleDirCollapse]);

  const handleWheel = useCallback((event: React.WheelEvent<HTMLCanvasElement>) => {
    event.preventDefault();
    zoomAt(event.deltaY > 0 ? 0.9 : 1.1, event.clientX, event.clientY);
  }, [zoomAt]);

  const toggleResultExpand = useCallback((nodeId: string) => {
    setExpandedResults((prev) => {
      const next = new Set(prev);
      if (next.has(nodeId)) next.delete(nodeId);
      else next.add(nodeId);
      return next;
    });
  }, []);

  const hasQuery = query.trim().length > 0;
  return (
    <div className="pc-graph" data-testid="personal-context-graph">
      <div className="pc-graph__toolbar">
        <div className="pc-graph__toolbar-info">
          <h2 className="pc-graph__toolbar-title">{t('personalContext.info.graphTitle')}</h2>
          <p className="pc-graph__toolbar-subtitle">{t('personalContext.info.graphSubtitle')}</p>
        </div>
        <div className="pc-graph__actions">
          <button
            type="button"
            className="pc-graph__refresh"
            onClick={refresh}
            disabled={loadingGraph || !isConnected}
            title={t('personalContext.info.refresh')}
          >
            {loadingGraph ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
            <span>{t('personalContext.info.refresh')}</span>
          </button>
          <button
            type="button"
            className="pc-graph__add"
            onClick={onNavigateServices}
          >
            <Plus size={16} />
            <span>{t('personalContext.info.addKnowledge')}</span>
          </button>
        </div>
      </div>

      {lastErrorText && (
        <div className="pc-graph__error" role="alert">
          {t('personalContext.info.publishFailed')}: {lastErrorText}
        </div>
      )}

      <div className="pc-graph__main">
        {/* 左侧栏：固定头部（页签 + 搜索）+ 滚动内容（文件树/搜索结果） */}
        <aside className="pc-graph__tree">
          <div className="pc-graph__tree-header">
            <div className="pc-graph__tabs">
              <span className="pc-graph__tab">
                {t('personalContext.info.tabNodes')}
                <span className="pc-graph__tab-count">{nodeCount}</span>
              </span>
              <span className="pc-graph__tab">
                {t('personalContext.info.tabEdges')}
                <span className="pc-graph__tab-count">{edgeCount}</span>
              </span>
            </div>

            <div className="pc-graph__search">
              <Search size={16} />
              <input
                className="pc-graph__search-input"
                placeholder={t('personalContext.info.searchPlaceholder')}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') handleSearch(); }}
              />
              {hasQuery && (
                <button
                  type="button"
                  className="pc-graph__search-clear"
                  onClick={() => { setQuery(''); setSearchResults([]); setSearchHits(new Set()); }}
                  aria-label="clear"
                >
                  <X size={14} />
                </button>
              )}
              {searchLoading && <Loader2 className="spin" size={14} />}
            </div>
          </div>

          <div className="pc-graph__tree-content">
            {hasQuery ? (
              <>
                <div className="pc-graph__search-meta">
                  <span className="pc-graph__search-count">
                    {searchResults.length}{t('personalContext.info.resultUnit')}
                  </span>
                </div>
                {searchResults.length > 0 && (
                  <div className="pc-graph__search-list">
                    {searchResults.map((r) => {
                      const expanded = expandedResults.has(r.node_id);
                      const { segments, count } = highlightSnippet(r.snippet, query.trim());
                      return (
                        <div key={r.node_id} className="pc-graph__search-card">
                          <button
                            type="button"
                            className="pc-graph__search-card-head"
                            onClick={() => toggleResultExpand(r.node_id)}
                          >
                            <ChevronDown
                              size={16}
                              className={expanded ? 'pc-graph__chevron--open' : 'pc-graph__chevron--closed'}
                            />
                            <span className="pc-graph__search-card-title">{r.title}</span>
                            {count > 0 && <span className="pc-graph__search-card-count">{count}</span>}
                          </button>
                          {expanded && (
                            <div
                              className="pc-graph__search-card-body"
                              onClick={() => {
                                autoFitCancelledRef.current = true;
                                setSelectedNodeId(r.node_id);
                              }}
                            >
                              {segments.map((seg, i) => (
                                seg.match
                                  ? <mark key={i} className="pc-graph__search-hit">{seg.text}</mark>
                                  : <span key={i}>{seg.text}</span>
                              ))}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </>
            ) : (
              fileTree.length === 0 ? (
                <div className="pc-graph__empty">{t(emptyHintKey)}</div>
              ) : (
                <FileTree
                  nodes={fileTree}
                  hits={searchHits}
                  onSelect={(id) => { autoFitCancelledRef.current = true; setSelectedNodeId(id); }}
                  selectedId={selectedNodeId}
                />
              )
            )}
          </div>
        </aside>

        {/* 图谱 canvas */}
        <div className="pc-graph__canvas-wrap">
          <canvas
            ref={canvasRef}
            className="pc-graph__canvas"
            tabIndex={-1}
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={handlePointerUp}
            onPointerLeave={() => { hoveredRef.current = null; dragRef.current.active = false; }}
            onWheel={handleWheel}
          />
          {!contextReady && nodeCount === 0 && (
            <div className="pc-graph__empty pc-graph__empty--overlay">{t(emptyHintKey)}</div>
          )}
        </div>

        {/* 节点详情：节点名称 + 采集来源 + 详细内容 */}
        {selectedNodeId && (
          <aside className="pc-graph__detail">
            <button className="pc-graph__detail-close" onClick={() => setSelectedNodeId(null)} aria-label="close">
              <X size={16} />
            </button>
            <h3 className="pc-graph__detail-name">{nodeDetail?.title ?? selectedNodeId}</h3>
            {nodeSources.length > 0 && (
              <div className="pc-graph__detail-sources">
                {nodeSources.map((s) => (
                  <span key={s} className="pc-graph__detail-source">{s}</span>
                ))}
              </div>
            )}
            <div className="pc-graph__detail-divider" />
            {detailLoading ? (
              <Loader2 className="spin" size={16} />
            ) : nodeDetail ? (
              <div className="pc-graph__detail-content">
                {sourceCardLoading && <Loader2 className="spin" size={14} />}
                {sourceCard && (
                  <div className="pc-graph__source-card">
                    <div className="pc-graph__source-head">
                      <span className="pc-graph__source-badge">{t('personalContext.info.sourceBadge')}</span>
                      <button className="pc-graph__source-close" onClick={() => setSourceCard(null)}>×</button>
                    </div>
                    <div className="pc-graph__source-title">{sourceCard.title}</div>
                    <dl className="pc-graph__source-meta">
                      <div>
                        <dt>{t('personalContext.info.sourceProvider')}</dt>
                        <dd>{(() => { const k = PROVIDER_LABEL_KEYS[sourceCard.provider as keyof typeof PROVIDER_LABEL_KEYS]; return k ? t(k) : sourceCard.provider; })()}</dd>
                      </div>
                      <div>
                        <dt>{t('personalContext.info.sourceType')}</dt>
                        <dd>{sourceCard.source_type}</dd>
                      </div>
                      <div>
                        <dt>{t('personalContext.info.sourceLocator')}</dt>
                        <dd>
                          {sourceCard.locator ? (
                            <a className="pc-graph__source-link" href={sourceCard.locator} target="_blank" rel="noopener noreferrer">{sourceCard.locator}</a>
                          ) : '—'}
                        </dd>
                      </div>
                      <div>
                        <dt>{t('personalContext.info.sourceFirstSeen')}</dt>
                        <dd>{sourceCard.first_seen || '—'}</dd>
                      </div>
                    </dl>
                  </div>
                )}
                <MarkdownRenderer content={nodeDetail.markdown} onLinkClick={handleDetailLinkClick} />
              </div>
            ) : (
              <div className="pc-graph__empty">—</div>
            )}
          </aside>
        )}
      </div>
    </div>
  );
}

/** 文件树渲染（递归，可折叠）。 */
function FileTree({
  nodes,
  hits,
  onSelect,
  selectedId,
  depth = 0,
}: {
  nodes: TreeNode[];
  hits: Set<string>;
  onSelect: (id: string) => void;
  selectedId: string | null;
  depth?: number;
}) {
  return (
    <ul className="pc-graph__tree-list" style={{ paddingLeft: depth > 0 ? 12 : 0 }}>
      {nodes.map((n) => {
        const nodeId = n.node?.id;
        const isHit = nodeId ? hits.has(nodeId) : false;
        const isSelected = nodeId === selectedId;
        return (
          <li key={n.path} className="pc-graph__tree-item">
            {n.node ? (
              <button
                type="button"
                className={`pc-graph__tree-row${isSelected ? ' pc-graph__tree-row--active' : ''}${isHit ? ' pc-graph__tree-row--hit' : ''}`}
                onClick={() => nodeId && onSelect(nodeId)}
              >
                <span className="pc-graph__tree-icon">
                  <FileIcon />
                </span>
                <span className="pc-graph__tree-name">{n.name}</span>
              </button>
            ) : (
              <FolderRow name={n.name} isHit={isHit}>
                {n.children.length > 0 && (
                  <FileTree nodes={n.children} hits={hits} onSelect={onSelect} selectedId={selectedId} depth={depth + 1} />
                )}
              </FolderRow>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** 文件夹行：文件夹图标 + 名称 + 右侧收起按钮（可折叠）。 */
function FolderRow({
  name,
  isHit,
  children,
}: {
  name: string;
  isHit: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(true);
  return (
    <>
      <div
        className={`pc-graph__tree-row pc-graph__tree-row--dir${isHit ? ' pc-graph__tree-row--hit' : ''}`}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="pc-graph__tree-icon">
          <FolderIcon open={open} />
        </span>
        <span className="pc-graph__tree-name">{name}</span>
        <span className="pc-graph__tree-toggle">
          <CollapseIcon open={open} />
        </span>
      </div>
      {open && children}
    </>
  );
}

function FolderIcon({ open }: { open: boolean }) {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" xmlns="http://www.w3.org/2000/svg">
      {open ? (
        <path d="M1 3.5C1 2.67 1.67 2 2.5 2h2.6l1 1.2H9.5c.83 0 1.5.67 1.5 1.5v.8H1V3.5z M1 5.5h10v3c0 .83-.67 1.5-1.5 1.5h-7C1.67 10 1 9.33 1 8.5v-3z" fill="#33bcf2" />
      ) : (
        <path d="M1 3.5C1 2.67 1.67 2 2.5 2h2.6l1 1.2H9.5c.83 0 1.5.67 1.5 1.5v4c0 .83-.67 1.5-1.5 1.5h-7C1.67 10 1 9.33 1 8.5v-5z" fill="#33bcf2" />
      )}
    </svg>
  );
}

function FileIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M3 1.5h3.5L9 4v6.5a.5.5 0 0 1-.5.5h-5a.5.5 0 0 1-.5-.5V2a.5.5 0 0 1 .5-.5z" fill="#fff" stroke="#bdbdbd" strokeWidth="0.8" />
      <path d="M6 1.5V4h2.5" fill="none" stroke="#bdbdbd" strokeWidth="0.8" />
    </svg>
  );
}

function CollapseIcon({ open }: { open: boolean }) {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" xmlns="http://www.w3.org/2000/svg" style={{ transform: open ? 'rotate(0deg)' : 'rotate(-90deg)', transition: 'transform 0.15s ease' }}>
      <path d="M3 4.5L6 7.5L9 4.5" stroke="#808080" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
