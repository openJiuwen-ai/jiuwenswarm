import type { SubagentActivity } from '../../types/subagent';

export interface SubagentActivityGroup {
  activity: SubagentActivity;
  activities: SubagentActivity[];
  summary: string;
  count: number;
}

export type SubagentTaskStatus = 'pending' | 'in_progress' | 'completed';

export interface SubagentTaskStatusChange {
  status: SubagentTaskStatus;
  atMs?: number;
  source: 'create' | 'update' | 'result' | 'final';
}

export interface SubagentTask {
  id: string;
  content: string;
  detail: string;
  status: SubagentTaskStatus;
  raw: Record<string, unknown>;
  statusHistory: SubagentTaskStatusChange[];
}

function firstNonEmptyLine(value: string): string {
  return value
    .split(/\r?\n/)
    .map(line => line.trim())
    .find(Boolean) || '';
}

function boundedPreview(value: string, maxLength = 120): string {
  const line = firstNonEmptyLine(value);
  return line.length > maxLength ? `${line.slice(0, maxLength - 1)}…` : line;
}

function unescapeQuotedValue(value: string): string {
  try {
    return JSON.parse(`"${value}"`) as string;
  } catch {
    return value.replace(/\\([\\"'])/g, '$1');
  }
}

function extractStringField(value: string, field: string): string {
  const doubleQuoted = value.match(new RegExp(`"${field}"\\s*:\\s*"((?:\\\\.|[^"\\\\])*)"`));
  const singleQuoted = value.match(new RegExp(`'${field}'\\s*:\\s*'((?:\\\\.|[^'\\\\])*)'`));
  const match = doubleQuoted || singleQuoted;
  return match ? unescapeQuotedValue(match[1]) : '';
}

function resultPreview(value: string): string {
  const content = value.match(/(?:^|\s)Content:\s*([\s\S]*)$/i)?.[1];
  if (content) return firstNonEmptyLine(content);
  return extractStringField(value, 'message') || extractStringField(value, 'result') || firstNonEmptyLine(value);
}

function parseJsonPayload(value: string): Record<string, unknown> | null {
  const start = value.indexOf('{');
  const end = value.lastIndexOf('}');
  if (start < 0 || end <= start) return null;
  try {
    const parsed = JSON.parse(value.slice(start, end + 1));
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

function normalizeTaskStatus(value: unknown): SubagentTaskStatus {
  if (value === 'completed' || value === 'complete' || value === 'done') return 'completed';
  if (value === 'in_progress' || value === 'in-progress' || value === 'running') return 'in_progress';
  return 'pending';
}

function taskEntries(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object')
    : [];
}

function applyTaskEntry(
  tasks: Map<string, SubagentTask>,
  entry: Record<string, unknown>,
  atMs: number | undefined,
  source: SubagentTaskStatusChange['source'],
): void {
  const id = typeof entry.id === 'string'
    ? entry.id
    : typeof entry.task_id === 'string'
      ? entry.task_id
      : '';
  const content = typeof entry.content === 'string'
    ? entry.content
    : typeof entry.activeForm === 'string'
      ? entry.activeForm
      : '';
  const detail = typeof entry.description === 'string'
    ? entry.description
    : typeof entry.activeForm === 'string'
      ? entry.activeForm
      : '';
  const previous = tasks.get(id);
  if (!id || (!content && !previous)) return;
  const status = entry.status === undefined && previous ? previous.status : normalizeTaskStatus(entry.status);
  const statusHistory = [...(previous?.statusHistory || [])];
  if (!previous || previous.status !== status) {
    statusHistory.push({ status, atMs, source });
  }
  tasks.set(id, {
    id,
    content: content || previous?.content || id,
    detail: detail || previous?.detail || '',
    status,
    raw: {
      ...(previous?.raw || {}),
      ...entry,
      status: entry.status === undefined ? status : entry.status,
    },
    statusHistory,
  });
}

function applyTaskResultLines(tasks: Map<string, SubagentTask>, summary: string, atMs?: number): void {
  const linePattern = /^\s*\[([^\]]*)\]\s*task_id:\s*([^,\s]+)\s*,\s*content:\s*(.*?)(?:\s+\(model:.*)?$/gm;
  for (const match of summary.matchAll(linePattern)) {
    const marker = match[1].trim();
    const status = marker.includes('✓') || marker.includes('✔')
      ? 'completed'
      : marker.includes('→') || marker.includes('>')
        ? 'in_progress'
        : 'pending';
    applyTaskEntry(tasks, { id: match[2], content: match[3].trim(), status }, atMs, 'result');
  }
}

type PendingTodoOperation = {
  toolName: string;
  payload: Record<string, unknown>;
};

function isSuccessfulTodoResult(summary: string): boolean {
  const normalized = summary.trim().toLowerCase();
  if (!normalized || normalized.includes('success=false') || normalized.includes('[error]')) return false;
  return !normalized.includes('error=') || normalized.includes('error=none');
}

function applyTodoPayload(tasks: Map<string, SubagentTask>, toolName: string, payload: Record<string, unknown>, atMs?: number): void {
  const source: SubagentTaskStatusChange['source'] = toolName.includes('create')
    ? 'create'
    : toolName.includes('complete')
      ? 'result'
      : 'update';
  for (const entry of taskEntries(payload.tasks ?? payload.todos)) applyTaskEntry(tasks, entry, atMs, source);
  if (typeof payload.task_id !== 'string') return;
  if (toolName.includes('remove')) {
    tasks.delete(payload.task_id);
    return;
  }
  applyTaskEntry(tasks, {
    ...payload,
    ...(toolName.includes('complete') ? { status: 'completed' } : {}),
  }, atMs, source);
}

export function isSubagentTodoActivity(activity: SubagentActivity): boolean {
  return activity.tool_name?.toLowerCase().includes('todo') ?? false;
}

/** Extract real todo tool data for the separate "Ta的任务" section. */
export function extractSubagentTasks(activities: SubagentActivity[]): SubagentTask[] {
  const tasks = new Map<string, SubagentTask>();
  const pendingOperations = new Map<string, PendingTodoOperation>();
  for (const activity of activities) {
    if (!isSubagentTodoActivity(activity)) continue;
    const toolName = activity.tool_name?.toLowerCase() || '';
    const payload = parseJsonPayload(activity.summary);
    if (activity.kind === 'tool_call') {
      if (payload && activity.tool_call_id) {
        pendingOperations.set(activity.tool_call_id, { toolName, payload });
      }
      continue;
    }
    if (activity.kind === 'tool_result') {
      const operation = activity.tool_call_id ? pendingOperations.get(activity.tool_call_id) : undefined;
      if (isSuccessfulTodoResult(activity.summary)) {
        if (operation) applyTodoPayload(tasks, operation.toolName, operation.payload, activity.at_ms);
        applyTaskResultLines(tasks, activity.summary, activity.at_ms);
      }
      if (activity.tool_call_id) pendingOperations.delete(activity.tool_call_id);
    }
  }
  return Array.from(tasks.values());
}

/** A successful final turn closes any todo items that lack individual completion events. */
export function finalizeSubagentTasks(tasks: SubagentTask[], turnCompleted: boolean, atMs?: number): SubagentTask[] {
  if (!turnCompleted) return tasks;
  return tasks.map(task => {
    const statusHistory = [...task.statusHistory];
    if (task.status !== 'completed') statusHistory.push({ status: 'completed', atMs, source: 'final' });
    return {
      ...task,
      status: 'completed',
      raw: { ...task.raw, status: 'completed' },
      statusHistory,
    };
  });
}

/**
 * Return the concise, user-facing label for a logical activity. The complete
 * call/result stream remains available through the row disclosure.
 */
export function getSubagentActivityPreview(group: SubagentActivityGroup): string {
  if (group.activity.kind === 'thinking') return boundedPreview(group.summary);

  const toolCall = group.activities.find(activity => activity.kind === 'tool_call');
  const toolResult = group.activities.find(activity => activity.kind === 'tool_result');
  const toolName = (toolCall?.tool_name || toolResult?.tool_name || group.activity.tool_name || '').toLowerCase();
  const callSummary = toolCall?.summary || '';
  const resultSummary = toolResult?.summary || '';

  if (toolName.includes('todo')) {
    return extractStringField(callSummary, 'content')
      || extractStringField(callSummary, 'activeForm')
      || resultPreview(resultSummary);
  }
  if (toolName.includes('search') || toolName.includes('fetch') || toolName.includes('web')) {
    return resultPreview(resultSummary) || extractStringField(callSummary, 'url');
  }
  if (toolName.includes('bash') || toolName.includes('terminal') || toolName.includes('shell')) {
    return extractStringField(callSummary, 'command') || resultPreview(resultSummary);
  }
  return resultPreview(resultSummary) || firstNonEmptyLine(callSummary || group.summary);
}

function createGroup(activity: SubagentActivity): SubagentActivityGroup {
  return {
    activity,
    activities: [activity],
    summary: activity.summary,
    count: 1,
  };
}

function canMergeThinking(left: SubagentActivity, right: SubagentActivity): boolean {
  return left.kind === 'thinking'
    && right.kind === 'thinking'
    && left.task_id === right.task_id
    && (
      (left.phase_id === undefined && right.phase_id === undefined)
      || (left.phase_id !== undefined && right.phase_id !== undefined && left.phase_id === right.phase_id)
    );
}

function phaseKey(activity: SubagentActivity): string | null {
  if (activity.kind !== 'thinking' || activity.phase_id === undefined) return null;
  return `${activity.task_id}:${activity.phase_id}`;
}

function appendSummary(group: SubagentActivityGroup, activity: SubagentActivity): void {
  const previous = group.activities[group.activities.length - 1];
  const summary = activity.summary || '';
  if (!summary) return;
  group.summary += previous.kind === activity.kind ? summary : `\n\n${summary}`;
}

/**
 * Keep tool events as independent rows, while coalescing contiguous thinking
 * chunks from the same task into one phase activity. A tool call/result is a
 * real phase boundary; task_id alone is not enough to merge later thinking.
 */
export function groupSubagentActivities(activities: SubagentActivity[]): SubagentActivityGroup[] {
  const groups: SubagentActivityGroup[] = [];
  const thinkingGroupsByPhase = new Map<string, SubagentActivityGroup>();
  for (const activity of activities) {
    if (activity.kind === 'thinking') {
      const key = phaseKey(activity);
      const existingPhase = key ? thinkingGroupsByPhase.get(key) : undefined;
      if (existingPhase) {
        appendSummary(existingPhase, activity);
        existingPhase.activities.push(activity);
        existingPhase.count += 1;
        continue;
      }
    }
    const previous = groups[groups.length - 1];
    const previousActivity = previous?.activities[previous.activities.length - 1];
    if (previous && previousActivity && canMergeThinking(previousActivity, activity)) {
      appendSummary(previous, activity);
      previous.activities.push(activity);
      previous.count += 1;
      continue;
    }
    const group = createGroup(activity);
    groups.push(group);
    const key = phaseKey(activity);
    if (key) thinkingGroupsByPhase.set(key, group);
  }
  return groups;
}
