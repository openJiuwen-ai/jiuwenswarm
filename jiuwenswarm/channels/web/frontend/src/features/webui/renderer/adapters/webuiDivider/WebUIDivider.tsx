import { Children } from 'react';
import { Divider, type DividerProps } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';

export function WebUIDivider({ node, children, appearance }: WebUIAdapterProps) {
  const p = node.props as Pick<DividerProps, 'orientation' | 'variant' | 'titlePlacement' | 'plain' | 'size'> & { children?: string };
  return <Divider data-testid="webui-divider" orientation={p.orientation} variant={p.variant}
    className={appearance?.className} style={appearance?.style}
    titlePlacement={p.orientation === 'vertical' ? undefined : p.titlePlacement} plain={p.plain}
    size={p.orientation === 'vertical' ? undefined : p.size}>
    {p.children ?? (Children.count(children) ? children : undefined)}
  </Divider>;
}
