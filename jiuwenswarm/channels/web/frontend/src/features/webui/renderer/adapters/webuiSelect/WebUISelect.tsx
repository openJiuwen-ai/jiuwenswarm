import { useEffect, useState } from 'react';
import { Select, type SelectProps } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';

type Selection = string | number | (string | number)[] | null;
export function WebUISelect({ node, context, emit, appearance }: WebUIAdapterProps) {
  const p = node.props as Pick<SelectProps<Selection>, 'options' | 'value' | 'defaultValue' | 'mode' | 'disabled'
    | 'placeholder' | 'allowClear' | 'size' | 'maxCount' | 'variant' | 'status' | 'maxTagCount'>
    & { showSearch: boolean; ariaLabel?: string };
  const [value, setValue] = useState<Selection>(p.value !== undefined ? p.value : p.defaultValue ?? (p.mode ? [] : null));
  useEffect(() => {
    if (p.value !== undefined) setValue(p.value);
  }, [p.value]);
  const disabled = p.disabled || !context.connected;
  return <Select<Selection>
    className={appearance?.className} style={appearance?.style}
    data-testid="webui-select" aria-label={p.ariaLabel} options={p.options} value={value} mode={p.mode}
    disabled={disabled} placeholder={p.placeholder} allowClear={p.allowClear} size={p.size} maxCount={p.maxCount}
    variant={p.variant} status={p.status} maxTagCount={p.maxTagCount}
    showSearch={p.showSearch ? { optionFilterProp: 'label', onSearch: (searchValue) => {
      if (!disabled && node.events['ui.search']) void emit('ui.search', { searchValue });
    } } : false}
    onChange={(next) => { if (!disabled) setValue(next ?? (p.mode ? [] : null)); }}
    onSelect={(next) => { if (!disabled && node.events['ui.select']) void emit('ui.select', { value: next }); }}
    onClear={() => { if (!disabled && node.events['ui.clear']) void emit('ui.clear', {}); }}
  />;
}
