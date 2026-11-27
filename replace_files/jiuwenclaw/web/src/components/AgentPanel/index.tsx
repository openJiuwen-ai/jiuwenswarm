import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FileViewer } from './FileViewer';
import { containsIgnoredDirectory } from '../../features/fileTreeFilters';

interface AgentPanelProps {
  sessionId: string;
}

interface FileInfo {
  name: string;
  path: string;
  isMarkdown: boolean;
}

const ROOT_FOLDER_KEY = '__root__';
const AGENT_ROOT_PREFIX = 'agent/';

interface DirectoryNode {
  key: string;
  label: string;
  isSkillFolder: boolean;
  childDirectoryKeys: string[];
  files: FileInfo[];
}

interface TreeData {
  directoryMap: Map<string, DirectoryNode>;
  rootChildDirectoryKeys: string[];
  rootFiles: FileInfo[];
  rootEntries: Array<
    | { kind: 'directory'; key: string; label: string }
    | { kind: 'file'; file: FileInfo }
  >;
  directoryCount: number;
  totalFileCount: number;
  previewableFileCount: number;
}

const compareByName = (a: string, b: string) => a.localeCompare(b, 'zh-Hans-CN');

const getFolderDisplayName = (folderKey: string, t: (key: string) => string) => {
  if (folderKey === ROOT_FOLDER_KEY) return t('agent.root');
  const segments = folderKey.split('/').filter(Boolean);
  return segments[segments.length - 1] || folderKey;
};

const isSkillFolder = (folderKey: string) =>
  folderKey === 'skills' ||
  folderKey.startsWith('skills/') ||
  folderKey === 'jiuwenclaw_workspace/skills' ||
  folderKey.startsWith('jiuwenclaw_workspace/skills/');

const normalizeFolderKey = (folderKey: string) => (folderKey ? folderKey : ROOT_FOLDER_KEY);

const isPreviewableFile = (fileName: string) => {
  const lowerName = fileName.toLowerCase();
  return lowerName.endsWith('.md') || lowerName.endsWith('.mdx') || lowerName.endsWith('.json');
};

const getParentFolderKey = (folderKey: string) => {
  if (folderKey === ROOT_FOLDER_KEY) return null;
  const separatorIndex = folderKey.lastIndexOf('/');
  if (separatorIndex === -1) return ROOT_FOLDER_KEY;
  return folderKey.substring(0, separatorIndex);
};

const getFolderKeyByFilePath = (filePath: string) => {
  if (!filePath.startsWith(AGENT_ROOT_PREFIX)) {
    return ROOT_FOLDER_KEY;
  }
  const relativePath = filePath.slice(AGENT_ROOT_PREFIX.length);
  const separatorIndex = relativePath.lastIndexOf('/');
  if (separatorIndex === -1) {
    return ROOT_FOLDER_KEY;
  }
  return relativePath.substring(0, separatorIndex) || ROOT_FOLDER_KEY;
};

const filterFolderData = (data: Record<string, FileInfo[]>) =>
  Object.fromEntries(
    Object.entries(data)
      .filter(([rawFolderKey]) => !containsIgnoredDirectory(rawFolderKey))
      .map(([rawFolderKey, files]) => [
        rawFolderKey,
        files.filter((file) => !containsIgnoredDirectory(file.path)),
      ]),
  );

