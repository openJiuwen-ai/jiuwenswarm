import { Avatar } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';
import { useGatewayResource } from '../../../client/GatewayResources.tsx';

export function WebUIAvatar({ node, appearance }: WebUIAdapterProps) {
  const p = node.props as {
    url: string;
    alt?: string;
    size?: 'small' | 'medium' | 'default' | 'large' | number;
    shape?: 'circle' | 'square';
  };
  const src = useGatewayResource(p.url);
  return (
    <Avatar
      data-testid="webui-avatar"
      src={src}
      alt={p.alt}
      size={p.size === 'medium' ? 'default' : p.size}
      shape={p.shape}
      className={appearance?.className}
      style={appearance?.style}
    />
  );
}
