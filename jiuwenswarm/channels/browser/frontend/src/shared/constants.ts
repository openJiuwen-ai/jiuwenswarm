/** Shared constants for the JiuwenSwarm browser extension. */

export const DEFAULT_HOST = "127.0.0.1";
export const DEFAULT_PORT = 19000;
export const CHANNEL_ID = "browser";

/**
 * Gateway channel identity (mirrors jiuwenswarm `RoutingKey.app_id`).
 *
 * Keep at "default" so sessions stay shareable with the web channel (the server
 * scopes session sharing by channel_id + app_id + user_id). When integrating the
 * extension as a first-class channel, flip this to "browser" — the gateway reads
 * it from the WS handshake query and will route/scope this client distinctly.
 */
export const APP_ID = "default";

export const WS_URL = (host = DEFAULT_HOST, port = DEFAULT_PORT): string =>
  `ws://${host}:${port}/ws?app_id=${APP_ID}`;

/**
 * chrome.storage.local keys.
 * Sessions are server-owned — the server's session registry is the single source
 * of truth, shared across the web app and extension. Only the active session
 * pointer and browser-specific state (pinned pages, settings) are stored locally.
 */
export const STORAGE_KEYS = {
  ACTIVE_SESSION: "jiuwen_active_session",
  PINNED_PAGES: "jiuwen_pinned_pages",
  SETTINGS: "jiuwen_settings",
  SESSION_NAMES: "jiuwen_session_names",
  HAS_SEEN_TOUR: "jiuwen_has_seen_tour",
  LAST_RESPONSE: "jiuwen_last_response",
  CHAT_HISTORY: "jiuwen_chat_history",
} as const;

/** Maximum number of pages that can be pinned in one research session */
export const MAX_PINNED_PAGES = 20;

/** Context block size limit sent to agent (characters) */
export const MAX_CONTEXT_CHARS = 120_000;

/** Extension command names (must match manifest.json) */
export const COMMANDS = {
  TOGGLE_PANEL: "toggle-panel",
  PIN_PAGE: "pin-page",
  ASK_SELECTION: "ask-selection",
} as const;

/** Internal message actions between extension parts */
export const MSG = {
  // content → background
  PAGE_CONTEXT: "page_context",
  SELECTION_TEXT: "selection_text",
  // background → content
  HIGHLIGHT_TEXT: "highlight_text",
  CLEAR_HIGHLIGHTS: "clear_highlights",
  FILL_FORM: "fill_form",
  // popup ↔ background
  GET_STATUS: "get_status",
  STATUS: "status",
  OPEN_PANEL: "open_panel",
  // sidepanel ↔ background
  SEND_AGENT: "send_agent",
  AGENT_EVENT: "agent_event",
  PIN_TAB: "pin_tab",
  PIN_TABS: "pin_tabs",
  UNPIN_TAB: "unpin_tab",
  GET_SESSION: "get_session",
  SET_SESSION: "set_session",
  LIST_SESSIONS: "list_sessions",
  NEW_SESSION: "new_session",
  GET_PENDING_ACTION: "get_pending_action",
  // background → content (agent tool dispatch)
  SCROLL_TO: "scroll_to",
  // sidepanel → background (one-shot; reading mode)
  GET_ACTIVE_CONTEXT: "get_active_context",
  // sidepanel → background (force reconnect)
  RECONNECT: "reconnect",
  // sidepanel → background (rename a session locally)
  RENAME_SESSION: "rename_session",
} as const;
