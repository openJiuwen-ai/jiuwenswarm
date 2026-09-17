import { webClient } from '../services/webClient';
import type { Message } from '../types';
import type {
  HumanShareCommand,
  TeamTask,
  TeamMemberExecutionEvent,
  TeamTaskStatus,
} from '../stores/sessionStore';
import { normalizeFinalContent } from '../utils/finalContent';
import {
  findOverlappingFileExecutionEvent,
  mergeFileDownloadItems,
} from '../utils/fileDownloadDedup';
import {
  createTaskProgressBaseline,
  registerConfirmedTaskCreation,
  type TaskProgressBaseline,
} from './teamTaskProgressBaseline';

interface TeamMember {
  id: string;
  member_id: string;
  status: string;
  timestamp: number;
  name?: string;
  execution_status?: string | null;
  mode?: string;
  role?: string;
  cli_agent?: string | null;
}

interface TeamTaskEvent {
  id: string;
  type: string;
  team_id: string;
  task_id: string;
  status: string;
  timestamp: number;
  member_id?: string;
  assignee?: string;
  team_name?: string;
  title?: string;
  content?: string;
  // Truncation observability flags — kept aligned with TeamTaskEvent in
  // stores/sessionStore.ts and components/teamArea/shared.tsx.
  title_truncated?: boolean;
  title_original_size?: number;
  content_truncated?: boolean;
  content_original_size?: number;
  updated_at?: number | string | null;
}

export interface TeamHistoryPanelState {
  members: TeamMember[];
  tasks: TeamTask[];
  taskEvents: TeamTaskEvent[];
  executionEvents: TeamMemberExecutionEvent[];
  messages: Message[];
  humanShareCommands: HumanShareCommand[];
  taskProgressBaseline: TaskProgressBaseline;
}

interface TeamHistoryGetResponse {
  records: Record<string, unknown>[];
  session_id: string;
  next_cursor?: number | string | null;
  has_more?: boolean;
}

const TEAM_TASK_STATUSES = new Set<TeamTaskStatus>([
  'pending',
  'blocked',
  'planning',
  'in_progress',
  'in_review',
  'completed',
  'cancelled',
]);

const TEAM_STATE_EVENT_TYPES = new Set([
  'team.message',
  'team.event',
  'team.member',
  'team.task',
]);

const HISTORY_RECORD_META_KEYS = new Set([
  'id',
  'role',
  'request_id',
  'channel_id',
  'session_id',
  'timestamp',
  'event_type',
  'event_payload',
  'mode',
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function pickString(input: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = input[key];
    if (typeof value === 'string' && value.trim()) {
      return value.trim();
    }
  }
  return '';
}

function stringList(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) {
    return undefined;
  }
  const items = value.filter(
    (item): item is string => typeof item === 'string' && item.trim().length > 0
  );
  return items.length ? items : undefined;
}

function recordTimestamp(record: Record<string, unknown>): number {
  return normalizeTimestamp(record.timestamp, Date.now());
}

function normalizeTimestamp(value: unknown, fallback: number): number {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value > 1_000_000_000_000 ? value : value * 1000;
  }
  if (typeof value === 'string') {
    const parsed = Date.parse(value);
    if (!Number.isNaN(parsed)) {
      return parsed;
    }
  }
  return fallback;
}

function taskTimestamp(rawTask: Record<string, unknown>, fallback: number): number {
  return normalizeTimestamp(rawTask.updated_at, fallback);
}

function extractTeamEvent(record: Record<string, unknown>): Record<string, unknown> | null {
  if (isRecord(record.event)) {
    return record.event;
  }
  if (isRecord(record.event_payload)) {
    if (isRecord(record.event_payload.event)) {
      return record.event_payload.event;
    }
    return record.event_payload;
  }

  const payload: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(record)) {
    if (!HISTORY_RECORD_META_KEYS.has(key)) {
      payload[key] = value;
    }
  }
  if (isRecord(payload.event)) {
    return payload.event;
  }
  return Object.keys(payload).length > 0 ? payload : null;
}

