import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Check, ChevronDown, ChevronRight, ChevronUp, FileCode2, Folder, LoaderCircle, Search } from 'lucide-react';
import CollapseAllIcon from '../../assets/collapse-all.svg?react';
import ExpandAllIcon from '../../assets/expand-all.svg?react';
import ViewVerticalIcon from '../../assets/view-vertical.svg?react';
import ViewHorizontalIcon from '../../assets/view-horizontal.svg?react';
import menuExpandIcon from '../../assets/menu-expand.svg';
import menuCollapseIcon from '../../assets/menu-collapse.svg';
import type { ProjectInfo, WebError } from '../../types';
import { CodeCommitPushControl } from './CodeCommitPushControl';
import { subscribeCodeTurnChange } from './codeTurnChangeEvents';
import { gitClient } from './gitClient';
import type { CodeReviewTarget, GitDiffFile, GitDiffHunk, GitDiffStats, GitTurnDiff } from './types';
import type { CodeGitDiffWatchController } from './useCodeGitDiffWatch';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';

type DiffViewMode = 'unified' | 'split';
type DiffLineKind = 'added' | 'removed' | 'context' | 'meta';

interface RenderedDiffLine {
  key: string;
  kind: DiffLineKind;
  marker: string;
  content: string;
  oldNumber: number | null;
  newNumber: number | null;
}

interface FileTreeFile {
  type: 'file';
  name: string;
  path: string;
  file: GitDiffFile;
}

interface FileTreeDirectory {
  type: 'directory';
  name: string;
  path: string;
  children: FileTreeNode[];
}

type FileTreeNode = FileTreeFile | FileTreeDirectory;

interface MutableFileTreeDirectory {
  directories: Map<string, MutableFileTreeDirectory>;
  files: FileTreeFile[];
}

const fileTreeCollator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

function buildFileTree(files: GitDiffFile[]): FileTreeNode[] {
  const root: MutableFileTreeDirectory = { directories: new Map(), files: [] };

  files.forEach(file => {
    const parts = file.file_path.split(/[\\/]+/).filter(Boolean);
    const fileName = parts.pop() || file.file_path;
    let current = root;
    parts.forEach(part => {
      let directory = current.directories.get(part);
      if (!directory) {
        directory = { directories: new Map(), files: [] };
        current.directories.set(part, directory);
      }
      current = directory;
    });
    current.files.push({ type: 'file', name: fileName, path: file.file_path, file });
  });

  const materialize = (directory: MutableFileTreeDirectory, parentPath = ''): FileTreeNode[] => {
    const directories = [...directory.directories.entries()]
      .sort(([left], [right]) => fileTreeCollator.compare(left, right))
      .map(([name, child]) => {
        const path = parentPath ? `${parentPath}/${name}` : name;
        return { type: 'directory' as const, name, path, children: materialize(child, path) };
      });
    const childFiles = [...directory.files].sort((left, right) => fileTreeCollator.compare(left.name, right.name));
    return [...directories, ...childFiles];
  };

  return materialize(root);
}

interface FileTreeNodesProps {
  nodes: FileTreeNode[];
  depth?: number;
  selectedPath: string;
  expandedDirectories: Set<string>;
  searchActive: boolean;
  onToggleDirectory: (path: string) => void;
  onSelectFile: (path: string) => void;
}

