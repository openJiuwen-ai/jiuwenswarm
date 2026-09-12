/**
 * JiuwenSwarm background service worker — entry point.
 *
 * Responsibilities:
 * 1. Maintain WebSocket connection to local JiuwenSwarm server (gateway
 *    JSON-RPC protocol, channel_id "browser")
 * 2. Manage research sessions (list, create, switch)
 * 3. Cache page context from content scripts
 * 4. Handle right-click context menus
 * 5. Watch tab lifecycle (context refresh, eviction)
 * 6. Route messages between side panel ↔ server
 * 7. Dispatch browser-native agent tool calls (ToolDispatcher)
 */

import { createLogger } from "@shared/logger";
import { MSG, MAX_CONTEXT_CHARS, COMMANDS, MAX_PINNED_PAGES } from "@shared/constants";
import { addPinnedPage, getPinnedPagesBySession, removePinnedPage } from "@shared/storage";
import { normalizeUrl } from "@shared/url";
import { SidePanelRequest, SidePanelAction } from "@shared/messages";
import { PinnedPage, PageContext } from "@shared/types";
import { GW_METHOD } from "@shared/protocol";
import { nanoid } from "nanoid";

import { WsClient } from "./WsClient";
import { SessionManager } from "./SessionManager";
import { ContextCache } from "./ContextCache";
import { TabWatcher } from "./TabWatcher";
import { ContextMenu } from "./ContextMenu";
import { ToolDispatcher } from "./ToolDispatcher";
import { openPanel } from "./PanelManager";

const log = createLogger("bg");

// ---------------------------------------------------------------------------
// Singletons
// ---------------------------------------------------------------------------

const client = new WsClient();
const cache = new ContextCache();
const sessionMgr = new SessionManager(client);
const tabWatcher = new TabWatcher(cache);
const contextMenu = new ContextMenu(onContextMenuAction, isUrlPinned);
const toolDispatcher = new ToolDispatcher(client, cache, tabWatcher, sessionMgr);

// Surface agent tool activity to the side panel (tool-action visibility).
toolDispatcher.onTool = (tool) => {
  broadcastToSidePanel({ action: "tool", tool });
};

// Push connection state to the side panel so the chat can enable/disable its
// input live.
client.onStatusChange((connected) => {
  broadcastToSidePanel({
    action: MSG.STATUS,
    connected,
    activeSessionId: sessionMgr.activeSessionId,
  });
});

// Whenever the session list / active pointer changes, keep the side panel's
// picker and header in sync.
sessionMgr.onChange((sessions, activeId) => {
  broadcastToSidePanel({ action: "sessions", sessions, activeId });
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function init(): Promise<void> {
  log.info("service worker starting");
  await sessionMgr.init();
  tabWatcher.start();
  contextMenu.setup();
  await client.connect();
  client.onEvent((env) => {
    if (env.type === "ack") {
      // The server auto-creates a session per connection — adopt it and pull
      // the full session list.
      const sid = env.payload.session_id as string | undefined;
      if (sid) sessionMgr.setSessionFromAck(sid, env.payload.mode as string | undefined);
      sessionMgr.refresh().catch(() => {});
      return;
    }
    if (env.type === "tool_call") {
      toolDispatcher.dispatch(env, env.session_id).catch((e) =>
        log.error("tool dispatch error", e),
      );
      return;
    }
    // Stream / status envelopes flow straight through to the side panel.
    broadcastToSidePanel(env);
  });
  log.info("init complete");
}

// Kick off — SW restarts invoke the module from scratch
init().catch((e) => log.error("init failed", e));

// ---------------------------------------------------------------------------
// Side panel port management
// ---------------------------------------------------------------------------

const sidePanelPorts: Set<chrome.runtime.Port> = new Set();

// A context-menu action that arrived while the side panel was not yet loaded.
// The panel pulls it once it connects (see GET_PENDING_ACTION), so actions like
// "Summarize this page" / "Ask about selection" are not lost to a race.
let _pendingAction: { action: string; tabId?: number | undefined; text?: string } | null = null;

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "sidepanel") return;
  sidePanelPorts.add(port);
  log.debug("side panel connected, total:", sidePanelPorts.size);

  // Deliver any queued context-menu action now that the panel is ready.
  if (_pendingAction) {
    port.postMessage(_pendingAction);
    _pendingAction = null;
  }

  port.onMessage.addListener((msg: Record<string, unknown>) => {
    // The port delivers untyped JSON; the registry types it as SidePanelRequest.
    handleSidePanelMsg(msg as SidePanelRequest, port);
  });

  port.onDisconnect.addListener(() => {
    sidePanelPorts.delete(port);
    log.debug("side panel disconnected, remaining:", sidePanelPorts.size);
  });
});

