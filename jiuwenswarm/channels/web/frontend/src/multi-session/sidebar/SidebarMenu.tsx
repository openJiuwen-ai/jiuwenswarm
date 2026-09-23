import { useTranslation } from 'react-i18next';
import type { ProjectInfo, Session } from '../../types';
import { SidebarActionMenu } from './SidebarActionMenu';
import type { TooltipHandlers } from './SidebarMenu.types';
import { useSidebarMenu } from './useSidebarMenu';

type SidebarMenuSharedProps = {
  ariaLabel?: string;
  triggerTestId?: string;
  contentTestId?: string;
  onOpenChange?: (open: boolean) => void;
  triggerTooltipHandlers?: TooltipHandlers;
};

export type SidebarMenuProps =
  | (SidebarMenuSharedProps & {
      type: 'session';
      session: Session;
      scope?: 'project' | 'conversation';
      activeSessionId: string | null;
      onLeaveActiveSession: () => void;
    })
  | (SidebarMenuSharedProps & {
      type: 'project';
      project: ProjectInfo;
      archiveSessionsDisabled?: boolean;
    });

/**
 * 侧边栏菜单统一中转层：按 type 区分业务 Hook 分支，再接到 Schema 组件。
 * 父组件只传判别联合 props，不参与菜单行为分发。
 */
export function SidebarMenu(props: SidebarMenuProps) {
  const { t } = useTranslation();
  const {
    ariaLabel,
    triggerTestId = props.type === 'session'
      ? 'multi-session-conversation-list-item-more'
      : 'multi-session-project-row-more',
    contentTestId,
    onOpenChange,
    triggerTooltipHandlers,
    ...menuOptions
  } = props;
  const { items, dialogs } = useSidebarMenu(menuOptions as Parameters<typeof useSidebarMenu>[0]);

  return (
    <>
      <SidebarActionMenu
        items={items}
        ariaLabel={ariaLabel ?? t('multiSession.moreActions')}
        triggerTestId={triggerTestId}
        contentTestId={contentTestId}
        onOpenChange={onOpenChange}
        triggerTooltipHandlers={triggerTooltipHandlers}
      />
      {dialogs}
    </>
  );
}
