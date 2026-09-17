/**
 * HarnessExtensionTree Component
 *
 * Displays a file tree for runtime extension files in auto_harness mode.
 * Uses runtime_path from extension_ready event to fetch file listing.
 * Supports hierarchical directory structure with expand/collapse.
 * Uses global cache to avoid repeated API calls.
 */

import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useChatStore, useHarnessStore, CachedFileTreeEntry } from '../../stores';
import { webRequest } from '../../services/webClient';
import { resolveHarnessError } from '../../utils';
import { ReadOnlyFileModal } from './ReadOnlyFileModal';

interface FileInfo {
  name: string;
  path: string;
  is_dir: boolean;
  children?: FileInfo[];
}

interface HarnessExtensionTreeProps {
  /** Runtime path to load files from (overrides store) */
  runtimePath?: string;
  /** Extension name to display (overrides store) */
  extensionName?: string;
  /** Whether to show export button (default: true for ToolPanel, false for HarnessPackagePanel) */
  showExport?: boolean;
  /** Force refresh (bypass cache) */
  forceRefresh?: boolean;
}

const compareByName = (a: string, b: string) => a.localeCompare(b, 'zh-Hans-CN');

function isPreviewableFile(fileName: string): boolean {
  const lowerName = fileName.toLowerCase();
  return (
    lowerName.endsWith('.md') ||
    lowerName.endsWith('.mdx') ||
    lowerName.endsWith('.json') ||
    lowerName.endsWith('.yaml') ||
    lowerName.endsWith('.yml') ||
    lowerName.endsWith('.py') ||
    lowerName.endsWith('.txt')
  );
}

// Convert cached entries to FileInfo format
function cachedToFileInfo(cached: CachedFileTreeEntry[]): FileInfo[] {
  return cached.map(entry => ({
    name: entry.name,
    path: entry.path,
    is_dir: entry.is_dir,
    children: entry.children ? cachedToFileInfo(entry.children) : undefined,
  }));
}

// Convert FileInfo to cached format
function fileInfoToCached(files: FileInfo[]): CachedFileTreeEntry[] {
  return files.map(file => ({
    name: file.name,
    path: file.path,
    is_dir: file.is_dir,
    children: file.children ? fileInfoToCached(file.children) : undefined,
  }));
}

