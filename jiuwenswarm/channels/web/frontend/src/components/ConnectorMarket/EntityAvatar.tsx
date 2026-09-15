import { useState } from 'react';
import type { AvatarStyle } from '../../utils/skillAvatar';

interface EntityAvatarProps {
  /** 后端下发的真实图标地址（connector.icon / plugin_packages.show 的 avatar）。传空/undefined
   * 或加载失败都会回退到 avatar 生成的首字符色块。 */
  iconUrl?: string;
  avatar: AvatarStyle;
  className: string;
}

// 2026-08-07：后端确实返回图标时优先展示（connector.icon / plugin avatar），拿不到（字段为空，
// 或者字段给了但资源加载失败——见 utils/skillAvatar.ts 头部注释，图标下发格式目前还没定，
// img.onError 兜底处理"给了地址但取不到图"这种情况）时回退成按名称首字符生成的确定性色块。
// 2026-08-19：头像风格跟技能面板统一（getSkillAvatar 生成，2026-08-19 之前插件/MCP 那边是
// 单独一套 hex 透明度混色的浅底彩字风格）。2026-09-08 起配色收敛为 5 个低饱和色，背景色和
// 同色 20% 透明度内描边由 `avatar.style` 作为内联样式应用，这里只保留尺寸/圆角/文字类。
export function EntityAvatar({ iconUrl, avatar, className }: EntityAvatarProps) {
  const [imgFailed, setImgFailed] = useState(false);
  if (iconUrl && !imgFailed) {
    return <img src={iconUrl} alt="" className={`${className} object-cover`} onError={() => setImgFailed(true)} />;
  }
  return (
    <span className={`${className} text-text-inverse`} style={avatar.style}>
      {avatar.firstChar}
    </span>
  );
}
