import { generateUuidV4 } from '../../utils/uuid';
import type { DesignerLibraryAsset } from './designerAssetLibraryStore';
import {
  DESIGNER_CONFIG_DELEGATE_HANDLER,
  DESIGNER_NODE_ROLE_AUDIO,
  DESIGNER_NODE_ROLE_IMAGE,
  DESIGNER_NODE_ROLE_VIDEO,
  DESIGNER_NODE_TYPE_AUDIO,
  DESIGNER_NODE_TYPE_IMAGE,
  DESIGNER_NODE_TYPE_TEXT,
  DESIGNER_NODE_TYPE_TABLE,
  DESIGNER_NODE_TYPE_VIDEO,
  type DesignerGraphEdge,
  type DesignerGraphNode,
  type DesignerNodeRole,
  type DesignerNodeType,
} from './executionGraphTypes';

export const DESIGNER_CANVAS_NODE_WIDTH = 280;
export const DESIGNER_CANVAS_NODE_HEIGHT = 160;
export const DESIGNER_NODE_HEADER_HEIGHT = 36;
export const DESIGNER_MEDIA_FIT_MAX_WIDTH = 280;
export const DESIGNER_MEDIA_FIT_MAX_BODY = 320;
export const DESIGNER_MEDIA_FIT_MIN_WIDTH = 140;
export const DESIGNER_DOC_FIT_MIN_WIDTH = 180;
export const DESIGNER_DOC_FIT_MIN_BODY = 64;
export const DESIGNER_DOC_TEXT_MAX_WIDTH = 280;
export const DESIGNER_DOC_TEXT_MAX_BODY = 360;
export const DESIGNER_DOC_TABLE_MAX_WIDTH = 280;
export const DESIGNER_DOC_TABLE_MAX_BODY = DESIGNER_DOC_TABLE_MAX_WIDTH - DESIGNER_NODE_HEADER_HEIGHT;
export const DESIGNER_DOC_TEXT_PAD_X = 24;
export const DESIGNER_DOC_TEXT_PAD_Y = 24;
export const DESIGNER_SUCCESSOR_GAP_X = 48;
export const DESIGNER_SUCCESSOR_GAP_Y = 24;
export const DESIGNER_LAYOUT_ORIGIN_X = 40;
export const DESIGNER_LAYOUT_ORIGIN_Y = 40;
export const DESIGNER_ASSET_DRAG_MIME = 'application/x-designer-asset-id';
export const DESIGNER_ADD_GROUP_ORDER: DesignerAddGroup[] = ['image', 'video', 'audio'];

export type DesignerCanvasTool = 'select' | 'hand';
export type DesignerDockPanel = 'add' | 'assets' | null;
export type DesignerAddGroup = 'image' | 'video' | 'audio';

export type DesignerAddTemplate = {
  id: string;
  type: DesignerNodeType;
  role: DesignerNodeRole;
  label: string;
  group: DesignerAddGroup;
};

export const DESIGNER_ADD_TEMPLATES: DesignerAddTemplate[] = [
  {
    id: 'image',
    type: DESIGNER_NODE_TYPE_IMAGE,
    role: DESIGNER_NODE_ROLE_IMAGE,
    label: 'Image',
    group: 'image',
  },
  {
    id: 'video',
    type: DESIGNER_NODE_TYPE_VIDEO,
    role: DESIGNER_NODE_ROLE_VIDEO,
    label: 'Video',
    group: 'video',
  },
  {
    id: 'audio',
    type: DESIGNER_NODE_TYPE_AUDIO,
    role: DESIGNER_NODE_ROLE_AUDIO,
    label: 'Audio',
    group: 'audio',
  },
];

export function groupedDesignerAddTemplates(): Array<{
  group: DesignerAddGroup;
  items: DesignerAddTemplate[];
}> {
  return DESIGNER_ADD_GROUP_ORDER.map((group) => ({
    group,
    items: DESIGNER_ADD_TEMPLATES.filter((item) => item.group === group),
  }));
}

