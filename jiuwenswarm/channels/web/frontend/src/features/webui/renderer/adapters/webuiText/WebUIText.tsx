/**
 * Schema 组件：Text。
 * 只服务插件渲染层：接收上层 schema 节点，内部直接处理 props（落地 antd Typography.Text），
 * 不考虑作为通用 UI 组件复用。
 */

import { Typography, theme } from 'antd';
import type { CSSProperties } from 'react';
import type { WebUIAdapterProps } from '../../types.ts';
import './WebUIText.css';

type TextType = 'secondary' | 'success' | 'warning' | 'danger';

function resolveType(props: Readonly<Record<string, unknown>>): TextType | undefined {
  const tone = typeof props.tone === 'string' ? props.tone : '';
  if (tone === 'success' || tone === 'warning' || tone === 'danger') {
    return tone;
  }
  if (!tone && props.variant === 'danger') return 'danger';
  if (!tone && props.variant === 'secondary') {
    return 'secondary';
  }
  return undefined;
}

export function WebUIText({ node, appearance }: WebUIAdapterProps) {
  const value = typeof node.props.value === 'string' ? node.props.value : '';
  const { token } = theme.useToken();
  const size = { sm: token.fontSizeSM, md: token.fontSize, lg: token.fontSizeLG,
    small: token.fontSizeSM, medium: token.fontSize, large: token.fontSizeLG };
  const weight = { regular: 400, medium: 500, bold: token.fontWeightStrong };
  // Typography.Text owns inline WebkitLineClamp; a CSS class supplies multiline truncation.
  const maxLines = typeof node.props.max_lines === 'number' && node.props.max_lines > 0 ? node.props.max_lines : undefined;
  const style: CSSProperties & { '--webui-text-max-lines'?: number } = {
    fontSize: size[node.props.size as keyof typeof size] ?? token.fontSize,
    fontWeight: weight[node.props.weight as keyof typeof weight] ?? weight.regular,
    fontFamily: node.props.monospace === true ? 'var(--font-mono)' : undefined,
    color: node.props.tone === 'info' ? token.colorInfo : node.props.tone ? undefined
      : node.props.variant === 'primary' ? token.colorPrimary
      : node.props.variant === 'ghost' ? token.colorTextTertiary : undefined,
    '--webui-text-max-lines': maxLines,
    ...appearance?.style,
  };
  return (
    <Typography.Text
      type={resolveType(node.props)}
      className={[maxLines ? 'webui-text-clamped' : undefined, appearance?.className].filter(Boolean).join(' ') || undefined}
      style={style}
      data-testid="webui-text"
    >
      {value}
    </Typography.Text>
  );
}
