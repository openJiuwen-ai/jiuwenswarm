import { Card, Flex, type CardProps } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';

export function WebUICard({ node, children, appearance }: WebUIAdapterProps) {
  const p = node.props as Pick<CardProps, 'title' | 'extra' | 'hoverable' | 'size' | 'variant'> & { cover?: string };
  return <Card data-testid="webui-card" title={p.title} extra={p.extra} hoverable={p.hoverable} size={p.size}
    className={appearance?.className} style={appearance?.style}
    variant={p.variant} cover={p.cover ? <img data-testid="webui-card-cover" src={p.cover} alt="" /> : undefined}>
    <Flex vertical gap="middle" className="min-w-0" data-testid="webui-card-content">
      {children}
    </Flex>
  </Card>;
}