export function HarnessExtensionTree(props?: HarnessExtensionTreeProps) {
  const { t } = useTranslation();
  const {
    packages,
    getFileTreeCache,
    setFileTreeCache,
    clearFileTreeCache,
    setFileTreeLoading,
    isFileTreeLoading,
  } = useHarnessStore();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const extensionReady = useHarnessStore((s) => s.runtimes[activeSessionId ?? '']?.extensionReady ?? null);
  const [files, setFiles] = useState<FileInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(new Set());
  const [selectedFile, setSelectedFile] = useState<FileInfo | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  // Use props if provided, otherwise fall back to store
  const runtimePath = props?.runtimePath || extensionReady?.runtimePath || '';
  const extensionName = props?.extensionName || extensionReady?.extensionName || '';
  const isSessionRuntimeRoot = !props?.runtimePath && Boolean(extensionReady?.sessionRuntimePath);
  const displayName = isSessionRuntimeRoot ? 'Runtime Extensions' : extensionName;
  const showExport = props?.showExport !== false; // Default to true, only false if explicitly set

  // Find package_id by matching extension_name
  const currentPackage = packages.find(p => p.extension_name === extensionName);
  const packageId = currentPackage?.id || null;
  const canExport = !isSessionRuntimeRoot && packageId && packageId !== 'native';

  // Recursively fetch directory contents (parallel fetch for subdirectories)
  const fetchDirectoryContents = async (dirPath: string): Promise<FileInfo[]> => {
    const encodedPath = encodeURIComponent(dirPath);
    const url = `/file-api/list-files?dir=${encodedPath}`;
    const response = await fetch(url, { cache: 'no-store' });

    if (!response.ok) {
      const errorData = await response.text();
      throw new Error(`HTTP ${response.status}: ${errorData.substring(0, 100)}`);
    }

    const data = await response.json();
    const entries: FileInfo[] = Array.isArray(data.files)
      ? data.files
          .filter((f: { name: string }) => f.name !== '__pycache__')
          .map((f: { name: string; path: string; isDirectory?: boolean }) => ({
            name: f.name,
            path: f.path,
            is_dir: Boolean(f.isDirectory),
          }))
      : [];

    // Sort: directories first, then files, alphabetically
    entries.sort((a, b) => {
      if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
      return compareByName(a.name, b.name);
    });

    // Parallel fetch contents for all subdirectories using Promise.all
    const subdirEntries = entries.filter(e => e.is_dir);
    const subdirFetches = subdirEntries.map(entry =>
      fetchDirectoryContents(entry.path)
        .then(children => { entry.children = children; return entry; })
        .catch(err => {
          console.error(`Failed to load subdirectory ${entry.path}:`, err);
          entry.children = [];
          return entry;
        })
    );
    await Promise.all(subdirFetches);

    return entries;
  };

  const loadFiles = async (forceRefresh = false) => {
    if (!runtimePath) {
      setFiles([]);
      setLoading(false);
      return;
    }

    // Check cache first (unless force refresh)
    if (!forceRefresh && !props?.forceRefresh) {
      const cached = getFileTreeCache(runtimePath);
      if (cached && cached.length > 0) {
        setFiles(cachedToFileInfo(cached));
        // Auto-expand root level directories from cache
        const rootDirPaths = cached.filter(f => f.is_dir).map(f => f.path);
        setExpandedPaths(new Set(rootDirPaths));
        return;
      }
    }

    // Check if already loading this path (prevent duplicate requests)
    if (isFileTreeLoading(runtimePath)) {
      return;
    }

    setLoading(true);
    setLoadError(null);
    setFileTreeLoading(runtimePath, true);

    try {
      const fileList = await fetchDirectoryContents(runtimePath);
      setFiles(fileList);
      // Save to cache
      setFileTreeCache(runtimePath, fileInfoToCached(fileList));
      // Auto-expand root level directories
      const rootDirPaths = fileList.filter(f => f.is_dir).map(f => f.path);
      setExpandedPaths(new Set(rootDirPaths));
    } catch (err) {
      console.error('Failed to load extension files:', err);
      setLoadError(err instanceof Error ? err.message : t('toolPanel.loadFailed'));
      setFiles([]);
    } finally {
      setLoading(false);
      setFileTreeLoading(runtimePath, false);
    }
  };

  const handleRefresh = () => {
    clearFileTreeCache(runtimePath);
    loadFiles(true);
  };

  useEffect(() => {
    loadFiles();
  }, [runtimePath]);

  const toggleDirectory = (path: string) => {
    setExpandedPaths((prev) => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  };

  const handleFileClick = (file: FileInfo) => {
    if (!isPreviewableFile(file.name)) return;
    setSelectedFile(file);
    setModalOpen(true);
  };

  const handleModalClose = () => {
    setModalOpen(false);
  };

  const handleExport = async () => {
    if (!packageId) return;

    setExporting(true);
    setExportError(null);

    try {
      // Send via WebSocket - now returns download URL instead of base64 content
      const result = await webRequest<{
        download_url?: string;  // new format - HTTP download URL
        file_content?: string;  // legacy format - base64 encoded
        filename: string;
      }>('harness.export', {
        package_id: packageId,
      });

      if (result.download_url) {
        // New format: direct HTTP download (avoids WebSocket size limits)
        const a = document.createElement('a');
        a.href = result.download_url;
        a.download = result.filename || `${extensionName || 'package'}.zip`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      } else if (result.file_content) {
        // Legacy format: decode base64 and download (for backwards compatibility)
        const binaryString = atob(result.file_content);
        const bytes = new Uint8Array(binaryString.length);
        for (let i = 0; i < binaryString.length; i++) {
          bytes[i] = binaryString.charCodeAt(i);
        }
        const blob = new Blob([bytes], { type: 'application/zip' });
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = result.filename || `${extensionName || 'package'}.zip`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        window.URL.revokeObjectURL(url);
      }
    } catch (err) {
      console.error('Export failed:', err);
      setExportError(resolveHarnessError(err, 'harnessPackage.exportError'));
    } finally {
      setExporting(false);
    }
  };

  if (!runtimePath) {
    return (
      <div data-testid="tool-panel-harness-tree-empty" className="h-full flex items-center justify-center text-text-muted text-sm">
        {t('toolPanel.noExtension')}
      </div>
    );
  }

  if (loading) {
    return (
      <div data-testid="tool-panel-harness-tree-loading" className="h-full flex items-center justify-center">
        <div className="w-8 h-8 rounded-full border-4 border-border border-t-accent animate-spin" />
      </div>
    );
  }

  // Render a directory item and its children recursively
  const renderDirectory = (entry: FileInfo, depth: number): JSX.Element => {
    const isExpanded = expandedPaths.has(entry.path);
    const hasChildren = entry.children && entry.children.length > 0;

    return (
      <div key={entry.path}>
        <button
          type="button"
          data-testid="tool-panel-harness-tree-dir"
          data-variant={entry.path}
          onClick={() => toggleDirectory(entry.path)}
          className="w-full min-h-9 flex items-center gap-2 rounded-lg px-2 py-1.5 text-left text-[13px] text-text-muted hover:bg-secondary/40 hover:text-text "
          style={{ paddingLeft: `${depth * 12 + 6}px` }}
          title={entry.name}
        >
          <span className="w-4 h-4 flex items-center justify-center text-text-muted/80">
            {hasChildren ? (
              <svg
                className={`w-3 h-3  ${isExpanded ? 'rotate-90' : ''}`}
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 6l6 6-6 6" />
              </svg>
            ) : (
              <span className="w-3 h-3" />
            )}
          </span>
          <svg className="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
            <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h4.5l1.5 2.25h10.5v8.25A2.25 2.25 0 0118 19.5H6A2.25 2.25 0 013.75 17.25V6.75z" />
          </svg>
          <span className="flex-1 min-w-0 truncate">{entry.name}</span>
        </button>

        {isExpanded && entry.children && (
          <div>
            {entry.children.map((child) => {
              if (child.is_dir) {
                return renderDirectory(child, depth + 1);
              }
              return renderFile(child, depth + 1);
            })}
          </div>
        )}
      </div>
    );
  };

  // Render a file item
  const renderFile = (file: FileInfo, depth: number): JSX.Element => {
    const previewable = isPreviewableFile(file.name);
    const selected = selectedFile?.path === file.path;

    return (
      <button
        key={file.path}
        type="button"
        data-testid="tool-panel-harness-tree-file"
        data-variant={file.path}
        className={`w-full min-h-9 flex items-center gap-2 rounded-lg px-2 py-1.5 text-left text-[13px]  ${
          selected
            ? 'bg-accent-subtle text-text border border-[var(--color-border-accent)]'
            : previewable
              ? 'text-text-muted hover:bg-secondary/40 hover:text-text border border-transparent'
              : 'text-text-muted/60 border border-transparent cursor-not-allowed'
        }`}
        style={{ paddingLeft: `${depth * 12 + 6}px` }}
        onClick={() => handleFileClick(file)}
        disabled={!previewable}
        title={file.name}
      >
        <span className="w-4 h-4 flex items-center justify-center" />
        <svg className="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
          <path strokeLinecap="round" strokeLinejoin="round" d="M6.75 3.75h7.5l4.5 4.5v12a1.5 1.5 0 01-1.5 1.5h-10.5a1.5 1.5 0 01-1.5-1.5v-15a1.5 1.5 0 011.5-1.5zM14.25 3.75v4.5h4.5" />
        </svg>
        <span className="flex-1 min-w-0 truncate">{file.name}</span>
      </button>
    );
  };

  return (
    <div data-testid="tool-panel-harness-tree" className="h-full flex flex-col overflow-hidden">
      <div data-testid="tool-panel-harness-tree-header" className="flex-shrink-0 px-3 py-2 border-b border-border bg-secondary/30">
        <div className="flex items-center justify-between gap-3">
          <h3 data-testid="tool-panel-harness-tree-title" className="text-sm font-medium text-text truncate min-w-0">{displayName}</h3>
          <div className="flex items-center gap-1 flex-shrink-0">
            {exportError && (
              <span data-testid="tool-panel-harness-tree-export-error" className="text-xs text-danger max-w-[100px] truncate">{exportError}</span>
            )}
            {showExport && canExport && (
              <button
                type="button"
                data-testid="tool-panel-harness-tree-export"
                onClick={handleExport}
                disabled={exporting}
                className="px-2 py-1 rounded-md border border-border bg-secondary/50 text-text-muted hover:text-text hover:bg-secondary  text-xs disabled:opacity-50"
                title={t('harnessPackage.export')}
              >
                {exporting ? t('harnessPackage.exporting') : t('harnessPackage.export')}
              </button>
            )}
            <button
              type="button"
              data-testid="tool-panel-harness-tree-refresh"
              onClick={handleRefresh}
              disabled={loading}
              className="px-2 py-1 rounded-md border border-border bg-secondary/50 text-text-muted hover:text-text hover:bg-secondary  text-xs disabled:opacity-50"
              title={t('toolPanel.refreshFiles')}
            >
              {loading ? t('common.refreshing') : t('common.refresh')}
            </button>
          </div>
        </div>
        <p data-testid="tool-panel-harness-tree-path" className="text-xs text-text-muted mono truncate mt-1" title={runtimePath}>
          {runtimePath}
        </p>
      </div>

      {loadError ? (
        <div data-testid="tool-panel-harness-tree-error" className="flex-1 flex items-center justify-center text-danger text-sm px-4">
          {loadError}
        </div>
      ) : (
        <div data-testid="tool-panel-harness-tree-list" className="flex-1 overflow-auto p-2">
          {files.length === 0 ? (
            <div data-testid="tool-panel-harness-tree-empty" className="h-full flex items-center justify-center text-text-muted text-sm">
              {t('toolPanel.noExtension')}
            </div>
          ) : (
            <div className="space-y-0.5">
              {files.map((entry) => {
                if (entry.is_dir) {
                  return renderDirectory(entry, 0);
                }
                return renderFile(entry, 0);
              })}
            </div>
          )}
        </div>
      )}

      <ReadOnlyFileModal
        open={modalOpen}
        filePath={selectedFile?.path || ''}
        fileName={selectedFile?.name || ''}
        onClose={handleModalClose}
      />
    </div>
  );
}
