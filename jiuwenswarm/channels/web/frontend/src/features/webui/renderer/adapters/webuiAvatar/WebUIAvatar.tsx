import { Avatar } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';

export function WebUIAvatar({ node, appearance }: WebUIAdapterProps) {
  const p = node.props as {
    url: string;
    alt?: string;
    size?: 'small' | 'medium' | 'default' | 'large' | number;
    shape?: 'circle' | 'square';
  };
  return (
    <Avatar
      data-testid="webui-avatar"
      src={p.url}
      alt={p.alt}
      size={p.size === 'medium' ? 'default' : p.size}
      shape={p.shape}
      className={appearance?.className}
      style={appearance?.style}
    />
  );
}
