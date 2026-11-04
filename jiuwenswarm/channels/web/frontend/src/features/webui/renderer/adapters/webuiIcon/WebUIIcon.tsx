/**
 * Schema 组件：Icon。
 * 只渲染白名单内的 Lucide 图标，尺寸映射到宿主字号 token；
 * 名称在协议层已校验，查不到时返回 null 仅作防御。
 */

import { theme } from 'antd';
import { WEBUI_ICONS } from '../../../icons/registry.generated.ts';
import type { WebUIAdapterProps } from '../../types.ts';

export function WebUIIcon({ node, appearance }: WebUIAdapterProps) {
  const p = node.props as { name?: string; size?: 'small' | 'medium' | 'large'; 'aria-label'?: string };
  const { token } = theme.useToken();
  const name = typeof p.name === 'string' ? p.name : '';
  const Icon = Object.prototype.hasOwnProperty.call(WEBUI_ICONS, name) ? WEBUI_ICONS[name] : undefined;
  if (!Icon) {
    return null;
  }
  const sizes: Readonly<Record<string, number>> = {
    small: token.fontSizeSM,
    medium: token.fontSizeLG,
    large: token.fontSizeXL,
  };
  const label = typeof p['aria-label'] === 'string' ? p['aria-label'].trim() : '';
  return (
    <Icon
      size={sizes[p.size ?? 'medium'] ?? token.fontSizeLG}
      role={label ? 'img' : undefined}
      aria-label={label || undefined}
      aria-hidden={label ? undefined : 'true'}
      className={appearance?.className}
      style={appearance?.style}
      data-testid="webui-icon"
      data-variant={name}
    />
  );
}
