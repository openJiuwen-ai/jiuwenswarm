import { useEffect, useState } from 'react';
import { Select, type SelectProps } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';
import { useTranslation } from 'react-i18next';
import type { LocalizedText } from '../../../catalog/components/localizedText.ts';
import { resolveLocalizedText, useLocalizedText } from '../../useLocalizedText.ts';

type Selection = string | number | (string | number)[] | null;
export function WebUISelect({ node, context, emit, appearance }: WebUIAdapterProps) {
  const { i18n } = useTranslation();
  const p = node.props as Pick<SelectProps<Selection>, 'value' | 'defaultValue' | 'mode' | 'disabled'
    | 'placeholder' | 'allowClear' | 'size' | 'maxCount' | 'variant' | 'status' | 'maxTagCount'>
    & { showSearch: boolean; ariaLabel?: LocalizedText; options: { value: string | number; label: LocalizedText; disabled?: boolean }[] };
  const ariaLabel = useLocalizedText(p.ariaLabel);
  const options = p.options.map(option => ({ ...option, label: resolveLocalizedText(option.label, i18n.resolvedLanguage ?? i18n.language) }));
  const [value, setValue] = useState<Selection>(p.value !== undefined ? p.value : p.defaultValue ?? (p.mode ? [] : null));
  useEffect(() => {
    if (p.value !== undefined) setValue(p.value);
  }, [p.value]);
  const disabled = p.disabled || !context.connected;
  return <Select<Selection>
    className={appearance?.className} style={appearance?.style}
    data-testid="webui-select" aria-label={ariaLabel} options={options} value={value} mode={p.mode}
    disabled={disabled} placeholder={p.placeholder} allowClear={p.allowClear} size={p.size} maxCount={p.maxCount}
    variant={p.variant} status={p.status} maxTagCount={p.maxTagCount}
    showSearch={p.showSearch ? { optionFilterProp: 'label', onSearch: (searchValue) => {
      if (!disabled && node.events['ui.search']) void emit('ui.search', { searchValue });
    } } : false}
    onChange={(next) => {
      if (disabled) return;
      const normalized = next ?? (p.mode ? [] : null);
      setValue(normalized);
      if (node.events['ui.change']) void emit('ui.change', { value: normalized });
    }}
    onSelect={(next) => { if (!disabled && node.events['ui.select']) void emit('ui.select', { value: next }); }}
    onClear={() => { if (!disabled && node.events['ui.clear']) void emit('ui.clear', {}); }}
  />;
}