export function nextTypeIndex(
  nodes: Array<{ type?: string }>,
  type: string,
): number {
  return nodes.filter((node) => String(node.type || '') === type).length + 1;
}

export function uniqueDesignerNodeId(existingIds: Iterable<string>, role: string): string {
  const used = new Set(existingIds);
  const slug = String(role || 'node').replace(/[^a-z0-9]+/gi, '_').replace(/^_|_$/g, '') || 'node';
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const id = `n_${slug}_${generateUuidV4().replace(/-/g, '').slice(0, 8)}`;
    if (!used.has(id)) return id;
  }
  return `n_${slug}_${Date.now().toString(36)}`;
}

export function parseContentAspect(value: unknown): number | null {
  if (typeof value === 'number' && value > 0 && Number.isFinite(value)) return value;
  const text = String(value || '').trim();
  if (!text) return null;
  const ratio = text.match(/^(\d+(?:\.\d+)?)\s*[:/]\s*(\d+(?:\.\d+)?)$/);
  if (ratio) {
    const width = Number(ratio[1]);
    const height = Number(ratio[2]);
    if (width > 0 && height > 0) return width / height;
  }
  const pixels = text.match(/^(\d+)\s*[*xX]\s*(\d+)$/);
  if (pixels) {
    const width = Number(pixels[1]);
    const height = Number(pixels[2]);
    if (width > 0 && height > 0) return width / height;
  }
  return null;
}

export function contentAspectFromNodeConfig(
  config: Record<string, unknown> | undefined | null,
): number | null {
  const lock = config?.aspect_lock;
  if (lock && typeof lock === 'object') {
    const rec = lock as Record<string, unknown>;
    return (
      parseContentAspect(rec.ratio) ||
      parseContentAspect(rec.video_size) ||
      parseContentAspect(rec.image_size)
    );
  }
  return parseContentAspect(config?.video_size) || parseContentAspect(config?.image_size);
}

export function sizeNodeForContentAspect(ratio: number): { width: number; height: number } {
  const r = ratio > 0 && Number.isFinite(ratio) ? ratio : 16 / 9;
  let width = DESIGNER_MEDIA_FIT_MAX_WIDTH;
  let body = width / r;
  if (body > DESIGNER_MEDIA_FIT_MAX_BODY) {
    body = DESIGNER_MEDIA_FIT_MAX_BODY;
    width = body * r;
  }
  if (width < DESIGNER_MEDIA_FIT_MIN_WIDTH) {
    width = DESIGNER_MEDIA_FIT_MIN_WIDTH;
    body = width / r;
  }
  return {
    width: Math.round(width),
    height: Math.round(body + DESIGNER_NODE_HEADER_HEIGHT),
  };
}

export function sizeNodeForDocumentContent(
  content: { width: number; height: number },
  kind: 'text' | 'table' = 'text',
): { width: number; height: number } {
  const padX = kind === 'table' ? 0 : DESIGNER_DOC_TEXT_PAD_X;
  const padY = kind === 'table' ? 0 : DESIGNER_DOC_TEXT_PAD_Y;
  const rawWidth = Math.max(0, content.width) + padX;
  const rawBody = Math.max(0, content.height) + padY;
  if (kind === 'table') {
    const minHeight = DESIGNER_NODE_HEADER_HEIGHT + DESIGNER_DOC_FIT_MIN_BODY;
    const maxHeight = DESIGNER_DOC_TABLE_MAX_BODY + DESIGNER_NODE_HEADER_HEIGHT;
    let width = Math.max(DESIGNER_DOC_FIT_MIN_WIDTH, Math.min(DESIGNER_DOC_TABLE_MAX_WIDTH, rawWidth));
    let height = Math.max(minHeight, Math.min(maxHeight, rawBody + DESIGNER_NODE_HEADER_HEIGHT));
    if (width > height) {
      width = Math.max(DESIGNER_DOC_FIT_MIN_WIDTH, height);
    } else if (height > width) {
      height = Math.min(maxHeight, Math.max(width, minHeight));
    }
    return { width: Math.round(width), height: Math.round(height) };
  }
  const width = Math.round(
    Math.max(DESIGNER_DOC_FIT_MIN_WIDTH, Math.min(DESIGNER_DOC_TEXT_MAX_WIDTH, rawWidth)),
  );
  const body = Math.round(
    Math.max(DESIGNER_DOC_FIT_MIN_BODY, Math.min(DESIGNER_DOC_TEXT_MAX_BODY, rawBody)),
  );
  return {
    width,
    height: body + DESIGNER_NODE_HEADER_HEIGHT,
  };
}

