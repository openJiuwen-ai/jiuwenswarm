import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import { execSync } from 'child_process';
import { WsClient, WsStatus } from '../client/WsClient';
import { SessionManager } from '../client/SessionManager';
import { JiuwenMessage, ExtToWebviewMsg, makeRequest, SessionMetrics } from '../client/protocol';
import { collectContext, estimateTokens, gatherWorkspaceFiles } from '../context/ContextCollector';
import { StatusBar } from './StatusBar';
import * as DiffApplier from '../editor/DiffApplier';
import { runCommand, extractCommand } from '../terminal/TerminalManager';
import { showDiffAndPrompt, computeProposedContent } from '../editor/DiffViewer';
import { SwarmStateManager } from '../swarm/SwarmStateManager';
import { SwarmMapPanel } from '../swarm/SwarmMapPanel';

export class ChatPanel implements vscode.Disposable {
  public static readonly viewId = 'jiuwenswarm.chatView';

  private panel?: vscode.WebviewPanel;
  private readonly webviewHtmlPath: string;
  private disposables: vscode.Disposable[] = [];
  private panelDisposables: vscode.Disposable[] = [];
  private pendingWebviewMessages: ExtToWebviewMsg[] = [];
  private webviewReady = false;
  private debugEnabled = false;
  private lastRequestId = '';
  private activeModel: string | null = null;
  private historyLoadedForSession: string | null = null;
  private sessionTokens = 0;
  private sessionCostUsd = 0;
  private hasCost = false;
  private latestInput = '';
  private latestContextMetrics: SessionMetrics = this.emptyMetrics();
  private memoryTimer?: NodeJS.Timeout;