function getRecordSessionId(record: Record<string, unknown>): string {
  if (isRecord(record.event_payload)) {
    const payloadSessionId = pickString(record.event_payload, ['session_id']);
    if (payloadSessionId) {
      return payloadSessionId;
    }
  }
  return pickString(record, ['session_id']);
}

function normalizeTaskStatus(status: unknown): TeamTaskStatus {
  return typeof status === 'string' && TEAM_TASK_STATUSES.has(status as TeamTaskStatus)
    ? status as TeamTaskStatus
    : 'pending';
}

function normalizeTaskStatusWithFallback(
  status: unknown,
  fallback: TeamTaskStatus
): TeamTaskStatus {
  return typeof status === 'string' && TEAM_TASK_STATUSES.has(status as TeamTaskStatus)
    ? status as TeamTaskStatus
    : fallback;
}

// Task status is resolved server-side (swarm layer) and carried on the event,
// so restore reads it directly instead of re-deriving it from the event type.
// Records persisted before the convergence may lack a status; those fall back
// to 'pending' via normalizeTaskStatus.

function shouldKeepMember(memberId: string): boolean {
  return Boolean(memberId) && memberId !== 'user' && memberId !== 'team_leader';
}

function isTeamTeammateRecord(record: Record<string, unknown>): boolean {
  return typeof record.role === 'string' && record.role.trim().toLowerCase() === 'teammate';
}

function eventId(...parts: unknown[]): string {
  return parts
    .map((part) => String(part ?? '').trim())
    .filter(Boolean)
    .join(':')
    .replace(/[^a-zA-Z0-9:_-]+/g, '-')
    .slice(0, 180);
}

function compactString(value: unknown): string {
  if (typeof value === 'string') {
    return value;
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value ?? '');
  }
}

function resolveTaskStatus(
  rawStatus: unknown,
  existing: TeamTask | undefined,
  fallbackStatus: TeamTaskStatus
): TeamTaskStatus {
  const nextStatus = normalizeTaskStatusWithFallback(
    rawStatus,
    existing?.status || fallbackStatus
  );
  if (existing?.status === 'completed' && nextStatus !== 'completed') {
    return 'completed';
  }
  return nextStatus;
}

