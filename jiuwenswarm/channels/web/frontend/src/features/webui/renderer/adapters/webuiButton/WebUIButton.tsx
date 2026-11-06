/**
 * Schema 组件：Button。
 * 只服务插件渲染层：直接透传 antd Button 属性与事件，接收宿主注入的外观，
 * 不考虑作为通用 UI 组件复用。
 */

import { Button as AntButton, type ButtonProps } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';
import { buttonIcon } from './buttonIcon.tsx';
import { useWebUIForms } from '../webuiForm/WebUIForm.tsx';
import type { LocalizedText } from '../../../catalog/components/localizedText.ts';
import { useLocalizedText } from '../../useLocalizedText.ts';

const SIZE_MAP: Readonly<Record<string, ButtonProps['size']>> = {
  sm: 'small',
  md: 'medium',
  lg: 'large',
  small: 'small',
  medium: 'medium',
  large: 'large',
};

export function WebUIButton({ node, context, emit, appearance }: WebUIAdapterProps) {
  const p = node.props as Pick<
    ButtonProps,
    | 'type'
    | 'color'
    | 'variant'
    | 'shape'
    | 'size'
    | 'danger'
    | 'ghost'
    | 'disabled'
    | 'block'
    | 'loading'
    | 'href'
    | 'target'
    | 'iconPlacement'
  > & {
    value?: LocalizedText;
    form?: string;
    icon?: string;
    'aria-label'?: LocalizedText;
  };
  const forms = useWebUIForms();
  const label = useLocalizedText(p.value) ?? '';
  // antd Button 没有 ariaLabel 属性；纯图标按钮的可访问名来自 HTML aria-label。
  const ariaLabel = useLocalizedText(p['aria-label'])?.trim() || undefined;
  const size = typeof p.size === 'string' ? (SIZE_MAP[p.size] ?? 'medium') : 'medium';
  const disabled = p.disabled === true || !context.connected;
  const submit = async (data: Record<string, unknown>) => {
    const result = await emit('ui.click', data);
    if (result.status === 'sent' && result.receipt?.accepted && typeof node.props.openPanel === 'string') {
      context.openPanel?.(node.props.openPanel);
    }
  };
  const onClick = node.events?.['ui.click'] ? async () => {
      if (typeof p.form !== 'string') {
        await submit({});
        return;
      }
    const form = forms?.get(p.form);
    if (!form) return;
    try {
      const values = await form.validateFields();
      await submit(values);
    } catch {
      // AntD Form.Item presents local validation errors; no event is sent.
    }
  } : undefined;
  const className = [appearance?.className, context.selected ? appearance?.activeClassName : undefined].filter(Boolean).join(' ');
  const artwork = appearance?.icon ?? buttonIcon(p.icon);
  const icon = artwork && <span className={appearance?.iconClassName} aria-hidden="true">{artwork}</span>;
  const hideLabel = appearance?.hideLabel ?? label.length === 0;
  const accessibleLabel = ariaLabel ?? (hideLabel && label ? label : undefined);
  const content = (
    <>
      {icon}
      {!hideLabel && <span className={appearance?.labelClassName} data-testid="webui-button-label">{label}</span>}
    </>
  );
  if (appearance?.unstyled) {
    return (
      <button
        type="button"
        className={className}
        style={appearance.style}
        disabled={disabled}
        title={accessibleLabel ?? (label || undefined)}
        aria-label={accessibleLabel ?? (label || undefined)}
        aria-pressed={appearance.activeClassName ? !!context.selected : undefined}
        onClick={onClick}
        data-testid="webui-button"
      >
        {content}
      </button>
    );
  }
  return (
    <AntButton
      type={p.type}
      color={p.color ?? (p.variant && p.variant !== 'solid' ? 'default' : undefined)}
      variant={p.variant}
      shape={p.shape}
      size={size}
      danger={p.danger}
      ghost={p.ghost}
      block={p.block}
      loading={p.loading}
      href={p.href}
      target={p.href ? p.target : undefined}
      iconPlacement={p.iconPlacement}
      disabled={disabled}
      className={className || undefined}
      style={appearance?.style}
      icon={icon}
      title={accessibleLabel ?? (appearance && label ? label : undefined)}
      aria-label={accessibleLabel}
      aria-pressed={appearance?.activeClassName ? !!context.selected : undefined}
      onClick={onClick}
      // schema 文本来自插件数据，必须逐字渲染；关闭 antd 对两个中文字的自动空格。
      autoInsertSpace={false}
      data-testid="webui-button"
    >
      {hideLabel ? null : appearance?.labelClassName
        ? <span className={appearance.labelClassName} data-testid="webui-button-label">{label}</span> : label}
    </AntButton>
  );
}
