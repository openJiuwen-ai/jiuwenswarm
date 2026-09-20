import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { WebConnectionState, WebError, WsEvent } from '../../types';
import { gitClient } from './gitClient';
import { gitWatchClient } from './gitWatchClient';
import type { GitDiffDetailWatchResponse, GitDiffFile, GitDiffFilesWatchResponse, GitDiffWatchResponse, GitDiffWatchSnapshot } from './types';

interface UseCodeGitDiffWatchOptions {
  projectId: string | null;
  sessionId: string | null;
  enabled: boolean;
}

export interface CodeGitDiffWatchController {
  connectionState: WebConnectionState;
  summary: GitDiffWatchSnapshot | null;
  summaryLoading: boolean;
  summaryError: string | null;
  files: Record<string, GitDiffFile>;
  filesReady: boolean;
  filesLoading: boolean;
  filesError: string | null;
  detailFiles: Record<string, GitDiffFile | null>;
  detailLoading: boolean;
  detailError: string | null;
  setFilesEnabled: (enabled: boolean) => void;
  setDetailPaths: (paths: string[]) => void;
  refresh: () => void;
}

function errorMessage(error: unknown, fallback: string): string {
  return (error as WebError)?.message || fallback;
}

function eventPayload(event: WsEvent): Record<string, unknown> {
  return event.payload && typeof event.payload === 'object' ? event.payload : {};
}

function revisionTimestamp(revision: string): number | null {
  const timestamp = Number(revision.split(':')[1]);
  return Number.isFinite(timestamp) ? timestamp : null;
}

function acceptRevision(ref: { current: string | null }, revision: unknown): boolean {
  if (typeof revision !== 'string') return true;
  const previous = ref.current;
  if (previous) {
    const previousTimestamp = revisionTimestamp(previous);
    const nextTimestamp = revisionTimestamp(revision);
    if (previousTimestamp !== null && nextTimestamp !== null && nextTimestamp < previousTimestamp) return false;
  }
  ref.current = revision;
  return true;
}

