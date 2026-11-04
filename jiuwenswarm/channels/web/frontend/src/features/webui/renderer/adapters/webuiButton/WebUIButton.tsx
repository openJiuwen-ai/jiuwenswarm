/**
 * Schema 组件：Button。
 * 只服务插件渲染层：处理 schema props 与事件，接收宿主注入的外观，默认使用 antd Button，
 * 不考虑作为通用 UI 组件复用。
 */

import { Button as AntButton } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';
import { buttonIcon } from './buttonIcon.tsx';
import { useWebUIForms } from '../webuiForm/WebUIForm.tsx';

type ButtonType = 'default' | 'primary' | 'dashed' | 'link' | 'text';
type ButtonSize = 'small' | 'middle' | 'large';

const SIZE_MAP: Readonly<Record<string, ButtonSize>> = {
  sm: 'small',
  md: 'middle',
  lg: 'large',
  small: 'small',
  medium: 'middle',
  large: 'large',
};

function resolveButton(props: Readonly<Record<string, unknown>>): { type: ButtonType; danger: boolean } {
  const variant = typeof props.variant === 'string' ? props.variant : 'default';
  if (variant === 'primary') {
    return { type: 'primary', danger: false };
  }
  if (variant === 'danger') {
    return { type: 'default', danger: true };
  }
  if (variant === 'ghost') {
    return { type: 'text', danger: false };
  }
  return { type: 'default', danger: false };
}

export function WebUIButton({ node, context, emit, appearance }: WebUIAdapterProps) {
  const forms = useWebUIForms();
  const label = typeof node.props.label === 'string' ? node.props.label : '';
  const size = typeof node.props.size === 'string' ? (SIZE_MAP[node.props.size] ?? 'middle') : 'middle';
  const disabled = node.props.disabled === true || !context.connected;
  const { type, danger } = resolveButton(node.props);
  const onClick = node.events?.['ui.click'] ? async () => {
    if (typeof node.props.form !== 'string') { await emit('ui.click', {}); return; }
    const form = forms?.get(node.props.form);
    if (!form) return;
    try {
      const values = await form.validateFields();
      await emit('ui.click', values);
    } catch {
      // AntD Form.Item presents local validation errors; no event is sent.
    }
  } : undefined;
  const className = [appearance?.className, context.selected ? appearance?.activeClassName : undefined].filter(Boolean).join(' ');
  const artwork = appearance?.icon ?? buttonIcon(node.props.icon);
  const icon = artwork && <span className={appearance?.iconClassName} aria-hidden="true">{artwork}</span>;
  const hideLabel = appearance?.hideLabel ?? node.props.iconOnly === true;
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
        title={label}
        aria-label={label}
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
      type={type}
      color={node.props.variant === 'secondary' ? 'default' : undefined}
      variant={node.props.variant === 'secondary' ? 'filled' : undefined}
      size={size}
      danger={danger}
      disabled={disabled}
      className={className || undefined}
      style={appearance?.style}
      icon={icon}
      title={appearance || hideLabel ? label : undefined}
      aria-label={hideLabel ? label : undefined}
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
