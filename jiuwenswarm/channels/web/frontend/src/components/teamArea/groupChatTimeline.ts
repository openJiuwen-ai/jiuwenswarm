/**
 * 子话题（团队群聊）时间线：把成员间消息与任务流转事件按时间合成一条流。
 *
 * 调度模式下成员不靠消息回复 leader（提交走 member_complete_task，验收由调度器拉起
 * 临时 reviewer），群聊里天然只有 leader→成员的交接消息。任务事件作为系统行穿插进来，
 * 子话题才读得出「开工 → 提交 → 验收 → 通过 / 返工」的完整流程。
 *
 * 本模块只做纯数据变换（无 React / i18n 依赖），便于单测。
 */

/** 框架模板消息（调度器交接等）的展示描述，由后端 monitor handler 从消息 meta 推导。 */
export interface TeamHandoff {
  kind: string;
  template?: string;
  task_id?: string;
  task_title?: string;
  max_rounds?: string;
}

/** 群聊里展示的一条任务流转事件。 */
export interface TeamTaskTimelineEntry {
  id: string;
  type: string;
  task_id: string;
  timestamp: number;
  member_id?: string;
  title?: string;
}

/** 进群聊展示的任务事件类型；unblocked / updated / plan_* 等过程噪声不展示。 */
export const GROUP_CHAT_TASK_EVENT_TYPES: ReadonlySet<string> = new Set([
  'team.task.created',
  'team.task.claimed',
  'team.task.submitted_for_review',
  'team.task.verified',
  'team.task.revision_requested',
  'team.task.completed',
  'team.task.cancelled',
  'team.task.released',
  'team.task.revoked',
]);

/**
 * 只入时间线、不单独成行的事件：started 紧挨着调度器的开工交接，单独显示是重复；
 * 留着它是为了给没有 handoff 字段的老历史消息推断「开工的是哪个任务」。
 */
const HIDDEN_TIMELINE_EVENT_TYPES: ReadonlySet<string> = new Set(['team.task.started']);

/** 开工交接紧跟在 task.started 之后落邮箱，两者间隔远小于此窗口。 */
const LEGACY_HANDOFF_WINDOW_MS = 2_000;

/** 同一 (type, task_id) 在此窗口内视为同一事件（实时与历史恢复的时间戳来源不同）。 */
const DUPLICATE_WINDOW_MS = 10_000;

function pickString(input: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = input[key];
    if (typeof value === 'string' && value.trim()) {
      return value.trim();
    }
  }
  return '';
}

/** 从后端 handoff 字段解析交接描述；字段缺失或形状不对返回 undefined。 */
export function parseTeamHandoff(value: unknown): TeamHandoff | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return undefined;
  }
  const raw = value as Record<string, unknown>;
  const kind = pickString(raw, ['kind']);
  if (!kind) {
    return undefined;
  }
  return {
    kind,
    template: pickString(raw, ['template']) || undefined,
    task_id: pickString(raw, ['task_id']) || undefined,
    task_title: pickString(raw, ['task_title']) || undefined,
    max_rounds: pickString(raw, ['max_rounds']) || undefined,
  };
}

/**
 * 把一条原始 team.task 事件转成时间线条目；不进群聊的类型返回 null。
 *
 * @param event 后端 team.task 事件的内层 event（type / task_id / member_id / title ...）。
 * @param timestamp 事件时间（毫秒）。
 */
export function toTaskTimelineEntry(event: Record<string, unknown>, timestamp: number): TeamTaskTimelineEntry | null {
  const type = pickString(event, ['type']);
  const taskId = pickString(event, ['task_id']);
  if (!taskId || (!GROUP_CHAT_TASK_EVENT_TYPES.has(type) && !HIDDEN_TIMELINE_EVENT_TYPES.has(type))) {
    return null;
  }
  return {
    id: `task-timeline-${type}-${taskId}-${timestamp}`,
    type,
    task_id: taskId,
    timestamp,
    member_id: pickString(event, ['member_id', 'assignee']) || undefined,
    title: pickString(event, ['title']) || undefined,
  };
}

function isDuplicate(a: TeamTaskTimelineEntry, b: TeamTaskTimelineEntry): boolean {
  return a.type === b.type && a.task_id === b.task_id && Math.abs(a.timestamp - b.timestamp) <= DUPLICATE_WINDOW_MS;
}