function broadcastToSidePanel(msg: unknown): void {
  for (const port of sidePanelPorts) {
    try {
      port.postMessage(msg);
    } catch {
      sidePanelPorts.delete(port);
    }
  }
}

/** Show the active session's pinned-page count on the toolbar badge. */
async function updatePinBadge(): Promise<void> {
  const activeId = sessionMgr.activeSessionId;
  if (!activeId) {
    await chrome.action.setBadgeText({ text: "" });
    return;
  }
  const pages = await getPinnedPagesBySession(activeId);
  await chrome.action.setBadgeText({ text: pages.length > 0 ? String(pages.length) : "" });
  await chrome.action.setBadgeBackgroundColor({ color: "#2563eb" });
}

/** Extract + persist a tab as a pinned page in the session. Returns the page or null. */
async function pinTabToSession(
  tabId: number,
  sessionId: string
): Promise<PinnedPage | null> {
  let ctx = cache.get(tabId);
  if (!ctx) {
    ctx = await tabWatcher.extractFromTab(tabId) ?? undefined;
    if (ctx) cache.set(tabId, ctx);
  }
  if (!ctx) return null;
  const pinned: PinnedPage = {
    id: nanoid(),
    tabId,
    sessionId,
    context: ctx,
    note: "",
    pinnedAt: new Date().toISOString(),
  };
  await addPinnedPage(pinned);
  return pinned;
}

// ---------------------------------------------------------------------------
// Messages from side panel / popup
// ---------------------------------------------------------------------------

type SidePanelHandler = (
  req: SidePanelRequest,
  port: chrome.runtime.Port
) => Promise<void> | void;

/** Registry of side-panel message handlers keyed by action. */
const sidePanelHandlers: Partial<Record<SidePanelAction, SidePanelHandler>> = {
  [MSG.SEND_AGENT]: handleSendAgent,
  [MSG.PIN_TAB]: handlePinTab,
  [MSG.PIN_TABS]: handlePinTabs,
  [MSG.UNPIN_TAB]: handleUnpinTab,
  [MSG.RECONNECT]: handleReconnect,
  [MSG.LIST_SESSIONS]: handleListSessions,
  [MSG.GET_PENDING_ACTION]: handleGetPendingAction,
  [MSG.NEW_SESSION]: handleNewSession,
  [MSG.SET_SESSION]: handleSetSession,
  [MSG.GET_STATUS]: handleGetStatus,
  [MSG.RENAME_SESSION]: handleRenameSession,
};

async function handleSidePanelMsg(
  msg: SidePanelRequest,
  port: chrome.runtime.Port
): Promise<void> {
  const handler = sidePanelHandlers[msg.action];
  if (handler) await handler(msg, port);
  else log.warn("unknown action from side panel", msg.action);
}

