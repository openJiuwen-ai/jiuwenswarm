import { useMemo, useState } from 'react';
import clsx from 'clsx';
import { ChevronDown, GitMerge } from 'lucide-react';
import type { BeamSearchNode, BeamSearchProgress } from '../../types/beamSearch';
import {
  buildBeamTree,
  type BeamTreeMergeEntry,
  type BeamTreeNodeEntry,
} from './beamSearchTreeModel';
import './BeamSearchTree.css';
import { useProcessTreeCollapse } from './useProcessTreeCollapse';

const MAX_VISIBLE_REJECTED_CHILDREN = 4;

interface BeamTreeCopy {
  title: string;
  seedStage: string;
  round: (roundIndex: number) => string;
  summary: (total: number, selected: number, rejected: number) => string;
  skillType: string;
  seedType: string;
  statuses: Record<BeamSearchNode['status'], string>;
  mergeTo: (label: string) => string;
  showRejected: (count: number) => string;
  hideRejected: string;
  empty: string;
}

const COPY: Record<BeamSearchProgress['language'], BeamTreeCopy> = {
  cn: {
    title: '技能编排',
    seedStage: '种子确认',
    round: (roundIndex) => `第 ${roundIndex} 轮`,
    summary: (total, selected, rejected) =>
      `${total} 个技能 · ${selected} 个入选 · ${rejected} 个未入选`,
    skillType: '技能',
    seedType: '种子',
    statuses: {
      seed: '种子技能',
      pending: '判断中',
      selected: '已入选',
      rejected: '未入选',
      final: '最终方案',
    },
    mergeTo: (label) => `汇合到 ${label}`,
    showRejected: (count) => `还有 ${count} 个未入选技能`,
    hideRejected: '收起未入选技能',
    empty: '正在准备候选技能',
  },
  en: {
    title: 'Skill orchestration',
    seedStage: 'Seeds ready',
    round: (roundIndex) => `Round ${roundIndex}`,
    summary: (total, selected, rejected) =>
      `${total} skills · ${selected} selected · ${rejected} rejected`,
    skillType: 'Skill',
    seedType: 'Seed',
    statuses: {
      seed: 'Seed skill',
      pending: 'Judging',
      selected: 'Selected',
      rejected: 'Rejected',
      final: 'Final plan',
    },
    mergeTo: (label) => `Merges into ${label}`,
    showRejected: (count) => `${count} more rejected skills`,
    hideRejected: 'Hide rejected skills',
    empty: 'Preparing candidate skills',
  },
};

function BeamMergeReference({ entry, copy }: {
  entry: BeamTreeMergeEntry;
  copy: BeamTreeCopy;
}) {
  const label = entry.node.label || entry.node.id;
  return (
    <div
      className={clsx('beam-tree__merge', `is-${entry.node.status}`)}
      title={copy.mergeTo(label)}
      data-testid={`beam-tree-merge-${entry.node.id}`}
    >
      <GitMerge size={13} aria-hidden="true" />
      <span>{copy.mergeTo(label)}</span>
    </div>
  );
}

function BeamTreeNodeCard({ entry, copy }: {
  entry: BeamTreeNodeEntry;
  copy: BeamTreeCopy;
}) {
  const node = entry.node;
  const label = node.label || node.id;
  return (
    <div
      className={clsx('beam-tree__node', `is-${node.status}`)}
      title={label}
      data-testid={`beam-tree-node-${node.id}`}
    >
      <div className="beam-tree__node-main">
        <span className="beam-tree__type">
          {node.status === 'seed' ? copy.seedType : copy.skillType}
        </span>
        <span className="beam-tree__label">{label}</span>
        <span className="beam-tree__state">{copy.statuses[node.status]}</span>
      </div>
    </div>
  );
}

function BeamTreeBranch({ entry, copy }: {
  entry: BeamTreeNodeEntry;
  copy: BeamTreeCopy;
}) {
  const [showAllRejected, setShowAllRejected] = useState(false);
  let rejectedSeen = 0;
  const hiddenRejectedCount = entry.children.reduce((count, child) => (
    child.node.status === 'rejected' ? count + 1 : count
  ), 0) - MAX_VISIBLE_REJECTED_CHILDREN;
  const visibleChildren = entry.children.filter((child) => {
    if (child.node.status !== 'rejected' || showAllRejected) return true;
    rejectedSeen += 1;
    return rejectedSeen <= MAX_VISIBLE_REJECTED_CHILDREN;
  });

  return (
    <div className="beam-tree__branch">
      <BeamTreeNodeCard entry={entry} copy={copy} />
      {(visibleChildren.length > 0 || hiddenRejectedCount > 0) && (
        <div className="beam-tree__children">
          {visibleChildren.map((child, index) => (
            <div
              className="beam-tree__child"
              key={`${entry.node.id}-${child.kind}-${child.node.id}-${index}`}
            >
              {child.kind === 'merge' ? (
                <BeamMergeReference entry={child} copy={copy} />
              ) : (
                <BeamTreeBranch entry={child} copy={copy} />
              )}
            </div>
          ))}
          {hiddenRejectedCount > 0 && (
            <button
              type="button"
              className="beam-tree__more"
              onClick={() => setShowAllRejected((value) => !value)}
              aria-expanded={showAllRejected}
            >
              <span>
                {showAllRejected
                  ? copy.hideRejected
                  : copy.showRejected(hiddenRejectedCount)}
              </span>
              <ChevronDown
                size={13}
                className={clsx('beam-tree__more-chevron', showAllRejected && 'is-open')}
                aria-hidden="true"
              />
            </button>
          )}
        </div>
      )}
    </div>
  );
}

interface BeamSearchTreeProps {
  progress: BeamSearchProgress;
  autoCollapse?: boolean;
}

export function BeamSearchTree({
  progress,
  autoCollapse = false,
}: BeamSearchTreeProps) {
  const [collapsed, setCollapsed] = useProcessTreeCollapse(autoCollapse);
  const model = useMemo(() => buildBeamTree(progress), [progress]);
  const copy = COPY[progress.language];
  const selected = progress.graph.nodes.filter(
    (node) => node.status === 'selected' || node.status === 'final'
  ).length;
  const rejected = progress.graph.nodes.filter((node) => node.status === 'rejected').length;
  const stage = progress.roundIndex > 0
    ? copy.round(progress.roundIndex)
    : copy.seedStage;

  return (
    <section className="beam-tree animate-rise" data-testid="beam-search-tree">
      <button
        type="button"
        className="beam-tree__header"
        onClick={() => setCollapsed((value) => !value)}
        aria-expanded={!collapsed}
      >
        <span className="beam-tree__title">
          <span className="beam-tree__badge">{copy.title}</span>
          <span className="beam-tree__stage">{stage}</span>
        </span>
        <span className="beam-tree__meta">
          {copy.summary(model.nodeCount, selected, rejected)}
          <ChevronDown
            className={clsx('beam-tree__chevron', !collapsed && 'is-open')}
            size={14}
            aria-hidden="true"
          />
        </span>
      </button>
      {!collapsed && (
        <div className="beam-tree__body">
          {model.roots.length > 0 ? (
            <div className="beam-tree__forest">
              {model.roots.map((root) => (
                <BeamTreeBranch key={root.node.id} entry={root} copy={copy} />
              ))}
            </div>
          ) : (
            <div className="beam-tree__empty">{copy.empty}</div>
          )}
        </div>
      )}
    </section>
  );
}
