import { useState } from 'react';
import { Dropdown, type MenuProps } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';
import './WebUIDropdown.css';

interface DropdownItem {
  key: string;
  label: string;
  disabled?: boolean;
  danger?: boolean;
}

/** Menu shell around the host-supplied trigger children; item clicks go through ui.select. */
export function WebUIDropdown({ node, context, children, emit, appearance }: WebUIAdapterProps) {
  const p = node.props as {
    items: DropdownItem[];
    trigger?: 'click' | 'hover';
    placement?: 'bottomLeft' | 'bottomRight' | 'topLeft' | 'topRight';
    disabled?: boolean;
    ariaLabel?: string;
  };
  const [open, setOpen] = useState(false);
  const disabled = Boolean(p.disabled) || !context.connected;
  const items: MenuProps['items'] = p.items.map((item) => ({
    key: item.key,
    danger: item.danger,
    disabled: item.disabled,
    label: <span data-testid="webui-dropdown-item" data-variant={item.key}>{item.label}</span>,
  }));
  const onMenuClick: MenuProps['onClick'] = ({ key }) => {
    if (disabled || !node.events['ui.select']) return;
    void emit('ui.select', { key });
  };
  return (
    <Dropdown
      menu={{ items, onClick: onMenuClick, style: { minWidth: 148 } }}
      trigger={[p.trigger ?? 'click']}
      placement={p.placement ?? 'topLeft'}
      disabled={disabled}
      open={open}
      onOpenChange={setOpen}
    >
      <span
        data-testid="webui-dropdown-trigger"
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-label={p.ariaLabel}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-disabled={disabled}
        className={['webui-dropdown__trigger', appearance?.className].filter(Boolean).join(' ')}
        style={appearance?.style}
        onKeyDown={(event) => {
          if (disabled) return;
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            setOpen((value) => !value);
          }
        }}
      >
        {children}
      </span>
    </Dropdown>
  );
}