async function handleSendAgent(
  msg: SidePanelRequest,
  port: chrome.runtime.Port
): Promise<void> {
  if (msg.action !== MSG.SEND_AGENT) return;
  const { message, tabId } = msg;
  const sessionId = sessionMgr.activeSessionId;
  if (!sessionId) {
    port.postMessage({ type: "error", payload: { message: "No active session" } });
    return;
  }
  const pinnedPages = await getPinnedPagesBySession(sessionId);
  const tabIds = pinnedPages.map((p) => p.tabId);
  // Include a page's context so actions like "summarize this page" /
  // "ask selection" give the agent the page content even when it is not
  // pinned. Prefer the tab the action originated from (tabId); fall
  // back to the active tab of the last focused window.
  const [activeTab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  const contextTabId = tabId ?? activeTab?.id;
  if (contextTabId != null && !tabIds.includes(contextTabId)) {
    const ctx = await tabWatcher.extractFromTab(contextTabId);
    log.debug("handleSendAgent: extracted tab", contextTabId, "ctx?", !!ctx);
    if (ctx) cache.set(contextTabId, ctx);
    tabIds.push(contextTabId);
  }
  const context = cache.aggregate(tabIds, MAX_CONTEXT_CHARS);
  log.debug("handleSendAgent: context chars =", context.length, "tabIds =", tabIds);
  // The gateway builds the agent prompt from `content`/`query` only — a
  // separate `context` param is silently ignored. Fold the page context
  // into `content` so the agent actually sees it.
  const fullContent = context
    ? `${context}\n\n---\n\n${message}`
    : message;
  client.send(GW_METHOD.CHAT_SEND, {
    content: fullContent,
    query: fullContent,
    session_id: sessionId,
  });
}

async function handlePinTab(
  msg: SidePanelRequest,
  port: chrome.runtime.Port
): Promise<void> {
  if (msg.action !== MSG.PIN_TAB) return;
  const { tabId } = msg;
  const sessionId = sessionMgr.activeSessionId;
  if (!sessionId) return;
  const existing = await getPinnedPagesBySession(sessionId);
  if (existing.length >= MAX_PINNED_PAGES) {
    port.postMessage({
      action: "error",
      message: `Session already has the maximum of ${MAX_PINNED_PAGES} pinned pages. Unpin one first.`,
    });
    return;
  }
  const pinned = await pinTabToSession(tabId, sessionId);
  if (!pinned) {
    port.postMessage({ action: "error", message: "Could not extract page context" });
    return;
  }
  await updatePinBadge();
  port.postMessage({ action: "pinned", page: pinned });
}

async function handlePinTabs(
  msg: SidePanelRequest,
  port: chrome.runtime.Port
): Promise<void> {
  if (msg.action !== MSG.PIN_TABS) return;
  const { tabIds } = msg;
  const sessionId = sessionMgr.activeSessionId;
  if (!sessionId || tabIds.length === 0) return;
  const existing = await getPinnedPagesBySession(sessionId);
  const room = MAX_PINNED_PAGES - existing.length;
  if (room <= 0) return;
  let added = 0;
  for (const tabId of tabIds) {
    if (added >= room) break;
    const pinned = await pinTabToSession(tabId, sessionId);
    if (pinned) {
      added += 1;
      port.postMessage({ action: "pinned", page: pinned });
    }
  }
  if (added > 0) await updatePinBadge();
}

async function handleUnpinTab(
  msg: SidePanelRequest,
  _port: chrome.runtime.Port
): Promise<void> {
  if (msg.action !== MSG.UNPIN_TAB) return;
  await removePinnedPage(msg.id);
  await updatePinBadge();
}

function handleReconnect(
  msg: SidePanelRequest,
  _port: chrome.runtime.Port
): void {
  if (msg.action !== MSG.RECONNECT) return;
  client.connect().catch((e) => log.error("reconnect failed", e));
}

function handleListSessions(
  msg: SidePanelRequest,
  port: chrome.runtime.Port
): void {
  if (msg.action !== MSG.LIST_SESSIONS) return;
  port.postMessage({
    action: "sessions",
    sessions: sessionMgr.sessions,
    activeId: sessionMgr.activeSessionId,
  });
}

function handleGetPendingAction(
  msg: SidePanelRequest,
  port: chrome.runtime.Port
): void {
  if (msg.action !== MSG.GET_PENDING_ACTION) return;
  if (_pendingAction) {
    port.postMessage(_pendingAction);
    _pendingAction = null;
  }
}

function handleNewSession(
  msg: SidePanelRequest,
  _port: chrome.runtime.Port
): void {
  if (msg.action !== MSG.NEW_SESSION) return;
  sessionMgr.createSession(msg.title || "New session");
}

async function handleSetSession(
  msg: SidePanelRequest,
  port: chrome.runtime.Port
): Promise<void> {
  if (msg.action !== MSG.SET_SESSION) return;
  await sessionMgr.setActiveSession(msg.sessionId);
  await updatePinBadge();
  port.postMessage({ action: "session_changed", activeId: sessionMgr.activeSessionId });
}

function handleGetStatus(
  msg: SidePanelRequest,
  port: chrome.runtime.Port
): void {
  if (msg.action !== MSG.GET_STATUS) return;
  port.postMessage({
    action: MSG.STATUS,
    connected: client.isConnected,
    activeSessionId: sessionMgr.activeSessionId,
    activeSessionTitle: sessionMgr.activeSession?.title ?? null,
  });
}

async function handleRenameSession(
  msg: SidePanelRequest,
  _port: chrome.runtime.Port
): Promise<void> {
  if (msg.action !== MSG.RENAME_SESSION) return;
  const { sessionId, name } = msg;
  if (sessionId) await sessionMgr.renameSession(sessionId, name);
}

// ---------------------------------------------------------------------------
// Content script messages (runtime.onMessage — short-lived)
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === MSG.PAGE_CONTEXT && sender.tab?.id != null) {
    cache.set(sender.tab.id, msg.context);
    sendResponse({ ok: true });
    return false;
  }
  if (msg.action === MSG.GET_STATUS) {
    sendResponse({
      connected: client.isConnected,
      activeSessionId: sessionMgr.activeSessionId,
      activeSessionTitle: sessionMgr.activeSession?.title ?? null,
    });
    return false;
  }
  if (msg.action === MSG.OPEN_PANEL) {
    openPanel(msg.windowId as number | undefined).catch(() => {});
    return false;
  }
  if (msg.action === MSG.GET_ACTIVE_CONTEXT) {
    getActiveContext().then((context) => sendResponse({ ok: true, context }));
    return true; // async response
  }
  return false;
});

