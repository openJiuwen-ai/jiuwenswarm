import * as path from 'path';
import { AgentLane, TeamTask, TeamMessage, SwarmSnapshot } from './SwarmState';

/**
 * Mirrors SwarmStateManager.kt — maintains in-memory swarm state built from
 * team event deltas that arrive over the WebSocket as "team.event:{json}"
 * prefixed chat.delta messages.
 */
export class SwarmStateManager {
  private sessionId = '';
  private teamName = '';
  // Map preserves insertion order so join order is maintained
  private lanes = new Map<string, AgentLane>();
  private tasks = new Map<string, TeamTask>();
  private messages: TeamMessage[] = [];  // ring buffer, max 50
  private lastEventAt = 0;

  // ──────────────────────────────────────────
  // Public API
  // ──────────────────────────────────────────

  /**
   * Parse the JSON string that follows "team.event:" and apply it to state.
   * Expected shape: { "event": { "type": "team.member.spawned", ... } }
   */
  applyTeamEvent(json: string): void {
    try {
      const root = JSON.parse(json) as Record<string, unknown>;
      const event = this.normalizeEvent(((root['event'] as Record<string, unknown>) ?? root));
      const type = event['type'] as string | undefined;
      if (!type) return;
      this.lastEventAt = (event['timestamp'] as number | undefined) ?? Date.now();
      const teamName = event['team_name'] as string | undefined;
      if (teamName && !this.teamName) this.teamName = teamName;

      switch (type) {
        case 'team.member.spawned':           this.onMemberSpawned(event); break;
        case 'team.member.status_changed':    this.onMemberStatusChanged(event); break;
        case 'team.member.execution_changed': this.onMemberExecutionChanged(event); break;
        case 'team.member.activity_changed':  this.onMemberActivityChanged(event); break;
        case 'team.member.shutdown':          this.onMemberShutdown(event); break;
        case 'team.task.created':             this.onTaskCreated(event); break;
        case 'team.task.claimed':             this.onTaskClaimed(event); break;
        case 'team.task.started':             this.onTaskStarted(event); break;
        case 'team.task.completed':           this.onTaskCompleted(event); break;
        case 'team.task.cancelled':           this.onTaskCancelled(event); break;
        case 'team.task.status_snapshot':     this.onTaskStatusSnapshot(event); break;
        default:
          if (type.startsWith('team.message.')) this.onTeamMessage(event);
      }
    } catch {
      // malformed JSON — ignore silently
    }
  }

  /**
   * Map the gateway's team-event field names to the shape this manager consumes.
   * The gateway sends member_id/name/mode/team_id; the lanes here use
   * member_name/display_name/role/team_name. Runs before dispatch so every
   * handler below reads one consistent vocabulary.
   */
  private normalizeEvent(raw: Record<string, unknown>): Record<string, unknown> {
    const e: Record<string, unknown> = { ...raw };
    if (e['member_id'] !== undefined && e['member_name'] === undefined) e['member_name'] = e['member_id'];
    if (e['name'] !== undefined && e['display_name'] === undefined) e['display_name'] = e['name'];
    if (e['mode'] !== undefined && e['role'] === undefined) {
      e['role'] = typeof e['mode'] === 'string' ? e['mode'].toUpperCase() : e['mode'];
    }
    if (e['team_id'] !== undefined && e['team_name'] === undefined) e['team_name'] = e['team_id'];
    return e;
  }

  /** Map a gateway status into the map's lane vocabulary (READY/BUSY/PAUSED/SHUTDOWN). */
  private normalizeLaneStatus(s: unknown): string | undefined {
    if (typeof s !== 'string' || !s) return undefined;
    switch (s.toUpperCase()) {
      case 'IDLE':      return 'READY';
      case 'RUNNING':   return 'RUNNING';
      case 'COMPLETED': return 'SHUTDOWN';
      default:          return s.toUpperCase();
    }
  }

  /**
   * Called from the chat.tool_call / chat.tool_update branches of onJiuwenMessage.
   * memberName may be undefined if the server does not include it in the payload.
   * args (the parsed tool arguments) lets the feed say what an action touches —
   * e.g. a file_path, a glob pattern, or the shell command.
   */
  applyToolCall(toolName: string, filePath: string | undefined, memberName: string | undefined, args?: Record<string, unknown>): void {
    if (!memberName) return;
    const lane = this.getOrStubLane(memberName);
    lane.lastToolName = toolName;
    const activity = this.formatActivity(toolName, filePath, args);
    lane.currentActivity = activity;
    lane.lastActivePath = filePath ?? null;
    lane.lastActiveAt = Date.now();
    lane.status = 'BUSY';
    this.pushFeed(lane, activity, lane.lastActiveAt);
    this.lastEventAt = lane.lastActiveAt;
  }