const buildTreeData = (data: Record<string, FileInfo[]>, t: (key: string) => string): TreeData => {
  const directoryKeys = new Set<string>([ROOT_FOLDER_KEY]);
  const sourceFolderKeys = Object.keys(data);

  sourceFolderKeys.forEach((rawFolderKey) => {
    const folderKey = normalizeFolderKey(rawFolderKey);
    if (folderKey === ROOT_FOLDER_KEY) {
      directoryKeys.add(ROOT_FOLDER_KEY);
      return;
    }

    const parts = folderKey.split('/').filter(Boolean);
    let current = '';
    parts.forEach((part) => {
      current = current ? `${current}/${part}` : part;
      directoryKeys.add(current);
    });
  });

  const directoryMap = new Map<string, DirectoryNode>();
  directoryKeys.forEach((folderKey) => {
    directoryMap.set(folderKey, {
      key: folderKey,
      label: getFolderDisplayName(folderKey, t),
      isSkillFolder: isSkillFolder(folderKey),
      childDirectoryKeys: [],
      files: [],
    });
  });

  directoryKeys.forEach((folderKey) => {
    if (folderKey === ROOT_FOLDER_KEY) return;
    const parentKey = getParentFolderKey(folderKey);
    if (!parentKey) return;
    const parent = directoryMap.get(parentKey);
    if (parent) {
      parent.childDirectoryKeys.push(folderKey);
    }
  });

  sourceFolderKeys.forEach((rawFolderKey) => {
    const folderKey = normalizeFolderKey(rawFolderKey);
    const node = directoryMap.get(folderKey);
    if (!node) return;
    node.files = (data[rawFolderKey] || [])
      .slice()
      .sort((a, b) => compareByName(a.name, b.name));
  });

  directoryMap.forEach((node) => {
    node.childDirectoryKeys.sort((a, b) => compareByName(getFolderDisplayName(a, t), getFolderDisplayName(b, t)));
  });

  const rootNode = directoryMap.get(ROOT_FOLDER_KEY);
  const rootChildDirectoryKeys = rootNode ? rootNode.childDirectoryKeys : [];
  const rootFiles = rootNode ? rootNode.files : [];
  const rootEntries = [
    ...rootChildDirectoryKeys.map((key) => ({
      kind: 'directory' as const,
      key,
      label: getFolderDisplayName(key, t),
    })),
    ...rootFiles.map((file) => ({
      kind: 'file' as const,
      file,
    })),
  ].sort((a, b) => {
    const aName = a.kind === 'directory' ? a.label : a.file.name;
    const bName = b.kind === 'directory' ? b.label : b.file.name;
    return compareByName(aName, bName);
  });
  const allFiles = sourceFolderKeys.flatMap((folderKey) => data[folderKey] || []);

  return {
    directoryMap,
    rootChildDirectoryKeys,
    rootFiles,
    rootEntries,
    directoryCount: Math.max(directoryMap.size - 1, 0),
    totalFileCount: allFiles.length,
    previewableFileCount: allFiles.filter((file) => isPreviewableFile(file.name)).length,
  };
};

