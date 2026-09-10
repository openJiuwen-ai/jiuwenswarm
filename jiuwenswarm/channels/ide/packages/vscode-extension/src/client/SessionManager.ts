import { WsClient } from './WsClient';
import { JiuwenMessage, SessionInfo, makeRequest } from './protocol';

type PendingRequest = {
  resolve: (payload: Record<string, unknown>) => void;
  reject: (err: Error) => void;
  timer: NodeJS.Timeout;
};

const REQUEST_TIMEOUT_SEC = 15;

export class SessionManager {
  private _sessionId: string | null = null;
  private _sessionTitle = 'JiuwenSwarm';
  private pending = new Map<string, PendingRequest>();
  private channelId: string;
  private sessionListeners: ((sid: string | null) => void)[] = [];

  constructor(private readonly ws: WsClient, channelId = 'ide') {
    this.channelId = channelId;
    this.ws.on('message', (msg) => this.handleMessage(msg));
  }

  get sessionId(): string | null { return this._sessionId; }
  get sessionTitle(): string { return this._sessionTitle; }

  addSessionListener(l: (sid: string | null) => void): void {
    this.sessionListeners.push(l);
  }

  removeSessionListener(l: (sid: string | null) => void): void {
    const idx = this.sessionListeners.indexOf(l);
    if (idx >= 0) this.sessionListeners.splice(idx, 1);
  }

  /** Returns available models and the currently active model name. */
  async listModels(): Promise<{ models: Record<string, unknown>[]; activeModel: string | null }> {
    const payload = await this.request('models.list', {});
    const models = (payload.models as Record<string, unknown>[]) || [];
    const activeModel = payload.active_model as string | undefined || null;
    return { models, activeModel };
  }

  /** List recent sessions */
  async listSessions(limit = 20): Promise<SessionInfo[]> {
    const payload = await this.request('session.list', { limit });
    const arr = payload.sessions as SessionInfo[] | undefined;
    if (!arr) return [];
    return arr.map((obj) => ({
      session_id: (obj.session_id as string) || '',
      title: obj.title as string | undefined,
      last_message_at: obj.last_message_at as number | undefined,
      created_at: obj.created_at as number | undefined,
      message_count: obj.message_count as number | undefined,
      history_path: (obj.history_path as string) || '',
    }));
  }

  /** Switch to an existing session */
  async switchSession(sid: string, mode?: string): Promise<void> {
    const params: Record<string, unknown> = { session_id: sid };
    if (mode) params.mode = mode;
    const payload = await this.request('session.switch', params, sid);
    this.setSessionId(sid);
    this._sessionTitle = (payload.title as string) || sid;
  }

  /**
   * Fire-and-forget chat message. Streaming events arrive via WsClient 'message' events.
   * If [ideContext] is non-null it is prepended to [content].
   * If [mediaItems] is non-null, image attachments are included in base64 format.
   */
  sendChat(
    content: string,
    mode: string,
    requestId: string,
    ideContext?: string,
    mediaItems?: unknown[],
    modelName?: string,
  ): boolean {
    const sid = this._sessionId;
    if (!sid) return false;
    const fullContent = ideContext && ideContext.trim()
      ? `${content}\n\n${ideContext}`
      : content;
    const params: Record<string, unknown> = {
      content: fullContent,
      mode,
      session_id: sid,
    };
    if (modelName) {
      params.model_name = modelName;
    }
    if (mediaItems && mediaItems.length > 0) {
      params.media_items = mediaItems;
    }
    const msg = makeRequest('chat.send', params, this.channelId);
    msg.id = requestId;
    return this.ws.send(msg);
  }

  /**
   * Fire-and-forget interrupt-resume answer (e.g. plan approval for exit_plan_mode).
   * [requestId] is the id of the original chat.send that triggered the interrupt; the
   * server resumes that suspended turn. We reuse it as msg.id so the resumed stream
   * keeps mapping to the same pending turn in the webview.
   */
  sendAnswer(
    requestId: string,
    answers: unknown[],
    source: string,
    mode: string,
  ): boolean {
    const sid = this._sessionId;
    if (!sid) return false;
    const msg = makeRequest('chat.send', {
      session_id: sid,
      request_id: requestId,
      answers,
      source,
      mode,
    }, this.channelId);
    msg.id = requestId;
    return this.ws.send(msg);
  }