export function isDesignerNodeUserResized(config?: Record<string, unknown> | null): boolean {
  return config?.user_resized === true;
}

export function isDefaultLandscapeNodeSize(width?: number, height?: number): boolean {
  if (typeof width !== 'number' || typeof height !== 'number' || width <= 0 || height <= 0) {
    return true;
  }
  const ratio = width / height;
  return ratio > 1.35 && width <= 300 && height <= 180;
}

export function resolvedNodeCanvasSize(node: DesignerGraphNode): { width: number; height: number } {
  const layout = node.layout;
  const storedWidth = typeof layout?.width === 'number' ? layout.width : undefined;
  const storedHeight = typeof layout?.height === 'number' ? layout.height : undefined;
  const isMedia = node.type === DESIGNER_NODE_TYPE_IMAGE || node.type === DESIGNER_NODE_TYPE_VIDEO;
  const aspect = isMedia
    ? contentAspectFromNodeConfig((node.config ?? {}) as Record<string, unknown>)
    : null;
  if (
    isMedia &&
    aspect &&
    isDefaultLandscapeNodeSize(storedWidth, storedHeight) &&
    !isDesignerNodeUserResized((node.config ?? {}) as Record<string, unknown>)
  ) {
    return sizeNodeForContentAspect(aspect);
  }
  return {
    width: positiveCanvasSize(storedWidth, DESIGNER_CANVAS_NODE_WIDTH),
    height: positiveCanvasSize(storedHeight, DESIGNER_CANVAS_NODE_HEIGHT),
  };
}

function positiveCanvasSize(value: number | undefined, fallback: number): number {
  return typeof value === 'number' && value > 0 && Number.isFinite(value) ? value : fallback;
}

function applyNodeLayouts(
  nodes: DesignerGraphNode[],
  placed: Map<string, { x: number; y: number; width: number; height: number }>,
): DesignerGraphNode[] {
  let changed = false;
  const next = nodes.map((node) => {
    const nextBox = placed.get(node.id);
    if (!nextBox) return node;
    const layout = node.layout ?? {};
    if (
      layout.x === nextBox.x &&
      layout.y === nextBox.y &&
      layout.width === nextBox.width &&
      layout.height === nextBox.height
    ) {
      return node;
    }
    changed = true;
    return {
      ...node,
      layout: {
        x: nextBox.x,
        y: nextBox.y,
        width: nextBox.width,
        height: nextBox.height,
      },
    };
  });
  return changed ? next : nodes;
}

function nodesShareColumn(left: { x: number }, right: { x: number }): boolean {
  return Math.abs(left.x - right.x) < 80;
}

