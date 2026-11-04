import { Avatar, type AvatarProps } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';
import { useGatewayResource } from '../../../client/GatewayResources.tsx';

export function WebUIAvatar({ node, appearance }: WebUIAdapterProps) {
  const p = node.props as Pick<AvatarProps, 'alt' | 'size' | 'shape'> & { src: string };
  const src = useGatewayResource(p.src);
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
