import { useCallback, useEffect, useRef, useState } from 'react';
import { getApiBase } from '../utils/env';
import { useChatStore } from '../stores/chatStore';
import { useSessionStore } from '../stores/sessionStore';
import { openSingleAgentPanel } from './singleAgentPanelState';
import { openTeamPanel } from './teamPanelState';
import { isDesktopBrowserHeadless, useDesktopBrowserHeadless } from './desktopBrowserDisplay';
import type { ElectronBrowserState } from '../types/electron';

const DESKTOP_BROWSER_TAB_EVENT = 'jiuwenclaw-desktop-browser-tab-change';
const DESKTOP_BROWSER_TAB_FLAGS_KEY = 'jiuwenclaw_desktop_browser_tab_flags';

const DESKTOP_BROWSER_FILE_EXTENSIONS = new Set(['html', 'htm', 'md', 'markdown']);

/** browser 页签的出现/关闭标志，按会话隔离避免跨会话互相污染。 */
interface DesktopBrowserTabFlags {
  /** A file preview or live browser task requested this session's pane. */
  requested: boolean;
  /** 用户点关闭按钮显式关掉了 browser 页签 */
  closed: boolean;
}

interface DesktopBrowserSessionActivity {
  /** Unknown after a failed read until the first observation establishes a baseline. */
  busy: Set<string> | null;
  /** Distinct busy snapshots received before the initial IPC reply. */
  pending: Set<string>[] | null;
}

const EMPTY_TAB_FLAGS: DesktopBrowserTabFlags = { requested: false, closed: false };

function tabFlagsStorageKey(sessionId: string): string {
  return `${DESKTOP_BROWSER_TAB_FLAGS_KEY}:${sessionId || 'default'}`;
}

function loadDesktopBrowserTabFlags(sessionId: string): DesktopBrowserTabFlags {
  try {
    const raw = window.localStorage.getItem(tabFlagsStorageKey(sessionId));
    if (!raw) return { ...EMPTY_TAB_FLAGS };
    const parsed = JSON.parse(raw) as Partial<DesktopBrowserTabFlags> | null;
    return {
      requested: parsed?.requested === true,
      closed: parsed?.closed === true,
    };
  } catch {
    return { ...EMPTY_TAB_FLAGS };
  }
}

function persistDesktopBrowserTabFlags(sessionId: string, patch: Partial<DesktopBrowserTabFlags>): void {
  const next = { ...loadDesktopBrowserTabFlags(sessionId), ...patch };
  try {
    window.localStorage.setItem(tabFlagsStorageKey(sessionId), JSON.stringify(next));
  } catch {
    // localStorage 不可用时仅当前内存态生效，不影响本次点击。
  }
  window.dispatchEvent(new CustomEvent(DESKTOP_BROWSER_TAB_EVENT));
}

function activeSessionId(): string {
  return useChatStore.getState().activeSessionId ?? '';
}

/**
 * 下载链接带上 inline=1：后端以 Content-Disposition: inline 返回，
 * BrowserView 才会直接渲染 html/md 而不是触发保存。
 */
function buildInlineFileUrl(downloadUrl: string | null | undefined): string | null {
  const trimmed = downloadUrl?.trim();
  if (!trimmed) return null;
  try {
    const url = new URL(trimmed, getApiBase() || window.location.href);
    url.searchParams.set('inline', '1');
    return url.toString();
  } catch {
    return null;
  }
}

function openDesktopBrowserPanel(): void {
  const mode = useSessionStore.getState().runtimes[activeSessionId()]?.mode ?? 'agent';
  if (mode === 'team' || mode === 'auto_harness') {
    openTeamPanel('browser');
    return;
  }
  openSingleAgentPanel('browser');
}

/** 桌面内置浏览器可打开文件的最小结构（FileDownloadItem / ArtifactItem 均满足）。 */
export interface DesktopBrowserOpenableFile {
  name: string;
  download_url?: string | null;
}

/**
 * Electron 内置浏览器打开聊天文件（.html/.md）。
 * 命中时导航 BrowserView 并切到 browser 页签，返回 true；非 Electron、
 * 不可预览或缺少下载链接时返回 false，调用方继续走产物面板预览。
 */
export function openFileInDesktopBrowser(file: DesktopBrowserOpenableFile): boolean {
  if (!window.jiuwenDesktop) return false;
  if (isDesktopBrowserHeadless()) return false;
  if (!isDesktopBrowserPreviewableFile(file.name)) return false;
  const url = buildInlineFileUrl(file.download_url);
  if (!url) return false;
  const sessionId = activeSessionId();
  persistDesktopBrowserTabFlags(sessionId, { requested: true, closed: false });
  void window.jiuwenDesktop.browser.navigate(url, sessionId).catch((error) => {
    console.warn('[desktop.browser] file open failed:', error);
  });
  openDesktopBrowserPanel();
  return true;
}

/** 关闭 browser 页签：页签隐藏、DesktopBrowserPane 卸载时自动隐藏 BrowserView。 */
export function closeDesktopBrowserTab(): void {
  persistDesktopBrowserTabFlags(activeSessionId(), { closed: true });
}

