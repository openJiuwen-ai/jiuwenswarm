import { useState } from 'react';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../../components/ui';
import type { SidebarMenuItem, TooltipHandlers } from './SidebarMenu.types';
import MoreIcon from '../../assets/work-mode/more-rimless.svg?react';

export interface SidebarActionMenuProps {
  items: SidebarMenuItem[];
  ariaLabel: string;
  /** 触发按钮 data-testid */
  triggerTestId: string;
  /** 菜单面板 data-testid，默认 multi-session-conversation-menu */
  contentTestId?: string;
  /** 打开/关闭时回调（行组件用 is-menu-open、清 tooltip） */
  onOpenChange?: (open: boolean) => void;
  /** 菜单关闭时挂到触发按钮上的自适应 tooltip handlers */
  triggerTooltipHandlers?: TooltipHandlers;
}

/** 侧边栏行「更多」下拉：仅负责布局与交互；业务由 items 自带 */
export function SidebarActionMenu({
  items,
  ariaLabel,
  triggerTestId,
  contentTestId = 'multi-session-conversation-menu',
  onOpenChange,
  triggerTooltipHandlers,
}: SidebarActionMenuProps) {
  const [menuOpen, setMenuOpen] = useState(false);

  return (
    <DropdownMenu
      open={menuOpen}
      onOpenChange={(open) => {
        if (open) triggerTooltipHandlers?.onMouseLeave?.();
        setMenuOpen(open);
        onOpenChange?.(open);
      }}
    >
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="conversation-list-item__actions"
          onClick={(event) => event.stopPropagation()}
          aria-label={ariaLabel}
          data-tooltip={ariaLabel}
          data-testid={triggerTestId}
          {...(menuOpen ? {} : triggerTooltipHandlers)}
        >
          <MoreIcon aria-hidden />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="bottom" align="end" data-testid={contentTestId}>
        {items.map((item) => (
          <DropdownMenuItem
            key={item.key}
            icon={item.icon}
            danger={item.danger}
            disabled={item.disabled}
            onSelect={item.onSelect}
            data-testid="multi-session-conversation-menu-item"
            data-variant={item.key}
          >
            {item.label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
