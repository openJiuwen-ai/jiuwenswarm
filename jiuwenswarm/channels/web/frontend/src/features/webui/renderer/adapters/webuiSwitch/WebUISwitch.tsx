import { useEffect, useState } from 'react';
import { Switch, type SwitchProps } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';

export function WebUISwitch({ node, context, emit, appearance }: WebUIAdapterProps) {
  const p = node.props as Pick<SwitchProps, 'checked' | 'defaultChecked' | 'disabled' | 'loading' | 'size'
    | 'checkedChildren' | 'unCheckedChildren'> & { ariaLabel?: string };
  const [checked, setChecked] = useState(p.checked ?? p.defaultChecked ?? false);
  useEffect(() => {
    if (p.checked !== undefined) setChecked(p.checked);
  }, [p.checked]);
  const disabled = p.disabled || p.loading || !context.connected;
  return <Switch data-testid="webui-switch" aria-label={p.ariaLabel} checked={checked} disabled={disabled}
    className={appearance?.className} style={appearance?.style}
    loading={p.loading} size={p.size} checkedChildren={p.checkedChildren} unCheckedChildren={p.unCheckedChildren}
    onChange={(next) => {
      if (disabled) return;
      setChecked(next);
      if (node.events['ui.change']) void emit('ui.change', { checked: next });
    }}
  />;
}