/** Reopen on a new MCP task binding, not on retained subagent history or navigation. */
export function useDesktopBrowserAutoOpen(sessionId: string, onOpen: () => void): void {
  const desktop = window.jiuwenDesktop;
  const headless = useDesktopBrowserHeadless();
  const current = useRef({ sessionId, onOpen });
  current.current = { sessionId, onOpen };
  const sessions = useRef(new Map<string, DesktopBrowserSessionActivity>());
  const pendingSessions = useRef(new Map<string, Set<string>>());

  const revealPending = useCallback(() => {
    const currentSession = current.current.sessionId;
    if (isDesktopBrowserHeadless() || !pendingSessions.current.has(currentSession)) return;
    pendingSessions.current.delete(currentSession);
    persistDesktopBrowserTabFlags(currentSession, { requested: true, closed: false });
    current.current.onOpen();
  }, []);

  const observe = useCallback((owner: string, nextBusy: Set<string>) => {
    const state = sessions.current.get(owner);
    if (!state) return;
    if (state.pending) {
      const previous = state.pending.at(-1);
      // Navigation/title updates do not add observations while the IPC read is pending.
      if (!previous || previous.size !== nextBusy.size || [...nextBusy].some((id) => !previous.has(id))) {
        state.pending.push(nextBusy);
      }
      return;
    }
    const previous = state.busy;
    const pending = pendingSessions.current.get(owner) ?? new Set<string>();
    if (previous) {
      for (const id of nextBusy) {
        if (!previous.has(id)) pending.add(id);
      }
    }
    for (const id of pending) {
      if (!nextBusy.has(id)) pending.delete(id);
    }
    if (pending.size) pendingSessions.current.set(owner, pending);
    else pendingSessions.current.delete(owner);
    state.busy = nextBusy;
  }, []);

  const ensureBaseline = useCallback((owner: string) => {
    if (!desktop?.isElectron || sessions.current.has(owner)) return;
    const state: DesktopBrowserSessionActivity = { busy: null, pending: [] };
    sessions.current.set(owner, state);
    const finish = (panels?: ElectronBrowserState[]) => {
      if (sessions.current.get(owner) !== state || !state.pending) return;
      if (panels) {
        state.busy = new Set(panels.filter((panel) => panel.sessionId === owner && panel.busy)
          .map((panel) => panel.panelId));
      }
      const pending = state.pending ?? [];
      state.pending = null;
      for (const nextBusy of pending) observe(owner, nextBusy);
      // Reveal only the final live state, never a task that already finished during the read.
      revealPending();
    };
    void desktop.browser.listPanels(owner).then(finish).catch((error) => {
      if (sessions.current.get(owner) !== state) return;
      console.warn('[desktop.browser] activity baseline failed:', error);
      // With no snapshot, the first observed state is a baseline, not a new task.
      finish();
    });
  }, [desktop, observe, revealPending]);

  useEffect(() => {
    if (!desktop?.isElectron) return;
    const acknowledgeClose = () => {
      const owner = activeSessionId();
      if (!loadDesktopBrowserTabFlags(owner).closed) return;
      pendingSessions.current.delete(owner);
      const state = sessions.current.get(owner);
      const latest = state?.pending?.at(-1);
      if (state && latest) {
        // A manual close acknowledges events already seen, even before the IPC reply.
        state.busy = latest;
        state.pending = null;
      }
    };
    window.addEventListener(DESKTOP_BROWSER_TAB_EVENT, acknowledgeClose);
    const unsubscribe = desktop.browser.onPanelsChanged((panels) => {
      const bySession = new Map<string, Set<string>>();
      for (const owner of sessions.current.keys()) bySession.set(owner, new Set());
      for (const panel of panels) {
        let busy = bySession.get(panel.sessionId);
        if (!busy) {
          busy = new Set();
          bySession.set(panel.sessionId, busy);
        }
        if (panel.busy) busy.add(panel.panelId);
      }
      for (const [owner, busy] of bySession) {
        ensureBaseline(owner);
        observe(owner, busy);
      }
      revealPending();
    });
    return () => {
      unsubscribe();
      window.removeEventListener(DESKTOP_BROWSER_TAB_EVENT, acknowledgeClose);
      sessions.current.clear();
      pendingSessions.current.clear();
    };
  }, [desktop, ensureBaseline, observe, revealPending]);

  useEffect(() => {
    ensureBaseline(sessionId);
  }, [sessionId, ensureBaseline]);

  useEffect(() => {
    revealPending();
  }, [headless, sessionId, revealPending]);
}

/**
 * Persist visibility across ToolPanel remounts. An explicit close hides the
 * tab until a new headed task or file preview requests it again.
 */
export function useDesktopBrowserTabFlags(sessionId: string): DesktopBrowserTabFlags {
  const [flags, setFlags] = useState<DesktopBrowserTabFlags>(() => loadDesktopBrowserTabFlags(sessionId));
  useEffect(() => {
    setFlags(loadDesktopBrowserTabFlags(sessionId));
    const handler = () => setFlags(loadDesktopBrowserTabFlags(sessionId));
    window.addEventListener(DESKTOP_BROWSER_TAB_EVENT, handler);
    return () => window.removeEventListener(DESKTOP_BROWSER_TAB_EVENT, handler);
  }, [sessionId]);
  return flags;
}

function isDesktopBrowserPreviewableFile(fileName: string): boolean {
  const extension = fileName.split('.').pop()?.toLowerCase() ?? '';
  return DESKTOP_BROWSER_FILE_EXTENSIONS.has(extension);
}
