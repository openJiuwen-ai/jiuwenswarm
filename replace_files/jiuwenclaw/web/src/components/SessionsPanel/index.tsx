import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FileViewer } from '../AgentPanel/FileViewer';
import { containsIgnoredDirectory } from '../../features/fileTreeFilters';
import { webRequest } from '../../services/webClient';

interface SessionsPanelProps {
  currentSessionId: string;
  isConnected: boolean;
  isProcessing: boolean;
  onRestoreSession: (sessionId: string) => void | Promise<void>;
  isRemoteSessionStorage?: boolean;
}

interface SessionListResponse {
  sessions?: unknown[];
  total?: number;
  limit?: number;
  offset?: number;
}

interface SessionFileItem {
  name: string;
  path: string;
  isMarkdown: boolean;
  isDirectory: boolean;
  depth: number;
}

interface ListFilesResponse {
  files?: unknown[];
}

/** list-files 在 Windows 上可能返回反斜杠 path，与前端字面量比较前需统一 */
function normalizeWorkspacePath(p: string): string {
  return p.replace(/\\/g, '/').replace(/\/+/g, '/').trim();
}

function pickNextSelectedSessionId(
  prev: string | null,
  sessionRows: SessionItem[],
  currentChatSessionId: string
): string | null {
  const ids = sessionRows.map(s => s.session_id);
  if (prev && ids.includes(prev)) return prev;
  if (currentChatSessionId && ids.includes(currentChatSessionId)) return currentChatSessionId;
  return sessionRows[0]?.session_id ?? null;
}

function formatDateTime(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  const hours = String(date.getHours()).padStart(2, '0');
  const minutes = String(date.getMinutes()).padStart(2, '0');
  const seconds = String(date.getSeconds()).padStart(2, '0');
  return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
}

function isPlausibleDate(date: Date): boolean {
  const year = date.getFullYear();
  return year >= 2020 && year <= 2100;
}

/** Shorten Discord IDs for list labels*/
function shortenDiscordIDForLabel(id: string): string {
  const s = id.trim();
  if (s.length <= 12) {
    return s;
  }
  return `${s.slice(0, 4)}…${s.slice(-4)}`;
}