/** Compact helper kept for tests. The canvas does not auto-apply this; click Auto layout to rearrange. */
export function packDesignerNodeLayouts(nodes: DesignerGraphNode[]): DesignerGraphNode[] {
  if (nodes.length === 0) return nodes;
  const boxes = nodes.map((node, index) => {
    const size = resolvedNodeCanvasSize(node);
    return {
      index,
      id: node.id,
      x: typeof node.layout?.x === 'number' ? node.layout.x : 0,
      y: typeof node.layout?.y === 'number' ? node.layout.y : 0,
      width: size.width,
      height: size.height,
    };
  });
  const ordered = [...boxes].sort(
    (left, right) => left.x - right.x || left.y - right.y || left.index - right.index,
  );
  const columns: Array<typeof boxes> = [];
  for (const box of ordered) {
    const column = columns.find((members) => members.some((member) => nodesShareColumn(member, box)));
    if (column) column.push(box);
    else columns.push([box]);
  }
  columns.sort((left, right) => {
    const leftX = Math.min(...left.map((box) => box.x));
    const rightX = Math.min(...right.map((box) => box.x));
    return leftX - rightX;
  });
  const placed = new Map<string, { x: number; y: number; width: number; height: number }>();
  let cursorX = Number.NEGATIVE_INFINITY;
  for (const members of columns) {
    members.sort((left, right) => left.y - right.y || left.index - right.index);
    const colWidth = Math.max(...members.map((box) => box.width));
    const originX = Number.isFinite(cursorX) ? cursorX : Math.min(...members.map((box) => box.x));
    let cursorY = members[0]?.y ?? 0;
    for (const box of members) {
      placed.set(box.id, {
        x: Math.round(originX),
        y: Math.round(cursorY),
        width: box.width,
        height: box.height,
      });
      cursorY += box.height + DESIGNER_SUCCESSOR_GAP_Y;
    }
    cursorX = originX + colWidth + DESIGNER_SUCCESSOR_GAP_X;
  }
  return applyNodeLayouts(nodes, placed);
}

const AUTO_LAYOUT_RANK: Record<string, number> = {
  brief: 0,
  text: 1,
  character: 2,
  character_design: 2,
  scene: 3,
  storyboard: 4,
  table: 5,
  frame: 6,
  image: 7,
  clip: 8,
  video: 9,
  compose: 10,
  music: 11,
  speech: 12,
  audio: 13,
};

function nodeLayoutKey(node: DesignerGraphNode): string {
  const config = (node.config ?? {}) as Record<string, unknown>;
  return String(config.pipeline || config.role || node.type || '');
}

function nodeAutoLayoutRank(node: DesignerGraphNode): number {
  return AUTO_LAYOUT_RANK[nodeLayoutKey(node)] ?? 50;
}

function nodeColumnKind(node: DesignerGraphNode): 'scene' | 'clip' | null {
  const config = (node.config ?? {}) as Record<string, unknown>;
  const pipeline = String(config.pipeline ?? '');
  const role = String(config.role ?? '');
  const label = String(node.label ?? '');
  const shotLabel = /\b(clip|shot)\b/i.test(label);
  if (
    pipeline === 'clip' ||
    pipeline === 'shot' ||
    role === 'clip' ||
    role === 'shot' ||
    (node.type === 'video' && shotLabel && pipeline !== 'compose' && role !== 'compose')
  ) {
    return 'clip';
  }
  if (
    pipeline === 'scene' ||
    pipeline === 'frame' ||
    role === 'scene' ||
    role === 'frame' ||
    (node.type === 'image' && /scene/i.test(label) && !/clip/i.test(label))
  ) {
    return 'scene';
  }
  return null;
}

function sortAutoLayoutColumn(members: DesignerGraphNode[]): DesignerGraphNode[] {
  return [...members].sort((left, right) => {
    const rank = nodeAutoLayoutRank(left) - nodeAutoLayoutRank(right);
    if (rank !== 0) return rank;
    const shot = nodeShotIndex(left) - nodeShotIndex(right);
    if (shot !== 0) return shot;
    const y = (left.layout?.y ?? 0) - (right.layout?.y ?? 0);
    if (y !== 0) return y;
    return left.id.localeCompare(right.id);
  });
}