export function useCodeGitDiffWatch({ projectId, sessionId, enabled }: UseCodeGitDiffWatchOptions): CodeGitDiffWatchController {
  const [connectionState, setConnectionState] = useState<WebConnectionState>(gitWatchClient.getState());
  const [summary, setSummary] = useState<GitDiffWatchSnapshot | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [files, setFiles] = useState<Record<string, GitDiffFile>>({});
  const [filesReady, setFilesReady] = useState(false);
  const [filesLoading, setFilesLoading] = useState(false);
  const [filesError, setFilesError] = useState<string | null>(null);
  const [detailFiles, setDetailFiles] = useState<Record<string, GitDiffFile | null>>({});
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [filesEnabled, setFilesEnabledState] = useState(false);
  const [detailPaths, setDetailPathsState] = useState<string[]>([]);
  const [refreshSequence, setRefreshSequence] = useState(0);
  const [watchId, setWatchId] = useState<string | null>(null);
  const watchIdRef = useRef<string | null>(null);
  const lifecycleSequenceRef = useRef(0);
  const filesRequestSequenceRef = useRef(0);
  const detailRequestSequenceRef = useRef(0);
  const summaryRevisionRef = useRef<string | null>(null);
  const filesRevisionRef = useRef<string | null>(null);
  const detailRevisionRef = useRef<string | null>(null);
  const detailPathsRef = useRef<string[]>([]);

  const setFilesEnabled = useCallback((nextEnabled: boolean) => {
    setFilesEnabledState(nextEnabled);
    if (!nextEnabled) setDetailPathsState([]);
  }, []);

  const setDetailPaths = useCallback((paths: string[]) => {
    const normalized = [...new Set(paths.filter(Boolean))].sort();
    detailPathsRef.current = normalized;
    setDetailPathsState(previous => {
      if (previous.length === normalized.length && previous.every((path, index) => path === normalized[index])) {
        return previous;
      }
      return normalized;
    });
  }, []);

  const refresh = useCallback(() => setRefreshSequence(sequence => sequence + 1), []);

  useEffect(() => {
    const lifecycleSequence = lifecycleSequenceRef.current + 1;
    lifecycleSequenceRef.current = lifecycleSequence;
    watchIdRef.current = null;
    setWatchId(null);
    setSummary(null);
    setSummaryError(null);
    setFiles({});
    setFilesReady(false);
    setFilesError(null);
    setDetailFiles({});
    setDetailError(null);
    summaryRevisionRef.current = null;
    filesRevisionRef.current = null;
    detailRevisionRef.current = null;
    detailPathsRef.current = [];

    if (!enabled || !projectId || !sessionId || sessionId === 'new') {
      setSummaryLoading(false);
      return;
    }

    let disposed = false;

    const loadSummaryFallback = async (requestSequence: number) => {
      try {
        const status = await gitClient.diffStatus(projectId, sessionId);
        if (disposed || lifecycleSequenceRef.current !== requestSequence) return;
        setSummary({
          project_id: status.project_id,
          session_id: status.session_id,
          repo: status.repo,
          current: status.current,
          last_turn: null,
          revision: `fallback:${status.generated_at}`,
        });
      } catch (fallbackError) {
        console.warn('[code-mode] Failed to load Git diff fallback', fallbackError);
      }
    };

    const startSummaryWatch = async () => {
      const requestSequence = lifecycleSequenceRef.current + 1;
      lifecycleSequenceRef.current = requestSequence;
      const previousWatchId = watchIdRef.current;
      watchIdRef.current = null;
      setWatchId(null);
      setSummaryLoading(true);
      setSummaryError(null);
      if (previousWatchId) {
        void gitWatchClient
          .request('project.git.diff_unwatch', {
            watch_id: previousWatchId,
            scope: 'all',
          })
          .catch(() => undefined);
      }
      try {
        const response = await gitWatchClient.request<GitDiffWatchResponse>('project.git.diff_watch', {
          project_id: projectId,
          session_id: sessionId,
          scope: 'summary',
          include_last_turn: false,
        });
        if (disposed || lifecycleSequenceRef.current !== requestSequence) return;
        watchIdRef.current = response.watch_id;
        setWatchId(response.watch_id);
        summaryRevisionRef.current = response.snapshot.revision;
        setSummary(response.snapshot);
      } catch (error) {
        if (disposed || lifecycleSequenceRef.current !== requestSequence) return;
        setSummaryError(errorMessage(error, '加载 Git 变更统计失败'));
        await loadSummaryFallback(requestSequence);
      } finally {
        if (!disposed && lifecycleSequenceRef.current === requestSequence) setSummaryLoading(false);
      }
    };

    const matchesCurrentWatch = (payload: Record<string, unknown>) =>
      typeof payload.watch_id === 'string' && payload.watch_id === watchIdRef.current && payload.project_id === projectId && payload.session_id === sessionId;

    const removeSummaryEvent = gitWatchClient.on('project.git.diff_changed', event => {
      const payload = eventPayload(event);
      if (!matchesCurrentWatch(payload)) return;
      if (!acceptRevision(summaryRevisionRef, payload.revision)) return;
      setSummary(previous =>
        previous
          ? {
              ...previous,
              repo: payload.repo && typeof payload.repo === 'object' ? (payload.repo as GitDiffWatchSnapshot['repo']) : previous.repo,
              current: (payload.current ?? null) as GitDiffWatchSnapshot['current'],
              revision: typeof payload.revision === 'string' ? payload.revision : previous.revision,
            }
          : previous
      );
    });

    const removeFilesEvent = gitWatchClient.on('project.git.diff_files_changed', event => {
      const payload = eventPayload(event);
      if (!matchesCurrentWatch(payload) || payload.source !== 'current') return;
      if (!acceptRevision(filesRevisionRef, payload.revision)) return;
      setFiles((payload.files ?? {}) as Record<string, GitDiffFile>);
      setFilesReady(true);
      setFilesError(null);
    });

    const removeDetailEvent = gitWatchClient.on('project.git.diff_detail_changed', event => {
      const payload = eventPayload(event);
      if (!matchesCurrentWatch(payload) || payload.source !== 'current') return;
      if (!acceptRevision(detailRevisionRef, payload.revision)) return;
      const changedFiles = (payload.files ?? {}) as Record<string, GitDiffFile | null>;
      const subscribedPaths = new Set(detailPathsRef.current);
      setDetailFiles(previous => {
        const next = Object.fromEntries(Object.entries(previous).filter(([path]) => subscribedPaths.has(path)));
        Object.entries(changedFiles).forEach(([path, file]) => {
          if (subscribedPaths.has(path)) next[path] = file;
        });
        return next;
      });
      setDetailError(null);
    });

    const removeStateHandler = gitWatchClient.onStateChange(state => {
      setConnectionState(state);
      if (state === 'ready' && !disposed) void startSummaryWatch();
    });

    if (gitWatchClient.getState() === 'ready') {
      void startSummaryWatch();
    } else {
      setSummaryLoading(true);
      void gitWatchClient.connect().catch(error => {
        if (disposed) return;
        setSummaryLoading(false);
        setSummaryError(errorMessage(error, '连接 Git 变更服务失败'));
        void loadSummaryFallback(lifecycleSequenceRef.current);
      });
    }

    return () => {
      disposed = true;
      lifecycleSequenceRef.current += 1;
      removeSummaryEvent();
      removeFilesEvent();
      removeDetailEvent();
      removeStateHandler();
      const currentWatchId = watchIdRef.current;
      watchIdRef.current = null;
      if (currentWatchId && gitWatchClient.getState() === 'ready') {
        void gitWatchClient
          .request('project.git.diff_unwatch', {
            watch_id: currentWatchId,
            scope: 'all',
          })
          .catch(() => undefined);
      }
    };
  }, [enabled, projectId, sessionId]);

  useEffect(() => () => gitWatchClient.disconnect(), []);

  useEffect(() => {
    if (refreshSequence === 0 || !enabled || !projectId || !sessionId || sessionId === 'new') return;
    let disposed = false;
    setSummaryLoading(true);
    setSummaryError(null);
    void gitClient
      .diffStatus(projectId, sessionId)
      .then(status => {
        if (disposed) return;
        setSummary(previous => ({
          project_id: status.project_id,
          session_id: status.session_id,
          repo: status.repo,
          current: status.current,
          last_turn: null,
          revision: previous?.revision ?? `fallback:${status.generated_at}`,
        }));
      })
      .catch(error => {
        if (disposed) return;
        setSummaryError(errorMessage(error, '刷新 Git 变更统计失败'));
      })
      .finally(() => {
        if (!disposed) setSummaryLoading(false);
      });
    if (gitWatchClient.getState() !== 'ready') {
      void gitWatchClient.connect().catch(() => undefined);
    }
    return () => {
      disposed = true;
    };
  }, [enabled, projectId, refreshSequence, sessionId]);

  useEffect(() => {
    const requestSequence = filesRequestSequenceRef.current + 1;
    filesRequestSequenceRef.current = requestSequence;
    if (!projectId || !filesEnabled) {
      setFiles({});
      setFilesReady(false);
      setFilesLoading(false);
      setFilesError(null);
      if (watchId && !filesEnabled) {
        void gitWatchClient.request('project.git.diff_unwatch', { watch_id: watchId, scope: 'detail' }).catch(() => undefined);
        void gitWatchClient.request('project.git.diff_unwatch', { watch_id: watchId, scope: 'files' }).catch(() => undefined);
      }
      return;
    }

    if (!watchId || connectionState !== 'ready') {
      setFilesLoading(true);
      return;
    }

    setFilesLoading(true);
    setFilesError(null);
    void gitWatchClient
      .request<GitDiffFilesWatchResponse>('project.git.diff_files_watch', {
        project_id: projectId,
        watch_id: watchId,
        source: 'current',
      })
      .then(response => {
        if (filesRequestSequenceRef.current !== requestSequence) return;
        filesRevisionRef.current = response.revision;
        setFiles(response.files);
        setFilesReady(true);
      })
      .catch(error => {
        if (filesRequestSequenceRef.current !== requestSequence) return;
        setFilesError(errorMessage(error, '加载工作区文件列表失败'));
      })
      .finally(() => {
        if (filesRequestSequenceRef.current === requestSequence) setFilesLoading(false);
      });
  }, [connectionState, filesEnabled, projectId, refreshSequence, watchId]);

  const detailPathKey = useMemo(() => detailPaths.join('\n'), [detailPaths]);

  useEffect(() => {
    const requestSequence = detailRequestSequenceRef.current + 1;
    detailRequestSequenceRef.current = requestSequence;
    if (!projectId || !filesEnabled || detailPaths.length === 0) {
      // 取消详情订阅时不能只停止服务端推送：大 diff 仍会被此状态引用。
      setDetailFiles({});
      detailRevisionRef.current = null;
      setDetailLoading(false);
      setDetailError(null);
      if (watchId) {
        void gitWatchClient.request('project.git.diff_unwatch', { watch_id: watchId, scope: 'detail' }).catch(() => undefined);
      }
      return;
    }

    if (!watchId || connectionState !== 'ready') {
      setDetailLoading(true);
      return;
    }

    // 切换文件时立即解除对上一份预览内容的引用，避免等待网络响应期间累积。
    setDetailFiles({});
    detailRevisionRef.current = null;
    setDetailLoading(true);
    setDetailError(null);
    void gitWatchClient
      .request<GitDiffDetailWatchResponse>('project.git.diff_detail_watch', {
        project_id: projectId,
        watch_id: watchId,
        source: 'current',
        files: detailPaths,
      })
      .then(response => {
        if (detailRequestSequenceRef.current !== requestSequence) return;
        detailRevisionRef.current = response.revision;
        setDetailFiles(response.files);
      })
      .catch(error => {
        if (detailRequestSequenceRef.current !== requestSequence) return;
        setDetailError(errorMessage(error, '加载文件差异详情失败'));
      })
      .finally(() => {
        if (detailRequestSequenceRef.current === requestSequence) setDetailLoading(false);
      });
  }, [connectionState, detailPathKey, detailPaths, filesEnabled, projectId, refreshSequence, watchId]);

  return {
    connectionState,
    summary,
    summaryLoading,
    summaryError,
    files,
    filesReady,
    filesLoading,
    filesError,
    detailFiles,
    detailLoading,
    detailError,
    setFilesEnabled,
    setDetailPaths,
    refresh,
  };
}
