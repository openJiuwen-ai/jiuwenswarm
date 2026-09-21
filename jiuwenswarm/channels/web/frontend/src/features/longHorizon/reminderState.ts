import type {
  LongHorizonInboxItem,
  LongHorizonReminderToast,
} from '../../types/longHorizon';

let reminders: LongHorizonReminderToast[] = [];
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) {
    listener();
  }
}

export function getReminders(): LongHorizonReminderToast[] {
  return reminders.slice();
}

export function subscribeReminders(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function resetReminders(): void {
  reminders = [];
  emit();
}

export function dismissReminder(id: string): void {
  const next = reminders.filter((item) => item.id !== id);
  if (next.length === reminders.length) return;
  reminders = next;
  emit();
}

export function pushReminder(toast: LongHorizonReminderToast): void {
  reminders = [...reminders.filter((item) => item.id !== toast.id), toast];
  emit();
}

function asText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

export function ingestStageDueEvent(payload: Record<string, unknown>): void {
  const eventType = asText(payload.event_type);
  if (eventType && eventType !== 'long_horizon.stage_due') {
    return;
  }
  const taskId = asText(payload.task_id);
  const stageId = asText(payload.stage_id);
  if (!taskId || !stageId) return;

  pushReminder({
    id: `${taskId}:${stageId}`,
    taskId,
    stageId,
    execSessionId: asText(payload.exec_session_id) || undefined,
    title: asText(payload.title) || '长程提醒',
    stageTitle: asText(payload.stage_title),
    hint: asText(payload.hint) || undefined,
    dueAt: asText(payload.due_at) || undefined,
  });
}

export function restoreRemindersFromInbox(
  inbox: Array<LongHorizonInboxItem | Record<string, unknown>>
): void {
  if (!Array.isArray(inbox)) return;
  for (const row of inbox) {
    const task = (row as LongHorizonInboxItem).task ?? (row as { task?: Record<string, unknown> }).task;
    const stage =
      (row as LongHorizonInboxItem).stage ?? (row as { stage?: Record<string, unknown> }).stage;
    if (!task || !stage) continue;
    const taskId = asText((task as { id?: unknown }).id);
    const stageId = asText((stage as { id?: unknown }).id);
    if (!taskId || !stageId) continue;
    pushReminder({
      id: `${taskId}:${stageId}`,
      taskId,
      stageId,
      execSessionId:
        asText((task as { exec_session_id?: unknown }).exec_session_id) || undefined,
      title: asText((task as { title?: unknown }).title) || '长程提醒',
      stageTitle: asText((stage as { title?: unknown }).title),
      hint: asText((stage as { hint?: unknown }).hint) || undefined,
      dueAt: asText((stage as { due_at?: unknown }).due_at) || undefined,
    });
  }
}

export function formatLongHorizonDue(iso?: string): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString();
}
