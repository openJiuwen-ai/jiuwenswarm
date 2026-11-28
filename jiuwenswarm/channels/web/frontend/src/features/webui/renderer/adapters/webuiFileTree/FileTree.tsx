import { useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type CSSProperties } from 'react';
import { Tree, type TreeDataNode } from 'antd';
import { Folder, FolderOpen, LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { FilePreviewIcon } from '../../../../../components/ui/FilePreviewPanel/FilePreviewIcon';
import { isWebUIInteractionFailed, type WebUIInteractionResult } from '../../../contracts/interaction.ts';
import type { WebUIComponentState } from '../../componentState.ts';
import './FileTree.css';

const SPINNER_DELAY_MS = 150;

function normalizePathPrefix(path: string): string {
  return path.replace(/\\/g, '/').replace(/\/+$/, '');
}

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

/** Local presentation retained by the adapter across directory data frames. */
export interface FileTreeViewport {
  scrollTop: number;
  width: number;
}

export interface FileTreeProps {
  treeData: FileTreeNode[];
  selection: WebUIComponentState<FileTreeSelection>;
  viewport?: FileTreeViewport;
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
  onSelect: (key: string) => Promise<WebUIInteractionResult>;
  onExpand: (data: { key: string; expanded: boolean; expandedKeys: string[] }) => Promise<WebUIInteractionResult>;
}

interface DisplayNode extends Omit<TreeDataNode, 'key' | 'children'> {
  key: string;
  kind: 'file' | 'directory';
  children?: DisplayNode[];
}

export function FileTree({ treeData, selection, viewport, selectedKey, expandedKeys, status = 'ready', truncated, readFailed,
  disabled, ariaLabel, emptyText, message, className, style, onSelect, onExpand }: FileTreeProps) {
  const { t } = useTranslation();
  const localViewport = useRef<FileTreeViewport>({ scrollTop: 0, width: 240 });
  const view = viewport ?? localViewport.current;
  const containerRef = useRef<HTMLElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const drag = useRef<{ pointerId: number; x: number; width: number } | null>(null);
  const [width, setWidth] = useState(view.width);
  useLayoutEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = view.scrollTop;
  }, [view]);
  const value = useSyncExternalStore(selection.subscribe, selection.getSnapshot, selection.getSnapshot);
  const [visiblePending, setVisiblePending] = useState<ReadonlySet<string>>(new Set());
  const [error, setError] = useState<'error' | 'limit' | null>(null);
  const pendingExpandRef = useRef(new Set<string>());
  const pendingSelectKeyRef = useRef<string | null>(null);
  const selectSeqRef = useRef(0);
  const spinnerTimersRef = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const mounted = useRef(true);
  const lifetime = useRef(0);
  const reconciledTree = useRef<FileTreeNode[] | null>(null);
  useLayoutEffect(() => {
    mounted.current = true;
    lifetime.current += 1;
    const expandPending = pendingExpandRef.current;
    const timers = spinnerTimersRef.current;
    return () => {
      mounted.current = false;
      lifetime.current += 1;
      expandPending.clear();
      pendingSelectKeyRef.current = null;
      for (const timer of timers.values()) clearTimeout(timer);
      timers.clear();
    };
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
    const frameUpdated = reconciledTree.current !== null && reconciledTree.current !== treeData;
    const nextSelected = selectedKey === undefined ? visibleSelected : value.selectedKey;
    const nextExpanded = expandedKeys === undefined
      ? visibleExpanded.filter((key) => (
        reconciledTree.current === treeData
        || nodes.get(key)?.loaded === true
        || pendingExpandRef.current.has(key)
      ))
      : value.expandedKeys;
    reconciledTree.current = treeData;
    if (frameUpdated) setError(null);
    if (nextSelected !== value.selectedKey || nextExpanded.length !== value.expandedKeys.length
      || nextExpanded.some((key, index) => key !== value.expandedKeys[index])) {
      selection.set({ selectedKey: nextSelected, expandedKeys: nextExpanded });
    }
  }, [nodes, treeData, readFailed, selectedKey, expandedKeys, value, selection]);

  function startSpinner(key: string): void {
    const existing = spinnerTimersRef.current.get(key);
    if (existing !== undefined) clearTimeout(existing);
    const timer = setTimeout(() => {
      spinnerTimersRef.current.delete(key);
      if (!mounted.current) return;
      setVisiblePending((prev) => {
        if (prev.has(key)) return prev;
        const next = new Set(prev);
        next.add(key);
        return next;
      });
    }, SPINNER_DELAY_MS);
    spinnerTimersRef.current.set(key, timer);
  }

  function stopSpinner(key: string): void {
    const timer = spinnerTimersRef.current.get(key);
    if (timer !== undefined) {
      clearTimeout(timer);
      spinnerTimersRef.current.delete(key);
    }
    if (!mounted.current) return;
    setVisiblePending((prev) => {
      if (!prev.has(key)) return prev;
      const next = new Set(prev);
      next.delete(key);
      return next;
    });
  }

  async function request(
    kind: 'expand' | 'select',
    operation: () => Promise<WebUIInteractionResult>,
    key: string,
    onRejected: () => void,
  ): Promise<void> {
    if (kind === 'expand' && pendingExpandRef.current.has(key)) return;
    const requestLifetime = lifetime.current;
    let requestSeq = 0;
    if (kind === 'select') {
      if (pendingSelectKeyRef.current && pendingSelectKeyRef.current !== key) {
        stopSpinner(pendingSelectKeyRef.current);
      }
      selectSeqRef.current += 1;
      requestSeq = selectSeqRef.current;
      pendingSelectKeyRef.current = key;
    } else {
      pendingExpandRef.current.add(key);
    }
    const isCurrent = () => mounted.current && lifetime.current === requestLifetime
      && (kind !== 'select' || selectSeqRef.current === requestSeq);
    startSpinner(key);
    try {
      const result = await operation();
      if (isCurrent() && isWebUIInteractionFailed(result)) {
        setError('error');
        // A stale binding means a newer frame already replaced the tree; do not roll back newer state.
        if (result.status !== 'stale') {
          onRejected();
        }
      }
    } catch {
      if (isCurrent()) {
        setError('error');
        onRejected();
      }
    } finally {
      if (kind === 'expand') {
        pendingExpandRef.current.delete(key);
        stopSpinner(key);
      } else if (selectSeqRef.current === requestSeq) {
        pendingSelectKeyRef.current = null;
        stopSpinner(key);
      }
    }
  }

  function toggleDirectory(key: string, expanded: boolean): void {
    if (disabled || pendingExpandRef.current.has(key)) return;
    const item = nodes.get(key);
    if (item?.kind !== 'directory') return;
    const itemPrefix = `${normalizePathPrefix(item.path)}/`;
    const next = expanded ? [...new Set([...visibleExpanded, key])]
      : visibleExpanded.filter((candidate) => {
        if (candidate === key) return false;
        const candidatePath = nodes.get(candidate)?.path;
        return !candidatePath || !normalizePathPrefix(candidatePath).startsWith(itemPrefix);
      });
    if (next.length > 100) { setError('limit'); return; }
    const data = { key, expanded, expandedKeys: next };
    const needsRequest = expandedKeys !== undefined || (expanded && item.loaded !== true);
    if (needsRequest && new TextEncoder().encode(JSON.stringify(data)).length > 16 * 1024) {
      setError('limit'); return;
    }
    setError(null);
    const previous = selection.getSnapshot();
    const removedKeys = expanded ? [] : previous.expandedKeys.filter((candidate) => !next.includes(candidate));
    if (expandedKeys === undefined) selection.set({ ...previous, expandedKeys: next });
    if (needsRequest) void request('expand', () => onExpand(data), key, () => {
      if (expandedKeys !== undefined) return;
      const latest = selection.getSnapshot();
      const restored = expanded
        ? latest.expandedKeys.filter((candidate) => candidate !== key)
        : [...new Set([...latest.expandedKeys, ...removedKeys])];
      selection.set({ ...latest, expandedKeys: restored });
    });
  }

  const displayNodes = useMemo(() => {
    function prepare(items: FileTreeNode[]): DisplayNode[] {
      return items.map((item) => ({
        key: item.key, kind: item.kind,
        title: <span data-testid="webui-file-tree-item" data-variant={item.key} title={item.path}>{item.title}</span>,
        icon: visiblePending.has(item.key) ? <LoaderCircle size={14} className="animate-spin" />
          : item.kind === 'file' ? <FilePreviewIcon fileName={item.title} size={16} />
          : ({ expanded }: { expanded?: boolean }) => expanded ? <FolderOpen size={14} /> : <Folder size={14} />,
        isLeaf: item.kind === 'file' || (item.loaded === true && !item.children?.length && !item.truncated),
        children: item.children ? prepare(item.children) : undefined,
      }));
    }
    return prepare(treeData);
  }, [treeData, visiblePending]);

  const warning = error ? t(`webuiFiles.tree.${error}`) : readFailed ? t('webuiFiles.tree.readFailed')
    : truncated ? t('webuiFiles.tree.truncated') : message;

  function resize(next: number): void {
    const available = containerRef.current?.parentElement?.getBoundingClientRect().width ?? 0;
    if (available <= 0) return;
    const minimum = Math.min(140, available / 2);
    const maximum = Math.max(minimum, available - 160);
    view.width = Math.min(maximum, Math.max(minimum, next));
    setWidth(view.width);
  }

  return (
    <section ref={containerRef} className={`webui-file-tree${className ? ` ${className}` : ''}`}
      style={{ flexBasis: width, ...style }}
      data-testid="webui-file-tree" aria-label={ariaLabel ?? t('webuiFiles.treeLabel')}>
      <header className="webui-file-tree__header" data-testid="webui-file-tree-title">{t('webuiFiles.treeTitle')}</header>
      {warning ? <div className="webui-file-tree__notice" role="status"
        data-testid="webui-file-tree-message">{warning}</div> : null}
      <div ref={bodyRef} className="webui-file-tree__body" data-testid="webui-file-tree-body"
        onScroll={(event) => { view.scrollTop = event.currentTarget.scrollTop; }}>
        {status !== 'ready' ? <div className="webui-file-tree__notice" data-testid="webui-file-tree-state"
          data-variant={status}>{t(`webuiFiles.tree.${status}`)}</div> : displayNodes.length === 0 ? (
          <div className="webui-file-tree__notice" data-testid="webui-file-tree-state" data-variant="empty">
            {emptyText ?? t('webuiFiles.emptyTree')}
          </div>
        ) : (
          <Tree<DisplayNode> data-testid="webui-file-tree-control" blockNode showIcon
            disabled={disabled}
            treeData={displayNodes} selectedKeys={visibleSelected ? [visibleSelected] : []}
            expandedKeys={visibleExpanded} autoExpandParent={false}
            onSelect={(_, info) => {
              if (disabled) return;
              if (info.node.kind === 'directory') {
                toggleDirectory(info.node.key, !visibleExpanded.includes(info.node.key)); return;
              }
              if (new TextEncoder().encode(JSON.stringify({ key: info.node.key })).length > 16 * 1024) {
                setError('limit'); return;
              }
              setError(null);
              const previous = selection.getSnapshot();
              if (selectedKey === undefined) selection.set({ ...previous, selectedKey: info.node.key });
              void request('select', () => onSelect(info.node.key), info.node.key, () => {
                if (selectedKey === undefined) selection.set({ ...selection.getSnapshot(), selectedKey: previous.selectedKey });
              });
            }}
            onExpand={(_, info) => toggleDirectory(info.node.key, info.expanded)} />
        )}
      </div>
      <div className="webui-file-tree__resize" role="separator" tabIndex={0} aria-orientation="vertical"
        aria-label={t('webuiFiles.resizeTree')} aria-valuenow={Math.round(width)}
        data-testid="webui-file-tree-resize"
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          event.preventDefault();
          drag.current = {
            pointerId: event.pointerId, x: event.clientX,
            width: containerRef.current?.getBoundingClientRect().width ?? width,
          };
          event.currentTarget.setPointerCapture(event.pointerId);
          event.currentTarget.focus({ preventScroll: true });
        }}
        onPointerMove={(event) => {
          if (drag.current?.pointerId === event.pointerId) resize(drag.current.width + event.clientX - drag.current.x);
        }}
        onPointerUp={(event) => {
          if (drag.current?.pointerId !== event.pointerId) return;
          drag.current = null;
          event.currentTarget.releasePointerCapture(event.pointerId);
        }}
        onLostPointerCapture={() => { drag.current = null; }}
        onPointerCancel={() => { drag.current = null; }}
        onKeyDown={(event) => {
          if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
          event.preventDefault();
          resize((containerRef.current?.getBoundingClientRect().width ?? width)
            + (event.key === 'ArrowRight' ? 24 : -24));
        }} />
    </section>
  );
}