function parseJsonRecord(value: unknown): Record<string, unknown> | null {
  if (isRecord(value)) {
    return value;
  }
  if (typeof value !== 'string' || !value.trim()) {
    return null;
  }
  try {
    const parsed = JSON.parse(value);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function isTeamModeRecord(record: Record<string, unknown>): boolean {
  return typeof record.mode === 'string' && record.mode.trim().toLowerCase() === 'team';
}

function extractToolCallInput(record: Record<string, unknown>): {
  name: string;
  args: Record<string, unknown>;
} | null {
  if (record.event_type !== 'chat.tool_call' || !isTeamModeRecord(record)) {
    return null;
  }
  if (!isRecord(record.tool_call)) {
    return null;
  }
  const name = pickString(record.tool_call, ['name']);
  if (!name) {
    return null;
  }
  return {
    name,
    args: parseJsonRecord(record.tool_call.arguments) || {},
  };
}

function parseShutdownMemberName(value: unknown): string {
  if (typeof value !== 'string') {
    return '';
  }
  const match = value.match(/Member shutdown:\s*member_name=([^\s,]+)/);
  return match?.[1]?.trim() || '';
}

function extractShutdownMemberFromToolResult(record: Record<string, unknown>): string {
  if (record.event_type !== 'chat.tool_result' || !isTeamModeRecord(record)) {
    return '';
  }
  const payload = extractTeamEvent(record) || record;
  const toolResult = isRecord(payload.tool_result) ? payload.tool_result : payload;
  const toolName = pickString(toolResult, ['tool_name', 'name']) || pickString(payload, ['tool_name', 'name']);
  if (toolName !== 'shutdown_member') {
    return parseShutdownMemberName(toolResult.result);
  }
  return parseShutdownMemberName(toolResult.result) || parseShutdownMemberName(toolResult.summary);
}

function extractTracerInput(record: Record<string, unknown>): {
  name: string;
  args: Record<string, unknown>;
} | null {
  if (record.event_type !== 'chat.tracer_agent' || !isTeamModeRecord(record)) {
    return null;
  }
  const name = pickString(record, ['name']);
  if (!name || !isRecord(record.inputs)) {
    return null;
  }
  const args = isRecord(record.inputs.inputs) ? record.inputs.inputs : record.inputs;
  return { name, args };
}

function extractTracerData(record: Record<string, unknown>): {
  name: string;
  data: Record<string, unknown>;
  args: Record<string, unknown>;
} | null {
  if (record.event_type !== 'chat.tracer_agent' || !isTeamModeRecord(record)) {
    return null;
  }
  if (record.status !== 'finish') {
    return null;
  }
  const name = pickString(record, ['name']);
  if (!name || !isRecord(record.outputs) || !isRecord(record.outputs.outputs)) {
    return null;
  }
  if (record.outputs.outputs.success === false) {
    return null;
  }
  const args = isRecord(record.inputs)
    ? isRecord(record.inputs.inputs)
      ? record.inputs.inputs
      : record.inputs
    : {};
  const data = record.outputs.outputs.data;
  return isRecord(data) ? { name, data, args } : null;
}

function collectTeamState(records: Record<string, unknown>[], sessionId: string): TeamHistoryPanelState {
  const members = new Map<string, TeamMember>();
  const taskEvents = new Map<string, TeamTaskEvent>();
  const tasks = new Map<string, TeamTask>();
  const executionEvents = new Map<string, TeamMemberExecutionEvent>();
  const humanShareCommands = new Map<string, HumanShareCommand>();
  const messages: Message[] = [];
  const shutdownMembers = new Set<string>();
  let taskProgressBaseline = createTaskProgressBaseline();
  let hasSeenMember = false;

  const addMember = (memberId: string, timestamp: number) => {
    if (!shouldKeepMember(memberId)) {
      return;
    }
    if (shutdownMembers.has(memberId)) {
      return;
    }
    hasSeenMember = true;
    const existing = members.get(memberId);
    members.set(memberId, {
      id: `hist-member-${memberId}`,
      member_id: memberId,
      status: existing?.status || 'idle',
      timestamp: Math.max(existing?.timestamp || 0, timestamp),
      name: existing?.name,
      execution_status: existing?.execution_status || 'idle',
      mode: existing?.mode,
    });
  };

  const applyMemberShutdown = (memberId: string) => {
    if (!shouldKeepMember(memberId)) {
      return;
    }
    shutdownMembers.add(memberId);
    members.delete(memberId);
  };

  const upsertHumanShareCommand = (
    memberName: string,
    patch: Partial<HumanShareCommand>,
    timestamp: number
  ) => {
    if (!memberName) {
      return;
    }
    const existing = humanShareCommands.get(memberName);
    const teamName = patch.teamName || existing?.teamName || '';
    const sessionRef =
      patch.sessionRef ||
      existing?.sessionRef ||
      (teamName ? `team_${teamName}_session_${sessionId}` : '');
    humanShareCommands.set(memberName, {
      memberName,
      displayName: patch.displayName || existing?.displayName,
      sessionId,
      teamName,
      sessionRef,
      joinCommand:
        patch.joinCommand ||
        existing?.joinCommand ||
        (sessionRef ? `/join ${sessionRef} as ${memberName}` : ''),
      exitCommand:
        patch.exitCommand ||
        existing?.exitCommand ||
        (sessionRef ? `/exit ${sessionRef}` : ''),
      status:
        patch.status === 'pending' && existing?.status && existing.status !== 'pending'
          ? existing.status
          : patch.status || existing?.status || 'pending',
      sourceChannel: patch.sourceChannel || existing?.sourceChannel,
      userId: patch.userId || existing?.userId,
      updatedAt: Math.max(existing?.updatedAt || 0, timestamp),
    });
  };

  const upsertTask = (
    rawTask: Record<string, unknown>,
    timestamp: number,
    fallbackStatus: TeamTaskStatus,
    allowCreate = true
  ) => {
    // Card identity is the DB `task_id` only. Never fall back to LLM `id` —
    // that mismatch is what produced duplicate cards historically.
    const taskId = pickString(rawTask, ['task_id']);
    if (!taskId) {
      return;
    }
    const existing = tasks.get(taskId);
    if (!existing && !allowCreate) {
      return;
    }
    const nextTimestamp = taskTimestamp(rawTask, timestamp);
    const title = pickString(rawTask, ['title', 'name', 'description']);
    const content = pickString(rawTask, ['content']);
    const assignee = pickString(rawTask, ['assignee', 'member_id', 'claimed_by', 'claimedBy', 'from_member']);
    const teamId = pickString(rawTask, ['team_id']);
    const skills = stringList(rawTask.skills);
    const files = stringList(rawTask.files);
    const existingTimestamp = existing?.timestamp || 0;
    const isStaleTaskVersion = nextTimestamp < existingTimestamp;
    let status: TeamTaskStatus;
    if (isStaleTaskVersion) {
      status = existing?.status || fallbackStatus;
    } else {
      status = resolveTaskStatus(rawTask.status, existing, fallbackStatus);
    }

    tasks.set(taskId, {
      task_id: taskId,
      title: title || existing?.title || `任务 ${taskId}`,
      content: content || existing?.content,
      status,
      assignee: assignee || existing?.assignee,
      team_id: teamId || existing?.team_id,
      timestamp: Math.max(existing?.timestamp || 0, nextTimestamp),
      skills: skills || existing?.skills,
      files: files || existing?.files,
      // Truncation flags: read raw with explicit guards so a status-only
      // record (no flags) falls back to `existing?` — never resets to false.
      // Mirrors the title/content `|| existing?` pattern above.
      title_truncated:
        rawTask.title_truncated === true ? true : existing?.title_truncated,
      title_original_size:
        typeof rawTask.title_original_size === 'number' &&
        Number.isFinite(rawTask.title_original_size)
          ? rawTask.title_original_size
          : existing?.title_original_size,
      content_truncated:
        rawTask.content_truncated === true ? true : existing?.content_truncated,
      content_original_size:
        typeof rawTask.content_original_size === 'number' &&
        Number.isFinite(rawTask.content_original_size)
          ? rawTask.content_original_size
          : existing?.content_original_size,
    });
    addMember(assignee, timestamp);
  };

  // Member lifecycle from tool_call only. Task cards are NEVER built from
  // tool_call arguments (LLM `id` like bubble-sort-task ≠ DB task_id).
  const applyToolInput = (
    name: string,
    args: Record<string, unknown>,
    timestamp: number,
    memberId: string
  ) => {
    if (name === 'shutdown_member') {
      applyMemberShutdown(pickString(args, ['member_name', 'member_id', 'name']) || memberId);
      return;
    }
    if (name === 'spawn_member') {
      const spawnedMemberId = pickString(args, ['member_name', 'member_id', 'name']) || memberId;
      shutdownMembers.delete(spawnedMemberId);
      addMember(spawnedMemberId, timestamp);
    }
  };

  // Tool *results* may carry the DB task_id + title (create_task brief()).
  // Old history `team.task` events often have no title/content; without this
  // path (and when team.snapshot is empty because the monitor is down) the
  // board would only show UUID placeholder cards. Never read args.tasks —
  // those carry the LLM `id`.
  const applyToolData = (
    name: string,
    data: Record<string, unknown>,
    _args: Record<string, unknown>,
    timestamp: number
  ) => {
    if (name !== 'create_task') {
      return;
    }
    if (Array.isArray(data.tasks)) {
      data.tasks.forEach((item) => {
        if (isRecord(item) && pickString(item, ['task_id'])) {
          upsertTask(item, timestamp, 'pending');
        }
      });
      return;
    }
    if (pickString(data, ['task_id'])) {
      upsertTask(data, timestamp, 'pending');
    }
  };

  const orderedRecords = [...records].sort((a, b) => recordTimestamp(a) - recordTimestamp(b));

  for (const record of orderedRecords) {
    const timestamp = recordTimestamp(record);
    const eventType = typeof record.event_type === 'string' ? record.event_type : '';
    const recordSessionId = getRecordSessionId(record);
    if (eventType === 'chat.final') {
      const actionPayload = isRecord(record.event_payload)
        ? { ...record, ...record.event_payload }
        : record;
      const memberAction = pickString(actionPayload, ['member_action']);
      const actionMemberName = pickString(actionPayload, ['member_name']);
      if (actionMemberName && (memberAction === 'joined' || memberAction === 'left')) {
        upsertHumanShareCommand(
          actionMemberName,
          {
            displayName: pickString(actionPayload, ['display_name']) || undefined,
            status: memberAction,
            sourceChannel: pickString(actionPayload, ['source_channel']) || undefined,
            userId: pickString(actionPayload, ['user_id']) || undefined,
          },
          timestamp
        );
      }
    }
    if (
      isTeamTeammateRecord(record) &&
      (!recordSessionId || recordSessionId === sessionId)
    ) {
      const memberId = pickString(record, ['member_name', 'member_id', 'source_member']);
      if (memberId) {
        addMember(memberId, timestamp);
        const payload = extractTeamEvent(record) || record;

        if (eventType === 'chat.final') {
          const content = normalizeFinalContent(payload);
          if (content.trim()) {
            const id = eventId('hist-final', record.id, memberId, timestamp, executionEvents.size, content.slice(0, 48));
            executionEvents.set(id, {
              id,
              member_id: memberId,
              kind: 'final',
              timestamp,
              title: '任务完成总结',
              content,
            });
          }
        } else if (eventType === 'chat.tool_call') {
          const toolCall = isRecord(payload.tool_call) ? payload.tool_call : payload;
          const toolName = pickString(toolCall, ['name', 'tool_name']) || pickString(payload, ['tool_name']) || 'unknown';
          const toolCallId = pickString(toolCall, ['tool_call_id', 'toolCallId', 'id']) || pickString(payload, ['tool_call_id', 'toolCallId']);
          const content = pickString(toolCall, ['description', 'formatted_args']) || compactString(toolCall.arguments);
          const id = eventId('hist-tool-call', record.id, memberId, toolCallId, timestamp);
          executionEvents.set(id, {
            id,
            member_id: memberId,
            kind: 'tool_call',
            timestamp,
            title: `调用 ${toolName}`,
            content,
            tool_name: toolName,
            tool_call_id: toolCallId || undefined,
          });
        } else if (eventType === 'chat.tool_result') {
          const toolResult = isRecord(payload.tool_result) ? payload.tool_result : payload;
          const toolName = pickString(toolResult, ['tool_name', 'name']) || pickString(payload, ['tool_name', 'name']) || 'unknown';
          const toolCallId = pickString(toolResult, ['tool_call_id', 'toolCallId']) || pickString(payload, ['tool_call_id', 'toolCallId']);
          const content = pickString(toolResult, ['summary', 'result', 'data', 'error']) || compactString(toolResult.result);
          const id = eventId('hist-tool-result', record.id, memberId, toolCallId, timestamp);
          executionEvents.set(id, {
            id,
            member_id: memberId,
            kind: 'tool_result',
            timestamp,
            title: `${toolName} 结果`,
            content,
            tool_name: toolName,
            tool_call_id: toolCallId || undefined,
          });
        } else if (eventType === 'chat.file') {
          const files = Array.isArray(payload.files)
            ? payload.files.filter(isRecord).map((file) => ({
                name: pickString(file, ['name']) || 'file',
                size: typeof file.size === 'number' ? file.size : undefined,
                mime_type: pickString(file, ['mime_type']) || undefined,
                download_url: pickString(file, ['download_url']) || undefined,
                path: pickString(file, ['path']) || undefined,
              }))
            : [];
          if (files.length > 0) {
            const existing = findOverlappingFileExecutionEvent(
              Array.from(executionEvents.values()),
              files,
              (event) => event.member_id === memberId && event.kind === 'file'
            );
            const mergedFiles = mergeFileDownloadItems(existing?.files, files);
            const id = existing?.id || eventId('hist-file', record.id, memberId, timestamp, files.map((file) => file.name).join(','));
            executionEvents.set(id, {
              id,
              member_id: memberId,
              kind: 'file',
              timestamp,
              title: '发送文件',
              content: mergedFiles.map((file) => file.name).join('\n'),
              files: mergedFiles,
            });
          }
        }
      }
    }
    const memberId = pickString(record, ['member_name', 'member_id']);
    const toolCall = extractToolCallInput(record);
    if (toolCall) {
      applyToolInput(toolCall.name, toolCall.args, timestamp, memberId);
      continue;
    }
    const shutdownMember = extractShutdownMemberFromToolResult(record);
    if (shutdownMember) {
      applyMemberShutdown(shutdownMember);
      continue;
    }
    const tracerInput = extractTracerInput(record);
    if (tracerInput) {
      applyToolInput(tracerInput.name, tracerInput.args, timestamp, memberId);
    }
    const tracerData = extractTracerData(record);
    if (tracerData) {
      applyToolData(tracerData.name, tracerData.data, tracerData.args, timestamp);
      continue;
    }

    if (!TEAM_STATE_EVENT_TYPES.has(eventType)) {
      continue;
    }
    const event = extractTeamEvent(record);
    if (!event) {
      continue;
    }
    if (getRecordSessionId(record) !== sessionId) {
      continue;
    }

    const eventTimestamp = typeof event.timestamp === 'number' ? event.timestamp : timestamp;
    if (eventType === 'team.message' || eventType === 'team.event') {
      const fromMember = pickString(event, ['from_member']);
      const toMember = pickString(event, ['to_member']);
      addMember(fromMember, eventTimestamp);
      addMember(toMember, eventTimestamp);
      messages.push({
        id: pickString(event, ['message_id', 'id']) || `hist-team-message-${eventTimestamp}`,
        role: 'system',
        content: `team.event:${JSON.stringify({ event })}`,
        timestamp: new Date(eventTimestamp).toISOString(),
      });
      continue;
    }

    if (eventType === 'team.member') {
      const memberId = pickString(event, ['member_id']);
      if (!memberId) {
        continue;
      }
      if (pickString(event, ['mode']) === 'human') {
        const teamName = pickString(event, ['team_id']);
        const sessionRef = teamName ? `team_${teamName}_session_${sessionId}` : '';
        upsertHumanShareCommand(
          memberId,
          {
            displayName: pickString(event, ['name']) || undefined,
            teamName,
            sessionRef,
            joinCommand: sessionRef ? `/join ${sessionRef} as ${memberId}` : '',
            exitCommand: sessionRef ? `/exit ${sessionRef}` : '',
            status: 'pending',
          },
          eventTimestamp
        );
      }
      if (pickString(event, ['type']) === 'team.member.shutdown') {
        applyMemberShutdown(memberId);
        continue;
      }
      if (!shouldKeepMember(memberId)) {
        continue;
      }
      const memberStatus = pickString(event, ['new_status', 'status']);
      if (memberStatus === 'shut_down') {
        applyMemberShutdown(memberId);
        continue;
      }
      const memberEventType = pickString(event, ['type']);
      if (
        shutdownMembers.has(memberId) &&
        memberEventType !== 'team.member.spawned' &&
        memberEventType !== 'team.member.restarted'
      ) {
        continue;
      }
      shutdownMembers.delete(memberId);
      if (shouldKeepMember(memberId)) {
        hasSeenMember = true;
      }
      // 回放是逐条覆盖同一个 member 记录的，而只有部分事件带 name / mode
      // （registered 带，spawned / status_changed 不带）。后到的事件不能把先前
      // 学到的展示名冲掉，否则恢复出来的面板会退回显示 member_id。
      const knownMember = members.get(memberId);
      members.set(memberId, {
        id: `hist-member-${memberId}`,
        member_id: memberId,
        status: pickString(event, ['status', 'new_status']) || 'idle',
        timestamp: eventTimestamp,
        name: pickString(event, ['name']) || knownMember?.name || undefined,
        execution_status: pickString(event, ['execution_status', 'new_status']) || 'idle',
        mode: pickString(event, ['mode']) || knownMember?.mode || undefined,
        role: pickString(event, ['role']) || knownMember?.role || undefined,
        cli_agent: pickString(event, ['cli_agent']) || knownMember?.cli_agent || undefined,
      });
      continue;
    }

    const taskId = pickString(event, ['task_id']);
    if (!taskId) {
      continue;
    }
    const type = pickString(event, ['type']);
    const status = normalizeTaskStatus(event.status);
    const assignee = pickString(event, ['assignee', 'member_id', 'claimed_by', 'claimedBy', 'from_member']);
    const title = pickString(event, ['title', 'name', 'description']);
    const content = pickString(event, ['content']);
    const teamId = pickString(event, ['team_id']);

    if (type === 'team.task.created') {
      taskProgressBaseline = registerConfirmedTaskCreation(
        Array.from(tasks.values()),
        taskProgressBaseline,
        taskId
      );
    }

    taskEvents.set(taskId, {
      id: `hist-task-${taskId}-${timestamp}`,
      type,
      team_id: teamId,
      task_id: taskId,
      status,
      timestamp: eventTimestamp,
      member_id: pickString(event, ['member_id']) || undefined,
      assignee: assignee || undefined,
      team_name: pickString(event, ['team_name']) || undefined,
      title: title || undefined,
      content: content || undefined,
      updated_at: (event.updated_at as number | string | null | undefined) ?? eventTimestamp,
    });
    upsertTask(event, eventTimestamp, status);
  }

  if (hasSeenMember && members.size === 0) {
    return {
      members: [],
      tasks: [],
      taskEvents: [],
      executionEvents: [],
      messages: [],
      humanShareCommands: [],
      taskProgressBaseline: createTaskProgressBaseline(),
    };
  }

  return {
    members: Array.from(members.values()),
    tasks: Array.from(tasks.values()),
    taskEvents: Array.from(taskEvents.values()),
    executionEvents: Array.from(executionEvents.values()),
    messages,
    humanShareCommands: Array.from(humanShareCommands.values()),
    taskProgressBaseline,
  };
}

export function parseTeamHistoryPanelRecords(records: unknown[], sessionId: string): TeamHistoryPanelState {
  return collectTeamState(records.filter(isRecord), sessionId);
}

interface TeamSnapshotResponse {
  tasks?: unknown;
}

function readSnapshotSize(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function snapshotItemToTask(item: Record<string, unknown>, fallbackTimestamp: number): TeamTask | null {
  const taskId = pickString(item, ['task_id']);
  if (!taskId) {
    return null;
  }
  const title = pickString(item, ['title', 'name', 'description']);
  const content = pickString(item, ['content']);
  const status = normalizeTaskStatus(item.status);
  const assignee = pickString(item, ['assignee', 'member_id', 'claimed_by', 'claimedBy']);
  const teamId = pickString(item, ['team_id']);
  const timestamp = taskTimestamp(item, fallbackTimestamp);
  return {
    task_id: taskId,
    title: title || `任务 ${taskId}`,
    content: content || undefined,
    status,
    assignee: assignee || undefined,
    team_id: teamId || undefined,
    timestamp,
    skills: stringList(item.skills),
    files: stringList(item.files),
    title_truncated: item.title_truncated === true ? true : undefined,
    title_original_size: readSnapshotSize(item.title_original_size),
    content_truncated: item.content_truncated === true ? true : undefined,
    content_original_size: readSnapshotSize(item.content_original_size),
  };
}

/**
 * Reconcile the restored task board against the live DB snapshot.
 *
 * History events alone can leave:
 * - placeholder-body cards (`任务 <task_id>`) in the waiting column, or
 * - orphan cards keyed by LLM `id` that were merged in from a prior live store.
 *
 * When `team.snapshot` returns tasks, it is the authoritative board (DB
 * `task_id` + title/content + status): replace the list so wrong-id / stale
 * waiting cards are refreshed to the correct ones. When the monitor is
 * inactive (empty snapshot) or the RPC fails, keep the history-derived state.
 */
async function reconcileTasksFromSnapshot(
  state: TeamHistoryPanelState,
  sessionId: string,
  signal?: AbortSignal
): Promise<TeamHistoryPanelState> {
  try {
    const response = await webClient.request<TeamSnapshotResponse>(
      'team.snapshot',
      { session_id: sessionId },
      { signal, timeoutMs: 5000 }
    );
    const snapshotTasks = Array.isArray(response?.tasks) ? response.tasks : [];
    if (snapshotTasks.length === 0) {
      return state;
    }

    const existingById = new Map(state.tasks.map((task) => [task.task_id, task]));
    const now = Date.now();
    const tasks: TeamTask[] = [];
    for (const item of snapshotTasks) {
      if (!isRecord(item)) {
        continue;
      }
      const fromSnap = snapshotItemToTask(item, now);
      if (!fromSnap) {
        continue;
      }
      const existing = existingById.get(fromSnap.task_id);
      // Snapshot wins for identity/body/status; keep history-only extras
      // (skills/files) when the snapshot omits them.
      tasks.push({
        ...fromSnap,
        skills: fromSnap.skills || existing?.skills,
        files: fromSnap.files || existing?.files,
        timestamp: Math.max(existing?.timestamp || 0, fromSnap.timestamp || 0),
      });
    }
    return { ...state, tasks };
  } catch {
    return state;
  }
}

export async function loadTeamHistoryPanelState(
  sessionId: string,
  signal?: AbortSignal
): Promise<TeamHistoryPanelState> {
  const records: Record<string, unknown>[] = [];
  let cursor: number | string | null | undefined = 0;
  let hasMore = true;
  let guard = 0;

  while (hasMore && guard < 10_000) {
    guard += 1;
    const result: TeamHistoryGetResponse = await webClient.request<TeamHistoryGetResponse>(
      'team.history.get',
      {
        session_id: sessionId,
        cursor,
        limit: 500,
        max_bytes: 1024 * 1024,
      },
      { signal, timeoutMs: 30_000 }
    );
    if (Array.isArray(result?.records)) {
      records.push(...result.records);
    }
    hasMore = Boolean(result?.has_more);
    const nextCursor: number | string | null | undefined = result?.next_cursor;
    if (!hasMore || nextCursor === undefined || nextCursor === null || nextCursor === cursor) {
      break;
    }
    cursor = nextCursor;
  }

  const state = parseTeamHistoryPanelRecords(records, sessionId);
  // Prefer live DB snapshot as the authoritative board when the monitor is
  // still up — refreshes placeholder / wrong-id waiting cards to real
  // task_id + body. When the monitor is down (empty snapshot), keep the
  // history-event-derived board. Runs only on this async path so the sync
  // export path (shareImageExport) stays side-effect free.
  return reconcileTasksFromSnapshot(state, sessionId, signal);
}
