import { useState } from 'react';
import { getSkillAvatar } from '../../../utils/skillAvatar';

export interface EntityAvatarProps {
  /** 实体展示名：字母兜底的取字符与颜色哈希来源（经内置 getSkillAvatar 生成） */
  name: string;
  /** 真实图标地址：有值渲染 img，空值/加载失败回退字母头像 */
  iconUrl?: string | null;
  /** 头像位 testid：img/letter 互斥同槽位，用 data-variant 区分形态 */
  testId?: string;
}

/**
 * 实体头像（PageCard/EntityHeader 头像位通用）：iconUrl 有值渲染 <img>
 * （尺寸/圆角由 .entity-header__avatar 容器与 .entity-header__avatar img 提供），
 * 空值/加载失败回退 getSkillAvatar(name) 生成的首字母头像。
 * 颜色按名称首字母哈希，同名实体在技能面板 / Agent 管理 / 连接器市场取色一致。
 */
export function EntityAvatar({ name, iconUrl, testId }: EntityAvatarProps) {
  const [failedUri, setFailedUri] = useState<string | null>(null);
  const fallback = getSkillAvatar(name);
  if (!iconUrl || failedUri === iconUrl) {
    return (
      <span
        className="entity-header__avatar-letter"
        style={fallback.style}
        data-testid={testId}
        data-variant="letter"
      >
        {fallback.firstChar}
      </span>
    );
  }
  return (
    <img
      src={iconUrl}
      alt=""
      data-testid={testId}
      data-variant="img"
      onError={() => setFailedUri(iconUrl)}
    />
  );
}
