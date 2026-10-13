import type { ReactNode } from 'react';

/** Schema 菜单项：携带完整展示与行为，不含业务概念 */
export type SidebarMenuItem = {
  key: string;
  label: string;
  icon: ReactNode;
  danger?: boolean;
  disabled?: boolean;
  onSelect: () => void;
};

export type TooltipHandlers = {
  onMouseEnter?: (event: { currentTarget: EventTarget | null }) => void;
  onMouseLeave?: () => void;
  onFocus?: (event: { currentTarget: EventTarget | null }) => void;
  onBlur?: () => void;
};
