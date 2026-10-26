/**
 * SkillTreePath
 *
 * Inline visualization for agentic skill retrieval. A tool group can contain
 * multiple skill_branch_explore / skill_branch_peek calls; this component folds
 * them into one horizontal browsing tree.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import clsx from 'clsx';
import type {
  SkillTreeCandidate,
  SkillTreeNamedId,
  SkillTreePath as SkillTreePathData,
} from '../../types/skillTree';
import './SkillTreePath.css';
import { useProcessTreeCollapse } from './useProcessTreeCollapse';

interface SkillTreePathProps {
  tree?: SkillTreePathData;
  trees?: SkillTreePathData[];
  autoCollapse?: boolean;
  viewedSkillIds?: string[];
  /** Kept for compatibility with the previous path replay component. */
  stepIntervalMs?: number;
}

type BrowseNodeKind = 'branch' | 'skill';

interface BrowseNode {
  id: string;
  label: string;
  kind: BrowseNodeKind;
  depth: number;
  parentId?: string;
  children: string[];
  sequence: number;
  exploreCount: number;
  peekCount: number;
  selectableCount?: number | null;
  hiddenBranchIds: Set<string>;
  candidate?: SkillTreeCandidate;
}

interface BrowseGraph {
  nodes: Map<string, BrowseNode>;
  rootIds: string[];
  exploreCount: number;
  peekCount: number;
  exposedSkillCount: number;
  foldedBranchCount: number;
  queryLabel: string;
}

const MAX_VISIBLE_CHILDREN = 8;

function collectTrees(tree?: SkillTreePathData, trees?: SkillTreePathData[]): SkillTreePathData[] {
  const result: SkillTreePathData[] = [];
  if (tree) {
    result.push(tree);
  }
  if (trees) {
    result.push(...trees.filter(Boolean));
  }
  return result;
}

function normalizeId(value: string | undefined | null, fallback = 'ROOT'): string {
  const text = String(value || '').trim();
  return text || fallback;
}

function normalizeLookupKey(value: string | undefined | null): string {
  return String(value || '').trim().toLowerCase();
}

function buildViewedSkillKeySet(viewedSkillIds?: string[]): Set<string> {
  const out = new Set<string>();
  for (const id of viewedSkillIds || []) {
    const key = normalizeLookupKey(id);
    if (key) {
      out.add(key);
    }
  }
  return out;
}

function displayLabel(item: SkillTreeNamedId): string {
  return item.label || item.id;
}

function isPeekTree(tree: SkillTreePathData): boolean {
  return tree.query.toLowerCase().includes('skill_branch_peek');
}

function isExploreTree(tree: SkillTreePathData): boolean {
  return tree.query.toLowerCase().includes('skill_branch_explore');
}

function addCandidateLookup(
  lookup: Map<string, SkillTreeCandidate>,
  key: string | undefined,
  candidate: SkillTreeCandidate
) {
  const normalized = normalizeLookupKey(key);
  if (normalized && !lookup.has(normalized)) {
    lookup.set(normalized, candidate);
  }
}

function buildCandidateLookup(trees: SkillTreePathData[]): Map<string, SkillTreeCandidate> {
  const lookup = new Map<string, SkillTreeCandidate>();
  for (const tree of trees) {
    for (const candidate of tree.candidates || []) {
      addCandidateLookup(lookup, candidate.worker_id, candidate);
      addCandidateLookup(lookup, candidate.label, candidate);
      addCandidateLookup(lookup, candidate.path?.[candidate.path.length - 1], candidate);
    }
  }
  return lookup;
}

function buildTouchedBranchIds(trees: SkillTreePathData[]): Set<string> {
  const ids = new Set<string>();
  for (const tree of trees) {
    for (const step of tree.steps || []) {
      ids.add(normalizeId(step.node_id));
    }
  }
  return ids;
}

function formatQueryLabel(trees: SkillTreePathData[]): string {
  if (trees.length === 0) {
    return '';
  }
  if (trees.length === 1) {
    const query = trees[0].query;
    const target = query.includes(':') ? query.split(':').slice(1).join(':').trim() : '';
    if (isExploreTree(trees[0])) {
      return target ? `展开 ${target}` : '展开分支';
    }
    if (isPeekTree(trees[0])) {
      return target ? `窥探 ${target}` : '窥探分支';
    }
    return query;
  }
  const exploreCount = trees.filter(isExploreTree).length;
  const peekCount = trees.filter(isPeekTree).length;
  return `${trees.length} 次浏览 · ${exploreCount} 展开 · ${peekCount} 窥探`;
}

