import { useEffect, useState } from 'react';
import { ChevronDown, ChevronRight, FileCode2, FileText } from 'lucide-react';
import FolderAssetIcon from '../../../assets/work-mode/folder.svg?react';
import FolderFoldAssetIcon from '../../../assets/work-mode/folder-fold.svg?react';
import { isCodeFileName, type FilePreviewStatus } from './filePreviewShared';

export type FilePreviewTreeNode = {
  /** 唯一路径，用作 React key / 选中判断 / testid variant */
  path: string;
  label: string;
  kind: 'file' | 'directory';
  /** false 时该节点不渲染（其后代一并隐藏） */
  visible?: boolean;
  /** 仅文件有效；false 时禁点并展示「不可预览」徽标 */
  previewable?: boolean;
  /** 需要强调展示的文件（如 Agent 的 SKILL.md） */
  highlight?: boolean;
  children?: FilePreviewTreeNode[];
};

export type FilePreviewTreeLabels = {
  loading: string;
  error: string;
  empty: string;
  retry: string;
  notPreviewable: string;
};

export type FilePreviewTreeProps = {
  nodes: FilePreviewTreeNode[];
  status: FilePreviewStatus;
  selectedPath: string | null;
  onSelectFile: (path: string) => void;
  onRetry?: () => void;
  labels: FilePreviewTreeLabels;
  testId?: string;
  ariaLabel?: string;
};

function findExpandedDirectories(nodes: FilePreviewTreeNode[]): Set<string> {
  const expanded = new Set<string>();
  const visit = (items: FilePreviewTreeNode[]) => {
    for (const item of items) {
      if (item.visible === false || item.kind !== 'directory') continue;
      expanded.add(item.path);
      visit(item.children || []);
    }
  };
  visit(nodes);
  return expanded;
}

function TreeEntry({
  entry,
  depth,
  expanded,
  onToggle,
  selectedPath,
  onSelectFile,
  notPreviewableLabel,
  itemTestId,
}: {
  entry: FilePreviewTreeNode;
  depth: number;
  expanded: Set<string>;
  onToggle: (path: string) => void;
  selectedPath: string | null;
  onSelectFile: (path: string) => void;
  notPreviewableLabel: string;
  itemTestId: string;
}) {
  if (entry.visible === false) return null;
  const isDirectory = entry.kind === 'directory';
  const isExpanded = expanded.has(entry.path);
  const unsupported = !isDirectory && entry.previewable === false;
  const className = [
    'file-preview-tree__entry',
    isDirectory ? 'is-directory' : '',
    selectedPath === entry.path ? 'is-selected' : '',
    unsupported ? 'is-unsupported' : '',
    entry.highlight ? 'is-highlight' : '',
  ]
    .filter(Boolean)
    .join(' ');
  return (
    <div>
      <button
        type="button"
        className={className}
        style={{ paddingLeft: `${depth * 24 + 8}px` }}
        data-testid={itemTestId}
        data-variant={entry.path}
        disabled={unsupported}
        onClick={() => (isDirectory ? onToggle(entry.path) : onSelectFile(entry.path))}
        aria-label={entry.label}
        title={entry.path}
      >
        <span className="file-preview-tree__entry-icon" aria-hidden="true">
          {isDirectory ? (
            isExpanded ? (
              <FolderFoldAssetIcon width={12} height={12} />
            ) : (
              <FolderAssetIcon width={12} height={12} />
            )
          ) : isCodeFileName(entry.label) ? (
            <FileCode2 size={16} strokeWidth={1.5} />
          ) : (
            <FileText size={16} strokeWidth={1.5} />
          )}
        </span>
        <span className="file-preview-tree__entry-label">{entry.label}</span>
        <span className="file-preview-tree__entry-chevron" aria-hidden="true">
          {isDirectory ? isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} /> : null}
        </span>
        {unsupported ? (
          <span className="file-preview-tree__entry-badge" data-testid={`${itemTestId}-unsupported`}>
            {notPreviewableLabel}
          </span>
        ) : null}
      </button>
      {isDirectory && isExpanded ? (
        <div>
          {(entry.children || []).map((child) => (
            <TreeEntry
              key={child.path}
              entry={child}
              depth={depth + 1}
              expanded={expanded}
              onToggle={onToggle}
              selectedPath={selectedPath}
              onSelectFile={onSelectFile}
              notPreviewableLabel={notPreviewableLabel}
              itemTestId={itemTestId}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

/**
 * 共享文件树：默认展开全部目录；加载/错误（可重试）/空态内聚在组件内。
 */
export function FilePreviewTree({
  nodes,
  status,
  selectedPath,
  onSelectFile,
  onRetry,
  labels,
  testId = 'file-preview-tree',
  ariaLabel,
}: FilePreviewTreeProps) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    if (status === 'success') setExpanded(findExpandedDirectories(nodes));
  }, [nodes, status]);

  const toggleFolder = (path: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const itemTestId = `${testId}-item`;
  return (
    <div className="file-preview-tree__root" data-testid={testId} aria-label={ariaLabel}>
      {status === 'loading' ? (
        <div className="file-preview-state" data-testid={`${testId}-state`} data-variant="loading">
          {labels.loading}
        </div>
      ) : null}
      {status === 'error' ? (
        <div
          className="file-preview-state file-preview-state--error"
          data-testid={`${testId}-state`}
          data-variant="error"
        >
          <p>{labels.error}</p>
          {onRetry ? (
            <button
              type="button"
              className="file-preview-tree__retry-btn"
              onClick={onRetry}
              data-testid={`${testId}-retry-btn`}
            >
              {labels.retry}
            </button>
          ) : null}
        </div>
      ) : null}
      {status === 'success' && nodes.length === 0 ? (
        <div className="file-preview-state" data-testid={`${testId}-state`} data-variant="empty">
          {labels.empty}
        </div>
      ) : null}
      {status === 'success' && nodes.length > 0
        ? nodes.map((entry) => (
            <TreeEntry
              key={entry.path}
              entry={entry}
              depth={0}
              expanded={expanded}
              onToggle={toggleFolder}
              selectedPath={selectedPath}
              onSelectFile={onSelectFile}
              notPreviewableLabel={labels.notPreviewable}
              itemTestId={itemTestId}
            />
          ))
        : null}
    </div>
  );
}