/** Extract (or return cached) context for the active tab of the focused window. */
async function getActiveContext(): Promise<PageContext | null> {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (tab?.id == null) return null;
  const cached = cache.get(tab.id);
  if (cached) return cached;
  const ctx = await tabWatcher.extractFromTab(tab.id);
  if (ctx) {
    cache.set(tab.id, ctx);
    return ctx;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Keyboard commands
// ---------------------------------------------------------------------------

chrome.commands.onCommand.addListener(async (command) => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

  switch (command) {
    case COMMANDS.TOGGLE_PANEL:
      await openPanel(tab?.windowId ?? undefined);
      break;

    case COMMANDS.PIN_PAGE:
      if (tab?.id != null) {
        broadcastToSidePanel({ action: MSG.PIN_TAB, tabId: tab.id });
      }
      break;

    case COMMANDS.ASK_SELECTION:
      if (tab?.id != null) {
        const response = await chrome.tabs.sendMessage(tab.id, { action: MSG.SELECTION_TEXT });
        if (response?.text) {
          broadcastToSidePanel({ action: "ask_selection", text: response.text });
        }
      }
      break;
  }
});

// ---------------------------------------------------------------------------
// Context menu handler
// ---------------------------------------------------------------------------

async function onContextMenuAction(
  action: "ask" | "toggle_pin" | "summarize" | "reader" | "search_selection",
  info: chrome.contextMenus.OnClickData,
  tab?: chrome.tabs.Tab
): Promise<void> {
  const tabId = tab?.id;
  const windowId = tab?.windowId;

  await openPanel(windowId);

  if (action === "ask" && info.selectionText) {
    _deliverAction({ action: "ask_selection", text: info.selectionText, tabId });
  } else if (action === "toggle_pin" && tabId != null) {
    const pinned = await isUrlPinned(tab?.url || "");
    if (pinned) {
      await unpinCurrentUrl(tab?.url);
    } else {
      await pinTabFromMenu(tabId);
    }
  } else if (action === "summarize" && tabId != null) {
    _deliverAction({ action: "summarize_tab", tabId });
  } else if (action === "reader") {
    _deliverAction({ action: "reader" });
  } else if (action === "search_selection" && info.selectionText) {
    _deliverAction({ action: "search_selection", text: info.selectionText });
  }
}

/** Remove any pinned page in the active session whose URL matches the given URL. */
async function unpinCurrentUrl(url?: string): Promise<void> {
  await sessionMgr.ready;
  const sessionId = sessionMgr.activeSessionId;
  if (!sessionId || !url) return;
  const norm = normalizeUrl(url);
  const pages = await getPinnedPagesBySession(sessionId);
  const toRemove = pages.filter((p) => normalizeUrl(p.context.url) === norm);
  for (const p of toRemove) {
    await removePinnedPage(p.id);
  }
  if (toRemove.length > 0) {
    await updatePinBadge();
    broadcastToSidePanel({ action: "refresh_pins" });
  }
}

/** True when the given URL is pinned in the active session. */
async function isUrlPinned(url: string): Promise<boolean> {
  await sessionMgr.ready;
  const sessionId = sessionMgr.activeSessionId;
  if (!sessionId || !url) return false;
  const norm = normalizeUrl(url);
  const pages = await getPinnedPagesBySession(sessionId);
  return pages.some((p) => normalizeUrl(p.context.url) === norm);
}

/** Pin the given tab to the active session directly in the background, notifying the panel. */
async function pinTabFromMenu(tabId: number): Promise<void> {
  await sessionMgr.ready;
  const sessionId = sessionMgr.activeSessionId;
  if (!sessionId) return;
  const existing = await getPinnedPagesBySession(sessionId);
  if (existing.length >= MAX_PINNED_PAGES) return;
  const pinned = await pinTabToSession(tabId, sessionId);
  if (!pinned) return;
  await updatePinBadge();
  broadcastToSidePanel({ action: "pinned", page: pinned });
}

/**
 * Send a context-menu action to the side panel. If the panel is not connected
 * yet (e.g. it was just opened), queue it so the panel receives it on connect.
 */
function _deliverAction(action: { action: string; tabId?: number | undefined; text?: string }): void {
  if (sidePanelPorts.size === 0) {
    _pendingAction = action;
    return;
  }
  broadcastToSidePanel(action);
}