export function AgentPanel({ sessionId: _sessionId }: AgentPanelProps) {
  const { t } = useTranslation();
  const [folderData, setFolderData] = useState<Record<string, FileInfo[]>>({});
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<FileInfo | null>(null);
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(
    () => new Set<string>([ROOT_FOLDER_KEY]),
  );

  const loadFolderData = async (options?: { rebuildBeforeFetch?: boolean }) => {
    const shouldRebuild = Boolean(options?.rebuildBeforeFetch);
    try {
      if (shouldRebuild) {
        const rebuildResponse = await fetch('/file-api/rebuild-agent-data', {
          method: 'POST',
        });
        if (!rebuildResponse.ok) {
          const rebuildError = await rebuildResponse.text();
          throw new Error(`REBUILD_FAILED:${rebuildError.substring(0, 160)}`);
        }
      }

      const res = await fetch('/file-api/file-content?path=agent/jiuwenclaw_workspace/agent-data.json');
      if (!res.ok) {
        throw new Error('FETCH_FAILED');
      }
      const rawData = await res.text();
      const parsedData = JSON.parse(rawData) as Record<string, FileInfo[]>;
      const data = filterFolderData(parsedData);
      setFolderData(data);
      setLoadError(null);

      const nextTreeData = buildTreeData(data, t);
      setSelectedFile((prev) => {
        if (!prev) {
          return null;
        }
        const stillExists = Object.values(data).some((files) =>
          files.some((file) => file.path === prev.path),
        );
        return stillExists ? prev : null;
      });
      setExpandedKeys((prev) => {
        const next = new Set<string>([ROOT_FOLDER_KEY]);
        prev.forEach((key) => {
          if (nextTreeData.directoryMap.has(key)) {
            next.add(key);
          }
        });
        return next;
      });
    } catch (err) {
      console.error('Failed to load agent files:', err);
      setFolderData({});
      if (err instanceof Error && err.message.startsWith('REBUILD_FAILED:')) {
        setLoadError(t('agent.errors.refreshFailed'));
      } else {
        setLoadError(t('agent.errors.loadFailed'));
      }
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => {
    void loadFolderData();
  }, []);

  const handleRefresh = () => {
    setRefreshing(true);
    void loadFolderData({ rebuildBeforeFetch: true });
  };

  const treeData = useMemo(() => buildTreeData(folderData, t), [folderData, t]);

  const expandFolderAndAncestors = (folderKey: string) => {
    setExpandedKeys((prev) => {
      const next = new Set(prev);
      let current: string | null = folderKey;
      while (current) {
        next.add(current);
        current = getParentFolderKey(current);
      }
      return next;
    });
  };

  const toggleFolder = (folderKey: string) => {
    setExpandedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(folderKey)) {
        next.delete(folderKey);
      } else {
        next.add(folderKey);
      }
      return next;
    });
  };

  const handleFileClick = (file: FileInfo) => {
    if (!isPreviewableFile(file.name)) {
      return;
    }
    const folderKey = getFolderKeyByFilePath(file.path);
    expandFolderAndAncestors(folderKey);
    setSelectedFile(file);
  };

  if (loading) {
    return (
      <div className="flex-1 min-h-0">
        <div className="card w-full h-full flex items-center justify-center">
          <div className="w-8 h-8 rounded-full border-4 border-border border-t-accent animate-spin" />
        </div>
      </div>
    );
  }

  const renderTree = (folderKey: string, depth: number): JSX.Element | null => {
    const node = treeData.directoryMap.get(folderKey);
    if (!node) {
      return null;
    }
    const isExpanded = expandedKeys.has(folderKey);
    const hasChildren = node.childDirectoryKeys.length > 0 || node.files.length > 0;

    return (
      <div key={`dir-${folderKey}`}>
        <button
          type="button"
          onClick={() => toggleFolder(folderKey)}
          className="w-full min-h-10 flex items-center gap-2 rounded-lg px-2 py-2 text-left text-[15px] text-text-muted hover:bg-secondary/40 hover:text-text transition-colors"
          style={{ paddingLeft: `${depth * 14 + 8}px` }}
          title={node.label}
        >
          <span className="w-4 h-4 flex items-center justify-center text-text-muted/80">
            {hasChildren ? (
              <svg
                className={`w-3 h-3 transition-transform ${isExpanded ? 'rotate-90' : ''}`}
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 6l6 6-6 6" />
              </svg>
            ) : null}
          </span>
          <svg className="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
            <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h4.5l1.5 2.25h10.5v8.25A2.25 2.25 0 0118 19.5H6A2.25 2.25 0 013.75 17.25V6.75z" />
          </svg>
          <span className="flex-1 min-w-0 truncate">{node.label}</span>
          {node.isSkillFolder ? (
            <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded border border-[var(--border-accent)] bg-accent-subtle text-accent">
              skills
            </span>
          ) : null}
        </button>

        {isExpanded ? (
          <div>
            {node.childDirectoryKeys.map((childKey) => renderTree(childKey, depth + 1))}
            {node.files.map((file) => {
              const selectable = isPreviewableFile(file.name);
              const selected = selectedFile?.path === file.path;
              return (
                <button
                  key={file.path}
                  type="button"
                  className={`w-full min-h-10 flex items-center gap-2 rounded-lg px-2 py-2 text-left text-[15px] transition-colors ${
                    selected
                      ? 'bg-accent-subtle text-text border border-[var(--border-accent)]'
                      : selectable
                        ? 'text-text-muted hover:bg-secondary/40 hover:text-text border border-transparent'
                        : 'text-text-muted/60 border border-transparent cursor-not-allowed'
                  }`}
                  style={{ paddingLeft: `${(depth + 1) * 14 + 8}px` }}
                  onClick={() => handleFileClick(file)}
                  disabled={!selectable}
                  title={file.name}
                >
                  <span className="w-4 h-4 flex items-center justify-center" />
                  <svg className="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                    {file.isMarkdown ? (
                      <path strokeLinecap="round" strokeLinejoin="round" d="M6.75 3.75h7.5l4.5 4.5v12a1.5 1.5 0 01-1.5 1.5h-10.5a1.5 1.5 0 01-1.5-1.5v-15a1.5 1.5 0 011.5-1.5zM9 14.25V9.75l3 3 3-3v4.5" />
                    ) : (
                      <path strokeLinecap="round" strokeLinejoin="round" d="M6.75 3.75h7.5l4.5 4.5v12a1.5 1.5 0 01-1.5 1.5h-10.5a1.5 1.5 0 01-1.5-1.5v-15a1.5 1.5 0 011.5-1.5zM14.25 3.75v4.5h4.5" />
                    )}
                  </svg>
                  <span className="flex-1 min-w-0 truncate">{file.name}</span>
                  {!selectable ? (
                    <span className="text-[10px] px-1.5 py-0.5 rounded border border-border bg-secondary/40">
                      {t('agent.notPreviewable')}
                    </span>
                  ) : null}
                </button>
              );
            })}
          </div>
        ) : null}
      </div>
    );
  };

  return (
    <div className="flex-1 min-h-0">
      <div className="card w-full h-full flex flex-col">
        <div className="flex items-center justify-between mb-3">
          <div>
            <h2 className="text-lg font-semibold">{t('agent.title')}</h2>
            <p className="text-sm text-text-muted mt-1">{t('agent.subtitle')}</p>
          </div>
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-2 text-xs text-text-muted">
              <span className="mono px-2.5 py-1 rounded-full border border-border bg-secondary/60">
                {t('agent.directoryCount', { count: treeData.directoryCount })}
              </span>
              <span className="mono px-2.5 py-1 rounded-full border border-border bg-secondary/60">
                {t('agent.fileCount', { count: treeData.totalFileCount })}
              </span>
              <span className="mono px-2.5 py-1 rounded-full border border-border bg-secondary/60">
                {t('agent.previewableCount', { count: treeData.previewableFileCount })}
              </span>
            </div>
          </div>
        </div>

        {loadError ? (
          <div className="mb-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
            {loadError}
          </div>
        ) : null}

        <div className="flex-1 min-h-0 grid grid-cols-[minmax(0,3fr)_minmax(0,7fr)] gap-4">
          <div className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col min-h-0">
            <div className="px-4 py-3 bg-secondary/30 border-b border-border">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <h3 className="text-sm font-medium text-text">{t('agent.workspace')}</h3>
                  <p className="text-xs text-text-muted mt-1 mono">
                    {t('agent.workspaceMeta', { directories: treeData.directoryCount, files: treeData.totalFileCount })}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={handleRefresh}
                  disabled={refreshing}
                  className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                  title={t('agent.refresh')}
                >
                  {refreshing ? t('common.refreshing') : t('common.refresh')}
                </button>
              </div>
            </div>
            <div className="flex-1 overflow-auto p-2">
              {treeData.rootChildDirectoryKeys.length === 0 && treeData.rootFiles.length === 0 ? (
                <div className="h-full flex items-center justify-center text-sm text-text-muted">{t('agent.empty')}</div>
              ) : (
                <div className="space-y-0.5">
                  {treeData.rootEntries.map((entry) => {
                    if (entry.kind === 'directory') {
                      return renderTree(entry.key, 0);
                    }
                    const file = entry.file;
                    const selectable = isPreviewableFile(file.name);
                    const selected = selectedFile?.path === file.path;
                    return (
                      <button
                        key={file.path}
                        type="button"
                        className={`w-full min-h-10 flex items-center gap-2 rounded-lg px-2 py-2 text-left text-[15px] transition-colors ${
                          selected
                            ? 'bg-accent-subtle text-text border border-[var(--border-accent)]'
                            : selectable
                              ? 'text-text-muted hover:bg-secondary/40 hover:text-text border border-transparent'
                              : 'text-text-muted/60 border border-transparent cursor-not-allowed'
                        }`}
                        style={{ paddingLeft: '8px' }}
                        onClick={() => handleFileClick(file)}
                        disabled={!selectable}
                      >
                        <svg className="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                          {file.isMarkdown ? (
                            <path strokeLinecap="round" strokeLinejoin="round" d="M6.75 3.75h7.5l4.5 4.5v12a1.5 1.5 0 01-1.5 1.5h-10.5a1.5 1.5 0 01-1.5-1.5v-15a1.5 1.5 0 011.5-1.5zM9 14.25V9.75l3 3 3-3v4.5" />
                          ) : (
                            <path strokeLinecap="round" strokeLinejoin="round" d="M6.75 3.75h7.5l4.5 4.5v12a1.5 1.5 0 01-1.5 1.5h-10.5a1.5 1.5 0 01-1.5-1.5v-15a1.5 1.5 0 011.5-1.5zM14.25 3.75v4.5h4.5" />
                          )}
                        </svg>
                        <span className="flex-1 min-w-0 truncate">{file.name}</span>
                        {!selectable ? (
                          <span className="text-[10px] px-1.5 py-0.5 rounded border border-border bg-secondary/40">
                            {t('agent.notPreviewable')}
                          </span>
                        ) : null}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          </div>

          <div className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col min-h-0">
            {selectedFile ? (
              <FileViewer filePath={selectedFile.path} fileName={selectedFile.name} />
            ) : (
              <>
                <div className="px-4 py-3 bg-secondary/30 border-b border-border">
                  <div className="flex items-center gap-3">
                    <span className="h-9 w-9 rounded-lg border border-border bg-card flex items-center justify-center text-text-muted flex-shrink-0">
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="h-7 w-7">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
                      </svg>
                    </span>
                    <div>
                      <h4 className="text-sm font-medium text-text">{t('agent.contentPreview')}</h4>
                      <p className="text-xs text-text-muted mt-1">{t('agent.selectMarkdown')}</p>
                    </div>
                  </div>
                </div>
                <div className="flex-1 min-h-0 flex items-center justify-center">
                  <div className="text-center text-text-muted">
                    <div className="mb-2 text-sm">{t('agent.selectMarkdownContent')}</div>
                    <div className="text-xs mono">{t('agent.markdownOnly')}</div>
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