function buildBrowseGraph(trees: SkillTreePathData[]): BrowseGraph {
  const nodes = new Map<string, BrowseNode>();
  const candidateLookup = buildCandidateLookup(trees);
  const touchedBranchIds = buildTouchedBranchIds(trees);
  let sequence = 0;
  let exploreCount = 0;
  let peekCount = 0;

  const ensureNode = (
    id: string,
    label: string,
    kind: BrowseNodeKind,
    depth: number,
    parentId?: string,
    candidate?: SkillTreeCandidate
  ): BrowseNode => {
    const normalizedId = normalizeId(id);
    const existing = nodes.get(normalizedId);
    if (existing) {
      if (!existing.label && label) {
        existing.label = label;
      }
      if (parentId && !existing.parentId) {
        existing.parentId = parentId;
      }
      if (candidate && !existing.candidate) {
        existing.candidate = candidate;
      }
      existing.depth = Math.min(existing.depth, depth);
      return existing;
    }
    const node: BrowseNode = {
      id: normalizedId,
      label: label || normalizedId,
      kind,
      depth,
      parentId,
      children: [],
      sequence,
      exploreCount: 0,
      peekCount: 0,
      hiddenBranchIds: new Set<string>(),
      candidate,
    };
    sequence += 1;
    nodes.set(normalizedId, node);
    return node;
  };

  const addChild = (parent: BrowseNode, child: BrowseNode) => {
    if (!parent.children.includes(child.id)) {
      parent.children.push(child.id);
    }
    if (!child.parentId) {
      child.parentId = parent.id;
    }
  };

  for (const tree of trees) {
    const treeIsPeek = isPeekTree(tree);
    const treeIsExplore = isExploreTree(tree);
    let candidateCursor = 0;
    for (const step of tree.steps || []) {
      if (step.event_type !== 'fragment_built') {
        continue;
      }

      const stepCandidateCount =
        typeof step.candidate_count === 'number'
          ? Math.max(0, step.candidate_count)
          : 0;
      const stepCandidates = treeIsExplore
        ? (tree.candidates || []).slice(candidateCursor, candidateCursor + stepCandidateCount)
        : [];
      if (treeIsExplore) {
        candidateCursor += stepCandidateCount;
      }

      const node = ensureNode(
        normalizeId(step.node_id),
        step.label || step.node_id,
        'branch',
        step.depth
      );
      node.selectableCount = step.selectable_count;

      if (treeIsPeek) {
        node.peekCount += 1;
        peekCount += 1;
        continue;
      }

      if (!treeIsExplore) {
        continue;
      }

      node.exploreCount += 1;
      exploreCount += 1;

      for (const branch of step.branches || []) {
        const branchId = normalizeId(branch.id);
        if (!touchedBranchIds.has(branchId)) {
          node.hiddenBranchIds.add(branchId);
          continue;
        }
        const child = ensureNode(
          branchId,
          displayLabel(branch),
          'branch',
          step.depth + 1,
          node.id
        );
        addChild(node, child);
      }

      for (const [leafIndex, leaf] of (step.leaves || []).entries()) {
        const leafId = normalizeId(leaf.id, displayLabel(leaf));
        const candidate =
          candidateLookup.get(normalizeLookupKey(leafId)) ||
          candidateLookup.get(normalizeLookupKey(leaf.label)) ||
          stepCandidates[leafIndex];
        const child = ensureNode(
          leafId,
          candidate?.label || displayLabel(leaf),
          'skill',
          step.depth + 1,
          node.id,
          candidate
        );
        addChild(node, child);
      }
    }
  }

  const rootIds = Array.from(nodes.values())
    .filter((node) => !node.parentId || !nodes.has(node.parentId))
    .sort((left, right) => left.sequence - right.sequence)
    .map((node) => node.id);
  const exposedSkillCount = Array.from(nodes.values()).filter((node) => node.kind === 'skill').length;
  const foldedBranchCount = Array.from(nodes.values()).reduce(
    (total, node) => total + node.hiddenBranchIds.size,
    0
  );

  return {
    nodes,
    rootIds,
    exploreCount,
    peekCount,
    exposedSkillCount,
    foldedBranchCount,
    queryLabel: formatQueryLabel(trees),
  };
}

