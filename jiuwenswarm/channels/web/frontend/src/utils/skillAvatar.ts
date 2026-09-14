import type { CSSProperties } from 'react';

// 2026-09-08：配色从 17 个 Tailwind bg-*（中饱和实心色块）收敛为 5 组用户指定的低饱和色，
// 头像为"surface 色 20% 浅色底 + surface 色实色 1px 外扩描边 + 配套 text 色字母"的浅底彩字风格，
// text 是每组 surface 对应的深色文字色，保证浅底上的对比度。
// 颜色仍按名称首字母哈希，同一名称在技能面板/连接器市场/各搜索弹窗里颜色一致。
const avatarPalette = [
  { surface: '#9DBDFC', text: '#0b51de' },
  { surface: '#8FE5C2', text: '#058358' },
  { surface: '#FCCE92', text: '#c7630a' },
  { surface: '#D9B1FD', text: '#8a21bc' },
  { surface: '#F99AC7', text: '#c40256' },
];

export interface AvatarStyle {
  firstChar: string;
  /** 头像容器内联样式：surface 色 20% 背景 + surface 色实色 inset 描边 + 配套 text 色文字；尺寸/圆角/字号由调用方 className 控制 */
  style: CSSProperties;
}

function hexToRgba(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

// 2026-08-19：整合 ConnectorMarket/avatar.ts 的 deriveAvatarStyle——原来插件/MCP 那边另起了一份
// hex 配色的头像生成，跟这里字母大小写/取色逻辑本质相同但两套实现、两套配色表，用户要求统一成
// 这一份，插件/MCP 详情页、卡片、选择弹窗全部改用这个函数，avatar.ts 已删除。
/** 通用首字母头像：展示字母大写；颜色按首字母小写哈希，避免 Weather/weather 颜色不一致。
 * `style` 会覆盖调用方的 `text-text-inverse`（浅色底上白字不可读，字母改用配套深色）。 */
export function getSkillAvatar(name: string): AvatarStyle {
  const trimmed = String(name || '').trim() || '?';
  const firstChar = trimmed.charAt(0).toUpperCase();
  const colorSeed = trimmed.charAt(0).toLowerCase().charCodeAt(0) || 0;
  const { surface, text } = avatarPalette[colorSeed % avatarPalette.length];
  return {
    firstChar,
    style: {
      backgroundColor: hexToRgba(surface, 0.2),
      boxShadow: `inset 0 0 0 1px ${surface}`,
      color: text,
    },
  };
}