  applyModelCallStart(memberName?: string): void {
    if (!memberName) return;
    const lane = this.getOrStubLane(memberName);
    lane.status = 'BUSY';
    lane.currentActivity = 'thinking…';
    lane.lastActiveAt = Date.now();
    this.pushFeed(lane, 'thinking…', lane.lastActiveAt);
    this.lastEventAt = lane.lastActiveAt;
  }

  /** First streamed token for a call: flip the lane from "thinking…" to "generating…". */
  applyModelTokenStart(memberName?: string): void {
    if (!memberName) return;
    const lane = this.lanes.get(memberName);
    if (!lane || lane.currentActivity !== 'thinking…') return;
    lane.currentActivity = 'generating…';
    lane.lastActiveAt = Date.now();
  }

  applyModelCallEnd(memberName?: string): void {
    if (!memberName) return;
    const lane = this.lanes.get(memberName);
    if (!lane) return;
    if (lane.currentActivity === 'thinking…' || lane.currentActivity === 'generating…') {
      lane.currentActivity = null;
    }
    lane.lastActiveAt = Date.now();
  }

  snapshot(): SwarmSnapshot {
    // Stable insertion order (join order) — lanes must not jump around as
    // statuses change; the webview renders status separately from position.
    const lanes = [...this.lanes.values()];
    const sortedTasks = [...this.tasks.values()].sort((a, b) => a.createdAt - b.createdAt);
    return {
      sessionId: this.sessionId,
      teamName: this.teamName,
      lanes,
      tasks: sortedTasks,
      messages: [...this.messages],
      lastEventAt: this.lastEventAt,
    };
  }

  reset(newSessionId: string): void {
    this.sessionId = newSessionId;
    this.teamName = '';
    this.lanes.clear();
    this.tasks.clear();
    this.messages = [];
    this.lastEventAt = 0;
  }

  isEmpty(): boolean {
    return this.lanes.size === 0;
  }

  // ──────────────────────────────────────────
  // Event handlers
  // ──────────────────────────────────────────

  private onMemberSpawned(e: Record<string, unknown>): void {
    const name = e['member_name'] as string | undefined;
    if (!name) return;
    const status = this.normalizeLaneStatus(e['status']) ?? 'READY';
    const prompt = e['prompt'] as string | undefined;
    const existing = this.lanes.get(name);
    if (existing) {
      existing.displayName = (e['display_name'] as string) ?? existing.displayName;
      existing.role = (e['role'] as string) ?? existing.role;
      existing.status = status;
      if (prompt) {
        existing.currentTaskTitle = prompt;
        existing.currentActivity = 'starting';
      }
      existing.startedAt = existing.startedAt ?? this.lastEventAt;
      this.pushFeed(existing, 'spawned', this.lastEventAt);
      existing.lastActiveAt = this.lastEventAt;
    } else {
      this.lanes.set(name, {
        memberName: name,
        displayName: (e['display_name'] as string) ?? name,
        role: (e['role'] as string) ?? 'TEAMMATE',
        status: status,
        executionStatus: 'IDLE',
        currentTaskId: null,
        currentTaskTitle: prompt ?? null,
        currentActivity: prompt ? 'starting' : null,
        lastToolName: null,
        lastActivePath: null,
        lastActiveAt: this.lastEventAt,
        startedAt: this.lastEventAt,
        activityFeed: [{ text: 'spawned', at: this.lastEventAt }],
        messageCount: 0,
        tasksDone: 0,
      });
    }
  }

  private onMemberStatusChanged(e: Record<string, unknown>): void {
    const name = e['member_name'] as string | undefined;
    if (!name) return;
    const lane = this.getOrStubLane(name);
    const status = this.normalizeLaneStatus(e['new_status'] ?? e['status']);
    if (status) lane.status = status;
    const outcome = e['outcome'] as string | undefined;
    if (outcome && status === 'SHUTDOWN') {
      lane.currentActivity = `done · ${outcome}`;
    }
    if (status === 'SHUTDOWN') this.pushFeed(lane, 'finished', this.lastEventAt);
    else if (status === 'PAUSED') this.pushFeed(lane, 'paused', this.lastEventAt);
    lane.lastActiveAt = this.lastEventAt;
  }

