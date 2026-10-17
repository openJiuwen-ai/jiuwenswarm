import { useCallback, useEffect, useRef, useState } from 'react';
import { webClient } from '../../../../services/webClient';
import {
  archivedTaskClient,
  type ArchivedListParams,
  type ArchivedListResponse,
  type ArchivedSession,
} from '../../../../features/workspace/archivedTaskClient';
import {
  mergeArchivedPageItems,
  normalizeArchivedListResponse,
} from '../../../../features/workspace/archivedTaskGrouping';
import type { WorkMode } from '../../../../features/workspace/projectTypes';

const PAGE_SIZE = 20;
const EVENT_REFRESH_DEBOUNCE_MS = 300;

/**
 * 归档相关的 WebSocket 事件：仅用于同步刷新，不替代请求结果。
 * `project.removed` / `project.restored` 必须订阅：项目可见性变化会改写
 * 归档项的 `project_hidden` 标记与分组说明，本页要跟着刷新。
 */
const ARCHIVE_EVENT_NAMES = [
  'session.archived',
  'session.unarchived',
  'session.deleted',
  'project.removed',
  'project.restored',
] as const;

export interface ResourceListState<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  hasMore: boolean;
  loading: boolean;
  loadingMore: boolean;
  error: string | null;
}

function createInitialListState<T>(): ResourceListState<T> {
  return {
    items: [],
    total: 0,
    limit: PAGE_SIZE,
    offset: 0,
    hasMore: false,
    loading: false,
    loadingMore: false,
    error: null,
  };
}

function startLoading<T>(prev: ResourceListState<T>, mode: 'replace' | 'more'): ResourceListState<T> {
  return mode === 'replace'
    ? { ...prev, loading: true, loadingMore: false, error: null }
    : { ...prev, loadingMore: true, error: null };
}

function applyLoadedPage<T>(
  prev: ResourceListState<T>,
  page: ArchivedListResponse<T>,
  mode: 'replace' | 'more',
  getId: (item: T) => string,
): ResourceListState<T> {
  return {
    items: mode === 'more' ? mergeArchivedPageItems(prev.items, page.items, getId) : page.items,
    total: page.total,
    limit: page.limit || PAGE_SIZE,
    offset: page.offset,
    hasMore: page.has_more,
    loading: false,
    loadingMore: false,
    error: null,
  };
}

function pageLoadFailed<T>(prev: ResourceListState<T>): ResourceListState<T> {
  return { ...prev, loading: false, loadingMore: false, error: 'loadFailed' };
}

/**
 * 已归档任务页的数据层：归档会话列表的请求、分页合并、竞态作废、
 * 归档事件订阅与本地行移除。搜索词/工作模式/连接状态变化时自动重拉。
 */
export function useArchivedTaskLists({ isConnected, keyword, workMode }: {
  isConnected: boolean;
  keyword: string;
  workMode: WorkMode;
}) {
  const [sessionsState, setSessionsState] = useState<ResourceListState<ArchivedSession>>(
    createInitialListState<ArchivedSession>,
  );

  // 竞态防护：代际计数让旧请求（搜索词/工作模式已变化、页面已卸载）的结果直接作废。
  const generationRef = useRef(0);
  const mountedRef = useRef(true);
  const sessionsStateRef = useRef(sessionsState);
  sessionsStateRef.current = sessionsState;
  const keywordRef = useRef(keyword);
  keywordRef.current = keyword;
  const workModeRef = useRef(workMode);
  workModeRef.current = workMode;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const listParams = useCallback((offset: number): ArchivedListParams => ({
    work_mode: workModeRef.current,
    ...(keywordRef.current ? { keyword: keywordRef.current } : {}),
    limit: PAGE_SIZE,
    offset,
  }), []);

  /** 分页加载：拉取→代际校验→写回。 */
  const fetchResource = useCallback((mode: 'replace' | 'more') => {
    const generation = ++generationRef.current;
    const currentItems = sessionsStateRef.current.items;
    const request = archivedTaskClient.listArchivedSessions(listParams(mode === 'more' ? currentItems.length : 0));
    setSessionsState((prev) => startLoading(prev, mode));
    request.then((payload) => {
      if (!mountedRef.current || generation !== generationRef.current) return;
      const page = normalizeArchivedListResponse<ArchivedSession>(payload, 'sessions');
      setSessionsState((prev) => applyLoadedPage(prev, page, mode, (item) => item.session_id));
    }).catch(() => {
      if (!mountedRef.current || generation !== generationRef.current) return;
      setSessionsState(pageLoadFailed);
    });
  }, [listParams]);

  const refreshLists = useCallback(() => {
    fetchResource('replace');
  }, [fetchResource]);

  // 页面进入、搜索词/工作模式/连接状态变化时刷新列表。
  useEffect(() => {
    if (!isConnected) return;
    fetchResource('replace');
  }, [isConnected, keyword, workMode, fetchResource]);

  // 归档事件到达后按当前条件幂等刷新（去抖合并可能成串到达的事件）。
  useEffect(() => {
    let timerId: number | null = null;
    const scheduleRefresh = () => {
      if (timerId !== null) window.clearTimeout(timerId);
      timerId = window.setTimeout(() => {
        timerId = null;
        fetchResource('replace');
      }, EVENT_REFRESH_DEBOUNCE_MS);
    };
    const unsubscribes = ARCHIVE_EVENT_NAMES.map((eventName) => webClient.on(eventName, scheduleRefresh));
    return () => {
      unsubscribes.forEach((unsubscribe) => unsubscribe());
      if (timerId !== null) window.clearTimeout(timerId);
    };
  }, [fetchResource]);

  const removeLocalSession = useCallback((sessionId: string) => {
    setSessionsState((prev) => ({
      ...prev,
      items: prev.items.filter((item) => item.session_id !== sessionId),
      total: Math.max(0, prev.total - 1),
    }));
  }, []);

  return {
    sessionsState,
    fetchResource,
    refreshLists,
    removeLocalSession,
  };
}
