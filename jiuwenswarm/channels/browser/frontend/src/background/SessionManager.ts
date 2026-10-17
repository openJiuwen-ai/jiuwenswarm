/**
 * Manages research session lifecycle in the background service worker.
 *
 * Sessions are server-owned. The active-session pointer is stored locally; the
 * session list is fetched from the server via `session.list` (same JSON-RPC
 * protocol as the IDE plugin / web app).
 */

import { createLogger } from "@shared/logger";
import { loadActiveSessionId, saveActiveSessionId, loadSessionDisplayNames, saveSessionDisplayName } from "@shared/storage";
import { ResearchSession } from "@shared/types";
import { GW_METHOD } from "@shared/protocol";
import type { WsClient } from "./WsClient";

const log = createLogger("bg/sessions");

type ServerSessionRow = {
  session_id: string;
  title?: string;
  created_at?: string;
  mode?: string;
};

type ChangeListener = (sessions: ResearchSession[], activeId: string | null) => void;

export class SessionManager {
  /** In-memory cache populated from server — not persisted locally. */
  private _sessions: ResearchSession[] = [];
  private _activeSessionId: string | null = null;
  private _listeners: Set<ChangeListener> = new Set();
  private _displayNames: Record<string, string> = {};
  private _ready!: Promise<void>;
  private _resolveReady!: () => void;

  constructor(private readonly _client: WsClient) {
    this._ready = new Promise((res) => {
      this._resolveReady = res;
    });
  }

  /** Resolves once the active-session pointer has been loaded from storage. */
  get ready(): Promise<void> {
    return this._ready;
  }

  /** Local display-name override for a session, if any. */
  private _displayName(id: string): string | undefined {
    const n = this._displayNames[id];
    return n && n.trim() ? n : undefined;
  }

  /** Apply a local display-name override, falling back to the server's name/id. */
  private _title(id: string, serverTitle?: string): string {
    return this._displayName(id) ?? serverTitle ?? id;
  }

  get sessions(): ResearchSession[] {
    return this._sessions;
  }

  get activeSessionId(): string | null {
    return this._activeSessionId;
  }

  get activeSession(): ResearchSession | undefined {
    return this._sessions.find((s) => s.id === this._activeSessionId);
  }

  /**
   * Load the active-session pointer from local storage. The session list is
   * fetched separately via refresh() once the connection is established.
   */
  async init(): Promise<void> {
    this._activeSessionId = await loadActiveSessionId();
    this._displayNames = await loadSessionDisplayNames();
    log.info("init, active pointer=", this._activeSessionId);
    this._resolveReady();
    this._notify();
  }

  /** Ask the server for the current session list (session.list → res). */
  async refresh(): Promise<void> {
    try {
      const payload = await this._client.request(GW_METHOD.SESSION_LIST, { limit: 50 });
      const rows = (payload.sessions as ServerSessionRow[]) || [];
      this._sessions = rows
        .filter((ss) => ss && typeof ss.session_id === "string")
        .map((ss) => ({
          id: ss.session_id,
          title: this._title(ss.session_id, ss.title),
          mode: (ss.mode as string) || "chat",
          createdAt: ss.created_at || new Date().toISOString(),
          updatedAt: ss.created_at || new Date().toISOString(),
          pinnedPageIds: [],
        }))
        .sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime());

      // Keep the active pointer even if the freshly-adopted connection session
      // (from connection.ack) is not in the list yet — it is valid for chat and
      // only becomes visible in session.list after it has content. Make sure it
      // stays visible in the picker too.
      if (this._activeSessionId && !this._sessions.find((s) => s.id === this._activeSessionId)) {
        this._sessions.unshift({
          id: this._activeSessionId,
          title: this._title(this._activeSessionId, undefined),
          mode: "chat",
          createdAt: new Date().toISOString(),
          updatedAt: new Date().toISOString(),
          pinnedPageIds: [],
        });
      }

      log.info(`server sessions updated: ${this._sessions.length} sessions`);
      this._notify();
    } catch (e) {
      log.error("session.list failed", e);
    }
  }

  /** Create a new session on the server and activate it. */
  async createSession(title: string): Promise<void> {
    try {
      const payload = await this._client.request(GW_METHOD.SESSION_CREATE, {
        title,
        create_token: this._randomHex(16),
      });
      const sid = payload.session_id as string | undefined;
      if (!sid) {
        log.warn("session.create returned no session_id");
        return;
      }
      this._activeSessionId = sid;
      await saveActiveSessionId(sid);
      // Refresh the list so the new session shows up in the picker.
      await this.refresh();
      this._notify();
    } catch (e) {
      log.error("session.create failed", e);
    }
  }

  /** Adopt the connection's auto-created session (from connection.ack). */
  setSessionFromAck(sessionId: string, mode?: string): void {
    if (!sessionId) return;
    const existing = this._sessions.find((s) => s.id === sessionId);
    if (!existing) {
      this._sessions.unshift({
        id: sessionId,
        title: this._title(sessionId, undefined),
        mode: mode || "chat",
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
        pinnedPageIds: [],
      });
    }
    this._activeSessionId = sessionId;
    void saveActiveSessionId(sessionId);
    this._notify();
  }

  async setActiveSession(id: string): Promise<void> {
    if (!this._sessions.find((s) => s.id === id)) {
      log.warn("setActiveSession: unknown id", id);
      return;
    }
    await this._client.request(GW_METHOD.SESSION_SWITCH, { session_id: id }).catch(() => {});
    this._activeSessionId = id;
    await saveActiveSessionId(id);
    this._notify();
  }

  /** Rename a session locally (display-name override). Empty name clears it. */
  async renameSession(id: string, name: string): Promise<void> {
    this._displayNames = await saveSessionDisplayName(id, name);
    const session = this._sessions.find((s) => s.id === id);
    if (session) {
      session.title = this._title(id, session.title);
    }
    this._notify();
  }

  onChange(listener: ChangeListener): () => void {
    this._listeners.add(listener);
    return () => this._listeners.delete(listener);
  }

  private _randomHex(bytes: number): string {
    const buf = new Uint8Array(bytes);
    crypto.getRandomValues(buf);
    return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  private _notify(): void {
    for (const l of this._listeners) {
      try {
        l(this._sessions, this._activeSessionId);
      } catch (e) {
        log.error("listener threw", e);
      }
    }
  }
}
