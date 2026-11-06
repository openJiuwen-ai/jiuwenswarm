/**
 * Schema 组件：Text。
 * 只服务插件渲染层：接收上层 schema 节点，内部直接透传 antd Typography.Text 属性，
 * 不考虑作为通用 UI 组件复用。
 */

import { Typography, theme } from 'antd';
import type { TextProps } from 'antd/es/typography/Text';
import type { CSSProperties } from 'react';
import type { WebUIAdapterProps } from '../../types.ts';
import type { LocalizedText } from '../../../catalog/components/localizedText.ts';
import { useLocalizedText } from '../../useLocalizedText.ts';

export function WebUIText({ node, appearance }: WebUIAdapterProps) {
  const p = node.props as Pick<
    TextProps,
    'type' | 'disabled' | 'mark' | 'code' | 'keyboard' | 'underline' | 'delete' | 'strong' | 'italic' | 'ellipsis'
  > & {
    value?: LocalizedText;
    size?: 'small' | 'medium' | 'large' | 'sm' | 'md' | 'lg';
  };
  const value = useLocalizedText(p.value) ?? '';
  const { token } = theme.useToken();
  const size = {
    sm: token.fontSizeSM,
    md: token.fontSize,
    lg: token.fontSizeLG,
    small: token.fontSizeSM,
    medium: token.fontSize,
    large: token.fontSizeLG,
  };
  const style: CSSProperties = {
    fontSize: size[p.size as keyof typeof size] ?? token.fontSize,
    ...appearance?.style,
  };
  return (
    <Typography.Text
      type={p.type}
      disabled={p.disabled}
      mark={p.mark}
      code={p.code}
      keyboard={p.keyboard}
      underline={p.underline}
      delete={p.delete}
      strong={p.strong}
      italic={p.italic}
      ellipsis={p.ellipsis}
      className={appearance?.className}
      style={style}
      data-testid="webui-text"
    >
      {value}
    </Typography.Text>
  );
}
