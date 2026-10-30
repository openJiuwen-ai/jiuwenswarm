import { useEffect, useRef, useState } from 'react';
import { Input, type InputProps } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';

export function WebUIInput({ node, context, emit, appearance }: WebUIAdapterProps) {
  const p = node.props as Pick<InputProps, 'value' | 'defaultValue' | 'placeholder' | 'disabled' | 'maxLength'
    | 'allowClear' | 'showCount' | 'size' | 'variant' | 'type' | 'readOnly' | 'status'> & { ariaLabel?: string };
  const [value, setValue] = useState(String(p.value ?? p.defaultValue ?? ''));
  const composing = useRef(false);
  const compositionCommit = useRef<string | null>(null);
  useEffect(() => {
    if (p.value !== undefined && !composing.current) setValue(String(p.value));
  }, [p.value]);
  const disabled = p.disabled || !context.connected;
  const emitChange = (next: string, isComposing: boolean) => {
    if (node.events['ui.change']) void emit('ui.change', { value: next, isComposing });
  };
  return <Input
    className={appearance?.className} style={appearance?.style}
    data-testid="webui-input" aria-label={p.ariaLabel} value={value} placeholder={p.placeholder}
    disabled={disabled} readOnly={p.readOnly} maxLength={p.maxLength} allowClear={p.allowClear}
    showCount={p.showCount} size={p.size} variant={p.variant} type={p.type} status={p.status}
    onCompositionStart={() => { composing.current = true; compositionCommit.current = null; }}
    onCompositionEnd={(event) => {
      composing.current = false;
      if (disabled || p.readOnly) return;
      const next = event.currentTarget.value;
      setValue(next);
      compositionCommit.current = next;
      emitChange(next, false);
    }}
    onClick={() => {
      if (node.events['ui.click'] && !disabled) void emit('ui.click', { value });
    }}
    onChange={(event) => {
      if (disabled || p.readOnly) return;
      const next = event.target.value;
      setValue(next);
      const isComposing = composing.current || (event.nativeEvent as InputEvent).isComposing === true;
      const duplicateCommit = !isComposing && compositionCommit.current === next;
      compositionCommit.current = null;
      if (!duplicateCommit) emitChange(next, isComposing);
    }}
    onPressEnter={(event) => {
      if (node.events['ui.press_enter'] && !disabled && !p.readOnly
        && !composing.current && !event.nativeEvent.isComposing && event.keyCode !== 229) {
        void emit('ui.press_enter', { value: event.currentTarget.value });
      }
    }}
  />;
}