function parseSessionDisplayLabel(sessionId: string, t: (key: string, options?: Record<string, unknown>) => string): string {
  if (!sessionId) return t('sessions.unknownSession');

  // 微信 iLink：形如 userhash@im.wechat，以 .wechat 结尾
  if (sessionId.endsWith('.wechat')) {
    const wechatLabel = t('sessions.prefixes.wechat');
    const at = sessionId.lastIndexOf('@');
    const local =
      at >= 0 ? sessionId.slice(0, at) : sessionId.replace(/\.wechat$/i, '').trim();
    const idPart = local.trim() || sessionId;
    return `${wechatLabel}-${shortenDiscordIDForLabel(idPart)}`;
  }

  // 处理以 sess_、cron_、feishu_、wechat_、xiaoyi_、dingtalk_ 开头的会话ID
  const prefixes = ['sess_', 'cron_', 'feishu_', 'wechat_', 'xiaoyi_', 'dingtalk_', 'wecom_'];
  const prefixMap: Record<string, string> = {
    'sess_': t('sessions.prefixes.session'),
    'cron_': t('sessions.prefixes.cron'),
    'feishu_': t('sessions.prefixes.feishu'),
    'wechat_': t('sessions.prefixes.wechat'),
    'xiaoyi_': t('sessions.prefixes.xiaoyi'),
    'dingtalk_': t('sessions.prefixes.dingtalk'),
    'wecom_': t('sessions.prefixes.wecom')
  };
  
  for (const prefix of prefixes) {
    if (sessionId.startsWith(prefix)) {
      const parts = sessionId.split('_');
      const hexTs = parts[1] ?? '';
      if (/^[0-9a-fA-F]+$/.test(hexTs)) {
        const ms = Number.parseInt(hexTs, 16);
        if (Number.isFinite(ms)) {
          const date = new Date(ms);
          if (!Number.isNaN(date.getTime()) && isPlausibleDate(date)) {
            return `${prefixMap[prefix]}-${formatDateTime(date)}`;
          }
        }
      }
      return `${prefixMap[prefix]}-${t('sessions.unknownTime')}`;
    }
  }

  if (sessionId.startsWith('heartbeat_')) {
    // heartbeat_{hex_ms}_{suffix}
    const parts = sessionId.split('_');
    const hexTs = parts[1] ?? '';
    if (/^[0-9a-fA-F]+$/.test(hexTs)) {
      const ms = Number.parseInt(hexTs, 16);
      if (Number.isFinite(ms)) {
        const date = new Date(ms);
        if (!Number.isNaN(date.getTime()) && isPlausibleDate(date)) {
          return `${t('sessions.prefixes.heartbeat')}-${formatDateTime(date)}`;
        }
      }
    }
    return `${t('sessions.prefixes.heartbeat')}-${t('sessions.unknownTime')}`;
  }

  // discord_{channel_id}_{user_id} from Discord Channel (not hex timestamps)
  if (sessionId.startsWith('discord_')) {
    const parts = sessionId.split('_');
    const discordLabel = t('sessions.prefixes.discord');
    if (parts.length >= 3) {
      const ch = shortenDiscordIDForLabel(parts[1]);
      const user = shortenDiscordIDForLabel(parts[2]);
      return `${discordLabel}-${ch}/${user}`;
    }
  }

  // 解析会话ID中可能包含的时间戳格式，如 YYYYMMDD_HHMMSS_xxxx
  const timestampRegex = /(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/;
  const match = sessionId.match(timestampRegex);
  if (match) {
    const [, year, month, day, hours, minutes, seconds] = match;
    const date = new Date(`${year}-${month}-${day} ${hours}:${minutes}:${seconds}`);
    if (!Number.isNaN(date.getTime()) && isPlausibleDate(date)) {
      const prefix = sessionId.includes('_') ? sessionId.split('_')[0] : t('sessions.prefixes.unknown');
      return `${prefix}-${formatDateTime(date)}`;
    }
  }

  const prefix = sessionId.includes('_') ? sessionId.split('_')[0] : t('sessions.prefixes.unknown');
  return `${prefix}-${t('sessions.unknownTime')}`;
}

interface SessionItem {
  session_id: string;
  title?: string;
  channel_id?: string;
  user_id?: string;
  last_message_at?: number;
  created_at?: number;
  message_count?: number;
  role?: string;
  timestamp?: number;
  content?: string;
}

function toSessionItems(raw: unknown[]): SessionItem[] {
  return raw
    .map((item) => {
      // 兼容新格式(对象 {session_id: "..."})和旧格式(纯字符串)
      if (typeof item === 'string') {
        return { session_id: item.trim() } as SessionItem;
      }
      if (item && typeof item === 'object' && 'session_id' in item) {
        const rec = item as Record<string, unknown>;
        const sid = rec.session_id;
        if (typeof sid !== 'string' || sid.trim().length === 0) {
          return null;
        }
        return {
          session_id: sid.trim(),
          title: typeof rec.title === 'string' ? rec.title : undefined,
          channel_id: typeof rec.channel_id === 'string' ? rec.channel_id : undefined,
          user_id: typeof rec.user_id === 'string' ? rec.user_id : undefined,
          last_message_at: typeof rec.last_message_at === 'number' ? rec.last_message_at : undefined,
          created_at: typeof rec.created_at === 'number' ? rec.created_at : undefined,
          message_count: typeof rec.message_count === 'number' ? rec.message_count : undefined,
          role: typeof rec.role === 'string' ? rec.role : undefined,
          timestamp: typeof rec.timestamp === 'number' ? rec.timestamp : undefined,
          content: typeof rec.content === 'string' ? rec.content : undefined,
        } as SessionItem;
      }
      return null;
    })
    .filter((item): item is SessionItem => item !== null);
}

function toSessionFiles(raw: unknown[]): SessionFileItem[] {
  const rows: SessionFileItem[] = [];
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
    rows.push({ name, path, isMarkdown, isDirectory, depth: 0 });
  }
  return rows;
}

function isPreviewableSessionFile(fileName: string): boolean {
  const lowerName = fileName.toLowerCase();
  return lowerName.endsWith('.md') || lowerName.endsWith('.mdx') || lowerName.endsWith('.json');
}

