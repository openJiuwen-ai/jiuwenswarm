import { Avatar } from 'antd';
import type { CSSProperties } from 'react';
import type { WebUIAdapterProps } from '../../types.ts';
import { useGatewayResource } from '../../../client/GatewayResources.tsx';

export function WebUIAvatar({ node, appearance }: WebUIAdapterProps) {
  const p = node.props as {
    url?: string;
    children?: string;
    alt?: string;
    gap?: number;
    style?: CSSProperties;
    size?: 'small' | 'medium' | 'default' | 'large' | number;
    shape?: 'circle' | 'square';
  };
  const src = useGatewayResource(p.url);
  return (
    <Avatar
      data-testid="webui-avatar"
      data-variant={p.url ? 'image' : 'text'}
      src={src}
      alt={p.alt}
      gap={p.gap}
      size={p.size === 'medium' ? 'default' : p.size}
      shape={p.shape}
      className={appearance?.className}
      style={{ ...p.style, ...appearance?.style }}
    >
      {p.children}
    </Avatar>
  );
}
