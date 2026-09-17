import { useCallback, useEffect, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { FileViewer } from '../AgentPanel/FileViewer';
import { webRequest } from '../../services/webClient';
import { containsIgnoredDirectory } from '../../features/fileTreeFilters';
import { isHistoryPreviewFile } from '../../features/historyFilePreview';

interface SessionListResponse {
  sessions?: unknown[];
}

interface TeamInfo {
  name: string;
  sessionCount: number;
}

interface DirectoryItem {
  name: string;
  path: string;
  isMarkdown: boolean;
  isDirectory: boolean;
}

interface ListFilesResponse {
  files?: unknown[];
}

interface TeamDeleteResponse {
  session_ids: string[];
}

interface TeamPanelProps {
  onSessionsDeleted?: (sessionIds: string[]) => void | Promise<void>;
}

const DIRECTORY_MIN_WIDTH = 180;
const FILE_PREVIEW_MIN_WIDTH = 320;
const RESIZE_DIVIDER_WIDTH = 4;

function getMaxDirectoryWidth(containerWidth: number): number {
  return Math.max(
    DIRECTORY_MIN_WIDTH,
    containerWidth - FILE_PREVIEW_MIN_WIDTH - RESIZE_DIVIDER_WIDTH,
  );
}

function clampDirectoryWidth(width: number, containerWidth: number): number {
  return Math.min(getMaxDirectoryWidth(containerWidth), Math.max(DIRECTORY_MIN_WIDTH, width));
}

function normalizePath(path: string): string {
  return path.replace(/\\/g, '/').replace(/\/+/g, '/').trim();
}

function isPreviewableFile(fileName: string): boolean {
  const lowerName = fileName.toLowerCase();
  return lowerName.endsWith('.md') || lowerName.endsWith('.mdx') || lowerName.endsWith('.json') || isHistoryPreviewFile(fileName);
}

function getParentPath(filePath: string): string {
  const normalizedPath = normalizePath(filePath);
  if (!normalizedPath.includes('/')) return '';
  return normalizedPath.slice(0, normalizedPath.lastIndexOf('/'));
}

function isHiddenTeamItem(item: DirectoryItem): boolean {
  if (item.name === '.team' && item.isDirectory) return false;
  if (item.name.startsWith('.')) return true;
  if (containsIgnoredDirectory(item.path)) return true;

  const lowerName = item.name.toLowerCase();
  const parentPath = getParentPath(item.path);
  return lowerName === 'skills_state.json' && parentPath.endsWith('/skills');
}

function toDirectoryItems(raw: unknown[]): DirectoryItem[] {
  const rows: DirectoryItem[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const rec = item as Record<string, unknown>;
    const name = rec.name;
    const path = rec.path;
    const isMarkdown = rec.isMarkdown;
    const isDirectory = rec.isDirectory;
    if (
      typeof name !== 'string' ||
      typeof path !== 'string' ||
      typeof isMarkdown !== 'boolean' ||
      typeof isDirectory !== 'boolean'
    ) {
      continue;
    }
    const row = { name, path: normalizePath(path), isMarkdown, isDirectory };
    if (isHiddenTeamItem(row)) continue;
    rows.push(row);
  }
  return rows.sort((a, b) => {
    if (a.isDirectory !== b.isDirectory) return a.isDirectory ? -1 : 1;
    return a.name.localeCompare(b.name, 'zh-Hans-CN');
  });
}

function toTeamInfos(raw: unknown[]): TeamInfo[] {
  const counts = new Map<string, number>();
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const rec = item as Record<string, unknown>;
    const mode = typeof rec.mode === 'string' ? rec.mode : '';
    const teamName = typeof rec.team_name === 'string' ? rec.team_name.trim() : '';
    if (mode !== 'team' || !teamName) continue;
    counts.set(teamName, (counts.get(teamName) ?? 0) + 1);
  }
  return Array.from(counts.entries())
    .map(([name, sessionCount]) => ({ name, sessionCount }))
    .sort((a, b) => a.name.localeCompare(b.name, 'zh-Hans-CN'));
}

