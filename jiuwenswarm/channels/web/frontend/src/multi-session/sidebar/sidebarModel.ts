export type SessionIndicator = 'waiting' | 'processing' | 'unread' | 'error' | 'time';

type Translate = (key: string, options?: Record<string, unknown>) => string;

type RuntimeLike = {
  pendingQuestions?: readonly unknown[];
  isProcessing?: boolean;
  executionError?: string | null;
};

type SessionLike = {
  session_id: string;
  title?: string;
  last_user_message_at?: number;
  last_message_at?: number;
  updated_at?: string;
  created_at?: string;
  pinned?: boolean;
  pin_order?: number;
};

function normalizeActivityTime(value: number | string | undefined): number | null {
  if (typeof value === 'number') {
    return value < 1e11 ? value * 1000 : value;
  }
  if (typeof value === 'string') {
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? null : parsed;
  }
  return null;
}

export function getSessionActivityAt(session: Pick<SessionLike, 'last_user_message_at' | 'last_message_at' | 'updated_at' | 'created_at'>): number {
  return normalizeActivityTime(session.last_user_message_at)
    ?? normalizeActivityTime(session.last_message_at)
    ?? normalizeActivityTime(session.updated_at)
    ?? normalizeActivityTime(session.created_at)
    ?? Date.now();
}

export function getSessionIndicator(
  runtime: RuntimeLike | undefined,
  unread: boolean,
  sessionProcessing = false,
  sessionError = false,
): SessionIndicator {
  if (sessionError) return 'error';
  if (runtime?.pendingQuestions?.[0]) return 'waiting';
  if (runtime?.isProcessing || sessionProcessing) return 'processing';
  if (unread) return 'unread';
  return 'time';
}

export function getTaskStatusLabel(indicator: SessionIndicator, translate: Translate): string {
  if (indicator === 'waiting') return translate('multiSession.status.waiting');
  if (indicator === 'processing') return translate('multiSession.status.processing');
  if (indicator === 'unread') return translate('multiSession.status.unread');
  if (indicator === 'error') return translate('multiSession.status.error');
  return translate('multiSession.status.read');
}

export function getProjectNewLabel(projectName: string, translate: Translate): string {
  return translate('multiSession.project.startConversation', { projectName });
}

export function sortSessionsForSidebar<T extends SessionLike>(sessions: T[]): T[] {
  return [...sessions].sort((left, right) => {
    const pinDelta = Number(Boolean(right.pinned)) - Number(Boolean(left.pinned));
    if (pinDelta !== 0) return pinDelta;
    const leftPinOrder = left.pin_order ?? 0;
    const rightPinOrder = right.pin_order ?? 0;
    if (left.pinned && right.pinned && leftPinOrder !== rightPinOrder) {
      return leftPinOrder - rightPinOrder;
    }
    return getSessionActivityAt(right) - getSessionActivityAt(left);
  });
}
