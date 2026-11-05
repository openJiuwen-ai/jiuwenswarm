import { Avatar, type AvatarProps } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';
import { useGatewayResource } from '../../../client/GatewayResources.tsx';

export function WebUIAvatar({ node, appearance }: WebUIAdapterProps) {
  const p = node.props as Pick<AvatarProps, 'alt' | 'children' | 'gap' | 'style' | 'shape'> & {
    src?: string;
    size?: AvatarProps['size'] | 'medium';
  };
  const src = useGatewayResource(p.src);
  return (
    <Avatar
      data-testid="webui-avatar"
      data-variant={p.src ? 'image' : 'text'}
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