function FileTreeNodes({ nodes, depth = 0, selectedPath, expandedDirectories, searchActive, onToggleDirectory, onSelectFile }: FileTreeNodesProps) {
  return (
    <>
      {nodes.map(node => {
        const paddingLeft = 8 + depth * 16;
        if (node.type === 'directory') {
          const expanded = searchActive || expandedDirectories.has(node.path);
          return (
            <div key={`directory:${node.path}`} className="code-review__tree-directory" data-testid="code-mode-review-tree-directory" data-variant={node.path}>
              <button
                type="button"
                className="code-review__tree-button code-review__tree-folder"
                style={{ paddingLeft }}
                onClick={() => onToggleDirectory(node.path)}
                aria-expanded={expanded}
                title={node.path}
                data-testid="code-mode-review-tree-folder"
              >
                {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                <Folder size={16} />
                <span>{node.name}</span>
              </button>
              {expanded ? (
                <FileTreeNodes
                  nodes={node.children}
                  depth={depth + 1}
                  selectedPath={selectedPath}
                  expandedDirectories={expandedDirectories}
                  searchActive={searchActive}
                  onToggleDirectory={onToggleDirectory}
                  onSelectFile={onSelectFile}
                />
              ) : null}
            </div>
          );
        }

        return (
          <button
            type="button"
            key={`file:${node.path}`}
            className={`code-review__tree-button code-review__tree-file${node.path === selectedPath ? ' is-active' : ''}`}
            style={{ paddingLeft }}
            onClick={() => onSelectFile(node.path)}
            title={node.path}
            data-testid="code-mode-review-tree-file"
            data-variant={node.path}
          >
            <FileCode2 size={15} />
            <span>{node.name}</span>
            <small className="code-stat-added">+{node.file.lines_added}</small>
            <small className="code-stat-removed">-{node.file.lines_removed}</small>
          </button>
        );
      })}
    </>
  );
}

function renderHunkLines(hunk: GitDiffHunk, hunkIndex: number): RenderedDiffLine[] {
  let oldNumber = hunk.old_start;
  let newNumber = hunk.new_start;
  return hunk.lines.map((line, lineIndex) => {
    const marker = line.charAt(0);
    const content = marker === '+' || marker === '-' || marker === ' ' ? line.slice(1) : line;
    const key = `${hunkIndex}:${lineIndex}`;
    if (marker === '+') {
      const rendered = { key, kind: 'added' as const, marker, content, oldNumber: null, newNumber };
      newNumber += 1;
      return rendered;
    }
    if (marker === '-') {
      const rendered = { key, kind: 'removed' as const, marker, content, oldNumber, newNumber: null };
      oldNumber += 1;
      return rendered;
    }
    if (marker === ' ') {
      const rendered = { key, kind: 'context' as const, marker: ' ', content, oldNumber, newNumber };
      oldNumber += 1;
      newNumber += 1;
      return rendered;
    }
    return { key, kind: 'meta', marker: '', content, oldNumber: null, newNumber: null };
  });
}

function UnifiedDiff({ file }: { file: GitDiffFile }) {
  return (
    <div className="code-diff-table code-diff-table--unified" data-testid="code-mode-review-diff-table" data-variant="unified">
      {file.hunks.map((hunk, hunkIndex) => (
        <div
          key={`${hunk.old_start}:${hunk.new_start}:${hunkIndex}`}
          className="code-diff-hunk"
          data-testid="code-mode-review-diff-hunk"
          data-variant={`${hunk.old_start}:${hunk.new_start}:${hunkIndex}`}
        >
          <div className="code-diff-hunk__header" data-testid="code-mode-review-diff-hunk-header">
            @@ -{hunk.old_start},{hunk.old_lines} +{hunk.new_start},{hunk.new_lines} @@
          </div>
          {renderHunkLines(hunk, hunkIndex).map(line => (
            <div key={line.key} className={`code-diff-line code-diff-line--${line.kind}`} data-testid="code-mode-review-diff-line" data-variant={line.kind}>
              <span className="code-diff-line__number">{line.oldNumber ?? ''}</span>
              <span className="code-diff-line__number">{line.newNumber ?? ''}</span>
              <span className="code-diff-line__marker">{line.marker}</span>
              <code>{line.content || ' '}</code>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

function SplitDiff({ file }: { file: GitDiffFile }) {
  const rows = useMemo(() => file.hunks.flatMap((hunk, hunkIndex) => renderHunkLines(hunk, hunkIndex)), [file]);

  return (
    <div className="code-diff-table code-diff-table--split" data-testid="code-mode-review-diff-table" data-variant="split">
      {rows.map(line => (
        <div key={line.key} className="code-diff-split-row">
          <div className={`code-diff-split-cell code-diff-split-cell--${line.kind === 'added' ? 'empty' : line.kind}`}>
            <span className="code-diff-line__number">{line.kind === 'added' ? '' : (line.oldNumber ?? '')}</span>
            <span className="code-diff-line__marker">{line.kind === 'removed' ? '-' : line.kind === 'context' ? ' ' : ''}</span>
            <code>{line.kind === 'added' ? ' ' : line.content || ' '}</code>
          </div>
          <div className={`code-diff-split-cell code-diff-split-cell--${line.kind === 'removed' ? 'empty' : line.kind}`}>
            <span className="code-diff-line__number">{line.kind === 'removed' ? '' : (line.newNumber ?? '')}</span>
            <span className="code-diff-line__marker">{line.kind === 'added' ? '+' : line.kind === 'context' ? ' ' : ''}</span>
            <code>{line.kind === 'removed' ? ' ' : line.content || ' '}</code>
          </div>
        </div>
      ))}
    </div>
  );
}

function FileDiff({ file, viewMode }: { file: GitDiffFile; viewMode: DiffViewMode }) {
  if (file.is_binary) return <div className="code-review__empty">二进制文件不能显示文本差异。</div>;
  if (file.is_large_file && file.hunks.length === 0) return <div className="code-review__empty">该文件差异超过 1MB，已跳过内容预览。</div>;
  if (file.hunks.length === 0) return <div className="code-review__empty">该文件没有可显示的差异内容。</div>;
  return viewMode === 'split' ? <SplitDiff file={file} /> : <UnifiedDiff file={file} />;
}

interface CodeReviewPanelProps {
  project: ProjectInfo;
  sessionId: string;
  target?: CodeReviewTarget | null;
  diffWatch: CodeGitDiffWatchController;
  isProcessing: boolean;
}

type CodeReviewSource = 'last_turn' | 'working_tree';

interface CodeReviewDocument {
  source: CodeReviewSource;
  branch: string | null;
  status?: string;
  turnIndex?: number;
  stats: GitDiffStats;
  files: Record<string, GitDiffFile>;
}

const EMPTY_STATS: GitDiffStats = {
  files_changed: 0,
  lines_added: 0,
  lines_removed: 0,
};

function getReviewErrorMessage(error: unknown): string {
  const webError = error as WebError;
  switch (webError.code) {
    case 'DIFF_HISTORY_EXPIRED':
      return '该轮差异历史已过期，无法恢复详情。';
    case 'CHANGE_SET_NOT_FOUND':
    case 'TURN_DIFF_NOT_FOUND':
      return '没有找到该轮代码修改记录。';
    case 'GIT_TRANSIENT_STATE':
      return '仓库正在合并或变基，暂时无法加载审核详情。';
    default:
      return webError.message || '加载审核结果失败';
  }
}

export function CodeReviewPanel({ project, sessionId, target = null, diffWatch, isProcessing }: CodeReviewPanelProps) {
  const [source, setSource] = useState<CodeReviewSource>(target?.source === 'working_tree' ? 'working_tree' : 'last_turn');
  const [turnDiff, setTurnDiff] = useState<GitTurnDiff | null>(null);
  const [turnLoading, setTurnLoading] = useState(false);
  const [turnError, setTurnError] = useState<string | null>(null);
  const [selectedPath, setSelectedPath] = useState('');
  const [search, setSearch] = useState('');
  const [filePanelOpen, setFilePanelOpen] = useState(false);
  const [sourceMenuOpen, setSourceMenuOpen] = useState(false);
  const [viewMode, setViewMode] = useState<DiffViewMode>('unified');
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(() => new Set());
  const [expandedDirectories, setExpandedDirectories] = useState<Set<string>>(() => new Set());
  const fileSectionRefs = useRef(new Map<string, HTMLElement>());
  const sourceMenuRef = useRef<HTMLDivElement>(null);
  const loadSequenceRef = useRef(0);
  const workingTreeIdentityRef = useRef('');
  const { tooltip, handlers: tooltipHandlers } = useAdaptiveTooltip();

  const loadTurnDiff = useCallback(async () => {
    const loadSequence = loadSequenceRef.current + 1;
    loadSequenceRef.current = loadSequence;
    setTurnLoading(true);
    setTurnError(null);
    try {
      let resolvedTarget = target?.source === 'last_turn' ? target : null;
      if (!resolvedTarget) {
        const history = await gitClient.turnDiffList(project.project_id, sessionId, { limit: 1 });
        const latestTurn = history.turns[0];
        if (!latestTurn) {
          if (loadSequenceRef.current === loadSequence) setTurnDiff(null);
          return;
        }
        resolvedTarget = {
          source: 'last_turn',
          changeSetId: latestTurn.change_set_id,
          turnIndex: latestTurn.turn_index,
        };
      }
      const turnDiff = await gitClient.turnDiff(project.project_id, sessionId, resolvedTarget, {
        includeFiles: true,
        includeHunks: true,
      });
      if (loadSequenceRef.current !== loadSequence) return;
      setTurnDiff(turnDiff);
    } catch (nextError) {
      if (loadSequenceRef.current !== loadSequence) return;
      setTurnDiff(null);
      setTurnError(getReviewErrorMessage(nextError));
    } finally {
      if (loadSequenceRef.current === loadSequence) setTurnLoading(false);
    }
  }, [project, sessionId, target]);

  useEffect(() => {
    loadSequenceRef.current += 1;
    setTurnDiff(null);
    setTurnError(null);
    setSource(target?.source === 'working_tree' ? 'working_tree' : 'last_turn');
  }, [project.project_id, sessionId, target]);

  useEffect(() => {
    if (source === 'last_turn') void loadTurnDiff();
  }, [loadTurnDiff, source]);

  useEffect(() => {
    if (source !== 'last_turn') return;
    return subscribeCodeTurnChange(event => {
      if (event.projectId !== project.project_id || event.sessionId !== sessionId) return;
      const targetMatches =
        target?.source === 'last_turn'
          ? Boolean(event.changeSetId && target.changeSetId === event.changeSetId) || target.turnIndex === event.turnIndex
          : !turnDiff || Boolean(event.changeSetId && turnDiff.change_set_id === event.changeSetId) || turnDiff.turn_index === event.turnIndex;
      if (!targetMatches) return;
      setTurnDiff(previous => (previous ? { ...previous, status: event.status } : previous));
      void loadTurnDiff();
    });
  }, [loadTurnDiff, project.project_id, sessionId, source, target, turnDiff]);

  useEffect(() => {
    diffWatch.setFilesEnabled(source === 'working_tree');
    return () => {
      if (source === 'working_tree') diffWatch.setFilesEnabled(false);
    };
  }, [diffWatch.setFilesEnabled, source]);

  useEffect(() => {
    const closeMenu = (event: MouseEvent) => {
      if (!sourceMenuRef.current?.contains(event.target as Node)) setSourceMenuOpen(false);
    };
    document.addEventListener('mousedown', closeMenu);
    return () => document.removeEventListener('mousedown', closeMenu);
  }, []);

  const workingTreeFiles = useMemo(
    () =>
      Object.fromEntries(
        Object.values(diffWatch.files).map(file => {
          const detail = diffWatch.detailFiles[file.file_path];
          return [file.file_path, detail ? { ...file, ...detail, hunks: detail.hunks } : { ...file, hunks: [] }];
        }),
      ),
    [diffWatch.detailFiles, diffWatch.files],
  );

  const reviewDocument = useMemo<CodeReviewDocument | null>(() => {
    if (source === 'last_turn') {
      if (!turnDiff) return null;
      return {
        source,
        branch: turnDiff.branch || project.git.branch,
        status: turnDiff.status,
        turnIndex: turnDiff.turn_index,
        stats: turnDiff.stats,
        files: turnDiff.files,
      };
    }
    if (!diffWatch.summary) return null;
    return {
      source,
      branch: diffWatch.summary.repo.branch || project.git.branch,
      stats: diffWatch.summary.current?.stats ?? EMPTY_STATS,
      files: workingTreeFiles,
    };
  }, [diffWatch.summary, project.git.branch, source, turnDiff, workingTreeFiles]);

  const files = useMemo(() => Object.values(reviewDocument?.files ?? {}), [reviewDocument]);
  useEffect(() => {
    if (!files.some(file => file.file_path === selectedPath)) setSelectedPath(files[0]?.file_path ?? '');
  }, [files, selectedPath]);

  useEffect(() => {
    const validPaths = new Set(files.map(file => file.file_path));
    setExpandedPaths(previous => {
      return new Set([...previous].filter(path => validPaths.has(path)));
    });
  }, [files]);

  useEffect(() => {
    setExpandedPaths(new Set());
    setExpandedDirectories(new Set());
    setSearch('');
    fileSectionRefs.current.clear();
  }, [project.project_id, sessionId, source, target]);

  useEffect(() => {
    // 详情可能接近 1MB；只保留当前查看文件的内容，切换或关闭时释放旧详情。
    const selectedFileIsExpanded = selectedPath && expandedPaths.has(selectedPath);
    diffWatch.setDetailPaths(source === 'working_tree' && selectedFileIsExpanded ? [selectedPath] : []);
  }, [diffWatch.setDetailPaths, expandedPaths, selectedPath, source]);

  const filteredFiles = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return files.filter(file => !query || file.file_path.toLocaleLowerCase().includes(query));
  }, [files, search]);
  const fileTree = useMemo(() => buildFileTree(filteredFiles), [filteredFiles]);
  const selectedFile = files.find(file => file.file_path === selectedPath) ?? files[0];

  const toggleDirectory = (path: string) => {
    setExpandedDirectories(previous => {
      const next = new Set(previous);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const toggleFile = (filePath: string) => {
    setSelectedPath(filePath);
    setExpandedPaths(previous => {
      return previous.has(filePath) ? new Set() : new Set([filePath]);
    });
  };

  const openFileFromSidebar = (filePath: string) => {
    setSelectedPath(filePath);
    setExpandedPaths(new Set([filePath]));
    window.requestAnimationFrame(() => fileSectionRefs.current.get(filePath)?.scrollIntoView({ block: 'start' }));
  };

  const sourceLoading =
    source === 'last_turn' ? turnLoading && !turnDiff : (diffWatch.summaryLoading && !diffWatch.summary) || (diffWatch.filesLoading && !diffWatch.filesReady);
  const sourceError = source === 'last_turn' ? turnError : diffWatch.summaryError || diffWatch.filesError;
  const repoUnavailable = source === 'working_tree' && diffWatch.summary && !diffWatch.summary.repo.is_git;
  const repoTransient = source === 'working_tree' && diffWatch.summary?.repo.transient;
  const workingTreeIdentity = source === 'working_tree' ? `${diffWatch.summary?.repo.branch ?? ''}:${diffWatch.summary?.repo.head ?? ''}` : '';
  const stats = reviewDocument?.stats ?? EMPTY_STATS;

  useEffect(() => {
    if (source !== 'working_tree' || !workingTreeIdentity) return;
    if (workingTreeIdentityRef.current && workingTreeIdentityRef.current !== workingTreeIdentity) {
      setSelectedPath('');
      setSearch('');
      setExpandedPaths(new Set());
      setExpandedDirectories(new Set());
      fileSectionRefs.current.clear();
    }
    workingTreeIdentityRef.current = workingTreeIdentity;
  }, [source, workingTreeIdentity]);

  const reload = () => {
    if (source === 'last_turn') void loadTurnDiff();
    else diffWatch.refresh();
  };

  const renderReviewBody = () => {
    if (sourceLoading) {
      return (
        <div className="code-review-state" data-testid="code-mode-review-state" data-variant="loading">
          <LoaderCircle className="code-mode-spin" size={18} />
          <span>正在加载审核结果…</span>
        </div>
      );
    }
    if ((sourceError && files.length === 0) || repoUnavailable || repoTransient) {
      const message =
        sourceError ||
        (repoUnavailable ? '当前项目不是可用的 Git 仓库，无法查看分支修改。' : null) ||
        (repoTransient ? '仓库正在执行 Git 操作，处理完成后再查看工作区差异。' : null);
      return (
        <div className="code-review-state" data-testid="code-mode-review-state" data-variant="error">
          <FileCode2 size={20} />
          <span>{message}</span>
          <button type="button" className="code-mode-button" onClick={reload} data-testid="code-mode-review-state-reload">
            重新加载
          </button>
        </div>
      );
    }
    if (files.length === 0) {
      return (
        <div className="code-review-state" data-testid="code-mode-review-state" data-variant="empty">
          <FileCode2 size={20} />
          <span>{source === 'working_tree' ? '当前分支没有未提交修改。' : '暂无最近一轮代码修改可供审核。'}</span>
        </div>
      );
    }
    return (
      <>
        {sourceError ? (
          <div className="code-review__notice" data-testid="code-mode-review-notice">
            {sourceError}，当前保留上次成功加载的内容。
          </div>
        ) : null}
        <div className="code-review__body" data-testid="code-mode-review-body">
          {filePanelOpen ? (
            <aside className="code-review__files" data-testid="code-mode-review-files">
              <label className="code-review__search" data-testid="code-mode-review-files-search">
                <Search size={15} />
                <input
                  value={search}
                  onChange={event => setSearch(event.target.value)}
                  placeholder="搜索文件"
                  data-testid="code-mode-review-files-search-input"
                />
              </label>
              <div className="code-review__file-list" data-testid="code-mode-review-file-list">
                <FileTreeNodes
                  nodes={fileTree}
                  selectedPath={selectedFile?.file_path ?? ''}
                  expandedDirectories={expandedDirectories}
                  searchActive={search.trim().length > 0}
                  onToggleDirectory={toggleDirectory}
                  onSelectFile={openFileFromSidebar}
                />
              </div>
            </aside>
          ) : null}
          <main className="code-review__diff" data-testid="code-mode-review-diff">
            <div className="code-review__diff-content" data-testid="code-mode-review-diff-content">
              {files.map(file => {
                const expanded = expandedPaths.has(file.file_path);
                const detailReady = Object.prototype.hasOwnProperty.call(diffWatch.detailFiles, file.file_path);
                return (
                  <section
                    key={file.file_path}
                    ref={element => {
                      if (element) fileSectionRefs.current.set(file.file_path, element);
                      else fileSectionRefs.current.delete(file.file_path);
                    }}
                    className="code-review__file-section"
                    data-testid="code-mode-review-file-section"
                    data-variant={file.file_path}
                  >
                    <button
                      type="button"
                      className="code-review__file-header"
                      onClick={() => toggleFile(file.file_path)}
                      aria-expanded={expanded}
                      data-testid="code-mode-review-file-header"
                    >
                      {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                      <span className="code-review__file-name">{file.file_path}</span>
                      <span className="code-stat-added">+{file.lines_added}</span>
                      <span className="code-stat-removed">-{file.lines_removed}</span>
                    </button>
                    {expanded && source === 'working_tree' && !detailReady ? (
                      <div className="code-review__empty" data-testid="code-mode-review-file-empty">
                        {diffWatch.detailError || (diffWatch.detailLoading ? '正在加载文件差异…' : '正在等待文件差异…')}
                      </div>
                    ) : expanded ? (
                      <FileDiff file={file} viewMode={viewMode} />
                    ) : null}
                  </section>
                );
              })}
            </div>
          </main>
        </div>
      </>
    );
  };

  return (
    <section className="code-review code-review--embedded" aria-label="审核代码修改" data-testid="code-mode-review-panel">
      <div className="code-review__toolbar" data-testid="code-mode-review-toolbar">
        <div className="code-review__toolbar-row">
          <button
            type="button"
            className="code-review__icon-button"
            onClick={() => setFilePanelOpen(open => !open)}
            title="切换文件侧边栏"
            data-testid="code-mode-review-toggle-files"
          >
            <img src={filePanelOpen ? menuCollapseIcon : menuExpandIcon} width={17} height={17} aria-hidden="true" />
          </button>
          <div className="code-review__toolbar-info">
            <div className="code-review__toolbar-info-row">
              <div ref={sourceMenuRef} className="code-review__source" data-testid="code-mode-review-source">
                <button
                  type="button"
                  className="code-review__source-trigger"
                  onClick={() => setSourceMenuOpen(open => !open)}
                  aria-expanded={sourceMenuOpen}
                  aria-haspopup="menu"
                  data-testid="code-mode-review-source-trigger"
                >
                  <span>{source === 'working_tree' ? '分支' : '上一轮'}</span>
                  <ChevronDown size={14} />
                </button>
                {sourceMenuOpen ? (
                  <div className="code-review__source-menu" role="menu" data-testid="code-mode-review-source-menu">
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => {
                        setSource('last_turn');
                        setSourceMenuOpen(false);
                      }}
                      data-testid="code-mode-review-source-menu-item"
                      data-variant="last_turn"
                    >
                      <span>上一轮</span>
                      {source === 'last_turn' ? <Check size={15} /> : null}
                    </button>
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => {
                        setSource('working_tree');
                        setSourceMenuOpen(false);
                      }}
                      data-testid="code-mode-review-source-menu-item"
                      data-variant="working_tree"
                    >
                      <span>分支</span>
                      {source === 'working_tree' ? <Check size={15} /> : null}
                    </button>
                  </div>
                ) : null}
              </div>
              <div className="code-review__summary" data-testid="code-mode-review-summary">
                <span className="code-review__summary-label" data-testid="code-mode-review-summary-files">
                  {stats.files_changed} 个文件已更改
                </span>
                <span className="code-review__stat code-review__stat--added" data-testid="code-mode-review-summary-lines-added">
                  +{stats.lines_added}
                </span>
                <span className="code-review__stat code-review__stat--removed" data-testid="code-mode-review-summary-lines-removed">
                  -{stats.lines_removed}
                </span>
                {source === 'last_turn' && reviewDocument?.status === 'discarded' ? (
                  <span className="code-review__discarded-status" role="status" data-testid="code-mode-review-discarded-status">
                    此修改已撤销
                  </span>
                ) : null}
              </div>
            </div>
            {source === 'working_tree' && (diffWatch.summary?.repo.branch || project.git.branch) ? (
              <div className="code-review__toolbar-branch" data-testid="code-mode-review-toolbar-branch">
                {diffWatch.summary?.repo.branch || project.git.branch}
              </div>
            ) : null}
          </div>
          <div className="code-review__toolbar-spacer" />
          <button
            type="button"
            className="code-review__icon-button"
            onClick={() => {
              if (files.length > 0 && expandedPaths.size === files.length) {
                setExpandedPaths(new Set());
              } else {
                setExpandedPaths(new Set(files.map(file => file.file_path)));
              }
            }}
            data-tooltip={files.length > 0 && expandedPaths.size === files.length ? '全部收起' : '全部展开'}
            data-testid="code-mode-review-collapse"
            {...tooltipHandlers}
          >
            {files.length > 0 && expandedPaths.size === files.length ? (
              <CollapseAllIcon className="h-[24px] w-[24px]" aria-hidden="true" />
            ) : (
              <ExpandAllIcon className="h-[24px] w-[24px]" aria-hidden="true" />
            )}
          </button>
          <button
            type="button"
            className="code-review__icon-button"
            onClick={() => setViewMode(viewMode === 'unified' ? 'split' : 'unified')}
            data-tooltip={viewMode === 'unified' ? '切换到统一差异视图' : '切换到拆分差异视图'}
            data-testid="code-mode-review-view-mode"
            data-variant={viewMode}
            {...tooltipHandlers}
          >
            {viewMode === 'unified' ? (
              <ViewVerticalIcon className="h-[24px] w-[24px]" aria-hidden="true" />
            ) : (
              <ViewHorizontalIcon className="h-[24px] w-[24px]" aria-hidden="true" />
            )}
          </button>
          {source === 'working_tree' ? (
            <CodeCommitPushControl
              project={project}
              branch={diffWatch.summary?.repo.branch || project.git.branch || null}
              hasChanges={Boolean(diffWatch.summary?.current?.is_dirty)}
              filesChanged={diffWatch.summary?.current?.stats.files_changed ?? 0}
              isGit={Boolean(diffWatch.summary?.repo.is_git)}
              transient={Boolean(diffWatch.summary?.repo.transient)}
              isProcessing={isProcessing}
              variant="review"
              onSuccess={diffWatch.refresh}
            />
          ) : null}
        </div>
      </div>
      {renderReviewBody()}
      <footer className="code-review__footer" data-testid="code-mode-review-footer">
        {source === 'last_turn'
          ? reviewDocument
            ? `当前展示第 ${reviewDocument.turnIndex} 轮 Agent 修改的历史快照${reviewDocument.status === 'discarded' ? '（已撤销）' : ''}，后续修改不会覆盖该轮差异。`
            : '上一轮使用固定历史快照，后续修改不会覆盖该轮差异。'
          : '当前展示工作区相对 HEAD 的修改，文件变化会实时更新。'}
      </footer>
      {tooltip}
    </section>
  );
}