function autoLayoutColumnPlan(
  nodes: DesignerGraphNode[],
  layerById: Map<string, number>,
): DesignerGraphNode[][] {
  const sceneNodes: DesignerGraphNode[] = [];
  const clipNodes: DesignerGraphNode[] = [];
  const mix = new Map<number, DesignerGraphNode[]>();
  for (const node of nodes) {
    const kind = nodeColumnKind(node);
    if (kind === 'scene') {
      sceneNodes.push(node);
      continue;
    }
    if (kind === 'clip') {
      clipNodes.push(node);
      continue;
    }
    const layer = layerById.get(node.id) ?? 0;
    const members = mix.get(layer);
    if (members) members.push(node);
    else mix.set(layer, [node]);
  }

  const sceneAnchor =
    sceneNodes.length > 0 ? Math.min(...sceneNodes.map((node) => layerById.get(node.id) ?? 0)) : Number.POSITIVE_INFINITY;
  const clipAnchor =
    clipNodes.length > 0
      ? Math.max(
          sceneNodes.length > 0 ? sceneAnchor + 1 : 0,
          Math.min(...clipNodes.map((node) => layerById.get(node.id) ?? 0)),
        )
      : Number.POSITIVE_INFINITY;

  const columns: DesignerGraphNode[][] = [];
  let insertedScene = false;
  let insertedClip = false;
  const insertScene = () => {
    if (insertedScene || sceneNodes.length === 0) return;
    columns.push(sortAutoLayoutColumn(sceneNodes));
    insertedScene = true;
  };
  const insertClip = () => {
    if (insertedClip || clipNodes.length === 0) return;
    columns.push(sortAutoLayoutColumn(clipNodes));
    insertedClip = true;
  };

  const sceneRank = AUTO_LAYOUT_RANK.scene;
  const clipRank = AUTO_LAYOUT_RANK.clip;
  for (const layer of [...mix.keys()].sort((left, right) => left - right)) {
    let leftover = mix.get(layer) ?? [];
    if (!insertedScene && sceneAnchor <= layer) {
      const before = leftover.filter((node) => nodeAutoLayoutRank(node) < sceneRank);
      leftover = leftover.filter((node) => nodeAutoLayoutRank(node) >= sceneRank);
      if (before.length > 0) columns.push(sortAutoLayoutColumn(before));
      insertScene();
    }
    if (!insertedClip && clipAnchor <= layer) {
      const before = leftover.filter((node) => nodeAutoLayoutRank(node) < clipRank);
      leftover = leftover.filter((node) => nodeAutoLayoutRank(node) >= clipRank);
      if (before.length > 0) columns.push(sortAutoLayoutColumn(before));
      insertClip();
    }
    if (leftover.length > 0) columns.push(sortAutoLayoutColumn(leftover));
  }
  insertScene();
  insertClip();
  return columns;
}