  // Swarm state — updated on every team event; feeds the Swarm Map panel
  readonly swarmStateManager = new SwarmStateManager();
  private swarmMapPanel?: SwarmMapPanel;

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly ws: WsClient,
    private readonly session: SessionManager,
    private readonly statusBar: StatusBar,
  ) {
    this.webviewHtmlPath = path.join(context.extensionPath, 'resources', 'chat.html');
    this.swarmMapPanel = new SwarmMapPanel(context);
    this.swarmMapPanel.onMessage = (msg) => this.handleSwarmMessage(msg);
    const statusListener = (s: WsStatus) => this.onStatusChange(s);
    const msgListener = (m: JiuwenMessage) => this.onJiuwenMessage(m);
    const sessionListener = (sid: string | null) => this.onSessionChange(sid);
    this.ws.on('status', statusListener);
    this.ws.on('message', msgListener);
    this.session.addSessionListener(sessionListener);

    this.disposables.push(
      new vscode.Disposable(() => this.ws.off('status', statusListener)),
      new vscode.Disposable(() => this.ws.off('message', msgListener)),
      new vscode.Disposable(() => this.session.removeSessionListener(sessionListener)),
    );
  }

  show(): void {
    if (this.panel) {
      this.panel.reveal(vscode.ViewColumn.Beside, true);
      this.sendCurrentStatus();
      this.refreshMetrics();
      return;
    }

    this.panel = vscode.window.createWebviewPanel(
      ChatPanel.viewId,
      'JiuwenSwarm',
      vscode.ViewColumn.Beside,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [
          vscode.Uri.joinPath(this.context.extensionUri, 'resources'),
        ],
      },
    );
    this.panel.iconPath = vscode.Uri.joinPath(this.context.extensionUri, 'resources', 'icon.svg');
    this.panel.webview.html = this.getHtml(this.panel.webview);

    this.panelDisposables.push(
      this.panel.webview.onDidReceiveMessage((raw) => this.handleWebviewMessage(raw)),
      this.panel.onDidDispose(() => {
        this.stopMemoryPolling();
        this.panelDisposables.forEach((d) => d.dispose());
        this.panelDisposables = [];
        this.pendingWebviewMessages = [];
        this.webviewReady = false;
        this.panel = undefined;
      }),
    );

    if (this.ws.isConnected()) {
      this.sendCurrentStatus();
    }
    this.refreshMetrics();
    this.startMemoryPolling();
  }

  postToWebview(msg: ExtToWebviewMsg): void {
    if (!this.panel) return;
    if (!this.webviewReady && msg.type !== 'debug_log') {
      this.pendingWebviewMessages.push(msg);
      return;
    }
    this.panel.webview.postMessage(msg);
  }

  dispose(): void {
    this.stopMemoryPolling();
    this.panelDisposables.forEach((d) => d.dispose());
    this.panelDisposables = [];
    this.disposables.forEach((d) => d.dispose());
    this.disposables = [];
    this.panel?.dispose();
    this.swarmMapPanel?.dispose();
  }

  // ──────────────────────────────────────────
  // Webview → Extension
  // ──────────────────────────────────────────
  private handleWebviewMessage(msg: Record<string, unknown>): void {
    const type = msg.type as string;
    switch (type) {
      case 'ready':
        this.webviewReady = true;
        this.flushPendingMessages();
        this.sendCurrentStatus();
        this.refreshMetrics();
        this.sendGitStatus();
        break;

      case 'input_changed': {
        this.latestInput = (msg.content as string) || '';
        this.refreshMetrics();
        break;
      }

      case 'send': {
        const content = msg.content as string;
        const mode = (msg.mode as string) || 'code.plan';
        const rid = msg.requestId as string;
        const model = (msg.model as string) || this.activeModel || undefined;
        const mediaItems = msg.media_items as unknown[] | undefined;
        const mentionedPaths = (msg.mentionedPaths as string[] | undefined) || [];
        if (!rid) return;
        this.lastRequestId = rid;
        // Clear snapshots from previous turn; rewind is no longer valid
        DiffApplier.clearSnapshots();
        this.postToWebview({ type: 'rewindable', enabled: false });
        if (!this.ws.isConnected() || !this.session.sessionId) {
          this.postToWebview({ type: 'error', message: 'Not connected to JiuwenSwarm' });
          return;
        }
        this.debug(`SEND→ requestId=${rid} mode=${mode} content=${content.substring(0, 60)}`);
        this.latestInput = content;
        const ideContext = collectContext(mentionedPaths);
        this.updateMetrics(content, ideContext.metrics, mediaItems?.length ?? 0);
        if (!this.session.sendChat(content, mode, rid, ideContext.text || undefined, mediaItems, model)) {
          this.debug('SEND→ FAILED (no session or disconnected)');
          this.postToWebview({ type: 'error', message: 'Not connected or no active session', requestId: rid });
        } else {
          this.debug('SEND→ OK');
        }
        break;
      }

      case 'answer': {
        const rid = msg.requestId as string;
        const answers = msg.answers as unknown[] | undefined;
        const source = (msg.source as string) || 'confirm_interrupt';
        const mode = (msg.mode as string) || 'code.plan';
        if (!rid || !Array.isArray(answers) || answers.length === 0) return;
        this.debug(`ANSWER→ requestId=${rid} source=${source} options=${answers.length}`);
        if (!this.session.sendAnswer(rid, answers, source, mode)) {
          this.postToWebview({ type: 'error', message: 'Not connected or no active session', requestId: rid });
        }
        break;
      }

      case 'new_session': {
        if (!this.ws.isConnected()) {
          this.postToWebview({ type: 'error', message: 'Not connected' });
          return;
        }
        this.debug('ACTION→ new_session (reconnecting for fresh session)');
        DiffApplier.clearSnapshots();
        this.postToWebview({ type: 'rewindable', enabled: false });
        this.resetSessionMetrics();
        this.ws.reconnect();
        break;
      }

      case 'toggle_debug': {
        this.debugEnabled = (msg.enabled as boolean) || false;
        this.debug(`Debug mode toggled: ${this.debugEnabled}`);
        break;
      }

      case 'list_sessions': {
        this.listSessions();
        break;
      }

      case 'switch_session': {
        const sid = msg.sessionId as string;
        if (sid) {
          // session.switch is a team-mode server operation — the user's
          // chat mode (code.plan / code.normal / code.team) is sent per-message.
          this.switchSession(sid, 'code.team');
        }
        break;
      }

      case 'delete_session': {
        const sid = msg.sessionId as string;
        if (sid) this.deleteSession(sid);
        break;
      }

      case 'list_skills': {
        this.listSkills();
        break;
      }

      case 'toggle_skill': {
        const skillId = msg.skillId as string;
        const enabled = msg.enabled as boolean;
        if (skillId !== undefined && enabled !== undefined) {
          this.toggleSkill(skillId, enabled);
        }
        break;
      }

      case 'switch_model': {
        const model = (msg.model as string) || null;
        this.activeModel = model;
        this.debug(`ACTION→ switch_model ${model}`);
        this.postToWebview({ type: 'model_changed', model });
        break;
      }

      case 'rename_session': {
        const title = (msg.title as string) || '';
        if (this.session.sessionId) {
          this.debug(`ACTION→ rename_session title=${title}`);
          void this.renameSession(title);
        }
        break;
      }

      case 'export_session': {
        const historyPath = (msg.historyPath as string) || '';
        const sessionTitle = (msg.sessionTitle as string) || 'session';
        if (historyPath) void this.exportSession(historyPath, sessionTitle);
        break;
      }

      case 'set_mode': {
        // Mode is handled purely in the webview; no backend action needed
        break;
      }

      case 'open_file': {
        const filePath = msg.path as string;
        const line = (msg.line as number) || 0;
        if (filePath) this.openFile(filePath, line);
        break;
      }

      case 'navigate_symbol': {
        const symbol = msg.symbol as string;
        if (symbol) this.navigateSymbol(symbol);
        break;
      }

      case 'rewind': {
        this.handleRewind();
        break;
      }

      case 'stop': {
        if (!this.ws.isConnected()) break;
        this.debug('ACTION→ stop (chat.interrupt)');
        const msg = makeRequest('chat.interrupt', {});
        this.ws.send(msg);
        break;
      }

      case 'files_request': {
        const files = gatherWorkspaceFiles();
        this.postToWebview({ type: 'files', files });
        break;
      }

      case 'git_status_request': {
        this.sendGitStatus();
        break;
      }

      case 'git_commit_request': {
        if (this.gitEnabled()) void this.handleGitCommit();
        break;
      }

      case 'git_push_request': {
        if (this.gitEnabled()) void this.handleGitPush();
        break;
      }
    }
  }

  // ──────────────────────────────────────────
  // JiuwenSwarm events → webview
  // ──────────────────────────────────────────
  private async onJiuwenMessage(msg: JiuwenMessage): Promise<void> {
    const msgType = msg.type;
    // Skip request-response protocol messages — SessionManager handles those
    if (msgType === 'res') return;

    this.debug(`RAW ← ${JSON.stringify(msg)}`);

    // ── Swarm team event interception ──
    // Team events arrive either as E2A chat.delta with "team.event:" prefix,
    // or as old-format { type:"event", event:"team.*" } messages.
    // Consume them here and do not forward to the webview.
    const rawTeamEvent = this.extractTeamEventDelta(msg);
    if (rawTeamEvent !== null) {
      this.swarmStateManager.applyTeamEvent(rawTeamEvent);
      this.swarmDebug('team.event: ' + rawTeamEvent);
      const snap = this.swarmStateManager.snapshot();
      this.swarmMapPanel?.postSnapshot(snap);
      // Auto-open the Swarm Map panel when the first agent spawns
      if (snap.lanes.length === 1 && snap.lanes[0].status !== 'SHUTDOWN') {
        this.swarmMapPanel?.show();
      }
      return;
    }

    const converted = this.convertServerMessageToLegacyEvent(msg);
    if (!converted) {
      this.debug('CONV  → dropped (not a recognised chat event)');
      return;
    }

    const et = converted.event_type as string;
    const payload = (converted.payload as Record<string, unknown>) || {};

    // ── Snapshot & apply file-edit tool calls ──
    if (et === 'chat.tool_call') {
      const toolName = payload.tool_name as string | undefined
        || ((payload.tool_call as Record<string, unknown>)?.name as string | undefined)
        || '';
      const isFileEdit = toolName === 'str_replace_editor' || toolName === 'write_file' || toolName === 'create_file' || toolName === 'edit_file';

      if (isFileEdit) {
        const args = (payload.tool_call as Record<string, unknown>)?.arguments as Record<string, unknown>
          || (payload.tool_input as Record<string, unknown>)
          || (payload.input as Record<string, unknown>)
          || {};
        const filePath = (args.file_path as string) || (args.path as string) || undefined;

        if (filePath) {
          const cfg = vscode.workspace.getConfiguration('jiuwenswarm');
          if (cfg.get<boolean>('rewindEnabled', true)) {
            DiffApplier.ensureSnapshot(filePath);
            this.debug(`SNAP  → snapshotted ${filePath}`);
          }

          if (cfg.get<boolean>('useDiffViewer', false)) {
            // Show native diff before applying
            const proposed = computeProposedContent({
              path: filePath,
              file_path: filePath,
              old_str: (args.old_str as string) || (args.old_string as string) || undefined,
              old_string: (args.old_string as string) || (args.old_str as string) || undefined,
              new_str: (args.new_str as string) || (args.new_string as string) || undefined,
              new_string: (args.new_string as string) || (args.new_str as string) || undefined,
              content: args.content as string | undefined,
              command: args.command as string | undefined,
            });
            if (proposed !== undefined) {
              const accepted = await showDiffAndPrompt(filePath, proposed, toolName ?? 'edit');
              if (!accepted) {
                this.debug(`DIFF  → rejected ${filePath}`);
                vscode.window.showInformationMessage(`JiuwenSwarm: rejected edit to ${path.basename(filePath)}`);
                // Still dispatch event to webview so card shows, but don't apply
              } else {
                this.debug(`DIFF  → accepted ${filePath}`);
                await DiffApplier.handleToolCall(converted);
              }
            } else {
              // Can't compute diff (old_str not found, etc.) — fall back to direct apply
              await DiffApplier.handleToolCall(converted);
            }
          } else {
            // Direct apply (existing behaviour)
            await DiffApplier.handleToolCall(converted);
          }
        }
      }

      // ── Terminal integration: run bash commands in IDE terminal ──
      if (toolName === 'bash' || toolName === 'run_command') {
        const cfg = vscode.workspace.getConfiguration('jiuwenswarm');
        if (cfg.get<boolean>('runCommandsInTerminal', true)) {
          const cmd = extractCommand(payload);
          if (cmd) {
            this.debug(`TERM  → ${cmd}`);
            runCommand(cmd);
          }
        }
      }

    }

    // ── Swarm tool attribution — update the agent lane's current activity ──
    // Handles both chat.tool_call (tool name nested at tool_call.name, args may be a
    // JSON string) and chat.tool_update (tool_name at top level) so that real
    // sub-agent activity stubs lanes even when team.member.spawned is not sent.
    if (et === 'chat.tool_call' || et === 'chat.tool_update') {
      const attrToolName = (payload.tool_name as string | undefined)
        || ((payload.tool_call as Record<string, unknown>)?.name as string | undefined)
        || '';
      const memberName = payload.member_name as string | undefined;
      const rawArgs = ((payload.tool_call as Record<string, unknown>)?.arguments as unknown)
        || (payload.arguments as unknown)
        || (payload.tool_input as unknown)
        || (payload.input as unknown);
      let attrArgs: Record<string, unknown> = {};
      if (typeof rawArgs === 'string') {
        try { attrArgs = JSON.parse(rawArgs) as Record<string, unknown>; } catch { attrArgs = {}; }
      } else if (rawArgs && typeof rawArgs === 'object') {
        attrArgs = rawArgs as Record<string, unknown>;
      }
      const attrFilePath = (attrArgs['path'] as string | undefined)
        || (attrArgs['file_path'] as string | undefined);
      if (attrToolName) {
        this.swarmStateManager.applyToolCall(attrToolName, attrFilePath, memberName, attrArgs);
        this.swarmDebug(`tool: ${attrToolName}${memberName ? ' · ' + memberName : ''}`);
        this.swarmMapPanel?.postSnapshot(this.swarmStateManager.snapshot());
      }
    }

    // ── Swarm model-call lifecycle — lane "thinking…" / "generating…" states ──
    // llm_call_start fires before the first token of every model call, so the
    // lane shows "thinking…" instead of drifting into "idle". The first streamed
    // token flips it to "generating…", and llm_call_end clears it back.
    if (et === 'chat.llm_call_start') {
      this.swarmStateManager.applyModelCallStart(payload.member_name as string | undefined);
      this.swarmMapPanel?.postSnapshot(this.swarmStateManager.snapshot());
    } else if (et === 'chat.llm_call_end') {
      this.swarmStateManager.applyModelCallEnd(payload.member_name as string | undefined);
      this.swarmMapPanel?.postSnapshot(this.swarmStateManager.snapshot());
    } else if ((et === 'chat.reasoning' || et === 'chat.delta') && payload.member_name) {
      this.swarmStateManager.applyModelTokenStart(payload.member_name as string);
    }

    // ── On turn end, promote snapshots and show rewind bar ──
    if (et === 'chat.final') {
      if (DiffApplier.promoteSnapshots()) {
        this.postToWebview({ type: 'rewindable', enabled: true });
        this.debug('SNAP  → turn complete, rewindable');
      }
      this.sendGitStatus();
    }

    this.debug(`CONV  → event_type=${et} request_id=${converted.request_id}`);
    this.postToWebview({ type: 'jiuwen_event', event: converted });
    this.trackTokenUsage(converted);
  }

  /**
   * Extract the raw team-event JSON from a server message, or return null if
   * the message is not a team event.
   *
   * Two wire formats:
   *  - E2A chunk: response_kind="e2a.chunk", body.event_type="chat.delta",
   *               body.delta starts with "team.event:" — the rest is the JSON payload.
   *  - Old format: type="event", event starts with "team." — wrap payload in envelope.
   *
   * The returned string is always in the { "event": { "type": ..., ... } } shape
   * that SwarmStateManager.applyTeamEvent() expects.
   */
  private extractTeamEventDelta(msg: JiuwenMessage): string | null {
    const responseKind = msg.response_kind;

    // E2A chunk path
    if (responseKind === 'e2a.chunk') {
      const body = msg.body;
      if (!body) return null;
      const eventType = body['event_type'] as string | undefined;
      const delta = body['delta'] as unknown;

      // Legacy shape: chat.delta whose text is a "team.event:"-prefixed JSON string.
      if (eventType === 'chat.delta' && typeof delta === 'string') {
        if (!delta.startsWith('team.event:')) return null;
        return delta.slice('team.event:'.length);
      }

      // Object shape: event_type is a team.* category (e.g. "team.task",
      // "team.member") and delta is the full payload { event: { type, ... } }.
      // Normalize it into the { "event": { "type": ..., ... } } shape the
      // SwarmStateManager dispatches on.
      if ((eventType?.startsWith('team.') || eventType === 'workflow.updated') && typeof delta === 'object' && delta) {
        const pl = delta as Record<string, unknown>;
        const inner = pl['event'] as Record<string, unknown> | undefined;
        if (inner && typeof inner['type'] === 'string') {
          return JSON.stringify({ event: inner });
        }
        return JSON.stringify(pl);
      }
      return null;
    }

    // Old-format path
    if (msg.type === 'event' && typeof msg.event === 'string' && msg.event.startsWith('team.')) {
      const payload = msg.payload || {};
      // The gateway nests the real event under payload.event (e.g. event:"team.member"
      // with payload.event.type === "team.member.spawned"). Use the inner event so the
      // type the SwarmStateManager dispatches on is the specific one, not the category.
      const inner = payload['event'] as Record<string, unknown> | undefined;
      if (inner && typeof inner === 'object' && typeof inner['type'] === 'string') {
        return JSON.stringify({ event: inner });
      }
      // Legacy fallback: flat payload with the category name as the event type.
      const flat = { ...payload, type: msg.event };
      return JSON.stringify({ event: flat });
    }

    return null;
  }

  /** Convert server messages (E2A or old format) to the legacy event format the webview expects.
   *  Webview expects: { event_type, request_id, payload }
   */
  private convertServerMessageToLegacyEvent(msg: JiuwenMessage): Record<string, unknown> | null {
    const responseKind = msg.response_kind
      || (msg.payload?.response_kind as string | undefined);

    // ── E2A format ──
    if (responseKind) {
      const requestId = msg.request_id
        || (msg.payload?.request_id as string)
        || '';
      const body = (msg.body as Record<string, unknown>)
        || (msg.payload?.body as Record<string, unknown>)
        || {};

      if (responseKind === 'e2a.chunk') {
        const eventType = (body.event_type as string) || '';
        const delta = body.delta;
        const payloadOut: Record<string, unknown> = {};
        if (eventType === 'chat.delta') {
          payloadOut.text = (delta as string) || '';
        } else if (eventType === 'chat.reasoning') {
          payloadOut.text = (delta as string) || '';
        } else if (delta && typeof delta === 'object') {
          Object.assign(payloadOut, delta);
        }
        // Carry the model-call prompt / output so the webview can show what was
        // sent to the LLM alongside the answer.
        if (eventType === 'chat.llm_call_start' && typeof body.prompt === 'string' && body.prompt.trim()) {
          payloadOut.prompt = body.prompt;
        } else if (eventType === 'chat.llm_call_end' && typeof body.content === 'string' && body.content.trim()) {
          payloadOut.content = body.content;
        }
        if (body.member_name) payloadOut.member_name = body.member_name as string;
        return { event_type: eventType, request_id: requestId, payload: payloadOut };
      }

      if (responseKind === 'e2a.complete') {
        const result = (body.result as Record<string, unknown>) || body;
        const eventType = (result.event_type as string) || 'chat.final';
        return { event_type: eventType, request_id: requestId, payload: result };
      }

      if (responseKind === 'e2a.error') {
        const details = body.details as Record<string, unknown> | undefined;
        const errorMsg = (body.message as string) || 'Unknown error';
        const payloadOut: Record<string, unknown> = { error: errorMsg };
        if (details) payloadOut.details = details;
        return { event_type: 'chat.error', request_id: requestId, payload: payloadOut };
      }

      return null;
    }

    // ── Old format ──
    if (msg.type === 'event' && msg.event) {
      const payload = { ...(msg.payload || {}) };
      // Webview expects "text" for delta events, gateway sends "content"
      if (msg.event === 'chat.delta' && payload.content !== undefined && payload.text === undefined) {
        payload.text = payload.content;
      }
      const rid = (msg.id as string)
        || (payload.request_id as string)
        || (msg.request_id as string)
        || this.lastRequestId
        || '';
      return {
        event_type: msg.event,
        request_id: rid,
        payload,
      };
    }

    return null;
  }

  /** Extract token counts from chat.usage_summary events.
   *  Token data is nested under payload.usage for this event type.
   *  Per-turn incremental display is handled in the webview via chat.usage_metadata.
   */
  private trackTokenUsage(event: Record<string, unknown>): void {
    const et = event.event_type as string;
    if (et !== 'chat.usage_summary') return;
    const payload = (event.payload as Record<string, unknown>) || {};
    const usage = (payload.usage as Record<string, unknown>) || {};
    const input = (usage.input_tokens as number) || 0;
    const output = (usage.output_tokens as number) || 0;
    const cost = (usage.total_cost as number) || (usage.cost_usd as number) || 0;
    this.sessionTokens += input + output;
    if (cost > 0) {
      this.sessionCostUsd += cost;
      this.hasCost = true;
    }
    this.publishMetrics({
      ...this.latestContextMetrics,
      sessionTokens: this.sessionTokens,
      sessionCostUsd: this.sessionCostUsd,
      hasCost: this.hasCost,
    });
  }

  private onStatusChange(s: WsStatus): void {
    this.sendCurrentStatus();
  }

  private onSessionChange(sid: string | null): void {
    this.statusBar.setSessionId(sid);
    this.resetSessionMetrics();
    this.swarmStateManager.reset(sid ?? '');
    this.swarmMapPanel?.postSnapshot(this.swarmStateManager.snapshot());
    if (this.ws.isConnected()) {
      this.sendCurrentStatus();
    }
  }

  private sendCurrentStatus(): void {
    const s = this.ws.getStatus();
    const sid = this.session.sessionId;
    const defaultMode = vscode.workspace.getConfiguration('jiuwenswarm')
      .get<string>('defaultMode', 'code.plan');
    this.debug(`STATUS→ ws=${s} session=${sid}`);
    if (s === 'connected' && sid) {
      // Send connected immediately so the webview stops showing "Connecting to JiuwenSwarm…"
      this.postToWebview({
        type: 'connected',
        sessionId: sid,
        sessionTitle: this.session.sessionTitle,
        defaultMode,
      });
      // Load history exactly once per session (connect / reconnect / switch).
      if (sid && this.historyLoadedForSession !== sid &&
          vscode.workspace.getConfiguration('jiuwenswarm').get<boolean>('loadHistoryOnSwitch', true)) {
        this.historyLoadedForSession = sid;
        this.postToWebview({ type: 'history_loading', loading: true });
        this.session.loadHistory(sid);
      }
      // Then load models in background and send a second update with model list
      this.session.listModels()
        .then(async ({ models, activeModel }) => {
          if (this.session.sessionId !== sid) return; // session changed in the meantime
          const modelList = models.map((m) => ({
            model_name: (m.model_name as string) || '',
            alias: (m.alias as string) || '',
            model_provider: (m.model_provider as string) || '',
          }));
          // Look up the current session's history.jsonl path so the ☰ menu can
          // offer "Copy session path".
          let historyPath = '';
          try {
            const sessions = await this.session.listSessions();
            const cur = sessions.find((s) => s.session_id === sid);
            historyPath = (cur && cur.history_path) || '';
          } catch { historyPath = ''; }
          this.postToWebview({
            type: 'connected',
            sessionId: sid,
            sessionTitle: this.session.sessionTitle,
            models: modelList,
            activeModel: activeModel || undefined,
            defaultMode,
            historyPath,
          });
        })
        .catch(() => { /* already sent basic connected, nothing more to do */ });
    } else if (s === 'connected') {
      this.postToWebview({
        type: 'connected',
        sessionId: null,
        sessionTitle: 'JiuwenSwarm',
        needsSession: true,
        defaultMode,
      });
    } else if (s === 'reconnecting') {
      this.postToWebview({ type: 'reconnecting' });
    } else {
      this.postToWebview({ type: 'disconnected' });
    }
  }

  // ──────────────────────────────────────────
  // Session helpers
  // ──────────────────────────────────────────
  private async listSessions(): Promise<void> {
    try {
      this.debug('ACTION→ list_sessions');
      const sessions = await this.session.listSessions();
      this.debug(`ACTION→ list_sessions returned ${sessions.length} sessions`);
      this.postToWebview({ type: 'sessions', sessions });
    } catch (e) {
      this.debug(`list_sessions failed: ${e}`);
      this.postToWebview({ type: 'sessions_error', message: String(e) });
    }
  }

  private async switchSession(sessionId: string, mode?: string): Promise<void> {
    try {
      this.debug(`ACTION→ switch_session ${sessionId} mode=${mode || 'default'}`);
      await this.session.switchSession(sessionId, mode);
      this.resetSessionMetrics();
      // The session change already fired onSessionChange → sendCurrentStatus,
      // which loads the new session's history exactly once (guarded by
      // historyLoadedForSession) — no separate loadHistory call here.
      this.postToWebview({
        type: 'connected',
        sessionId: this.session.sessionId!,
        sessionTitle: this.session.sessionTitle,
      });
    } catch (e) {
      this.postToWebview({ type: 'error', message: String(e) });
    }
  }

  private async deleteSession(sessionId: string): Promise<void> {
    try {
      this.debug(`ACTION→ delete_session ${sessionId}`);
      await this.session.deleteSession(sessionId);
      this.postToWebview({ type: 'session_deleted', sessionId });
    } catch (e) {
      this.debug(`delete_session failed: ${e}`);
      this.postToWebview({ type: 'sessions_error', message: String(e) });
    }
  }

  private async renameSession(title: string): Promise<void> {
    try {
      this.debug(`ACTION→ session.rename title=${title}`);
      await this.session.renameSession(title);
      this.postToWebview({ type: 'session_renamed', title: title.trim() });
    } catch (e) {
      this.debug(`session.rename failed: ${e}`);
      this.postToWebview({ type: 'error', message: String(e) });
    }
  }

  // ──────────────────────────────────────────
  // Skills helpers
  // ──────────────────────────────────────────
  private async listSkills(): Promise<void> {
    try {
      this.debug('ACTION→ list_skills');
      const skills = await this.session.listSkills();
      this.debug(`ACTION→ list_skills returned ${skills.length} skills`);
      const skillMaps = skills.map((obj) => ({
        skill_id: (obj.skill_id as string) || '',
        name: (obj.name as string) || (obj.skill_id as string) || '',
        description: (obj.description as string) || '',
        enabled: (obj.enabled as boolean) ?? true,
        trigger: (obj.trigger as string) || '',
      }));
      this.postToWebview({ type: 'skills', skills: skillMaps });
    } catch (e) {
      this.debug(`list_skills failed: ${e}`);
      this.postToWebview({ type: 'skills_error', message: String(e) });
    }
  }

  private async toggleSkill(skillId: string, enabled: boolean): Promise<void> {
    try {
      this.debug(`ACTION→ toggle_skill ${skillId} enabled=${enabled}`);
      await this.session.toggleSkill(skillId, enabled);
      this.postToWebview({ type: 'skill_toggled', skillId, enabled });
    } catch (e) {
      this.debug(`toggle_skill failed: ${e}`);
      this.postToWebview({ type: 'skills_error', message: String(e) });
    }
  }

  // ──────────────────────────────────────────
  // Swarm Map → Extension messages
  // ──────────────────────────────────────────
  private handleSwarmMessage(msg: Record<string, unknown>): void {
    if (msg['type'] === 'open_lane') {
      const memberName = msg['memberName'] as string | undefined;
      if (!memberName) return;
      const lane = this.swarmStateManager.snapshot().lanes.find((l) => l.memberName === memberName);
      const filePath = lane?.lastActivePath;
      if (filePath) void this.openFile(filePath, 0);
    }
  }

  /** Feed a line into the Swarm Map's debug console (no-op until that panel exists). */
  private swarmDebug(line: string): void {
    this.swarmMapPanel?.postDebug(line);
  }

  // ──────────────────────────────────────────
  // File open
  // ──────────────────────────────────────────
  private async openFile(filePath: string, line: number): Promise<void> {
    try {
      const uri = vscode.Uri.file(filePath);
      const doc = await vscode.workspace.openTextDocument(uri);
      // Open in a real editor group (not over the chat webview). If the file is
      // already visible in some group, showTextDocument reuses that editor.
      const editor = await vscode.window.showTextDocument(doc, {
        viewColumn: vscode.ViewColumn.One,
        preview: true,
      });
      if (line > 0) {
        const pos = new vscode.Position(Math.max(0, line - 1), 0);
        editor.selection = new vscode.Selection(pos, pos);
        editor.revealRange(new vscode.Range(pos, pos), vscode.TextEditorRevealType.InCenter);
      }
      // Keep the chat panel visible so the user isn't left looking at an empty editor.
      if (this.panel) {
        this.panel.reveal(vscode.ViewColumn.Two, true);
      }
    } catch (e) {
      this.debug(`open_file failed: ${e}`);
    }
  }

  private async exportSession(historyPath: string, sessionTitle: string): Promise<void> {
    try {
      this.debug(`ACTION→ export_session historyPath=${historyPath}`);
      if (!fs.existsSync(historyPath)) {
        this.postToWebview({ type: 'error', message: `History file not found: ${historyPath}` });
        return;
      }
      const lines = fs.readFileSync(historyPath, 'utf-8').split('\n');
      const parts: string[] = [`# ${sessionTitle}\n\n_Exported from JiuwenSwarm IDE_\n\n---\n`];
      for (const raw of lines) {
        const line = raw.trim();
        if (!line) continue;
        try {
          const obj = JSON.parse(line) as { role?: string; content?: unknown };
          if (!obj.role || obj.role === 'system') continue;
          let text = '';
          if (typeof obj.content === 'string') {
            text = obj.content.trim();
          } else if (Array.isArray(obj.content)) {
            text = (obj.content as Array<{ type?: string; text?: string }>)
              .filter((b) => b.type === 'text' && b.text)
              .map((b) => b.text!.trim())
              .join('\n');
          }
          if (!text) continue;
          const heading = obj.role === 'user' ? '## User' : '## Assistant';
          parts.push(`\n${heading}\n\n${text}\n\n---\n`);
        } catch {
          // skip malformed lines
        }
      }
      const safeName = sessionTitle.replace(/[^\w\- ]/g, '').trim().replace(/\s+/g, '-') || 'session';
      const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? path.dirname(historyPath);
      const outPath = path.join(workspaceRoot, `jiuwenswarm-export-${safeName}.md`);
      fs.writeFileSync(outPath, parts.join(''), 'utf-8');
      await this.openFile(outPath, 0);
      this.postToWebview({ type: 'export_done', path: outPath });
    } catch (e) {
      this.debug(`export_session failed: ${e}`);
      this.postToWebview({ type: 'error', message: `Export failed: ${e}` });
    }
  }

  /** Navigate to a symbol definition mentioned by the agent. */
  private async navigateSymbol(symbol: string): Promise<void> {
    try {
      const symbols = await vscode.commands.executeCommand<vscode.SymbolInformation[] | vscode.DocumentSymbol[]>(
        'vscode.executeWorkspaceSymbolProvider',
        symbol,
      );
      if (!symbols || symbols.length === 0) {
        vscode.window.showInformationMessage(`JiuwenSwarm: No definition found for "${symbol}"`);
        return;
      }
      const first = symbols[0];
      let uri: vscode.Uri;
      let range: vscode.Range;
      if ('location' in first) {
        uri = first.location.uri;
        range = first.location.range;
      } else {
        // DocumentSymbol
        uri = vscode.window.activeTextEditor?.document.uri ?? vscode.Uri.file('');
        range = first.range;
      }
      const doc = await vscode.workspace.openTextDocument(uri);
      const editor = await vscode.window.showTextDocument(doc);
      editor.selection = new vscode.Selection(range.start, range.start);
      editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
    } catch (e) {
      this.debug(`navigate_symbol failed: ${e}`);
    }
  }

  // ──────────────────────────────────────────
  // Rewind
  // ──────────────────────────────────────────
  private async handleRewind(): Promise<void> {
    const snapshots = DiffApplier.getLastTurnSnapshots();
    if (snapshots.size === 0) return;
    this.debug(`ACTION→ rewind ${snapshots.size} file(s)`);
    const { restored, failed } = await DiffApplier.performRewind(snapshots);
    DiffApplier.clearLastTurnSnapshots();
    const message = failed === 0
      ? `Rewound ${restored} file(s)`
      : `Rewound ${restored} file(s), ${failed} failed`;
    this.postToWebview({ type: 'rewind_done', message, restored, failed });
    this.debug(`REWIND→ ${message}`);
  }

  // ──────────────────────────────────────────
  // Git quick actions
  // ──────────────────────────────────────────
  private gitRoot(): string | undefined {
    return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  }

  private gitEnabled(): boolean {
    return vscode.workspace.getConfiguration('jiuwenswarm').get<boolean>('gitEnabled', false);
  }

  private sendGitStatus(): void {
    if (!this.gitEnabled()) return;
    const root = this.gitRoot();
    if (!root) return;
    try {
      const branch = execSync('git rev-parse --abbrev-ref HEAD', { cwd: root, encoding: 'utf-8', timeout: 3000 }).trim();
      if (!branch || branch === 'HEAD') return; // detached HEAD or not a git repo
      const status = execSync('git status --porcelain', { cwd: root, encoding: 'utf-8', timeout: 3000 }).trim();
      const changedCount = status ? status.split('\n').filter((l) => l.trim()).length : 0;
      this.postToWebview({ type: 'git_status', branch, changedCount });
    } catch {
      // not a git repo or git unavailable — hide the bar silently
    }
  }

  private async handleGitCommit(): Promise<void> {
    const root = this.gitRoot();
    if (!root) return;
    const defaultMsg = this.latestInput
      ? `AI: ${this.latestInput.substring(0, 72)}`
      : 'AI: agent changes';
    const message = await vscode.window.showInputBox({
      prompt: 'Commit message (staged: tracked modified files)',
      value: defaultMsg,
      placeHolder: 'Enter commit message',
    });
    if (!message) return; // user cancelled
    try {
      execSync('git add -u', { cwd: root, encoding: 'utf-8', timeout: 5000 });
      execSync(`git commit -m ${JSON.stringify(message)}`, { cwd: root, encoding: 'utf-8', timeout: 10000 });
      const hash = execSync('git rev-parse --short HEAD', { cwd: root, encoding: 'utf-8', timeout: 3000 }).trim();
      this.postToWebview({ type: 'git_committed', hash });
      this.sendGitStatus();
    } catch (e: unknown) {
      this.postToWebview({ type: 'git_error', message: (e instanceof Error) ? e.message : 'commit failed' });
    }
  }

  private async handleGitPush(): Promise<void> {
    const root = this.gitRoot();
    if (!root) return;
    try {
      execSync('git push', { cwd: root, encoding: 'utf-8', timeout: 20000 });
      this.postToWebview({ type: 'git_pushed' });
      this.sendGitStatus();
    } catch (e: unknown) {
      this.postToWebview({ type: 'git_error', message: (e instanceof Error) ? e.message.split('\n')[0] : 'push failed' });
    }
  }

  private debug(line: string): void {
    if (this.debugEnabled) {
      console.log(`[JiuwenSwarm] ${line}`);
      this.postToWebview({ type: 'debug_log', line });
    }
  }

  refreshMetrics(): void {
    const ideContext = collectContext();
    this.updateMetrics(this.latestInput, ideContext.metrics, 0);
  }

  resetMetrics(): void {
    this.resetSessionMetrics();
  }

  private updateMetrics(content: string, contextMetrics: { byteLength: number; estimatedTokens: number }, attachmentsCount: number): void {
    const promptEstimatedTokens = estimateTokens(content) + contextMetrics.estimatedTokens;
    this.publishMetrics({
      contextBytes: contextMetrics.byteLength,
      contextEstimatedTokens: contextMetrics.estimatedTokens,
      promptEstimatedTokens,
      sessionTokens: this.sessionTokens,
      sessionCostUsd: this.sessionCostUsd,
      hasCost: this.hasCost,
      attachmentsCount,
    });
  }

  private publishMetrics(metrics: SessionMetrics): void {
    this.latestContextMetrics = metrics;
    this.statusBar.setMetrics(metrics);
    this.postToWebview({ type: 'metrics', metrics });
  }

  private resetSessionMetrics(): void {
    this.sessionTokens = 0;
    this.sessionCostUsd = 0;
    this.hasCost = false;
    this.publishMetrics({
      ...this.latestContextMetrics,
      sessionTokens: 0,
      sessionCostUsd: 0,
      hasCost: false,
    });
  }

  // ──────────────────────────────────────────
  // Memory polling
  // ──────────────────────────────────────────
  private startMemoryPolling(): void {
    this.stopMemoryPolling();
    this.pollMemory();
    this.memoryTimer = setInterval(() => this.pollMemory(), 10000);
  }

  private stopMemoryPolling(): void {
    if (this.memoryTimer) {
      clearInterval(this.memoryTimer);
      this.memoryTimer = undefined;
    }
  }

  private async pollMemory(): Promise<void> {
    if (!this.panel || !this.ws.isConnected()) return;
    try {
      const usage = await this.session.getMemoryUsage();
      if (this.panel) {
        this.postToWebview({
          type: 'memory',
          rssMb: usage.rss_mb,
          totalMb: usage.total_mb,
          availableMb: usage.available_mb,
        });
      }
    } catch {
      // memory.compute may be unavailable; silently ignore
    }
  }

  private emptyMetrics(): SessionMetrics {
    return {
      contextBytes: 0,
      contextEstimatedTokens: 0,
      promptEstimatedTokens: 0,
      sessionTokens: 0,
      sessionCostUsd: 0,
      hasCost: false,
      attachmentsCount: 0,
    };
  }

  private flushPendingMessages(): void {
    if (!this.panel || this.pendingWebviewMessages.length === 0) return;
    const pending = this.pendingWebviewMessages.splice(0);
    pending.forEach((msg) => this.panel?.webview.postMessage(msg));
  }

  // ──────────────────────────────────────────
  // HTML
  // ──────────────────────────────────────────
  private getHtml(webview: vscode.Webview): string {
    try {
      let html = fs.readFileSync(this.webviewHtmlPath, 'utf-8');
      html = html.replace(
        /<meta http-equiv="Content-Security-Policy"[^>]*>/,
        `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline' ${webview.cspSource}; script-src 'unsafe-inline' ${webview.cspSource}; img-src data: blob: ${webview.cspSource};">`,
      );
      return html;
    } catch {
      return this.getFallbackHtml();
    }
  }

  private getFallbackHtml(): string {
    return `<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
body { background: #1e1e1e; color: #d4d4d4; font-family: sans-serif; padding: 16px; }
</style></head>
<body>
<p>⚠ Could not load chat UI.<br>
Make sure <code>resources/chat.html</code> exists in the extension folder.</p>
</body></html>`;
  }
}
