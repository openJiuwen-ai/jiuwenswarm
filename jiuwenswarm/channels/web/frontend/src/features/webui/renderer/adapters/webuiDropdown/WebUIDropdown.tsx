import { useEffect, useState } from 'react';
import { Button, Dropdown, type MenuProps } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';
import { buttonIcon } from '../webuiButton/buttonIcon.tsx';
import './WebUIDropdown.css';

interface DropdownItem {
  key: string;
  label: string;
  disabled?: boolean;
  danger?: boolean;
}

/** Menu with an optional built-in icon trigger; item clicks go through ui.select. */
export function WebUIDropdown({ node, context, children, emit, appearance }: WebUIAdapterProps) {
  const p = node.props as {
    items: DropdownItem[];
    trigger?: 'click' | 'hover';
    placement?: 'bottomLeft' | 'bottomRight' | 'topLeft' | 'topRight';
    disabled?: boolean;
    ariaLabel?: string;
    icon?: string;
  };
  const [open, setOpen] = useState(false);
  const disabled = Boolean(p.disabled) || !context.connected;
  const expanded = open && !disabled;
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  const className = ['webui-dropdown__trigger', p.icon ? 'webui-dropdown__trigger--icon' : undefined,
    appearance?.className].filter(Boolean).join(' ');
  const items: MenuProps['items'] = p.items.map((item) => ({
    key: item.key,
    danger: item.danger,
    disabled: item.disabled,
    label: <span data-testid="webui-dropdown-item" data-variant={item.key}>{item.label}</span>,
  }));
  const onMenuClick: MenuProps['onClick'] = ({ key }) => {
    if (disabled || !p.items.some((item) => item.key === key && !item.disabled)) return;
    setOpen(false);
    if (node.events['ui.select']) void emit('ui.select', { key });
  };
  return (
    <Dropdown
      menu={{ items, onClick: onMenuClick, style: { minWidth: 148 } }}
      trigger={[p.trigger ?? 'click']}
      placement={p.placement ?? 'topLeft'}
      disabled={disabled}
      open={expanded}
      onOpenChange={(value) => setOpen(!disabled && value)}
    >
      {p.icon ? <Button
        type="text"
        icon={buttonIcon(p.icon)}
        disabled={disabled}
        title={p.ariaLabel}
        aria-label={p.ariaLabel}
        aria-haspopup="menu"
        aria-expanded={expanded}
        data-testid="webui-dropdown-trigger"
        className={className}
        style={appearance?.style}
        onKeyDown={(event) => {
          if (!disabled && event.key === 'Escape') { event.preventDefault(); setOpen(false); }
        }}
      /> : <span
        data-testid="webui-dropdown-trigger"
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-label={p.ariaLabel}
        aria-haspopup="menu"
        aria-expanded={expanded}
        aria-disabled={disabled}
        className={className}
        style={appearance?.style}
        onKeyDown={(event) => {
          if (disabled) return;
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            setOpen((value) => !value);
          }
          if (event.key === 'Escape') setOpen(false);
        }}
      >
        {children}
      </span>}
    </Dropdown>
  );
}