export function SessionsPanel({
  currentSessionId,
  isConnected,
  isProcessing,
  onRestoreSession,
  isRemoteSessionStorage = false,
}: SessionsPanelProps) {
  const { t } = useTranslation();
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [loadingSessions, setLoadingSessions] = useState(true);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [deletingSessionId, setDeletingSessionId] = useState<string | null>(null);
  const [files, setFiles] = useState<SessionFileItem[]>([]);
  const [loadingFiles, setLoadingFiles] = useState(false);
  const [filesError, setFilesError] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<SessionFileItem | null>(null);
  const [filePreviewReloadNonce, setFilePreviewReloadNonce] = useState(0);

  const selectedFileRef = useRef<SessionFileItem | null>(null);
  selectedFileRef.current = selectedFile;
  const selectedSessionIdRef = useRef<string | null>(selectedSessionId);
  selectedSessionIdRef.current = selectedSessionId;
  const currentSessionIdRef = useRef(currentSessionId);
  currentSessionIdRef.current = currentSessionId;

  const loadSessionFilesForSession = useCallback(
    async (
      sessionId: string | null,
      options?: { preserveSelectionPath?: string | null }
    ) => {
      const preservePath = options?.preserveSelectionPath;
      const preserve =
        typeof preservePath === 'string' && preservePath.length > 0;

      if (!sessionId) {
        setFiles([]);
        setFilesError(null);
        setSelectedFile(null);
        return;
      }
      if (isRemoteSessionStorage) {
        setFiles([]);
        setFilesError(null);
        setSelectedFile(null);
        return;
      }
      setLoadingFiles(true);
      setFilesError(null);
      if (!preserve) {
        setSelectedFile(null);
      }
      try {
        const fetchDirEntries = async (dir: string, depth: number): Promise<SessionFileItem[]> => {
          const encodedDir = encodeURIComponent(dir);
          const resp = await fetch(`/file-api/list-files?dir=${encodedDir}`, { cache: 'no-store' });
          if (!resp.ok) {
            const text = await resp.text();
            throw new Error(`HTTP ${resp.status}: ${text.substring(0, 120)}`);
          }
          const payload = (await resp.json()) as ListFilesResponse;
          const rows = Array.isArray(payload?.files) ? toSessionFiles(payload.files) : [];
          const withDepth = rows.map((item) => ({ ...item, depth }));
          const result: SessionFileItem[] = [];
          for (const item of withDepth) {
            if (containsIgnoredDirectory(item.path)) {
              continue;
            }
            result.push(item);
            if (!item.isDirectory) continue;
            const children = await fetchDirEntries(item.path, depth + 1);
            result.push(...children);
          }
          return result;
        };

        const rootDir = `agent/sessions/${sessionId}`;
        const rows = await fetchDirEntries(rootDir, 0);

        // Check for todo.json in DeepAgent workspace todo directory
        const todoPath = `agent/jiuwenclaw_workspace/todo/${sessionId}/todo.json`;
        try {
          const todoResp = await fetch(`/file-api/file-content?path=${encodeURIComponent(todoPath)}`, { cache: 'no-store' });
          if (todoResp.ok) {
            rows.push({
              name: 'todo.json',
              path: todoPath,
              isMarkdown: false,
              isDirectory: false,
              depth: 0,
            });
          }
        } catch {
          // todo.json not found or error, ignore
        }

        setFiles(rows);
        if (preserve && preservePath) {
          const np = normalizeWorkspacePath(preservePath);
          const match = rows.find(
            (f) => !f.isDirectory && normalizeWorkspacePath(f.path) === np
          );
          if (match) {
            setSelectedFile(match);
            setFilePreviewReloadNonce((n) => n + 1);
          } else {
            const prev = selectedFileRef.current;
            if (prev && normalizeWorkspacePath(prev.path) === np) {
              setFilePreviewReloadNonce((n) => n + 1);
            } else {
              setSelectedFile(null);
            }
          }
        } else {
          // 自动选择第一个可预览的文件
          const firstPreviewableFile = rows.find(
            (f) => !f.isDirectory && isPreviewableSessionFile(f.name)
          );
          if (firstPreviewableFile) {
            setSelectedFile(firstPreviewableFile);
          }
        }
      } catch (error) {
        console.error('Failed to load session files:', error);
        setFiles([]);
        setFilesError(t('sessions.errors.loadFiles'));
        setSelectedFile(null);
      } finally {
        setLoadingFiles(false);
      }
    },
    [isRemoteSessionStorage, t]
  );

  const loadSessions = useCallback(async () => {
    setLoadingSessions(true);
    try {
      const payload = await webRequest<SessionListResponse>('session.list', { limit: 20 });
      const rows = Array.isArray(payload?.sessions) ? toSessionItems(payload.sessions) : [];
      setSessions(rows);
      setSessionsError(null);
      // 必须在 setState 之外同步算出 nextSelected：await 之后的函数式 setState 在 React 18+ 可能延后执行，
      // 若依赖 updater 内对 nextSelected 的赋值，此处仍为 null，会误调 loadSessionFilesForSession(null) 并清空文件列表。
      const nextSelected = pickNextSelectedSessionId(
        selectedSessionIdRef.current,
        rows,
        currentSessionIdRef.current
      );
      setSelectedSessionId(nextSelected);
      const openPath = selectedFileRef.current?.path ?? null;
      const nOpen = openPath ? normalizeWorkspacePath(openPath) : '';
      const sessionPrefix = nextSelected
        ? normalizeWorkspacePath(`agent/sessions/${nextSelected}/`)
        : '';
      const underNext = Boolean(nOpen && sessionPrefix && nOpen.startsWith(sessionPrefix));
      await loadSessionFilesForSession(
        nextSelected,
        underNext ? { preserveSelectionPath: openPath } : undefined
      );
    } catch (error) {
      console.error('Failed to load sessions:', error);
      setSessions([]);
      setSessionsError(t('sessions.errors.loadSessions'));
      setSelectedSessionId(null);
    } finally {
      setLoadingSessions(false);
    }
  }, [t, loadSessionFilesForSession]);

  useEffect(() => {
    void loadSessions();
    // loadSessions 随 currentSessionId / t 更新；此处仅希望在语言切换时与首屏拉列表，避免把 loadSessions 放进 deps 引发多余轮询
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [t]);

  // 聊天处理完成后刷新会话列表，拾取自动生成的标题
  const prevProcessingRef = useRef(false);
  useEffect(() => {
    if (prevProcessingRef.current && !isProcessing) {
      void loadSessions();
    }
    prevProcessingRef.current = isProcessing;
  }, [isProcessing, loadSessions]);

  const handleDeleteSession = async (sessionId: string) => {
    const displayLabel = parseSessionDisplayLabel(sessionId, t);
    const confirmed = window.confirm(t('sessions.deleteConfirm', { session: displayLabel }));
    if (!confirmed) return;

    setDeletingSessionId(sessionId);
    try {
      await webRequest('session.delete', { session_id: sessionId });
      await loadSessions();
      if (selectedSessionId === sessionId) {
        setSelectedFile(null);
      }
    } catch (error) {
      console.error('Failed to delete session:', error);
      setSessionsError(t('sessions.errors.deleteSession', { sessionId }));
    } finally {
      setDeletingSessionId(null);
    }
  };

  const selectedSessionLabel = useMemo(
    () => (selectedSessionId ? parseSessionDisplayLabel(selectedSessionId, t) : t('sessions.noneSelected')),
    [selectedSessionId, t]
  );
  const canRestoreSelectedSession =
    Boolean(selectedSessionId?.startsWith('sess_')) && isConnected && !isProcessing;
  const restoreButtonTitle = !isConnected
    ? t('sessions.restoreDisabledNotConnected')
    : isProcessing
      ? t('sessions.restoreDisabledProcessing')
      : !selectedSessionId?.startsWith('sess_')
        ? t('sessions.restoreDisabledUnsupported')
        : t('sessions.restore');

  return (
    <div className="flex-1 min-h-0">
      <div className="card w-full h-full flex flex-col">
        <div className="flex items-center justify-between gap-4 mb-4">
          <div>
            <h2 className="text-lg font-semibold">{t('sessions.title')}</h2>
            <p className="text-sm text-text-muted mt-1">{t('sessions.subtitle')}</p>
          </div>
          <button
            type="button"
            onClick={() => void loadSessions()}
            disabled={loadingSessions}
            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loadingSessions ? t('common.refreshing') : t('common.refresh')}
          </button>
        </div>

        {sessionsError ? (
          <div className="mb-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
            {sessionsError}
          </div>
        ) : null}

        <div className="flex-1 min-h-0 grid grid-cols-[minmax(0,1fr)_minmax(0,4fr)] gap-4">
          <div className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col min-h-0">
            <div className="px-4 py-3 bg-secondary/30 border-b border-border">
              <div className="flex items-stretch justify-between gap-4">
                <div>
                  <h3 className="text-sm font-medium text-text">{t('sessions.history')}</h3>
                  <p className="text-xs text-text-muted mt-1 mono">
                    {t('sessions.count', { count: sessions.length })}
                  </p>
                </div>
                <button
                  type="button"
                  title={restoreButtonTitle}
                  disabled={!canRestoreSelectedSession}
                  onClick={() => {
                    if (!selectedSessionId || !canRestoreSelectedSession) return;
                    void onRestoreSession(selectedSessionId);
                  }}
                  className="btn !px-3 !py-1.5 shrink-0 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {t('sessions.restore')}
                </button>
              </div>
            </div>
            <div className="flex-1 overflow-auto p-2 space-y-1">
              {!loadingSessions && sessions.length === 0 ? (
                <div className="h-full flex items-center justify-center text-sm text-text-muted">{t('sessions.empty')}</div>
              ) : (
                sessions.map((session) => (
                  <div key={session.session_id} className="group grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2">
                    <button
                      type="button"
                      className={`w-full min-w-0 text-left px-3 py-2 rounded-lg border text-sm transition-colors ${
                        selectedSessionId === session.session_id
                          ? 'border-[var(--border-accent)] bg-accent-subtle text-text'
                          : 'border-transparent hover:bg-secondary/40 text-text-muted hover:text-text'
                      }`}
                      onClick={() => {
                        if (selectedSessionId === session.session_id) return;
                        setSelectedSessionId(session.session_id);
                        void loadSessionFilesForSession(session.session_id);
                      }}
                      title={session.title || parseSessionDisplayLabel(session.session_id, t)}
                    >
                      <span className="truncate block">{session.title || parseSessionDisplayLabel(session.session_id, t)}</span>
                      {session.content ? (
                        <span className="truncate block text-xs text-text-muted mt-1">
                          {session.content}
                        </span>
                      ) : null}
                    </button>
                    <button
                      type="button"
                      title={t('sessions.delete')}
                      className="shrink-0 p-1.5 rounded-md text-text-muted hover:text-danger hover:bg-danger-subtle transition-colors disabled:opacity-50"
                      disabled={deletingSessionId === session.session_id}
                      onClick={() => void handleDeleteSession(session.session_id)}
                    >
                      <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
                      </svg>
                    </button>
                  </div>
                ))
              )}
            </div>
          </div>

          <div className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm grid grid-cols-[minmax(0,1fr)_minmax(0,3fr)] min-h-0">
            <div className="border-r border-border flex flex-col min-h-0">
              <div className="px-4 py-3 bg-secondary/30 border-b border-border">
                <div>
                  <h3 className="text-sm font-medium text-text">{t('sessions.files')}</h3>
                  <p className="text-xs text-text-muted mt-1 truncate" title={selectedSessionLabel}>
                    {selectedSessionLabel}
                  </p>
                </div>
              </div>
              <div className="flex-1 overflow-auto p-2">
                {!selectedSessionId ? (
                  <div className="h-full flex items-center justify-center text-sm text-text-muted">{t('sessions.selectFirst')}</div>
                ) : isRemoteSessionStorage ? (
                  <div className="h-full flex items-center justify-center text-sm text-text-muted">
                    {t('sessions.remoteFilesUnavailable')}
                  </div>
                ) : loadingFiles ? (
                  <div className="h-full flex items-center justify-center text-sm text-text-muted">{t('sessions.loadingFiles')}</div>
                ) : filesError ? (
                  <div className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">{filesError}</div>
                ) : files.length === 0 ? (
                  <div className="h-full flex items-center justify-center text-sm text-text-muted">{t('sessions.emptyFiles')}</div>
                ) : (
                  <div className="space-y-1">
                    {files.map((file) => {
                      const canPreview = !file.isDirectory && isPreviewableSessionFile(file.name);
                      return (
                        <button
                          key={file.path}
                          type="button"
                          className={`w-full text-left px-3 py-2 rounded-lg border text-sm transition-colors ${
                            canPreview &&
                              normalizeWorkspacePath(selectedFile?.path ?? '') ===
                                normalizeWorkspacePath(file.path)
                              ? 'border-[var(--border-accent)] bg-accent-subtle text-text'
                              : 'border-transparent text-text-muted'
                          } ${canPreview ? 'hover:bg-secondary/40 hover:text-text' : 'cursor-default'}`}
                          onClick={() => {
                            if (!canPreview) return;
                            setSelectedFile(file);
                          }}
                        >
                          <span className="flex items-center justify-between gap-2">
                            <span
                              className="truncate block"
                              style={{ paddingLeft: `${file.depth * 16}px` }}
                            >
                              {file.name}
                            </span>
                            {file.isDirectory ? (
                              <span className="text-[10px] px-1.5 py-0.5 rounded border border-border bg-secondary/50 text-text-muted">
                                {t('sessions.folder')}
                              </span>
                            ) : !canPreview ? (
                              <span className="text-[10px] px-1.5 py-0.5 rounded border border-border bg-secondary/50 text-text-muted">
                                {t('sessions.notPreviewable')}
                              </span>
                            ) : null}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
            <div className="flex-1 min-h-0">
              {selectedFile ? (
                <FileViewer
                  filePath={selectedFile.path}
                  fileName={selectedFile.name}
                  reloadNonce={filePreviewReloadNonce}
                />
              ) : (
                <div className="h-full flex items-center justify-center text-text-muted">
                  {t('sessions.selectFile')}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