export function TeamPanel({ onSessionsDeleted }: TeamPanelProps) {
  const { t } = useTranslation();
  const [teams, setTeams] = useState<TeamInfo[]>([]);
  const [selectedTeamName, setSelectedTeamName] = useState('');
  const [loadingTeams, setLoadingTeams] = useState(true);
  const [loadingFiles, setLoadingFiles] = useState(false);
  const [deletingTeamName, setDeletingTeamName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [dirChildren, setDirChildren] = useState<Map<string, DirectoryItem[]>>(new Map());
  const [loadingDirs, setLoadingDirs] = useState<Set<string>>(new Set());
  const [selectedFile, setSelectedFile] = useState<DirectoryItem | null>(null);
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(new Set());
  const [directoryWidth, setDirectoryWidth] = useState<number | null>(null);
  const [splitPaneWidth, setSplitPaneWidth] = useState(0);
  const splitPaneRef = useRef<HTMLDivElement>(null);
  const directoryPaneRef = useRef<HTMLDivElement>(null);
  const resizeDragRef = useRef<{ pointerId: number; startX: number; startWidth: number } | null>(null);

  const deletingTeam = Boolean(deletingTeamName);
  const selectedRoot = selectedTeamName ? `.agent_teams/${selectedTeamName}` : '';

  const loadTeams = useCallback(async () => {
    setLoadingTeams(true);
    try {
      const payload = await webRequest<SessionListResponse>('session.list', {});
      const nextTeams = Array.isArray(payload?.sessions) ? toTeamInfos(payload.sessions) : [];
      setTeams(nextTeams);
      setError(null);
      setSelectedTeamName((current) => {
        if (current && nextTeams.some((team) => team.name === current)) {
          return current;
        }
        return nextTeams[0]?.name ?? '';
      });
    } catch (loadError) {
      console.error('Failed to load teams:', loadError);
      setTeams([]);
      setSelectedTeamName('');
      setError(t('teams.errors.loadTeams'));
    } finally {
      setLoadingTeams(false);
    }
  }, [t]);

  const loadDirectory = useCallback(async (dir: string, options?: { initial?: boolean }) => {
    const normalizedDir = normalizePath(dir);
    if (!normalizedDir) return;

    if (options?.initial) {
      setLoadingFiles(true);
      setDirChildren(new Map());
      setExpandedPaths(new Set());
      setSelectedFile(null);
    } else {
      setLoadingDirs((prev) => {
        const next = new Set(prev);
        next.add(normalizedDir);
        return next;
      });
    }
    setError(null);

    try {
      const encodedDir = encodeURIComponent(dir);
      const response = await fetch(`/file-api/list-files?dir=${encodedDir}`, { cache: 'no-store' });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(`HTTP ${response.status}: ${text.substring(0, 120)}`);
      }
      const payload = (await response.json()) as ListFilesResponse;
      const rows = Array.isArray(payload?.files) ? toDirectoryItems(payload.files) : [];
      setDirChildren((prev) => {
        const next = new Map(prev);
        next.set(normalizedDir, rows);
        return next;
      });
    } catch (loadError) {
      console.error('Failed to load team directory:', loadError);
      setError(t('teams.errors.loadDirectory'));
    } finally {
      if (options?.initial) {
        setLoadingFiles(false);
      } else {
        setLoadingDirs((prev) => {
          const next = new Set(prev);
          next.delete(normalizedDir);
          return next;
        });
      }
    }
  }, [t]);

  const loadTeamFiles = useCallback(async (teamName: string) => {
    if (!teamName) {
      setDirChildren(new Map());
      setSelectedFile(null);
      setExpandedPaths(new Set());
      return;
    }
    await loadDirectory(`.agent_teams/${teamName}`, { initial: true });
  }, [loadDirectory]);

  useEffect(() => {
    void loadTeams();
  }, [loadTeams]);

  useEffect(() => {
    void loadTeamFiles(selectedTeamName);
  }, [loadTeamFiles, selectedTeamName]);

  useEffect(() => {
    const splitPane = splitPaneRef.current;
    if (!splitPane) return;

    const updateWidth = () => {
      const nextWidth = splitPane.getBoundingClientRect().width;
      setSplitPaneWidth(nextWidth);
      setDirectoryWidth((currentWidth) => (
        currentWidth === null ? null : clampDirectoryWidth(currentWidth, nextWidth)
      ));
    };

    updateWidth();
    const observer = new ResizeObserver(updateWidth);
    observer.observe(splitPane);
    return () => observer.disconnect();
  }, []);

  const handleResizePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || !directoryPaneRef.current) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    resizeDragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startWidth: directoryPaneRef.current.getBoundingClientRect().width,
    };
  };

  const handleResizePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = resizeDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId || !splitPaneRef.current) return;
    const containerWidth = splitPaneRef.current.getBoundingClientRect().width;
    setDirectoryWidth(clampDirectoryWidth(drag.startWidth + event.clientX - drag.startX, containerWidth));
  };

  const finishResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (resizeDragRef.current?.pointerId !== event.pointerId) return;
    resizeDragRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  const handleRefresh = () => {
    void loadTeams();
    if (selectedTeamName) {
      void loadTeamFiles(selectedTeamName);
    }
  };

  const deleteTeam = async (teamName: string) => {
    if (!teamName || deletingTeam) return;
    const confirmed = window.confirm(t('teams.deleteConfirm', { team: teamName }));
    if (!confirmed) return;

    setDeletingTeamName(teamName);
    try {
      let response: TeamDeleteResponse;
      try {
        response = await webRequest<TeamDeleteResponse>('team.delete', {
          team_name: teamName,
          mode: 'team',
        });
      } catch (deleteError) {
        console.error('Failed to delete team:', deleteError);
        setError(t('teams.errors.deleteTeam', { team: teamName }));
        return;
      }

      if (selectedTeamName === teamName) {
        setSelectedFile(null);
        setDirChildren(new Map());
      }
      await loadTeams();

      try {
        await onSessionsDeleted?.(response.session_ids);
      } catch (syncError) {
        console.error('Failed to synchronize deleted team sessions:', syncError);
      }
    } finally {
      setDeletingTeamName('');
    }
  };

  const toggleExpanded = (path: string) => {
    const normalizedPath = normalizePath(path);
    setExpandedPaths((prev) => {
      const next = new Set(prev);
      if (next.has(normalizedPath)) {
        next.delete(normalizedPath);
      } else {
        next.add(normalizedPath);
      }
      return next;
    });
    if (!expandedPaths.has(normalizedPath) && !dirChildren.has(normalizedPath)) {
      void loadDirectory(normalizedPath);
    }
  };

  const renderDirectoryChildren = (parentPath: string, depth: number): JSX.Element[] => {
    const children = dirChildren.get(parentPath) ?? [];
    return children.flatMap((item) => {
      const selectable = !item.isDirectory && isPreviewableFile(item.name);
      const selected = selectedFile?.path === item.path;
      if (item.isDirectory) {
        const isExpanded = expandedPaths.has(item.path);
        const isLoading = loadingDirs.has(item.path);
        const hasChildren = isLoading || !dirChildren.has(item.path) || (dirChildren.get(item.path) ?? []).length > 0;
        return [
          <div key={item.path}>
            <button
              type="button"
              onClick={() => toggleExpanded(item.path)}
              className="w-full min-h-10 flex items-center gap-2 rounded-lg px-2 py-2 text-left text-sm text-text-muted hover:bg-secondary/40 hover:text-text "
              style={{ paddingLeft: `${depth * 16 + 12}px` }}
              title={item.path}
              data-testid="team-panel-directory-folder-item"
              data-variant={item.path}
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
                ) : null}
              </span>
              <svg className="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h4.5l1.5 2.25h10.5v8.25A2.25 2.25 0 0118 19.5H6A2.25 2.25 0 013.75 17.25V6.75z" />
              </svg>
              <span className="flex-1 min-w-0 truncate">{item.name}</span>
              {isLoading ? (
                <span className="w-3 h-3 rounded-full border-2 border-border border-t-accent animate-spin" />
              ) : null}
            </button>
            {isExpanded ? renderDirectoryChildren(item.path, depth + 1) : null}
          </div>,
        ];
      }
      return [
        <button
          key={item.path}
          type="button"
          disabled={!selectable}
          onClick={() => {
            if (selectable) setSelectedFile(item);
          }}
          className={`w-full min-h-10 flex items-center gap-2 rounded-lg px-2 py-2 text-left text-sm  ${
            selected
              ? 'border border-[var(--color-border-accent)] bg-accent-subtle text-text'
              : selectable
                ? 'border border-transparent text-text-muted hover:bg-secondary/40 hover:text-text'
                : 'border border-transparent text-text-muted/70 cursor-default'
          }`}
          style={{ paddingLeft: `${depth * 16 + 12}px` }}
          title={item.path}
          data-testid="team-panel-directory-file-item"
          data-variant={item.path}
        >
          <span className="w-4 h-4 flex items-center justify-center" />
          <svg className="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
            {item.isMarkdown ? (
              <path strokeLinecap="round" strokeLinejoin="round" d="M6.75 3.75h7.5l4.5 4.5v12a1.5 1.5 0 01-1.5 1.5h-10.5a1.5 1.5 0 01-1.5-1.5v-15a1.5 1.5 0 011.5-1.5zM9 14.25V9.75l3 3 3-3v4.5" />
            ) : (
              <path strokeLinecap="round" strokeLinejoin="round" d="M6.75 3.75h7.5l4.5 4.5v12a1.5 1.5 0 01-1.5 1.5h-10.5a1.5 1.5 0 01-1.5-1.5v-15a1.5 1.5 0 011.5-1.5zM14.25 3.75v4.5h4.5" />
            )}
          </svg>
          <span className="flex-1 min-w-0 truncate" data-testid="team-panel-directory-file-name">{item.name}</span>
          {!selectable ? (
            <span className="text-[10px] px-1.5 py-0.5 rounded border border-border bg-secondary/50 text-text-muted" data-testid="team-panel-directory-file-not-previewable">
              {t('teams.notPreviewable')}
            </span>
          ) : null}
        </button>,
      ];
    });
  };

  const resolvedDirectoryWidth = directoryWidth ?? clampDirectoryWidth(splitPaneWidth * 0.25, splitPaneWidth);

  return (
    <div className="flex-1 min-h-0" data-testid="team-panel-root">
      <div className="card w-full h-full flex flex-col" data-testid="team-panel-card">
        <div className="flex items-center justify-between gap-4 mb-4">
          <div>
            <h2 className="text-lg font-semibold" data-testid="team-panel-title">{t('teams.title')}</h2>
            <p className="text-sm text-text-muted mt-1" data-testid="team-panel-subtitle">{t('teams.subtitle')}</p>
          </div>
          <button
            type="button"
            onClick={handleRefresh}
            disabled={loadingTeams || loadingFiles}
            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
            data-testid="team-panel-refresh-button"
          >
            {loadingTeams || loadingFiles ? t('common.refreshing') : t('common.refresh')}
          </button>
        </div>

        {error ? (
          <div className="mb-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger" data-testid="team-panel-error">
            {error}
          </div>
        ) : null}

        <div className="flex-1 min-h-0 grid grid-cols-[minmax(240px,1.2fr)_minmax(0,4fr)] gap-4">
          <div className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col min-h-0" data-testid="team-panel-list-pane">
            <div className="px-4 py-3 bg-secondary/30 border-b border-border">
              <h3 className="text-sm font-medium text-text" data-testid="team-panel-list-heading">{t('teams.teamList')}</h3>
              <p className="text-xs text-text-muted mt-1" data-testid="team-panel-list-count">
                {t('teams.count', { count: teams.length })}
              </p>
            </div>
            <div className="flex-1 overflow-auto p-2 space-y-1" data-testid="team-panel-team-list">
              {loadingTeams ? (
                <div className="h-full flex items-center justify-center" data-testid="team-panel-team-list-state" data-variant="loading">
                  <div className="w-7 h-7 rounded-full border-4 border-border border-t-accent animate-spin" />
                </div>
              ) : teams.length === 0 ? (
                <div className="h-full flex items-center justify-center text-sm text-text-muted" data-testid="team-panel-team-list-state" data-variant="empty">{t('teams.empty')}</div>
              ) : (
                teams.map((team) => (
                  <div
                    key={team.name}
                    className={`w-full min-w-0 flex items-center gap-2 rounded-lg border px-3 py-2 text-sm  ${
                      selectedTeamName === team.name
                        ? 'border-[var(--color-border-accent)] bg-accent-subtle text-text'
                        : 'border-transparent hover:bg-secondary/40 text-text-muted hover:text-text'
                    }`}
                    data-testid="team-panel-team-item"
                    data-variant={team.name}
                  >
                    <button
                      type="button"
                      onClick={() => setSelectedTeamName(team.name)}
                      className="min-w-0 flex-1 text-left"
                      title={team.name}
                      data-testid="team-panel-team-select-button"
                    >
                      <span className="truncate block font-medium" data-testid="team-panel-team-name">{team.name}</span>
                      <span className="mt-1 block text-[11px] text-text-muted" data-testid="team-panel-team-session-count">
                        {t('teams.sessionCount', { count: team.sessionCount })}
                      </span>
                    </button>
                    <button
                      type="button"
                      onClick={(event) => {
                        event.stopPropagation();
                        void deleteTeam(team.name);
                      }}
                      disabled={deletingTeam}
                      className="shrink-0 text-xs px-2 py-1 rounded-md border border-danger/30 text-danger hover:bg-danger-subtle disabled:opacity-50 disabled:cursor-not-allowed"
                      title={t('teams.deleteTeam')}
                      data-testid="team-panel-team-delete-button"
                    >
                      {deletingTeamName === team.name ? t('teams.deleting') : t('teams.deleteTeam')}
                    </button>
                  </div>
                ))
              )}
            </div>
          </div>

          <div
            ref={splitPaneRef}
            className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex min-h-0"
            data-testid="team-panel-split-pane"
          >
            <div
              ref={directoryPaneRef}
              className="shrink-0 flex flex-col min-h-0 overflow-hidden"
              style={{ width: resolvedDirectoryWidth }}
              data-testid="team-panel-directory-pane"
            >
              <div className="px-4 py-3 bg-secondary/30 border-b border-border">
                <div className="min-w-0">
                  <h3 className="text-sm font-medium text-text" data-testid="team-panel-directory-heading">{t('teams.directory')}</h3>
                  <p className="text-xs text-text-muted mt-1 truncate" title={selectedTeamName || t('teams.noneSelected')} data-testid="team-panel-directory-selected-name">
                    {selectedTeamName || t('teams.noneSelected')}
                  </p>
                </div>
              </div>
              <div className="flex-1 overflow-auto p-2">
                {!selectedTeamName ? (
                  <div className="h-full flex items-center justify-center text-sm text-text-muted" data-testid="team-panel-directory-body" data-variant="empty">{t('teams.selectFirst')}</div>
                ) : loadingFiles ? (
                  <div className="h-full flex items-center justify-center text-sm text-text-muted" data-testid="team-panel-directory-body" data-variant="loading">{t('teams.loadingDirectory')}</div>
                ) : (
                  <div className="space-y-1" data-testid="team-panel-directory-body" data-variant="tree">
                    <div>
                      <button
                        type="button"
                        onClick={() => toggleExpanded(selectedRoot)}
                        className="w-full min-h-10 flex items-center gap-2 rounded-lg px-2 py-2 text-left text-sm text-text-muted hover:bg-secondary/40 hover:text-text "
                        title={selectedRoot}
                        data-testid="team-panel-root-folder-toggle"
                      >
                        <span className="w-4 h-4 flex items-center justify-center text-text-muted/80">
                          <svg
                            className={`w-3 h-3  ${expandedPaths.has(selectedRoot) ? 'rotate-90' : ''}`}
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="2"
                          >
                            <path strokeLinecap="round" strokeLinejoin="round" d="M9 6l6 6-6 6" />
                          </svg>
                        </span>
                        <svg className="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h4.5l1.5 2.25h10.5v8.25A2.25 2.25 0 0118 19.5H6A2.25 2.25 0 013.75 17.25V6.75z" />
                        </svg>
                        <span className="flex-1 min-w-0 truncate" data-testid="team-panel-root-folder-name">{selectedTeamName}</span>
                      </button>
                      {expandedPaths.has(selectedRoot) ? renderDirectoryChildren(selectedRoot, 1) : null}
                    </div>
                  </div>
                )}
              </div>
            </div>

            <div
              className="resize-divider touch-none select-none"
              onPointerDown={handleResizePointerDown}
              onPointerMove={handleResizePointerMove}
              onPointerUp={finishResize}
              onPointerCancel={finishResize}
              onLostPointerCapture={() => {
                resizeDragRef.current = null;
              }}
              data-testid="team-panel-resize-divider"
            />

            <div className="flex-1 min-w-0 min-h-0" data-testid="team-panel-preview-pane">
              {selectedFile ? (
                <div data-testid="team-panel-file-viewer">
                  <FileViewer filePath={selectedFile.path} fileName={selectedFile.name} />
                </div>
              ) : (
                <div className="h-full flex items-center justify-center text-text-muted" data-testid="team-panel-preview-empty">
                  {t('teams.selectFile')}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
