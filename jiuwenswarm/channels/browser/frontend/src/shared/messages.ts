/**
 * Typed internal messages between the side panel and the background service
 * worker (over a chrome.runtime.Port).
 *
 * Outbound (side panel → background) is a clean discriminated union on `action`,
 * so the background can type each handler's payload instead of casting
 * `Record<string, unknown>`.
 */

import { MSG } from "./constants";
import { PinnedPage, ResearchSession } from "./types";

// ---------------------------------------------------------------------------
// Outbound — side panel → background
// ---------------------------------------------------------------------------

export type SidePanelRequest =
  | { action: typeof MSG.SEND_AGENT; message: string; tabId?: number; sessionId?: string }
  | { action: typeof MSG.PIN_TAB; tabId: number }
  | { action: typeof MSG.PIN_TABS; tabIds: number[] }
  | { action: typeof MSG.UNPIN_TAB; id: string }
  | { action: typeof MSG.RECONNECT }
  | { action: typeof MSG.LIST_SESSIONS }
  | { action: typeof MSG.GET_PENDING_ACTION }
  | { action: typeof MSG.NEW_SESSION; title?: string }
  | { action: typeof MSG.SET_SESSION; sessionId: string }
  | { action: typeof MSG.GET_STATUS }
  | { action: typeof MSG.RENAME_SESSION; sessionId: string; name: string };

export type SidePanelAction = SidePanelRequest["action"];

/** Narrow a request to a specific action. */
export type RequestOf<A extends SidePanelAction> = Extract<SidePanelRequest, { action: A }>;

// ---------------------------------------------------------------------------
// Inbound — background → side panel
//
// Uses a mixed discriminant: background replies carry `action`, while raw server
// envelopes carry `type`. The side panel dispatches on `action ?? type`.
// ---------------------------------------------------------------------------

export type BackgroundReply =
  | { action: "status"; connected: boolean; activeSessionId: string | null }
  | { action: "sessions"; sessions: ResearchSession[]; activeId: string | null }
  | { action: "session_created"; session: ResearchSession }
  | { action: "session_changed"; activeId: string | null }
  | { action: "pinned"; page: PinnedPage }
  | { action: "refresh_pins" }
  | { action: "ask_selection"; text: string; tabId?: number }
  | { action: "summarize_tab"; tabId: number }
  | { action: "reader" }
  | { action: "search_selection"; text: string }
  | { action: "tool"; tool: string }
  | { action: "error"; message: string }
  | { type: "token"; payload: { text: string } }
  | { type: "done"; payload: { text: string } }
  | { type: "error"; payload: { message: string } };

/** The dispatch key used by the side panel: `action` for replies, `type` for envelopes. */
export function replyKey(msg: BackgroundReply): string {
  return (msg as { action?: string }).action ?? (msg as { type?: string }).type ?? "";
}
