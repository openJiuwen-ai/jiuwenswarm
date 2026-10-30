import { useEffect, useState } from 'react';
import { Slider, theme, type SliderSingleProps } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';

export function WebUISlider({ node, context, emit, appearance }: WebUIAdapterProps) {
  const p = node.props as Pick<SliderSingleProps, 'min' | 'max' | 'step' | 'value' | 'defaultValue' | 'marks'
    | 'dots' | 'included' | 'reverse' | 'orientation'> & { disabled?: boolean; ariaLabel?: string };
  const [value, setValue] = useState(p.value ?? p.defaultValue ?? 0);
  useEffect(() => {
    if (p.value !== undefined) setValue(p.value);
  }, [p.value]);
  const { token } = theme.useToken();
  const disabled = p.disabled || !context.connected;
  return <Slider data-testid="webui-slider" ariaLabelForHandle={p.ariaLabel} range={false}
    className={appearance?.className}
    min={p.min} max={p.max} step={p.step} value={value} marks={p.marks} dots={p.dots}
    included={p.included} reverse={p.reverse} orientation={p.orientation} disabled={disabled}
    style={{ ...(p.orientation === 'vertical' ? { minHeight: token.controlHeightLG * 4 } : {}), ...appearance?.style }}
    onChange={(next) => {
      if (disabled) return;
      setValue(next);
      if (node.events['ui.change']) void emit('ui.change', { value: next });
    }}
  />;
}