  /** Fetch current process & system memory usage (MB). */
  async getMemoryUsage(): Promise<{ rss_mb: number; total_mb: number; available_mb: number }> {
    const payload = await this.request('memory.compute', {});
    return {
      rss_mb: (payload.rss_mb as number) || 0,
      total_mb: (payload.total_mb as number) || 0,
      available_mb: (payload.available_mb as number) || 0,
    };
  }

  /** List available skills */
  async listSkills(): Promise<Record<string, unknown>[]> {
    const payload = await this.request('skills.list', {});
    return (payload.skills as Record<string, unknown>[]) || [];
  }

  /** Enable/disable a skill */
  async toggleSkill(skillId: string, enabled: boolean): Promise<void> {
    await this.request('skills.toggle', { skill_id: skillId, enabled });
  }

  /** Delete a session */
  async deleteSession(sid: string): Promise<void> {
    await this.request('session.delete', { session_id: sid }, sid);
  }

  /** Rename the active session (empty title clears it). */
  async renameSession(title: string): Promise<void> {
    const sid = this._sessionId;
    if (!sid) return;
    await this.request('session.rename', { session_id: sid, title });
    this._sessionTitle = title.trim() || sid;
  }

  /**
   * Fire-and-forget request to load paginated session history.
   * The server streams back history.message events through the WebSocket which
   * flow to all message listeners (including ChatPanel.onJiuwenMessage).
   * A history.done event marks the end of the page.
   */
  loadHistory(sid: string, pageIdx = 1): boolean {
    const msg = makeRequest('history.get', { session_id: sid, page_idx: pageIdx }, this.channelId);
    return this.ws.send(msg);
  }

  // ──────────────────────────────────────────
  // Internal
  // ──────────────────────────────────────────

  private request(method: string, params: Record<string, unknown>, sid?: string): Promise<Record<string, unknown>> {
    const p = { ...params };
    if (sid) p.session_id = sid;
    const msg = makeRequest(method, p, this.channelId);
    return new Promise((resolve, reject) => {
      const id = msg.id!;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Request '${method}' timed out`));
      }, REQUEST_TIMEOUT_SEC * 1000);

      this.pending.set(id, { resolve, reject, timer });
      if (!this.ws.send(msg)) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new Error('WebSocket not connected'));
      }
    });
  }

  private handleMessage(msg: JiuwenMessage): void {
    // ── E2A format responses ──
    const responseKind = msg.response_kind
      || (msg.payload?.response_kind as string | undefined);
    if (responseKind) {
      const rid = msg.request_id
        || (msg.payload?.request_id as string | undefined);
      if (!rid) return;
      const future = this.pending.get(rid);
      if (!future) return;
      if (responseKind === 'e2a.complete') {
        const body = (msg.body as Record<string, unknown>)
          || (msg.payload?.body as Record<string, unknown>)
          || {};
        const result = (body.result as Record<string, unknown>) || body;
        clearTimeout(future.timer);
        this.pending.delete(rid);
        future.resolve(result);
      } else if (responseKind === 'e2a.error') {
        const body = (msg.body as Record<string, unknown>)
          || (msg.payload?.body as Record<string, unknown>)
          || {};
        const err = (body.message as string) || 'E2A error';
        clearTimeout(future.timer);
        this.pending.delete(rid);
        future.reject(new Error(err));
      } else {
        clearTimeout(future.timer);
        this.pending.delete(rid);
        future.reject(new Error(`Unexpected response_kind: ${responseKind}`));
      }
      return;
    }

    // ── Gateway legacy format responses ──
    if (msg.type === 'res' && msg.id) {
      const future = this.pending.get(msg.id);
      if (future) {
        clearTimeout(future.timer);
        this.pending.delete(msg.id);
        if (msg.ok) {
          future.resolve((msg.payload as Record<string, unknown>) || {});
        } else {
          const err =
            (msg.payload as Record<string, unknown> | undefined)?.error as string | undefined
            || 'Request failed';
          future.reject(new Error(err));
        }
      }
      return;
    }

    // ── Events (connection.ack gives us the session) ──
    if (msg.type === 'event' && msg.event) {
      const payload = (msg.payload as Record<string, unknown>) || {};
      if (msg.event === 'connection.ack') {
        const sid = payload.session_id as string | undefined;
        if (sid) {
          this.setSessionId(sid);
        }
      }
    }
  }

  private setSessionId(sid: string | null): void {
    if (this._sessionId === sid) return;
    this._sessionId = sid;
    this.sessionListeners.forEach((l) => l(sid));
  }
}