  private onMemberExecutionChanged(e: Record<string, unknown>): void {
    const name = e['member_name'] as string | undefined;
    if (!name) return;
    const lane = this.getOrStubLane(name);
    const execution = e['new_status'] ?? e['execution_status'];
    if (typeof execution === 'string' && execution) lane.executionStatus = execution.toUpperCase();
    lane.lastActiveAt = this.lastEventAt;
  }

  /** Live mid-run activity (e.g. a swarmflow worker tool call) → lane activity. */
  private onMemberActivityChanged(e: Record<string, unknown>): void {
    const name = e['member_name'] as string | undefined;
    if (!name) return;
    const activity = e['activity'] as string | undefined;
    if (!activity) return;
    const lane = this.getOrStubLane(name);
    lane.status = 'BUSY';
    lane.currentActivity = activity;
    this.pushFeed(lane, activity, this.lastEventAt);
    lane.lastActiveAt = this.lastEventAt;
  }

  private onMemberShutdown(e: Record<string, unknown>): void {
    const name = e['member_name'] as string | undefined;
    if (!name) return;
    const lane = this.lanes.get(name);
    if (!lane) return;
    lane.status = 'SHUTDOWN';
    lane.executionStatus = 'IDLE';
    lane.currentActivity = null;
    this.pushFeed(lane, 'shutdown', this.lastEventAt);
    lane.lastActiveAt = this.lastEventAt;
  }

  private onTaskCreated(e: Record<string, unknown>): void {
    const id = e['task_id'] as string | undefined;
    if (!id) return;
    this.tasks.set(id, {
      taskId: id,
      title: (e['title'] as string) ?? id,
      status: 'pending',
      assignee: null,
      createdAt: this.lastEventAt,
    });
  }

  private onTaskClaimed(e: Record<string, unknown>): void {
    const id = e['task_id'] as string | undefined;
    if (!id) return;
    const task = this.tasks.get(id)
      ?? { taskId: id, title: (e['title'] as string) ?? id, status: 'pending', assignee: null, createdAt: this.lastEventAt };
    this.tasks.set(id, task);
    // Gateway names the claimer member_id; normalizeEvent maps it to member_name.
    task.assignee = (e['assignee'] as string | null) ?? (e['member_name'] as string | null) ?? null;
    // Server carries the authoritative status (e.g. "in_progress") — prefer it;
    // fall back to started semantics for older gateways that only mark claimed.
    task.status = (e['status'] as string) ?? 'in_progress';
    // Link task into the lane so the board reflects the current work.
    if (task.assignee) {
      const lane = this.lanes.get(task.assignee);
      if (lane) {
        lane.currentTaskId = id;
        lane.currentTaskTitle = task.title;
        lane.lastActiveAt = this.lastEventAt;
      }
    }
  }

  /** Convergence event: apply a task's authoritative status/assignee snapshot. */
  private onTaskStatusSnapshot(e: Record<string, unknown>): void {
    const id = e['task_id'] as string | undefined;
    if (!id) return;
    const status = e['status'] as string | undefined;
    const assignee = (e['assignee'] as string | null) ?? (e['member_name'] as string | null) ?? null;
    const task = this.tasks.get(id)
      ?? { taskId: id, title: (e['title'] as string) ?? id, status: 'pending', assignee: null, createdAt: this.lastEventAt };
    if (status) task.status = status;
    if (assignee) task.assignee = assignee;
    this.tasks.set(id, task);
    if (task.assignee) {
      const lane = this.lanes.get(task.assignee);
      if (lane && task.status === 'completed' && lane.currentTaskId === id) {
        lane.currentTaskId = null;
        lane.currentTaskTitle = null;
        lane.tasksDone++;
        lane.lastActiveAt = this.lastEventAt;
      }
    }
  }

  private onTaskStarted(e: Record<string, unknown>): void {
    const id = e['task_id'] as string | undefined;
    if (!id) return;
    const task = this.tasks.get(id) ?? { taskId: id, title: id, status: 'pending', assignee: null, createdAt: this.lastEventAt };
    this.tasks.set(id, task);
    task.status = 'in_progress';
    task.assignee = (e['assignee'] as string) ?? task.assignee;
    if (task.assignee) {
      const lane = this.lanes.get(task.assignee);
      if (lane) {
        lane.currentTaskId = id;
        lane.currentTaskTitle = task.title;
        lane.lastActiveAt = this.lastEventAt;
      }
    }
  }

