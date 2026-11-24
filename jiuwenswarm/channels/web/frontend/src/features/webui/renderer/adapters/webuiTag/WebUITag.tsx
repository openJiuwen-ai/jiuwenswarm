import { Tag, type TagProps } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';

export function WebUITag({ node, appearance }: WebUIAdapterProps) {
  const p = node.props as Pick<TagProps, 'color' | 'variant' | 'disabled' | 'href' | 'target'> & { children?: string };
  return <Tag data-testid="webui-tag" color={p.color} variant={p.variant} disabled={p.disabled}
    className={appearance?.className} style={appearance?.style}
    href={p.href} target={p.href ? p.target : undefined} rel={p.href && p.target === '_blank' ? 'noopener noreferrer' : undefined}>
    {p.children}
  </Tag>;
}
