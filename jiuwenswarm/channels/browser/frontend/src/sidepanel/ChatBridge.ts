/**
 * Bridge between the background service worker port and the side panel UI.
 *
 * Keeps a single chrome.runtime port open (name "sidepanel"), forwards UI
 * actions to the background, and re-dispatches every background message as a
 * "jiuwen:bg" CustomEvent on window so the side panel can render connection
 * status, session state, and agent stream events natively.
 */

import { createLogger } from "@shared/logger";
import { MSG } from "@shared/constants";
import { SidePanelRequest, BackgroundReply } from "@shared/messages";

const log = createLogger("sidepanel/bridge");

export class ChatBridge {
  private _port: chrome.runtime.Port | null = null;
  private _sessionId: string | null = null;

  connect(): void {
    if (this._port) {
      try {
        this._port.disconnect();
      } catch {
        /* ignore */
      }
      this._port = null;
    }
    try {
      this._port = chrome.runtime.connect({ name: "sidepanel" });
    } catch (e) {
      log.warn("port connect failed", e);
      this._port = null;
    }
    if (!this._port) {
      this._scheduleReconnect();
      return;
    }

    this._port.onMessage.addListener((msg: BackgroundReply) => {
      window.dispatchEvent(new CustomEvent("jiuwen:bg", { detail: msg }));
    });

    this._port.onDisconnect.addListener(() => {
      // MV3 terminates the service worker when idle, invalidating this port.
      // The panel stays open, so reconnect and re-sync on our own.
      log.debug("port disconnected — reconnecting");
      this._port = null;
      this._scheduleReconnect();
    });

    log.info("bridge connected");

    // Request initial state (including any queued context-menu action).
    this._send({ action: MSG.GET_STATUS });
    this._send({ action: MSG.LIST_SESSIONS });
    this._send({ action: MSG.GET_PENDING_ACTION });
  }

  /** Reconnect if needed, then re-pull status, sessions, and any pending action. */
  refresh(): void {
    if (!this._port) {
      this.connect();
      return;
    }
    this._send({ action: MSG.GET_STATUS });
    this._send({ action: MSG.LIST_SESSIONS });
    this._send({ action: MSG.GET_PENDING_ACTION });
  }

  private _scheduleReconnect(): void {
    window.setTimeout(() => {
      if (!this._port) this.connect();
    }, 500);
  }

  setActiveSession(sessionId: string): void {
    this._sessionId = sessionId;
    this._send({ action: MSG.SET_SESSION, sessionId });
  }

  sendChat(message: string, contextTabId?: number): void {
    this._send({
      action: MSG.SEND_AGENT,
      message,
      ...(contextTabId != null ? { tabId: contextTabId } : {}),
      ...(this._sessionId ? { sessionId: this._sessionId } : {}),
    });
  }

  pinCurrentTab(): void {
    chrome.tabs.query({ active: true, currentWindow: true }, ([tab]) => {
      if (tab?.id != null) {
        this._send({ action: MSG.PIN_TAB, tabId: tab.id });
      }
    });
  }

  pinAllTabs(): void {
    chrome.tabs.query({ currentWindow: true }, (tabs) => {
      const ids = tabs
        .filter((t) => t.id != null)
        .map((t) => t.id as number);
      if (ids.length > 0) this._send({ action: MSG.PIN_TABS, tabIds: ids });
    });
  }

  createSession(title: string): void {
    this._send({ action: MSG.NEW_SESSION, title });
  }

  renameSession(sessionId: string, name: string): void {
    this._send({ action: MSG.RENAME_SESSION, sessionId, name });
  }

  reconnect(): void {
    this._send({ action: MSG.RECONNECT });
  }

  retryPin(tabId: number, oldPinId: string): void {
    this._send({ action: MSG.UNPIN_TAB, id: oldPinId });
    this._send({ action: MSG.PIN_TAB, tabId });
  }

  private _send(msg: SidePanelRequest): void {
    if (!this._port) {
      // Reconnect
      this.connect();
      return;
    }
    try {
      this._port.postMessage(msg);
    } catch (e) {
      log.warn("port postMessage failed", e);
      this._port = null;
    }
  }
}
