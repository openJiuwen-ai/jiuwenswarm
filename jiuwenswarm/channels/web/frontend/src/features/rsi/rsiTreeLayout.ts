/**
 * RSI 演进树布局算法（纯函数）。
 * 有向树形图：1 根、每节点任意子节点、父→子单向、层级不限。
 * 布局方向：从左到右——根节点在左，子节点在右、同层兄弟纵向排开。
 * 支持节点展开/收起：收起的节点不布局其子树。
 * 节点尺寸随内容自适应（分数多行展开 / 评测中文本行数 / 评测中宽度≤280），
 * 布局与组件共用 nodeMetrics，保证布局高度=渲染高度。
 */

import type { RsiTreeNode } from './types';
import { nodeMetrics, nodeRuntimeKindForNode, type NodeRuntimeKind } from './rsiPresentation';

export interface LayoutNode {
  node: RsiTreeNode;
  // 节点左上角坐标（相对画布原点）
  x: number;
  y: number;
  // 中心点坐标（用于连线）
  cx: number;
  cy: number;
  // 节点实际宽高（自适应）
  width: number;
  height: number;
  depth: number;
  hasChildren: boolean;
  childCount: number;
}

export interface LayoutEdge {
  from: LayoutNode;
  to: LayoutNode;
}

export interface TreeLayout {
  nodes: LayoutNode[];
  edges: LayoutEdge[];
  width: number;
  height: number;
}

// 默认尺寸（仅用于无 metrics 时的回退与外部参考）
export const NODE_W = 180;
export const NODE_H = 74;
// 层间水平间距（父→子，横向）
export const DEPTH_GAP = 80;
// 同层兄弟纵向间距
export const SIBLING_GAP = 24;

interface Subtree {
  node: RsiTreeNode;
  children: Subtree[];
  // 该子树占据的纵向高度（含自身 + SIBLING_GAP 余量）
  height: number;
  // 节点自身宽高（来自 nodeMetrics）
  w: number;
  h: number;
}

function kindOf(node: RsiTreeNode, taskRunning: boolean): NodeRuntimeKind {
  return nodeRuntimeKindForNode(node, taskRunning);
}

function buildSubtree(
  node: RsiTreeNode,
  byId: Map<string, RsiTreeNode>,
  collapsed: Set<string>,
  taskRunning: boolean,
  scoreExpanded: Set<string>,
): Subtree {
  const kind = kindOf(node, taskRunning);
  const m = nodeMetrics(node, kind, scoreExpanded.has(node.node_id));
  const h = m.barH + m.bodyH;
  if (collapsed.has(node.node_id)) {
    return { node, children: [], height: h + SIBLING_GAP, w: m.width, h };
  }
  const childNodes = Array.from(byId.values()).filter((n) => n.parent_id === node.node_id);
  const subs = childNodes.map((c) => buildSubtree(c, byId, collapsed, taskRunning, scoreExpanded));
  const totalChildHeight = subs.length ? subs.reduce((acc, s) => acc + Math.max(s.height, s.h + SIBLING_GAP), 0) : 0;
  return { node, children: subs, height: Math.max(h + SIBLING_GAP, totalChildHeight), w: m.width, h };
}

// 收集每层最大宽度，用于计算列左边界（同层节点中心对齐）
function collectLayerWidths(sub: Subtree, depth: number, acc: number[]): void {
  acc[depth] = Math.max(acc[depth] ?? 0, sub.w);
  for (const c of sub.children) collectLayerWidths(c, depth + 1, acc);
}

function assign(
  sub: Subtree,
  depth: number,
  centerY: number,
  layerLeft: number[],
  layerWidth: number[],
  out: LayoutNode[],
  byId: Map<string, RsiTreeNode>,
  collapsed: Set<string>,
  taskRunning: boolean,
  scoreExpanded: Set<string>,
): void {
  const cx = layerLeft[depth] + layerWidth[depth] / 2;
  const childCount = Array.from(byId.values()).filter((n) => n.parent_id === sub.node.node_id).length;
  const hasChildren = childCount > 0;

  if (sub.children.length === 0) {
    // 叶子：直接用传入的中心点
    const cy = centerY;
    out.push({
      node: sub.node,
      x: cx - sub.w / 2,
      y: cy - sub.h / 2,
      cx,
      cy,
      width: sub.w,
      height: sub.h,
      depth,
      hasChildren,
      childCount,
    });
    return;
  }

  // 有子节点：先布局子节点，再把父节点对齐到「中间子节点」最终 cy。
  const childHeights = sub.children.map((s) => Math.max(s.height, s.h + SIBLING_GAP));
  const total = childHeights.reduce((a, b) => a + b, 0);
  let cursor = centerY - total / 2;
  const directChildCy: number[] = [];
  sub.children.forEach((child) => {
    const ch = childHeights[sub.children.indexOf(child)];
    const childCy = cursor + ch / 2;
    assign(child, depth + 1, childCy, layerLeft, layerWidth, out, byId, collapsed, taskRunning, scoreExpanded);
    directChildCy.push(out[out.length - 1].cy);
    cursor += ch;
  });
  const mid = directChildCy.length / 2;
  const cy =
    directChildCy.length % 2 === 1 ? directChildCy[Math.floor(mid)] : (directChildCy[mid - 1] + directChildCy[mid]) / 2;
  out.push({
    node: sub.node,
    x: cx - sub.w / 2,
    y: cy - sub.h / 2,
    cx,
    cy,
    width: sub.w,
    height: sub.h,
    depth,
    hasChildren,
    childCount,
  });
}

export function layoutTree(
  rootNodes: RsiTreeNode[],
  allNodes: RsiTreeNode[],
  collapsed: Set<string> = new Set(),
  taskRunning = false,
  scoreExpanded: Set<string> = new Set(),
): TreeLayout {
  const byId = new Map(allNodes.map((n) => [n.node_id, n]));
  const roots = rootNodes.length ? rootNodes : allNodes.filter((n) => n.parent_id === null);
  const subs = roots.map((r) => buildSubtree(r, byId, collapsed, taskRunning, scoreExpanded));

  // 计算每层列宽与列左边界
  const layerWidth: number[] = [];
  for (const s of subs) collectLayerWidths(s, 0, layerWidth);
  const layerLeft: number[] = [];
  let acc = 0;
  for (let d = 0; d < layerWidth.length; d++) {
    layerLeft[d] = acc;
    acc += layerWidth[d] + DEPTH_GAP;
  }

  const layoutNodes: LayoutNode[] = [];
  let cursorY = 0;
  for (const s of subs) {
    const h = Math.max(s.height, s.h + SIBLING_GAP);
    assign(s, 0, cursorY + h / 2, layerLeft, layerWidth, layoutNodes, byId, collapsed, taskRunning, scoreExpanded);
    cursorY += h;
  }

  let maxX = 0;
  let maxY = 0;
  for (const n of layoutNodes) {
    maxX = Math.max(maxX, n.x + n.width);
    maxY = Math.max(maxY, n.y + n.height);
  }

  const edges: LayoutEdge[] = [];
  for (const n of layoutNodes) {
    if (n.node.parent_id) {
      const parentLn = layoutNodes.find((p) => p.node.node_id === n.node.parent_id);
      if (parentLn) edges.push({ from: parentLn, to: n });
    }
  }

  return {
    nodes: layoutNodes,
    edges,
    width: maxX + DEPTH_GAP,
    height: maxY + SIBLING_GAP,
  };
}
