type Translate = (key: string, options?: Record<string, unknown>) => string;

/** 菜单项纯 schema（无 icon / onSelect），供 Hook 装配与单测 */
export type SidebarMenuItemDef = {
  key: string;
  label: string;
  danger?: boolean;
  disabled?: boolean;
};

const PIN_LABEL_PAIRS = {
  project: ['multiSession.project.pinProject', 'multiSession.project.unpinProject'],
  projectSession: ['multiSession.project.pinConversation', 'multiSession.project.unpinConversation'],
  conversation: ['multiSession.project.pin', 'multiSession.project.unpin'],
} as const;

export function getProjectMenuItems(
  isPinned: boolean,
  translate: Translate,
  options: { isDefault?: boolean; archiveSessionsDisabled?: boolean } = {},
): SidebarMenuItemDef[] {
  const batchItems: SidebarMenuItemDef[] = [
    {
      key: 'archive-sessions',
      label: translate('multiSession.project.archiveSessions'),
      disabled: options.archiveSessionsDisabled,
    },
  ];
  if (options.isDefault) return batchItems;
  return [
    {
      key: 'pin',
      label: translate(isPinned ? PIN_LABEL_PAIRS.project[1] : PIN_LABEL_PAIRS.project[0]),
    },
    { key: 'rename', label: translate('multiSession.project.rename') },
    // 项目移除是软删除（可从 toast 撤销），文案与真正的删除操作区分，不共用 multiSession.delete。
    { key: 'delete', label: translate('multiSession.project.removeProject'), danger: true },
    ...batchItems,
  ];
}

export function getSessionMenuItems(
  isPinned: boolean,
  translate: Translate,
  options: {
    scope?: 'project' | 'conversation';
    /** cron 会话禁止单独归档 */
    archivable?: boolean;
  } = {},
): SidebarMenuItemDef[] {
  const scope = options.scope ?? 'conversation';
  const pinLabels = scope === 'project' ? PIN_LABEL_PAIRS.projectSession : PIN_LABEL_PAIRS.conversation;
  const items: SidebarMenuItemDef[] = [
    {
      key: 'pin',
      label: translate(isPinned ? pinLabels[1] : pinLabels[0]),
    },
    { key: 'rename', label: translate('multiSession.project.rename') },
  ];
  if (options.archivable !== false) {
    items.push({
      key: 'archive',
      label: translate('multiSession.project.archiveConversation'),
    });
  }
  items.push({ key: 'delete', label: translate('multiSession.delete'), danger: true });
  return items;
}