/** 合并两段时间线（历史恢复 + 实时），去重后按时间升序。先出现的条目保留。 */
export function mergeTaskTimeline(
  base: TeamTaskTimelineEntry[],
  incoming: TeamTaskTimelineEntry[],
): TeamTaskTimelineEntry[] {
  const merged = [...base];
  for (const entry of incoming) {
    if (!merged.some((existing) => isDuplicate(existing, entry))) {
      merged.push(entry);
    }
  }
  return merged.sort((a, b) => a.timestamp - b.timestamp);
}

/**
 * 为老历史里没有 handoff 字段的空正文交接消息推断交接内容。
 *
 * 调度器开工时先把任务置为 in_progress（task.started），紧接着给 owner 投开工交接；
 * 所以消息前后窗口内、同一成员的 started 事件就是它交接的任务。推不出来返回 undefined。
 */
export function inferLegacyHandoff(
  toMember: string | undefined,
  timestamp: number,
  timeline: TeamTaskTimelineEntry[],
): TeamHandoff | undefined {
  if (!toMember) {
    return undefined;
  }
  let best: TeamTaskTimelineEntry | undefined;
  for (const entry of timeline) {
    if (entry.type !== 'team.task.started' || entry.member_id !== toMember) {
      continue;
    }
    const gap = Math.abs(timestamp - entry.timestamp);
    if (gap <= LEGACY_HANDOFF_WINDOW_MS && (!best || gap < Math.abs(timestamp - best.timestamp))) {
      best = entry;
    }
  }
  return best ? { kind: 'task_start', task_id: best.task_id } : undefined;
}

/** 时间线里能拿到的任务标题（created 事件带标题，后续事件大多不带）。 */
export function collectTimelineTitles(timeline: TeamTaskTimelineEntry[]): Map<string, string> {
  const titles = new Map<string, string>();
  for (const entry of timeline) {
    if (entry.title) {
      titles.set(entry.task_id, entry.title);
    }
  }
  return titles;
}

export type GroupChatItem<M> =
  | { kind: 'message'; timestamp: number; message: M }
  | { kind: 'task'; timestamp: number; type: string; entries: TeamTaskTimelineEntry[] };

/**
 * 把群聊消息与任务事件合成一条按时间排序的流。
 *
 * - 连续的 team.task.created 合成一行（leader 一次建多个任务时不刷屏）；
 * - 验收通过后紧跟的 completed 与 verified 说的是同一件事，只留 verified。
 *
 * @param messages 已解析的群聊消息及其时间。
 * @param timeline 任务事件时间线。
 */
export function buildGroupChatItems<M>(
  messages: { message: M; timestamp: number }[],
  timeline: TeamTaskTimelineEntry[],
): GroupChatItem<M>[] {
  const verified = timeline.filter((entry) => entry.type === 'team.task.verified');
  const visibleTimeline = timeline.filter(
    (entry) =>
      !HIDDEN_TIMELINE_EVENT_TYPES.has(entry.type) &&
      (entry.type !== 'team.task.completed' ||
      !verified.some(
        (item) => item.task_id === entry.task_id && Math.abs(item.timestamp - entry.timestamp) <= DUPLICATE_WINDOW_MS,
      )),
  );

  const items: GroupChatItem<M>[] = [
    ...messages.map(({ message, timestamp }) => ({ kind: 'message' as const, timestamp, message })),
    ...visibleTimeline.map((entry) => ({
      kind: 'task' as const,
      timestamp: entry.timestamp,
      type: entry.type,
      entries: [entry],
    })),
  ];
  // 稳定排序：同一时刻任务事件排在消息前（先有任务状态变化，再有调度器交接）。
  items.sort((a, b) => a.timestamp - b.timestamp || (a.kind === b.kind ? 0 : a.kind === 'task' ? -1 : 1));

  const grouped: GroupChatItem<M>[] = [];
  for (const item of items) {
    const last = grouped[grouped.length - 1];
    if (
      item.kind === 'task' &&
      item.type === 'team.task.created' &&
      last?.kind === 'task' &&
      last.type === 'team.task.created'
    ) {
      last.entries.push(...item.entries);
      continue;
    }
    grouped.push(item.kind === 'task' ? { ...item, entries: [...item.entries] } : item);
  }
  return grouped;
}
