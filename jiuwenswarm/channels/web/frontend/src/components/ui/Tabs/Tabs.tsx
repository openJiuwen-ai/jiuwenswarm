import type { AriaRole, ReactNode } from 'react';
import './Tabs.css';

export interface TabsItem {
  value: string;
  label: ReactNode;
  /** 动作项：传入 onClick 后该条目不再是可选中页签——点击只触发自身回调（不经过 onChange）、
      永远不会呈现选中态，也不参与 tablist 的 tab 语义。用于 tab 行内混排的动作按钮
      （如连接器市场工具栏的"应用插件"） */
  onClick?: () => void;
}

export interface TabsProps<T extends string = string> {
  items: readonly TabsItem[];
  value: T;
  onChange?: (value: T) => void;
  /** 底部 1px 分隔线（inset box-shadow 实现） */
  bordered?: boolean;
  wrapperTestId?: string;
  itemTestId?: string;
  className?: string;
  role?: AriaRole;
  ariaLabel?: string;
}

export function Tabs<T extends string = string>({
  items,
  value,
  onChange,
  bordered = false,
  wrapperTestId,
  itemTestId,
  className,
  role,
  ariaLabel,
}: TabsProps<T>) {
  const classes = ['tabs', bordered ? 'tabs--bordered' : '', className].filter(Boolean).join(' ');
  return (
    <div className={classes} role={role} aria-label={ariaLabel} data-testid={wrapperTestId}>
      {items.map((item) => (
        <button
          key={item.value}
          type="button"
          role={role === 'tablist' && !item.onClick ? 'tab' : undefined}
          aria-selected={role === 'tablist' && !item.onClick ? item.value === value : undefined}
          onClick={item.onClick ?? (onChange ? () => onChange(item.value as T) : undefined)}
          className={!item.onClick && item.value === value ? 'is-active' : ''}
          data-testid={itemTestId}
          data-variant={item.value}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}
