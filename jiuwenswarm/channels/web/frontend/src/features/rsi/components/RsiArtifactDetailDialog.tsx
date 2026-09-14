import { useCallback, useEffect, useMemo, useState } from 'react';
import { ChevronRight, Copy, Download, File as FileIcon, Folder, LoaderCircle, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { FilePreview } from '../../../components/ArtifactsPanel/FilePreview';
import { previewKind } from '../../../components/ArtifactsPanel/filePreviewModel';
import { RsiLatexPreview } from './RsiLatexPreview';
import {
  artifactMimeType,
  isPreviewableEntry,
  loadRsiArtifactFiles,
  readRsiArtifactFile,
  type RsiArtifactFileEntry,
  type RsiArtifactSource,
} from '../rsiArtifactFiles';

interface RsiArtifactDetailDialogProps {
  source: RsiArtifactSource | null;
  title: string;
  onClose: () => void;
}

interface FileTreeNode {
  name: string;
  path: string;
  relativePath: string;
  isDirectory: boolean;
  children: FileTreeNode[];
}

function createFileTree(entries: RsiArtifactFileEntry[]): FileTreeNode {
  const root: FileTreeNode = { name: '', path: '', relativePath: '', isDirectory: true, children: [] };
  const directoryMap = new Map<string, FileTreeNode>([['', root]]);

  for (const entry of entries) {
    const relativePath = entry.relativePath ?? entry.path;
    const parts = relativePath.split('/').filter(Boolean);
    let currentPath = '';
    let currentNode = root;
    for (let index = 0; index < parts.length; index += 1) {
      const part = parts[index];
      currentPath = currentPath ? `${currentPath}/${part}` : part;
      const isLast = index === parts.length - 1;
      if (isLast && !entry.isDirectory) {
        currentNode.children.push({
          name: entry.name,
          path: entry.path,
          relativePath: currentPath,
          isDirectory: false,
          children: [],
        });
      } else {
        let nextNode = directoryMap.get(currentPath);
        if (!nextNode) {
          nextNode = { name: part, path: currentPath, relativePath: currentPath, isDirectory: true, children: [] };
          directoryMap.set(currentPath, nextNode);
          currentNode.children.push(nextNode);
        }
        currentNode = nextNode;
      }
    }
  }

  const sortNode = (node: FileTreeNode) => {
    node.children.sort((a, b) => {
      if (a.isDirectory !== b.isDirectory) return a.isDirectory ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    node.children.forEach(sortNode);
  };
  sortNode(root);
  return root;
}

function pruneEmptyDirectories(node: FileTreeNode): boolean {
  node.children = node.children.filter((child) => !child.isDirectory || pruneEmptyDirectories(child));
  return node.children.length > 0;
}

function firstFile(node: FileTreeNode): FileTreeNode | null {
  for (const child of node.children) {
    if (!child.isDirectory) return child;
    const nested = firstFile(child);
    if (nested) return nested;
  }
  return null;
}

function ancestorDirectories(path: string): string[] {
  const parts = path.split('/').filter(Boolean);
  const result: string[] = [];
  for (let index = 1; index < parts.length; index += 1) {
    result.push(parts.slice(0, index).join('/'));
  }
  return result;
}

function decodeBase64(value: string): ArrayBuffer {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}

export function RsiArtifactDetailDialog({ source, title, onClose }: RsiArtifactDetailDialogProps) {
  const { t } = useTranslation();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [entries, setEntries] = useState<RsiArtifactFileEntry[]>([]);
  const [selectedPath, setSelectedPath] = useState('');
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<{ path: string; url: string } | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [fileContent, setFileContent] = useState('');
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const sourceTaskId = source?.taskId ?? '';
  const sourcePath = source?.path ?? '';
  const sourceInitialPath = source?.initialFilePath ?? '';

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    setEntries([]);
    setSelectedPath('');
    setPreview(null);
    setPreviewLoading(false);
    setFileContent('');

    if (!source) {
      setError(t('rsi.artifact.empty'));
      setLoading(false);
      return () => {
        cancelled = true;
      };
    }

    void loadRsiArtifactFiles(source)
      .then((result) => {
        if (cancelled) return;
        const previewableEntries = result.entries.filter((entry) => entry.isDirectory || isPreviewableEntry(entry));
        setEntries(previewableEntries);
        const tree = createFileTree(previewableEntries);
        pruneEmptyDirectories(tree);
        const initialEntry = result.initialFilePath
          ? previewableEntries.find((entry) => entry.path === result.initialFilePath && isPreviewableEntry(entry))
          : null;
        const selected = initialEntry ?? firstFile(tree);
        if (selected) {
          setSelectedPath(selected.path);
          setExpandedFolders(new Set(ancestorDirectories(selected.relativePath ?? selected.path)));
        }
        setLoading(false);
      })
      .catch((loadError: unknown) => {
        if (cancelled) return;
        setError(loadError instanceof Error ? loadError.message : String(loadError));
        setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [sourceTaskId, sourcePath, sourceInitialPath, t]);

  const selectedEntry = useMemo(
    () => entries.find((entry) => entry.path === selectedPath && isPreviewableEntry(entry)) ?? null,
    [entries, selectedPath],
  );

  const fileTree = useMemo(() => {
    const tree = createFileTree(entries);
    pruneEmptyDirectories(tree);
    return tree;
  }, [entries]);

  useEffect(() => {
    if (!source || !selectedEntry) {
      setPreview(null);
      setPreviewLoading(false);
      setFileContent('');
      return;
    }

    let cancelled = false;
    let objectUrl = '';
    setPreview(null);
    setPreviewLoading(true);
    setFileContent('');

    void readRsiArtifactFile(source, selectedEntry.path)
      .then((file) => {
        if (cancelled) return;
        const blob = file.encoding === 'text'
          ? new Blob([file.content], { type: file.type })
          : new Blob([decodeBase64(file.content)], { type: file.type });
        objectUrl = URL.createObjectURL(blob);
        setPreview({ path: selectedEntry.path, url: objectUrl });
        setPreviewLoading(false);
        setFileContent(file.encoding === 'text' ? file.content : selectedEntry.path);
      })
      .catch(() => {
        if (!cancelled) {
          setPreview(null);
          setPreviewLoading(false);
          setFileContent(selectedEntry.path);
        }
      });

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [selectedEntry, sourceTaskId, sourcePath]);

  useEffect(() => {
    setCopyState('idle');
  }, [selectedPath]);

  const previewArtifact = useMemo(() => {
    if (!selectedEntry || !preview || preview.path !== selectedEntry.path) return null;
    return {
      id: selectedEntry.path,
      name: selectedEntry.name,
      mimeType: artifactMimeType(selectedEntry),
      downloadUrl: preview.url,
      size: selectedEntry.size,
    };
  }, [preview, selectedEntry]);

  const toggleFolder = useCallback((path: string) => {
    setExpandedFolders((previous) => {
      const next = new Set(previous);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  const handleCopy = useCallback(async () => {
    if (!selectedEntry) return;
    try {
      const kind = previewKind({ name: selectedEntry.name, mimeType: artifactMimeType(selectedEntry) });
      const text = kind === 'markdown' || kind === 'text' || kind === 'code' || kind === 'json' || kind === 'jsonl'
        ? fileContent
        : selectedEntry.path;
      await navigator.clipboard.writeText(text);
      setCopyState('copied');
    } catch {
      setCopyState('failed');
    }
  }, [fileContent, selectedEntry]);

  const handleDownload = useCallback(() => {
    if (!selectedEntry || !preview || preview.path !== selectedEntry.path) return;
    const anchor = document.createElement('a');
    anchor.href = preview.url;
    anchor.download = selectedEntry.name;
    anchor.click();
  }, [preview, selectedEntry]);

  const renderTreeNode = useCallback(
    (treeNode: FileTreeNode, depth: number): JSX.Element => {
      if (!treeNode.isDirectory) {
        const selected = treeNode.path === selectedPath;
        return (
          <button
            key={treeNode.path}
            type="button"
            className={`rsi-artifact-dialog__file${selected ? ' rsi-artifact-dialog__file--selected' : ''}`}
            style={{ paddingLeft: 12 + depth * 14 }}
            onClick={() => setSelectedPath(treeNode.path)}
          >
            <FileIcon size={12} />
            <span>{treeNode.name}</span>
          </button>
        );
      }

      const expanded = expandedFolders.has(treeNode.path);
      return (
        <div key={treeNode.path || 'root'}>
          {treeNode.path ? (
            <button
              type="button"
              className="rsi-artifact-dialog__folder"
              style={{ paddingLeft: 12 + depth * 14 }}
              onClick={() => toggleFolder(treeNode.path)}
              aria-expanded={expanded}
            >
              <ChevronRight size={12} className={expanded ? 'rsi-artifact-dialog__chevron--expanded' : ''} />
              <Folder size={12} />
              <span>{treeNode.name}</span>
            </button>
          ) : null}
          {(expanded || !treeNode.path) && (
            <div>{treeNode.children.map((child) => renderTreeNode(child, treeNode.path ? depth + 1 : depth))}</div>
          )}
        </div>
      );
    },
    [expandedFolders, selectedPath, toggleFolder],
  );

  const showEmpty = !loading && !error && (!source || fileTree.children.length === 0 || !selectedEntry);

  return (
    <div className="rsi-artifact-dialog" role="dialog" aria-modal="true" onClick={onClose}>
      <div className="rsi-artifact-dialog__panel" onClick={(event) => event.stopPropagation()}>
        <header className="rsi-artifact-dialog__header">
          <span className="rsi-artifact-dialog__title">{title}</span>
          <button type="button" className="rsi-artifact-dialog__close" onClick={onClose} aria-label="close">
            <X size={18} />
          </button>
        </header>
        <div className="rsi-artifact-dialog__body">
          <aside className="rsi-artifact-dialog__tree">
            {loading ? (
              <div className="rsi-artifact-dialog__loading">
                <LoaderCircle size={18} className="animate-spin" />
                <span>{t('common.loading')}</span>
              </div>
            ) : error ? (
              <div className="rsi-artifact-dialog__error">{error}</div>
            ) : fileTree.children.length === 0 ? (
              <div className="rsi-artifact-dialog__empty"></div>
            ) : (
              <div>{fileTree.children.map((child) => renderTreeNode(child, 0))}</div>
            )}
          </aside>
          <section className="rsi-artifact-dialog__preview">
            {loading ? (
              <div className="rsi-artifact-dialog__preview-empty">
                <LoaderCircle size={18} className="animate-spin" />
                <span>{t('common.loading')}</span>
              </div>
            ) : error ? (
              <div className="rsi-artifact-dialog__preview-empty">{t('rsi.artifact.loadFailed')}</div>
            ) : showEmpty ? (
              <div className="rsi-artifact-dialog__preview-empty">{t('rsi.artifact.empty')}</div>
            ) : selectedEntry && previewLoading ? (
              <div className="rsi-artifact-dialog__preview-empty">
                <LoaderCircle size={18} className="animate-spin" />
                <span>{t('common.loading')}</span>
              </div>
            ) : selectedEntry && previewArtifact ? (
              <>
                <div className="rsi-artifact-dialog__preview-header">
                  <span className="rsi-artifact-dialog__filename">{selectedEntry.name}</span>
                  <div className="rsi-artifact-dialog__actions">
                    <button type="button" className="rsi-artifact-dialog__action" onClick={handleCopy}>
                      <Copy size={16} />
                    </button>
                    <button type="button" className="rsi-artifact-dialog__action" onClick={handleDownload}>
                      <Download size={16} />
                    </button>
                  </div>
                </div>
                <div className="rsi-artifact-dialog__divider" />
                <div className="rsi-artifact-dialog__preview-content">
                  {/\.(?:tex|bib|sty)$/i.test(selectedEntry.name) && previewArtifact.downloadUrl ? (
                    <RsiLatexPreview url={previewArtifact.downloadUrl} />
                  ) : (
                    <FilePreview artifact={previewArtifact} />
                  )}
                </div>
                {copyState !== 'idle' && (
                  <div className="rsi-artifact-dialog__copy-state">
                    {copyState === 'copied' ? t('rsi.artifact.copied') : t('rsi.artifact.copyFailed')}
                  </div>
                )}
              </>
            ) : (
              <div className="rsi-artifact-dialog__preview-empty">{t('rsi.artifact.selectFile')}</div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