function nodeStateLabel(node: BrowseNode): string {
  if (node.kind === 'skill') {
    return '已披露技能';
  }
  if (node.exploreCount > 0) {
    return '已展开';
  }
  if (node.peekCount > 0) {
    return '已窥探';
  }
  return '已披露分支';
}

function nodeTitle(node: BrowseNode): string {
  if (node.kind === 'skill') {
    return node.candidate?.description || node.id;
  }
  return node.id;
}

interface BrowseNodeViewProps {
  graph: BrowseGraph;
  nodeId: string;
  expandedNodeIds: Set<string>;
  newNodeIds: Set<string>;
  viewedSkillKeys: Set<string>;
  onToggleExpanded: (nodeId: string) => void;
}

function BrowseNodeView({
  graph,
  nodeId,
  expandedNodeIds,
  newNodeIds,
  viewedSkillKeys,
  onToggleExpanded,
}: BrowseNodeViewProps) {
  const node = graph.nodes.get(nodeId);
  if (!node) {
    return null;
  }

  const children = node.children
    .map((id) => graph.nodes.get(id))
    .filter((child): child is BrowseNode => Boolean(child))
    .sort((left, right) => left.sequence - right.sequence);
  const expanded = expandedNodeIds.has(node.id);
  const visibleChildren = expanded ? children : children.slice(0, MAX_VISIBLE_CHILDREN);
  const hiddenChildCount = Math.max(0, children.length - visibleChildren.length);
  const hasChildren = children.length > 0;
  const hasFoldedBranches = node.hiddenBranchIds.size > 0;
  const isNewNode = newNodeIds.has(node.id);
  const isViewedSkill = node.kind === 'skill' && (
    viewedSkillKeys.has(normalizeLookupKey(node.id)) ||
    viewedSkillKeys.has(normalizeLookupKey(node.label)) ||
    viewedSkillKeys.has(normalizeLookupKey(node.candidate?.worker_id)) ||
    viewedSkillKeys.has(normalizeLookupKey(node.candidate?.label)) ||
    viewedSkillKeys.has(normalizeLookupKey(node.candidate?.path?.[node.candidate.path.length - 1]))
  );
  const nodeMeta = node.kind === 'skill'
    ? node.candidate?.description || node.candidate?.worker_id || node.id
    : hasChildren
    ? `${children.length} 个已展开子项`
    : hasFoldedBranches
    ? `${node.hiddenBranchIds.size} 个未查看分支已折叠`
    : '暂无展开子项';

  return (
    <div className="skill-path-tree__row" style={{ paddingLeft: `${Math.min(node.depth, 6) * 14}px` }}>
      <div
        className={clsx(
          'skill-path-tree__node',
          `skill-path-tree__node--${node.kind}`,
          node.exploreCount > 0 && 'is-explored',
          node.peekCount > 0 && node.exploreCount === 0 && 'is-peeked',
          isNewNode && 'is-new',
          isViewedSkill && 'is-viewed'
        )}
        title={nodeTitle(node)}
      >
        <div className="skill-path-tree__node-main">
          <span className="skill-path-tree__type">
            {node.kind === 'skill' ? '技能' : '分支'}
          </span>
          <span className="skill-path-tree__label">{node.label}</span>
          <span className="skill-path-tree__state">
            {isViewedSkill ? '已查看' : nodeStateLabel(node)}
          </span>
        </div>
        {nodeMeta && <div className="skill-path-tree__meta">{nodeMeta}</div>}
      </div>

      {hasChildren && (
        <div className="skill-path-tree__children">
          {visibleChildren.map((child) => (
            <BrowseNodeView
              key={child.id}
              graph={graph}
              nodeId={child.id}
              expandedNodeIds={expandedNodeIds}
              newNodeIds={newNodeIds}
              viewedSkillKeys={viewedSkillKeys}
              onToggleExpanded={onToggleExpanded}
            />
          ))}
          {hiddenChildCount > 0 && (
            <button
              type="button"
              className="skill-path-tree__more"
              onClick={() => onToggleExpanded(node.id)}
            >
              展开其余 {hiddenChildCount} 项
            </button>
          )}
          {hiddenChildCount === 0 && children.length > MAX_VISIBLE_CHILDREN && (
            <button
              type="button"
              className="skill-path-tree__more"
              onClick={() => onToggleExpanded(node.id)}
            >
              收起部分子项
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export function SkillTreePath({
  tree,
  trees,
  autoCollapse = false,
  viewedSkillIds,
  stepIntervalMs = 0,
}: SkillTreePathProps) {
  void stepIntervalMs;
  const allTrees = useMemo(() => collectTrees(tree, trees), [tree, trees]);
  const graph = useMemo(() => buildBrowseGraph(allTrees), [allTrees]);
  const [accumulatedViewedSkillIds, setAccumulatedViewedSkillIds] = useState<string[]>(() => viewedSkillIds || []);
  const viewedSkillKeys = useMemo(
    () => buildViewedSkillKeySet(accumulatedViewedSkillIds),
    [accumulatedViewedSkillIds]
  );
  const [collapsed, setCollapsed] = useProcessTreeCollapse(
    autoCollapse,
    graph.queryLabel
  );
  const [expandedNodeIds, setExpandedNodeIds] = useState<Set<string>>(() => new Set());
  const [newNodeIds, setNewNodeIds] = useState<Set<string>>(() => new Set());
  const previousNodeIdsRef = useRef<Set<string> | null>(null);

  useEffect(() => {
    if (!viewedSkillIds || viewedSkillIds.length === 0) {
      return;
    }
    setAccumulatedViewedSkillIds((current) => {
      const next = Array.from(new Set([...current, ...viewedSkillIds]));
      return next.length === current.length ? current : next;
    });
  }, [viewedSkillIds]);

  useEffect(() => {
    const nextNodeIds = new Set(graph.nodes.keys());
    const previousNodeIds = previousNodeIdsRef.current;
    previousNodeIdsRef.current = nextNodeIds;

    if (!previousNodeIds) {
      return;
    }

    const addedNodeIds = Array.from(nextNodeIds).filter((nodeId) => !previousNodeIds.has(nodeId));
    if (addedNodeIds.length === 0) {
      setNewNodeIds((current) => (current.size > 0 ? new Set() : current));
      return;
    }

    setNewNodeIds(new Set(addedNodeIds));
    const timer = window.setTimeout(() => {
      setNewNodeIds((current) => (current.size > 0 ? new Set() : current));
    }, 520);
    return () => window.clearTimeout(timer);
  }, [graph]);

  const toggleExpanded = (nodeId: string) => {
    setExpandedNodeIds((current) => {
      const next = new Set(current);
      if (next.has(nodeId)) {
        next.delete(nodeId);
      } else {
        next.add(nodeId);
      }
      return next;
    });
  };

  if (allTrees.length === 0 || graph.nodes.size === 0) {
    return null;
  }

  return (
    <div className="skill-path animate-rise" data-testid="skill-tree-path">
      <button
        type="button"
        className="skill-path__header"
        onClick={() => setCollapsed((value) => !value)}
        aria-expanded={!collapsed}
      >
        <span className="skill-path__title">
          <span className="skill-path__badge">技能检索树</span>
          {graph.queryLabel && (
            <span className="skill-path__query" title={graph.queryLabel}>
              {graph.queryLabel}
            </span>
          )}
        </span>
        <span className="skill-path__meta">
          <span>{graph.exploreCount} 次展开</span>
          <span>{graph.peekCount} 次窥探</span>
          <span>{graph.exposedSkillCount} 个披露技能</span>
          {graph.foldedBranchCount > 0 && <span>{graph.foldedBranchCount} 个分支折叠</span>}
          <span className={clsx('skill-path__chevron', !collapsed && 'is-open')} aria-hidden="true">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path strokeLinecap="round" strokeLinejoin="round" d="m6.5 8 3.5 4 3.5-4" />
            </svg>
          </span>
        </span>
      </button>

      {!collapsed && (
        <div className="skill-path__body-wrap">
          <div className="skill-path-tree">
            <div className="skill-path-tree__canvas">
              <div className="skill-path-tree__forest">
                {graph.rootIds.map((rootId) => (
                  <BrowseNodeView
                    key={rootId}
                    graph={graph}
                    nodeId={rootId}
                    expandedNodeIds={expandedNodeIds}
                    newNodeIds={newNodeIds}
                    viewedSkillKeys={viewedSkillKeys}
                    onToggleExpanded={toggleExpanded}
                  />
                ))}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
