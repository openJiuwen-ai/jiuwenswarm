/**
 * 外观偏好（界面字体 / 界面字号 / 代码字体）纯逻辑模块。
 *
 * - 偏好仅保存在本机 localStorage（key: `jiuwenswarm.appearance.v1`），不写 config.yaml、不上行服务端。
 * - 默认档位与 foundation.css 的固定值完全一致（14/16/13/12/11px），不设置偏好时零行为变化。
 * - 自定义字体名会做归一化，避免用户输入直接拼进 CSS 污染样式。
 */

export const APPEARANCE_STORAGE_KEY = 'jiuwenswarm.appearance.v1';

/** 界面字号基准值，须与 styles/foundation.css 中的 --font-size-* 保持一致。 */
const BASE_FONT_SIZES = {
  base: 14,
  lg: 16,
  sm: 13,
  xs: 12,
  '2xs': 11,
} as const;

export type FontSizeVarName = keyof typeof BASE_FONT_SIZES;

export const FONT_SCALE_MIN = 90;
export const FONT_SCALE_MAX = 140;

/** 设置页可选的字号档位（百分比）。 */
export const FONT_SCALE_STEPS = [90, 100, 110, 125, 140] as const;

const MAX_FONT_FAMILIES = 8;
const MAX_FONT_FAMILY_LENGTH = 64;

/** CSS 通用字体族关键字，归一化时保留小写关键字形式。 */
const GENERIC_FONT_FAMILIES = new Set([
  'serif',
  'sans-serif',
  'monospace',
  'cursive',
  'fantasy',
  'system-ui',
  'ui-serif',
  'ui-sans-serif',
  'ui-monospace',
  'ui-rounded',
  'math',
  'emoji',
  'fangsong',
]);

export interface AppearancePreferences {
  /** 界面字体预设 id；`custom` 表示使用自定义字体串。 */
  uiFontPreset: string;
  /** 自定义界面字体输入（逗号分隔的字体名列表）。 */
  uiFontCustom: string;
  /** 界面字号百分比（90–140）。 */
  fontScale: number;
  /** 代码字体预设 id；`custom` 表示使用自定义字体串。 */
  codeFontPreset: string;
  /** 自定义代码字体输入。 */
  codeFontCustom: string;
}

export const defaultAppearancePreferences: AppearancePreferences = {
  uiFontPreset: 'default',
  uiFontCustom: '',
  fontScale: 100,
  codeFontPreset: 'default',
  codeFontCustom: '',
};

/** 界面字体预设；`default` 为 null 表示沿用主题默认字体栈。 */
export const UI_FONT_PRESETS: Record<string, string | null> = {
  default: null,
  'zh-hei': '"Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif',
  'zh-song': '"SimSun", "Songti SC", "Source Han Serif SC", "Noto Serif CJK SC", serif',
  'zh-kai': '"Kaiti SC", "STKaiti", "KaiTi", "LXGW WenKai", "LXGW WenKai Lite", serif',
};

/** 代码字体预设；`default` 为 null 表示沿用主题默认等宽字体栈。 */
export const CODE_FONT_PRESETS: Record<string, string | null> = {
  default: null,
  'jetbrains-fira': '"JetBrains Mono", "Fira Code", "Cascadia Mono", ui-monospace, monospace',
  'cascadia-code': '"Cascadia Code", "Cascadia Mono", ui-monospace, monospace',
};

/**
 * 归一化自定义字体列表：
 * 剥离引号与反斜杠、剔除分号 / 花括号 / 尖括号 / 控制字符，最多 8 个族、每族 ≤64 字符；
 * 通用字体族保留关键字形式。
 */
export function normalizeFontList(input: unknown): string {
  if (typeof input !== 'string') return '';
  const families = input
    .split(',')
    .map((raw) => stripUnsafeCharacters(raw))
    .filter((name) => name.length > 0)
    .slice(0, MAX_FONT_FAMILIES)
    .map((name) =>
      GENERIC_FONT_FAMILIES.has(name.toLowerCase()) ? name.toLowerCase() : name.slice(0, MAX_FONT_FAMILY_LENGTH),
    );
  return families.join(', ');
}

function stripUnsafeCharacters(value: string): string {
  let out = '';
  for (const char of value) {
    const code = char.codePointAt(0) ?? 0;
    if (code <= 0x1f || code === 0x7f) continue;
    if ('"\'`\\;{}<>'.includes(char)) continue;
    out += char;
  }
  // 被剔除字符留下的连续空白折叠为单空格，避免拼出多余空格
  return out.replace(/\s+/g, ' ').trim();
}

/** 钳制字号档位：上下界 90–140，非法输入回退 100，结果保留两位小数。 */
export function clampFontScale(value: unknown): number {
  let parsed: number;
  if (typeof value === 'number') {
    parsed = value;
  } else if (typeof value === 'string' && value.trim() !== '') {
    parsed = Number(value);
  } else {
    return 100;
  }
  if (!Number.isFinite(parsed)) return 100;
  const rounded = Math.round(parsed * 100) / 100;
  return Math.min(FONT_SCALE_MAX, Math.max(FONT_SCALE_MIN, rounded));
}

/**
 * 解析字体栈：预设命中返回预设栈；`custom` 返回归一化后的自定义串（为空回退默认）；
 * 其余情况（含 `default` 与未知值）返回 null 表示沿用主题默认。
 */
