import { useEffect, useState } from 'react';
import { Button, Dropdown, type DropdownProps, type MenuProps } from 'antd';
import { useTranslation } from 'react-i18next';
import type { WebUIAdapterProps } from '../../types.ts';
import type { WebUIDropdownItem } from '../../../catalog/components/dropdown.ts';
import { buttonIcon } from '../webuiButton/buttonIcon.tsx';
import './WebUIDropdown.css';

/** Native AntD menu; selected items use the published ui.select contract. */
export function WebUIDropdown({ node, context, children, contentGroups, emit, appearance }: WebUIAdapterProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const p = node.props;
  const items = p.items as WebUIDropdownItem[];
  const disabled = p.disabled === true || !context.connected;
  const expanded = open && !disabled;
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  const className = ['webui-dropdown__trigger', p.icon ? 'webui-dropdown__trigger--icon' : undefined,
    appearance?.className].filter(Boolean).join(' ');
  const onMenuClick: MenuProps['onClick'] = ({ key }) => {
    if (disabled || !items.some((item) => item.type !== 'divider' && item.key === key && !item.disabled)) return;
    setOpen(false);
    if (node.events['ui.select']) void emit('ui.select', { key });
  };
  return (
    <Dropdown
      menu={{ items: items.map((item, index) => item.type === 'divider' ? item : ({
        ...item, label: <span data-testid="webui-dropdown-item" data-variant={item.key}>
          {contentGroups[String(index)] ?? (typeof item.label === 'string' ? item.label : null)}
        </span>,
      })), onClick: onMenuClick }}
      trigger={[(p.trigger as 'click' | 'hover') ?? 'click']}
      placement={p.placement as DropdownProps['placement']}
      align={p.align as DropdownProps['align']}
      autoFocus={p.autoFocus as boolean | undefined}
      destroyOnHidden={p.destroyOnHidden as boolean | undefined}
      styles={p.styles as DropdownProps['styles']}
      disabled={disabled}
      open={expanded}
      onOpenChange={(value) => setOpen(!disabled && value)}
    >
      {p.icon ? <Button
        type="text"
        icon={buttonIcon(p.icon)}
        disabled={disabled}
        title={p.ariaLabel as string | undefined}
        aria-label={p.ariaLabel as string}
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
        aria-label={typeof p.ariaLabel === 'string' ? p.ariaLabel : t('webui.menuLabel')}
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
      >{children}</span>}
    </Dropdown>
  );
}