  private onTaskCompleted(e: Record<string, unknown>): void {
    const id = e['task_id'] as string | undefined;
    if (!id) return;
    const task = this.tasks.get(id);
    if (!task) return;
    task.status = 'completed';
    if (task.assignee) {
      const lane = this.lanes.get(task.assignee);
      if (lane && lane.currentTaskId === id) {
        lane.currentTaskId = null;
        lane.currentTaskTitle = null;
        lane.tasksDone++;
        lane.lastActiveAt = this.lastEventAt;
      }
    }
  }

  private onTaskCancelled(e: Record<string, unknown>): void {
    const id = e['task_id'] as string | undefined;
    if (!id) return;
    const task = this.tasks.get(id);
    if (!task) return;
    task.status = 'cancelled';
    if (task.assignee) {
      const lane = this.lanes.get(task.assignee);
      if (lane && lane.currentTaskId === id) {
        lane.currentTaskId = null;
        lane.currentTaskTitle = null;
        lane.lastActiveAt = this.lastEventAt;
      }
    }
  }

  private onTeamMessage(e: Record<string, unknown>): void {
    const from = e['from_member'] as string | undefined;
    if (!from) return;
    const lane = this.getOrStubLane(from);
    lane.messageCount++;
    lane.lastActiveAt = this.lastEventAt;
    // Capture message content for the inter-agent message log
    const content = e['content'] as string | undefined;
    if (content) {
      const to = (e['to_member'] as string | undefined) ?? null;
      this.messages.push({ from, to, content, timestamp: this.lastEventAt });
      if (this.messages.length > 50) this.messages.shift();
    }
  }

  // ──────────────────────────────────────────
  // Helpers
  // ──────────────────────────────────────────

  private getOrStubLane(memberName: string): AgentLane {
    const existing = this.lanes.get(memberName);
    if (existing) return existing;
    const now = this.lastEventAt || Date.now();
    const stub: AgentLane = {
      memberName,
      displayName: memberName,
      role: 'TEAMMATE',
      status: 'READY',
      executionStatus: 'IDLE',
      currentTaskId: null,
      currentTaskTitle: null,
      currentActivity: null,
      lastToolName: null,
      lastActivePath: null,
      lastActiveAt: now,
      startedAt: now,
      activityFeed: [],
      messageCount: 0,
      tasksDone: 0,
    };
    this.lanes.set(memberName, stub);
    return stub;
  }

  /** Append one entry to a lane's activity feed, keeping it capped.
   *  A duplicate of the previous entry within 1s (the tool_call + tool_update
   *  pair for one call) is collapsed into the existing row instead of flooding
   *  the feed; genuinely separate calls still get their own rows. */
  private pushFeed(lane: AgentLane, text: string, at: number): void {
    const last = lane.activityFeed[lane.activityFeed.length - 1];
    if (last && last.text === text && at - last.at < 1000) {
      last.at = at;
      return;
    }
    lane.activityFeed.push({ text, at });
    if (lane.activityFeed.length > 10) lane.activityFeed.shift();
  }

  private formatActivity(toolName: string, filePath: string | undefined, args?: Record<string, unknown>): string {
    const base = filePath ? path.basename(filePath) : undefined;
    const pattern = args && typeof args['pattern'] === 'string' ? args['pattern'] : undefined;
    const command = args && (
      (typeof args['command'] === 'string' && args['command']) ||
      (typeof args['cmd'] === 'string' && args['cmd']) ||
      (typeof args['command_line'] === 'string' && args['command_line'])
    );
    switch (toolName) {
      case 'write_file':
      case 'create_file':        return base ? `writing · ${base}` : 'writing';
      case 'str_replace_editor':
      case 'edit_file':          return base ? `editing · ${base}` : 'editing';
      case 'read_file':          return base ? `reading · ${base}` : 'reading';
      case 'bash':
      case 'run_command':
      case 'powershell':
      case 'shell':
      case 'cmd':                return command ? `running · ${this.trunc(command, 40)}` : 'running command';
      case 'list_files':         return pattern ? `listing · ${pattern}` : base ? `listing · ${base}` : 'listing';
      case 'glob':
      case 'grep':
      case 'search_files':       return pattern ? `searching · ${pattern}` : base ? `searching · ${base}` : 'searching';
      case 'view_task':          return args && args['task_id'] ? `viewing task · ${args['task_id']}` : 'viewing task';
      case 'claim_task':         return args && args['task_id'] ? `claiming task · ${args['task_id']}` : 'claiming task';
      case 'send_message':       return args && args['to'] ? `messaging · ${args['to']}` : 'messaging';
      default:                   return toolName;
    }
  }

  private trunc(s: string, n: number): string {
    return s.length > n ? s.slice(0, n) + '\u2026' : s;
  }
}
