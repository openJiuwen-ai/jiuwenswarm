import { useEffect, useRef, useState } from 'react';
import { Input, type InputProps } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';
import type { LocalizedText } from '../../../catalog/components/localizedText.ts';
import { useLocalizedText } from '../../useLocalizedText.ts';

export function WebUIInput({ node, context, emit, appearance, fieldControl }: WebUIAdapterProps) {
  const p = node.props as Pick<InputProps, 'value' | 'defaultValue' | 'placeholder' | 'disabled' | 'maxLength'
    | 'allowClear' | 'showCount' | 'size' | 'variant' | 'type' | 'readOnly' | 'status' | 'autoComplete'> & { ariaLabel?: LocalizedText };
  const ariaLabel = useLocalizedText(p.ariaLabel);
  const controlled = fieldControl?.onChange !== undefined;
  const [localValue, setValue] = useState(String(p.value ?? p.defaultValue ?? ''));
  const value = controlled ? String(fieldControl?.value ?? '') : localValue;
  const updateValue = (next: string) => {
    if (controlled) fieldControl?.onChange?.(next);
    else setValue(next);
  };
  const composing = useRef(false);
  const compositionCommit = useRef<string | null>(null);
  useEffect(() => {
    if (!controlled && p.value !== undefined && !composing.current) setValue(String(p.value));
  }, [p.value, controlled]);
  const disabled = p.disabled || !context.connected;
  const emitChange = (next: string, isComposing: boolean) => {
    if (!controlled && node.events['ui.change']) void emit('ui.change', { value: next, isComposing });
  };
  return <Input
    className={appearance?.className} style={appearance?.style}
    id={fieldControl?.id} aria-invalid={fieldControl?.['aria-invalid']}
    aria-describedby={fieldControl?.['aria-describedby']} aria-required={fieldControl?.['aria-required']}
    data-testid="webui-input" aria-label={ariaLabel} value={value} placeholder={p.placeholder}
    disabled={disabled} readOnly={p.readOnly} maxLength={p.maxLength} allowClear={p.allowClear}
    showCount={p.showCount} size={p.size} variant={p.variant} type={p.type} status={p.status} autoComplete={p.autoComplete}
    onCompositionStart={() => { composing.current = true; compositionCommit.current = null; }}
    onCompositionEnd={(event) => {
      composing.current = false;
      if (disabled || p.readOnly) return;
      const next = event.currentTarget.value;
      updateValue(next);
      compositionCommit.current = next;
      emitChange(next, false);
    }}
    onClick={() => {
      if (!controlled && node.events['ui.click'] && !disabled) void emit('ui.click', { value });
    }}
    onChange={(event) => {
      if (disabled || p.readOnly) return;
      const next = event.target.value;
      updateValue(next);
      const isComposing = composing.current || (event.nativeEvent as InputEvent).isComposing === true;
      const duplicateCommit = !isComposing && compositionCommit.current === next;
      compositionCommit.current = null;
      if (!duplicateCommit) emitChange(next, isComposing);
    }}
    onPressEnter={(event) => {
      if (!controlled && node.events['ui.press_enter'] && !disabled && !p.readOnly
        && !composing.current && !event.nativeEvent.isComposing && event.keyCode !== 229) {
        void emit('ui.press_enter', { value: event.currentTarget.value });
      }
    }}
    onBlur={() => { if (!composing.current) fieldControl?.onBlur?.(); }}
  />;
}
