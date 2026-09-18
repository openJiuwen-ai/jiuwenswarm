export type SessionIndicator = 'waiting' | 'processing' | 'unread' | 'error' | 'time';

export type SidebarMenuAction =
  | 'archive-sessions'
  | 'pin'
  | 'rename'
  | 'archive'
  | 'delete';

export type SidebarMenuItem = {
  action: SidebarMenuAction;
  label: string;
  danger?: boolean;
  pinned?: boolean;
};

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

const PIN_LABEL_PAIRS = {
  project: ['multiSession.project.pinProject', 'multiSession.project.unpinProject'],
  projectSession: ['multiSession.project.pinConversation', 'multiSession.project.unpinConversation'],
  conversation: ['multiSession.project.pin', 'multiSession.project.unpin'],
} as const;

function buildSidebarMenuItems(
  isPinned: boolean,
  pinLabels: readonly [string, string],
  translate: Translate,
  options: { archiveLabel: string },
): SidebarMenuItem[] {
  return [
    { action: 'pin', label: translate(isPinned ? pinLabels[1] : pinLabels[0]), pinned: isPinned },
    { action: 'rename', label: translate('multiSession.project.rename') },
    { action: 'archive', label: options.archiveLabel },
  ];
}

export function getProjectMenuItems(isPinned: boolean, translate: Translate, isDefault = false): SidebarMenuItem[] {
  // “删除已归档会话”属于归档管理页的项目分组操作，项目菜单只保留批量归档。
  const batchItems: SidebarMenuItem[] = [
    { action: 'archive-sessions', label: translate('multiSession.project.archiveSessions') },
  ];
  if (isDefault) return batchItems;
  // 项目不再有整体归档；菜单 = 置顶/重命名/删除 + 项目级批量会话操作。
  return [
    { action: 'pin', label: translate(isPinned ? PIN_LABEL_PAIRS.project[1] : PIN_LABEL_PAIRS.project[0]), pinned: isPinned },
    { action: 'rename', label: translate('multiSession.project.rename') },
    { action: 'delete', label: translate('multiSession.delete'), danger: true },
    ...batchItems,
  ];
}

export function getProjectSessionMenuItems(isPinned: boolean, translate: Translate): SidebarMenuItem[] {
  return buildSidebarMenuItems(isPinned, PIN_LABEL_PAIRS.projectSession, translate, {
    archiveLabel: translate('multiSession.project.archiveConversation'),
  });
}

export function getConversationMenuItems(
  isPinned: boolean,
  translate: Translate,
  options: { archivable?: boolean; deletable?: boolean } = {},
): SidebarMenuItem[] {
  const items = buildSidebarMenuItems(isPinned, PIN_LABEL_PAIRS.conversation, translate, {
    archiveLabel: translate('multiSession.project.archiveConversation'),
  });
  // cron/heartbeat 触发会话被后端禁止单独归档，不提供必然失败的菜单项。
  const visible = options.archivable === false
    ? items.filter((item) => item.action !== 'archive')
    : items;
  if (options.deletable) visible.push({ action: 'delete', label: translate('multiSession.delete'), danger: true });
  return visible;
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
