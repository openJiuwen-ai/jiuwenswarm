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
    // 0 / 负数 / NaN 都表示"没有有效时间"，不能当作合法时刻参与排序与展示。
    // 早先用 `?? Date.now()` 兜底，会让这类会话被伪装成"刚刚"并顶到列表最前。
    if (!Number.isFinite(value) || value <= 0) return null;
    const milliseconds = value < 1e11 ? value * 1000 : value;
    return Number.isFinite(milliseconds) && milliseconds > 0 ? milliseconds : null;
  }
  if (typeof value === 'string') {
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) || parsed <= 0 ? null : parsed;
  }
  return null;
}

export function getSessionActivityAt(session: Pick<SessionLike, 'last_user_message_at' | 'last_message_at' | 'updated_at' | 'created_at'>): number {
  return normalizeActivityTime(session.last_user_message_at)
    ?? normalizeActivityTime(session.last_message_at)
    ?? normalizeActivityTime(session.updated_at)
    ?? normalizeActivityTime(session.created_at)
    // 与 stores/workspaceStore.ts 的同名函数保持一致：无法确定时间时返回 0，
    // 由排序与展示两侧各自处理"未知时间"，避免同一会话在两处排序结果相反。
    ?? 0;
}

/**
 * 侧栏会话时间文案。
 *
 * - 未来时间戳（时钟漂移）按"刚刚"处理；
 * - 7 天内用相对时间；
 * - 7 天以上显示月日，跨年时补上年份，避免「1月5日」无法区分年份；
 * - 完全无法确定时间时返回空串，不臆造"刚刚"。
 */
export function formatSessionTime(
  activityAt: number,
  now: number,
  language: string,
  translate: Translate,
): string {
  if (!Number.isFinite(activityAt) || activityAt <= 0) return '';
  const elapsed = Math.max(0, now - activityAt);
  if (elapsed < 60_000) return translate('time.justNow');
  if (elapsed < 3_600_000) return translate('time.minutesAgo', { count: Math.floor(elapsed / 60_000) });
  if (elapsed < 86_400_000) return translate('time.hoursAgo', { count: Math.floor(elapsed / 3_600_000) });
  if (elapsed < 604_800_000) return translate('time.daysAgo', { count: Math.floor(elapsed / 86_400_000) });
  const activityDate = new Date(activityAt);
  const sameYear = activityDate.getFullYear() === new Date(now).getFullYear();
  return activityDate.toLocaleDateString(language, sameYear
    ? { month: 'short', day: 'numeric' }
    : { year: 'numeric', month: 'short', day: 'numeric' });
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

<<<<<<< HEAD
export function getViewedSessionId(
  workMode: string,
  activeSessionId: string | null,
  activeDesignSessionId: string | undefined,
): string | null {
  return workMode === 'design' ? activeDesignSessionId || null : activeSessionId;
}

||||||| e487e9a30
=======
export function getViewedSessionId(
  designCanvasOpen: boolean,
  activeSessionId: string | null,
  activeDesignSessionId: string | undefined,
): string | null {
  return designCanvasOpen ? activeDesignSessionId || null : activeSessionId;
}

>>>>>>> origin/develop
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
