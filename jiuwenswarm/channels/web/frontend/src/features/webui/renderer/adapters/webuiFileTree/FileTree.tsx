import { useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type CSSProperties } from 'react';
import { Tree, type TreeDataNode } from 'antd';
import { Folder, FolderOpen, LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { FilePreviewIcon } from '../../../../../components/ui/FilePreviewPanel/FilePreviewIcon';
import type { WebUIComponentState } from '../../componentState.ts';
import './FileTree.css';

export interface FileTreeNode {
  key: string;
  title: string;
  path: string;
  kind: 'file' | 'directory';
  children?: FileTreeNode[];
  loaded?: boolean;
  truncated?: boolean;
}

export interface FileTreeSelection {
  selectedKey: string | null;
  expandedKeys: string[];
}

interface FileTreeEventResult {
  status: string;
  receipt?: { accepted: boolean } | null;
}

export interface FileTreeProps {
  treeData: FileTreeNode[];
  selection: WebUIComponentState<FileTreeSelection>;
  selectedKey?: string | null;
  expandedKeys?: string[];
  status?: 'ready' | 'unavailable' | 'unsupported' | 'error';
  truncated?: boolean;
  readFailed?: boolean;
  disabled?: boolean;
  ariaLabel?: string;
  emptyText?: string;
  message?: string;
  className?: string;
  style?: CSSProperties;
  onSelect: (key: string) => Promise<FileTreeEventResult>;
  onExpand: (data: { key: string; expanded: boolean; expandedKeys: string[] }) => Promise<FileTreeEventResult>;
}

interface DisplayNode extends Omit<TreeDataNode, 'key' | 'children'> {
  key: string;
  kind: 'file' | 'directory';
  children?: DisplayNode[];
}

export function FileTree({ treeData, selection, selectedKey, expandedKeys, status = 'ready', truncated, readFailed,
  disabled, ariaLabel, emptyText, message, className, style, onSelect, onExpand }: FileTreeProps) {
  const { t } = useTranslation();
  const value = useSyncExternalStore(selection.subscribe, selection.getSnapshot, selection.getSnapshot);
  const [pending, setPending] = useState<ReadonlySet<string>>(new Set());
  const [error, setError] = useState<'error' | 'limit' | null>(null);
  const pendingRef = useRef(new Set<string>());
  const mounted = useRef(true);
  const lifetime = useRef(0);
  const reconciledTree = useRef<FileTreeNode[] | null>(null);
  useLayoutEffect(() => {
    mounted.current = true;
    lifetime.current += 1;
    return () => { mounted.current = false; lifetime.current += 1; pendingRef.current.clear(); };
  }, []);
  const nodes = useMemo(() => {
    const result = new Map<string, FileTreeNode>();
    function walk(items: FileTreeNode[]): void {
      for (const item of items) { result.set(item.key, item); walk(item.children ?? []); }
    }
    walk(treeData);
    return result;
  }, [treeData]);
  const currentSelected = selectedKey !== undefined ? selectedKey : value.selectedKey;
  const currentExpanded = expandedKeys ?? value.expandedKeys;
  const visibleSelected = currentSelected && nodes.get(currentSelected)?.kind === 'file' ? currentSelected : null;
  const visibleExpanded = currentExpanded.filter((key) => nodes.get(key)?.kind === 'directory');

  useLayoutEffect(() => {
    const nextSelected = selectedKey === undefined ? visibleSelected : value.selectedKey;
    const nextExpanded = expandedKeys === undefined
      ? visibleExpanded.filter((key) => reconciledTree.current === treeData || nodes.get(key)?.loaded === true)
      : value.expandedKeys;
    reconciledTree.current = treeData;
    if (nextSelected !== value.selectedKey || nextExpanded.length !== value.expandedKeys.length
      || nextExpanded.some((key, index) => key !== value.expandedKeys[index])) {
      selection.set({ selectedKey: nextSelected, expandedKeys: nextExpanded });
    }
  }, [nodes, treeData, readFailed, selectedKey, expandedKeys, value, selection]);

  async function request(operation: () => Promise<FileTreeEventResult>, key: string,
    onRejected: () => void): Promise<void> {
    if (pendingRef.current.size > 0) return;
    const requestLifetime = lifetime.current;
    const isCurrent = () => mounted.current && lifetime.current === requestLifetime;
    pendingRef.current.add(key);
    setPending(new Set(pendingRef.current));
    try {
      const result = await operation();
      if (isCurrent() && (result.status !== 'sent' || !result.receipt?.accepted)) {
        setError('error');
        // A stale binding can mean the successful data frame arrived before its receipt.
        if ((result.status !== 'sent' && result.status !== 'stale') || result.receipt?.accepted === false) {
          onRejected();
        }
      }
    } catch {
      if (isCurrent()) setError('error');
    } finally {
      if (isCurrent()) {
        pendingRef.current.delete(key);
        setPending(new Set(pendingRef.current));
      }
    }
  }

  function toggleDirectory(key: string, expanded: boolean): void {
    if (disabled || pendingRef.current.size > 0) return;
    const item = nodes.get(key);
    if (item?.kind !== 'directory') return;
    const next = expanded ? [...new Set([...visibleExpanded, key])]
      : visibleExpanded.filter((candidate) => candidate !== key
        && !nodes.get(candidate)?.path.startsWith(`${item.path}/`));
    if (next.length > 100) { setError('limit'); return; }
    const data = { key, expanded, expandedKeys: next };
    const needsRequest = expandedKeys !== undefined || (expanded && item.loaded !== true);
    if (needsRequest && new TextEncoder().encode(JSON.stringify(data)).length > 16 * 1024) {
      setError('limit'); return;
    }
    setError(null);
    const previous = selection.getSnapshot();
    if (expandedKeys === undefined) selection.set({ ...previous, expandedKeys: next });
    if (needsRequest) void request(() => onExpand(data), key, () => {
      if (expandedKeys === undefined) selection.set({ ...selection.getSnapshot(), expandedKeys: previous.expandedKeys });
    });
  }

  const displayNodes = useMemo(() => {
    function prepare(items: FileTreeNode[]): DisplayNode[] {
      return items.map((item) => ({
        key: item.key, kind: item.kind,
        title: <span data-testid="webui-file-tree-item" data-variant={item.key} title={item.path}>{item.title}</span>,
        icon: pending.has(item.key) ? <LoaderCircle size={14} className="animate-spin" />
          : item.kind === 'file' ? <FilePreviewIcon fileName={item.title} size={16} />
          : ({ expanded }: { expanded?: boolean }) => expanded ? <FolderOpen size={14} /> : <Folder size={14} />,
        isLeaf: item.kind === 'file' || (item.loaded === true && !item.children?.length && !item.truncated),
        children: item.children ? prepare(item.children) : undefined,
      }));
    }
    return prepare(treeData);
  }, [treeData, pending]);

  const warning = error ? t(`webuiFiles.tree.${error}`) : readFailed ? t('webuiFiles.tree.readFailed')
    : truncated ? t('webuiFiles.tree.truncated') : message;
  return (
    <section className={`webui-file-tree${className ? ` ${className}` : ''}`} style={style}
      data-testid="webui-file-tree" aria-label={ariaLabel ?? t('webuiFiles.treeLabel')}>
      <header className="webui-file-tree__header" data-testid="webui-file-tree-title">{t('webuiFiles.treeTitle')}</header>
      {warning ? <div className="webui-file-tree__notice" role="status"
        data-testid="webui-file-tree-message">{warning}</div> : null}
      <div className="webui-file-tree__body" data-testid="webui-file-tree-body">
        {status !== 'ready' ? <div className="webui-file-tree__notice" data-testid="webui-file-tree-state"
          data-variant={status}>{t(`webuiFiles.tree.${status}`)}</div> : displayNodes.length === 0 ? (
          <div className="webui-file-tree__notice" data-testid="webui-file-tree-state" data-variant="empty">
            {emptyText ?? t('webuiFiles.emptyTree')}
          </div>
        ) : (
          <Tree<DisplayNode> data-testid="webui-file-tree-control" blockNode showIcon
            disabled={disabled || pending.size > 0}
            treeData={displayNodes} selectedKeys={visibleSelected ? [visibleSelected] : []}
            expandedKeys={visibleExpanded} autoExpandParent={false}
            onSelect={(_, info) => {
              if (disabled || pendingRef.current.size > 0) return;
              if (info.node.kind === 'directory') {
                toggleDirectory(info.node.key, !visibleExpanded.includes(info.node.key)); return;
              }
              if (new TextEncoder().encode(JSON.stringify({ key: info.node.key })).length > 16 * 1024) {
                setError('limit'); return;
              }
              setError(null);
              const previous = selection.getSnapshot();
              if (selectedKey === undefined) selection.set({ ...previous, selectedKey: info.node.key });
              void request(() => onSelect(info.node.key), info.node.key, () => {
                if (selectedKey === undefined) selection.set({ ...selection.getSnapshot(), selectedKey: previous.selectedKey });
              });
            }}
            onExpand={(_, info) => toggleDirectory(info.node.key, info.expanded)} />
        )}
      </div>
    </section>
  );
}
