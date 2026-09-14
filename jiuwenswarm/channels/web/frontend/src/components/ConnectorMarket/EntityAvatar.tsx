import { useState } from 'react';
import type { AvatarStyle } from '../../utils/skillAvatar';

interface EntityAvatarProps {
  /** 后端下发的真实图标地址（connector.icon / plugin_packages.list|show 的 avatar）。传空/undefined
   * 或加载失败都会回退到 avatar 生成的首字符色块。 */
  iconUrl?: string;
  avatar: AvatarStyle;
  className: string;
}

// 2026-08-07：后端确实返回图标时优先展示（connector.icon / plugin avatar），拿不到（字段为空，
// 或者字段给了但资源加载失败——见 utils/skillAvatar.ts 头部注释，图标下发格式目前还没定，
// img.onError 兜底处理"给了地址但取不到图"这种情况）时回退成按名称首字符生成的确定性色块。
// 2026-08-19：头像风格跟技能面板统一（getSkillAvatar 的 Tailwind bg-* 实心圆 + 白字母），不再是
// 单独一套 hex 透明度混色的浅底彩字风格，`avatar.color` 直接当 className 拼进去，不需要 style 了。
export function EntityAvatar({ iconUrl, avatar, className }: EntityAvatarProps) {
  const [imgFailed, setImgFailed] = useState(false);
  if (iconUrl && !imgFailed) {
    return <img src={iconUrl} alt="" className={`${className} object-cover`} onError={() => setImgFailed(true)} />;
  }
  return (
    <span className={`${className} ${avatar.color} text-text-inverse`}>
      {avatar.firstChar}
    </span>
  );
}