export function resolveFontStack(
  preset: unknown,
  customInput: unknown,
  presets: Record<string, string | null>,
): string | null {
  const key = typeof preset === 'string' ? preset : 'default';
  if (key === 'custom') {
    const normalized = normalizeFontList(customInput);
    return normalized.length > 0 ? normalized : null;
  }
  if (Object.prototype.hasOwnProperty.call(presets, key)) {
    return presets[key] ?? null;
  }
  return null;
}

/** 按百分比换算字号变量；100% 时与 foundation.css 现状完全一致。 */
export function computeFontSizeVars(scalePercent: number): Record<FontSizeVarName, string> {
  const scale = clampFontScale(scalePercent) / 100;
  const scaled = (px: number): string => `${Math.round(px * scale * 100) / 100}px`;
  return {
    base: scaled(BASE_FONT_SIZES.base),
    lg: scaled(BASE_FONT_SIZES.lg),
    sm: scaled(BASE_FONT_SIZES.sm),
    xs: scaled(BASE_FONT_SIZES.xs),
    '2xs': scaled(BASE_FONT_SIZES['2xs']),
  };
}

export interface AppearanceStorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** 读取偏好；storage 缺失、JSON 损坏或字段非法时回退默认值。 */
export function readAppearancePreferences(storage: AppearanceStorageLike | null): AppearancePreferences {
  if (!storage) return { ...defaultAppearancePreferences };
  try {
    const raw = storage.getItem(APPEARANCE_STORAGE_KEY);
    if (!raw) return { ...defaultAppearancePreferences };
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      return { ...defaultAppearancePreferences };
    }
    const record = parsed as Record<string, unknown>;
    const text = (value: unknown): string => (typeof value === 'string' ? value : '');
    return {
      uiFontPreset: text(record.uiFontPreset) || 'default',
      uiFontCustom: text(record.uiFontCustom),
      fontScale: clampFontScale(record.fontScale),
      codeFontPreset: text(record.codeFontPreset) || 'default',
      codeFontCustom: text(record.codeFontCustom),
    };
  } catch {
    return { ...defaultAppearancePreferences };
  }
}

export function saveAppearancePreferences(storage: AppearanceStorageLike | null, prefs: AppearancePreferences): void {
  if (!storage) return;
  try {
    storage.setItem(APPEARANCE_STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // storage 不可用时静默降级：偏好仅在本会话内生效
  }
}

export function clearAppearancePreferences(storage: AppearanceStorageLike | null): void {
  if (!storage) return;
  try {
    storage.removeItem(APPEARANCE_STORAGE_KEY);
  } catch {
    // 同上，静默降级
  }
}

export interface AppearanceStyleTarget {
  setProperty(name: string, value: string): void;
  removeProperty(name: string): string;
}

const UI_FONT_PROPERTIES = ['--font-body', '--font-display'] as const;
const CODE_FONT_PROPERTIES = ['--font-mono'] as const;
const FONT_SIZE_PROPERTIES: Record<FontSizeVarName, string> = {
  base: '--font-size-base',
  lg: '--font-size-lg',
  sm: '--font-size-sm',
  xs: '--font-size-xs',
  '2xs': '--font-size-2xs',
};

/**
 * 将偏好应用到目标元素（通常为 `document.documentElement.style`）：
 * 只覆盖 CSS 变量、不动组件；默认档位时移除覆盖，完全回到主题默认。
 */
export function applyAppearancePreferences(prefs: AppearancePreferences, target: AppearanceStyleTarget | null): void {
  if (!target) return;
  const uiStack = resolveFontStack(prefs.uiFontPreset, prefs.uiFontCustom, UI_FONT_PRESETS);
  if (uiStack) {
    for (const property of UI_FONT_PROPERTIES) target.setProperty(property, uiStack);
  } else {
    for (const property of UI_FONT_PROPERTIES) target.removeProperty(property);
  }

  const codeStack = resolveFontStack(prefs.codeFontPreset, prefs.codeFontCustom, CODE_FONT_PRESETS);
  if (codeStack) {
    for (const property of CODE_FONT_PROPERTIES) target.setProperty(property, codeStack);
  } else {
    for (const property of CODE_FONT_PROPERTIES) target.removeProperty(property);
  }

  const sizeNames = Object.keys(FONT_SIZE_PROPERTIES) as FontSizeVarName[];
  if (prefs.fontScale !== 100) {
    const sizes = computeFontSizeVars(prefs.fontScale);
    for (const name of sizeNames) target.setProperty(FONT_SIZE_PROPERTIES[name], sizes[name]);
  } else {
    for (const name of sizeNames) target.removeProperty(FONT_SIZE_PROPERTIES[name]);
  }
}

/** 启动引导：在首屏渲染前应用已保存的外观偏好；任何异常都不阻断应用启动。 */
export function applySavedAppearancePreferences(): void {
  try {
    const storage: AppearanceStorageLike | null =
      typeof window === 'undefined' || !window.localStorage ? null : window.localStorage;
    const target: AppearanceStyleTarget | null =
      typeof document === 'undefined' || !document.documentElement ? null : document.documentElement.style;
    applyAppearancePreferences(readAppearancePreferences(storage), target);
  } catch {
    // 忽略：外观偏好失败不应影响应用启动
  }
}
