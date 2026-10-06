import { useEffect, useState } from 'react';
import { getApiBase } from '../utils/env';
import { useChatStore } from '../stores/chatStore';
import { useSessionStore } from '../stores/sessionStore';
import { openSingleAgentPanel } from './singleAgentPanelState';
import { openTeamPanel } from './teamPanelState';

const DESKTOP_BROWSER_TAB_EVENT = 'jiuwenclaw-desktop-browser-tab-change';
const DESKTOP_BROWSER_TAB_FLAGS_KEY = 'jiuwenclaw_desktop_browser_tab_flags';

const DESKTOP_BROWSER_FILE_EXTENSIONS = new Set(['html', 'htm', 'md', 'markdown']);

/** browser 页签的出现/关闭标志，按会话隔离避免跨会话互相污染。 */
interface DesktopBrowserTabFlags {
  /** 本会话用内置浏览器打开过聊天文件 */
  requested: boolean;
  /** 用户点关闭按钮显式关掉了 browser 页签 */
  closed: boolean;
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

/**
 * browser 页签可见性标志：文件打开请求过即保持可见，显式关闭后隐藏；
 * 状态持久化，ToolPanel 卸载重建（面板隐藏/切换会话）后仍能恢复。
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
