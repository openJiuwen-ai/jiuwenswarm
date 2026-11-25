import { useEffect, useState } from 'react';
import { Flex, Tabs, type TabsProps } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';
import { WebUIContentActivity } from '../../ContentActivity.tsx';

export function WebUITabs({ node, context, emit, contentGroups, appearance }: WebUIAdapterProps) {
  const p = node.props as Pick<TabsProps, 'activeKey' | 'defaultActiveKey' | 'type' | 'tabPlacement'
    | 'size' | 'centered' | 'destroyOnHidden'> & {
      items: { key: string; label: string; disabled?: boolean }[]; ariaLabel?: string;
    };
  const firstEnabledKey = p.items.find((item) => !item.disabled)?.key ?? '';
  const [localActiveKey, setLocalActiveKey] = useState(p.activeKey ?? p.defaultActiveKey ?? firstEnabledKey);
  const activeKey = p.activeKey ?? (
    p.items.some((item) => item.key === localActiveKey && !item.disabled) ? localActiveKey : firstEnabledKey
  );
  useEffect(() => {
    if (localActiveKey !== activeKey) setLocalActiveKey(activeKey);
  }, [activeKey, localActiveKey]);
  const disabled = !context.connected;
  return <Tabs data-testid="webui-tabs" aria-label={p.ariaLabel} activeKey={activeKey} type={p.type}
    className={appearance?.className} style={appearance?.style}
    tabPlacement={p.tabPlacement} size={p.size} centered={p.centered} destroyOnHidden={p.destroyOnHidden}
    items={p.items.map((item, index) => ({
      key: item.key, disabled: item.disabled || disabled,
      label: <span data-testid="webui-tab-label" data-variant={item.key}>{item.label}</span>,
      children: <WebUIContentActivity active={item.key === activeKey && !item.disabled}>
        <Flex vertical gap="middle" className="min-w-0" data-testid="webui-tab-content" data-variant={item.key}>
          {contentGroups[String(index)]}
        </Flex>
      </WebUIContentActivity>,
    }))}
    onChange={(next) => {
      if (disabled) return;
      if (p.activeKey === undefined) setLocalActiveKey(next);
      if (node.events['ui.change']) void emit('ui.change', { activeKey: next });
    }}
  />;
}