function nodeShotIndex(node: DesignerGraphNode): number {
  const raw = (node.config as Record<string, unknown> | undefined)?.shot_index;
  const value = typeof raw === 'number' ? raw : Number(raw);
  if (Number.isFinite(value) && value > 0) return value;
  const fromLabel = String(node.label ?? '').match(/(\d+)\s*$/);
  if (!fromLabel) return 0;
  const parsed = Number(fromLabel[1]);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

function incomingByNode(
  nodes: DesignerGraphNode[],
  edges: DesignerGraphEdge[],
): Map<string, string[]> {
  const ids = new Set(nodes.map((node) => node.id));
  const incoming = new Map<string, string[]>();
  for (const node of nodes) incoming.set(node.id, []);
  const add = (source: string, target: string) => {
    if (!ids.has(source) || !ids.has(target) || source === target) return;
    const list = incoming.get(target);
    if (list && !list.includes(source)) list.push(source);
  };
  for (const edge of edges) add(String(edge.source || ''), String(edge.target || ''));
  for (const node of nodes) {
    const inputs = Array.isArray(node.config?.inputs) ? node.config.inputs.map(String) : [];
    for (const source of inputs) add(source, node.id);
  }
  return incoming;
}

function assignAutoLayoutLayers(
  nodes: DesignerGraphNode[],
  incoming: Map<string, string[]>,
): Map<string, number> {
  const remaining = new Set(nodes.map((node) => node.id));
  const layer = new Map<string, number>();
  let guard = 0;
  while (remaining.size > 0 && guard < nodes.length + 2) {
    guard += 1;
    const ready = [...remaining].filter((id) =>
      (incoming.get(id) ?? []).every((pred) => !remaining.has(pred)),
    );
    const nextId = [...remaining][0];
    const batch = ready.length > 0 ? ready : nextId ? [nextId] : [];
    for (const id of batch) {
      const predLayers = (incoming.get(id) ?? [])
        .map((pred) => layer.get(pred))
        .filter((value): value is number => typeof value === 'number');
      layer.set(id, predLayers.length > 0 ? Math.max(...predLayers) + 1 : 0);
      remaining.delete(id);
    }
  }
  return layer;
}

/** Layer nodes left-to-right by connections; Scene/Frame share one column, Clips the next. */
export function autoLayoutDesignerNodes(
  nodes: DesignerGraphNode[],
  edges: DesignerGraphEdge[] = [],
): DesignerGraphNode[] {
  if (nodes.length === 0) return nodes;
  const incoming = incomingByNode(nodes, edges);
  const layerById = assignAutoLayoutLayers(nodes, incoming);
  const columns = autoLayoutColumnPlan(nodes, layerById);
  const placed = new Map<string, { x: number; y: number; width: number; height: number }>();
  let cursorX = DESIGNER_LAYOUT_ORIGIN_X;
  for (const members of columns) {
    let cursorY = DESIGNER_LAYOUT_ORIGIN_Y;
    let colWidth = 0;
    for (const node of members) {
      const size = resolvedNodeCanvasSize(node);
      placed.set(node.id, {
        x: Math.round(cursorX),
        y: Math.round(cursorY),
        width: size.width,
        height: size.height,
      });
      cursorY += size.height + DESIGNER_SUCCESSOR_GAP_Y;
      colWidth = Math.max(colWidth, size.width);
    }
    cursorX += colWidth + DESIGNER_SUCCESSOR_GAP_X;
  }
  return applyNodeLayouts(nodes, placed);
}

export function autoLayoutDesignerGraph<T extends { nodes: DesignerGraphNode[]; edges: DesignerGraphEdge[] }>(
  graph: T,
): T {
  const nodes = autoLayoutDesignerNodes(graph.nodes, graph.edges);
  if (nodes === graph.nodes) return graph;
  return { ...graph, nodes };
}

export function packDesignerGraphLayout<T extends { nodes: DesignerGraphNode[] }>(graph: T): T {
  const nodes = packDesignerNodeLayouts(graph.nodes);
  if (nodes === graph.nodes) return graph;
  return { ...graph, nodes };
}

export function offsetCanvasPosition(
  origin: { x: number; y: number },
  existingCount: number,
): { x: number; y: number } {
  const shift = existingCount * 28;
  return {
    x: origin.x - DESIGNER_CANVAS_NODE_WIDTH / 2 + shift,
    y: origin.y - DESIGNER_CANVAS_NODE_HEIGHT / 2 + shift,
  };
}

export function positionRightOfNode(
  source: { id?: string; layout?: { x?: number; y?: number; width?: number; height?: number } },
  existing: Array<{ id: string; layout?: { x?: number; y?: number; width?: number; height?: number } }>,
): { x: number; y: number } {
  const width = source.layout?.width ?? DESIGNER_CANVAS_NODE_WIDTH;
  const height = source.layout?.height ?? DESIGNER_CANVAS_NODE_HEIGHT;
  const x = (source.layout?.x ?? 0) + width + DESIGNER_SUCCESSOR_GAP_X;
  const baseY = source.layout?.y ?? 0;
  const occupied = existing
    .filter((node) => node.id !== source.id)
    .filter((node) => Math.abs((node.layout?.x ?? 0) - x) < Math.max(width, DESIGNER_CANVAS_NODE_WIDTH) * 0.6)
    .map((node) => ({
      y: node.layout?.y ?? 0,
      height: node.layout?.height ?? DESIGNER_CANVAS_NODE_HEIGHT,
    }))
    .sort((left, right) => left.y - right.y);
  let y = baseY;
  for (const used of occupied) {
    if (y < used.y + used.height && y + height > used.y) {
      y = used.y + used.height + DESIGNER_SUCCESSOR_GAP_Y;
    }
  }
  return { x, y };
}

export function buildSuccessorEdge(sourceId: string, targetId: string): DesignerGraphEdge {
  return {
    id: `e_${sourceId}_${targetId}_${generateUuidV4().replace(/-/g, '').slice(0, 8)}`,
    source: sourceId,
    target: targetId,
    kind: 'data',
  };
}

export function withPredecessorInput(
  node: DesignerGraphNode,
  sourceId: string,
): DesignerGraphNode {
  const current = Array.isArray(node.config?.inputs) ? node.config.inputs.map(String) : [];
  if (current.includes(sourceId)) return node;
  return {
    ...node,
    config: {
      ...(node.config ?? {}),
      inputs: [...current, sourceId],
    } as DesignerGraphNode['config'],
  };
}

export type UserCanvasEdit = {
  op: 'add' | 'remove' | 'connect' | 'disconnect' | 'replace';
  node_id: string;
  peer_id?: string;
  label?: string;
  role?: string;
  type?: string;
  at: number;
};

export function appendUserCanvasEdit(
  metadata: Record<string, unknown> | undefined,
  edit: Omit<UserCanvasEdit, 'at'> & { at?: number },
): Record<string, unknown> {
  const previous = Array.isArray(metadata?.user_canvas_edits)
    ? metadata.user_canvas_edits.filter((item) => item && typeof item === 'object')
    : [];
  const next: UserCanvasEdit = {
    op: edit.op,
    node_id: edit.node_id,
    ...(edit.peer_id ? { peer_id: edit.peer_id } : {}),
    ...(edit.label ? { label: edit.label } : {}),
    ...(edit.role ? { role: edit.role } : {}),
    ...(edit.type ? { type: edit.type } : {}),
    at: edit.at ?? Date.now(),
  };
  return {
    ...(metadata ?? {}),
    user_topology_edit: true,
    user_canvas_edits: [...previous, next].slice(-40),
  };
}

const CANVAS_EDIT_OPS = new Set<UserCanvasEdit['op']>([
  'add',
  'remove',
  'connect',
  'disconnect',
  'replace',
]);

/** Latest canvas edit op. The visible sentence lives in designer.canvasEdit i18n. */
export function canvasEditGlance(edits: unknown): string {
  const list = Array.isArray(edits) ? edits : [];
  for (let index = list.length - 1; index >= 0; index -= 1) {
    const item = list[index];
    if (!item || typeof item !== 'object') continue;
    const op = String((item as Partial<UserCanvasEdit>).op || '') as UserCanvasEdit['op'];
    if (CANVAS_EDIT_OPS.has(op)) return op;
  }
  return '';
}

export function connectNodeToGraph(
  graph: { nodes: DesignerGraphNode[]; edges: DesignerGraphEdge[] },
  sourceId: string,
  node: DesignerGraphNode,
): { nodes: DesignerGraphNode[]; edges: DesignerGraphEdge[] } | null {
  const source = String(sourceId || '').trim();
  const target = String(node.id || '').trim();
  if (!source || !target) return null;
  if (!graph.nodes.some((item) => item.id === source)) return null;
  if (graph.nodes.some((item) => item.id === target)) return null;
  const nextNode = withPredecessorInput(node, source);
  const duplicateEdge = graph.edges.some(
    (edge) => edge.source === source && edge.target === target,
  );
  return {
    nodes: [...graph.nodes, nextNode],
    edges: duplicateEdge ? graph.edges : [...graph.edges, buildSuccessorEdge(source, target)],
  };
}

export function removeNodesFromGraph(
  graph: { nodes: DesignerGraphNode[]; edges: DesignerGraphEdge[] },
  nodeIds: Iterable<string>,
): { nodes: DesignerGraphNode[]; edges: DesignerGraphEdge[] } {
  const removeSet = new Set(
    [...nodeIds].map((id) => String(id || '').trim()).filter(Boolean),
  );
  if (removeSet.size === 0) {
    return { nodes: graph.nodes, edges: graph.edges };
  }
  const nodes = graph.nodes
    .filter((node) => !removeSet.has(node.id))
    .map((node) => {
      const inputs = Array.isArray(node.config?.inputs) ? node.config.inputs.map(String) : null;
      if (!inputs) return node;
      const nextInputs = inputs.filter((id) => !removeSet.has(id));
      if (nextInputs.length === inputs.length) return node;
      return {
        ...node,
        config: {
          ...(node.config ?? {}),
          inputs: nextInputs,
        } as DesignerGraphNode['config'],
      };
    });
  const edges = graph.edges.filter(
    (edge) => !removeSet.has(edge.source) && !removeSet.has(edge.target),
  );
  return { nodes, edges };
}

export function buildManualDesignerNode(params: {
  template: DesignerAddTemplate;
  existing: DesignerGraphNode[];
  position: { x: number; y: number };
  upload?: { filename: string; asset_id?: string; mime_type?: string };
  outputRef?: DesignerGraphNode['output_ref'];
}): DesignerGraphNode {
  const index = nextTypeIndex(params.existing, params.template.type);
  const label = `${params.template.label} ${index}`;
  const interactionMode = params.upload
    ? 'upload'
    : params.template.type === DESIGNER_NODE_TYPE_TEXT || params.template.type === DESIGNER_NODE_TYPE_TABLE
      ? 'edit'
      : 'generate';
  const node: DesignerGraphNode = {
    id: uniqueDesignerNodeId(params.existing.map((item) => item.id), params.template.role),
    type: params.template.type,
    label,
    config: {
      role: params.template.role,
      delegate: DESIGNER_CONFIG_DELEGATE_HANDLER,
      interaction_mode: interactionMode,
      user_added: true,
      kind: 'agent',
      ...(params.upload ? { upload: params.upload } : {}),
    },
    layout: {
      x: params.position.x,
      y: params.position.y,
      width: DESIGNER_CANVAS_NODE_WIDTH,
      height: DESIGNER_CANVAS_NODE_HEIGHT,
    },
  };
  if (params.outputRef) {
    node.output_ref = params.outputRef;
  }
  return node;
}

export function templateForAssetKind(kind: DesignerLibraryAsset['kind']): DesignerAddTemplate {
  if (kind === 'video') {
    return DESIGNER_ADD_TEMPLATES.find((item) => item.id === 'video') as DesignerAddTemplate;
  }
  if (kind === 'audio') {
    return DESIGNER_ADD_TEMPLATES.find((item) => item.id === 'audio') as DesignerAddTemplate;
  }
  return DESIGNER_ADD_TEMPLATES.find((item) => item.id === 'image') as DesignerAddTemplate;
}

export function buildNodeFromLibraryAsset(params: {
  asset: DesignerLibraryAsset;
  existing: DesignerGraphNode[];
  position: { x: number; y: number };
}): DesignerGraphNode {
  const template = templateForAssetKind(params.asset.kind);
  return buildManualDesignerNode({
    template,
    existing: params.existing,
    position: params.position,
    upload: {
      filename: params.asset.filename,
      asset_id: params.asset.id,
      mime_type: params.asset.mime_type,
    },
    outputRef: {
      kind: template.type,
      uri: params.asset.objectUrl,
      mime_type: params.asset.mime_type,
      label: params.asset.filename,
    },
  });
}
