/**
 * 外观派生（Gateway S_08 第 3 节）：强调色变量与半透明预设，全部写成浏览器可直接解析的颜色值，
 * 以便 antd 主题桥接从计算样式读取。半透明规则只在应用背景显示时生效。
 */

import { DEFAULT_SURFACE_OPACITY, type WebUIAppearance } from './appearance.ts';

export type RGB = readonly [number, number, number];

const WHITE: RGB = [255, 255, 255];
const BLACK: RGB = [0, 0, 0];

const ACCENT_STRONG_VARIABLES = [
  '--color-action-primary', '--color-border-focus', '--color-chat-accent', '--color-chat-send-active',
  '--color-switch-track-on', '--color-settings-switch-checked', '--color-skill-capsule-active',
  '--color-conversation-unread', '--color-settings-link', '--color-cron-action-link', '--color-text-link',
];

/** Surface variables of the translucent preset, grouped by opacity rule (S_08 §3.2). */
const SURFACE_GROUPS = {
  page: ['--color-surface-page', '--color-surface-page-accent', '--color-extension-section-surface'],
  side: ['--color-sidebar-surface', '--color-settings-nav-surface', '--color-surface-chrome', '--color-rsi-canvas-bg'],
  panel: ['--color-surface-content', '--color-surface-panel', '--color-rsi-page-bg'],
  card: ['--color-surface-card', '--color-surface-elevated'],
} as const;
export const SURFACE_BASE_VARIABLES: readonly string[] = Object.values(SURFACE_GROUPS).flat();

// Theme defaults used when a base variable cannot be read as a color.
const FALLBACK_BASES: Readonly<Record<keyof typeof SURFACE_GROUPS, RGB>> = {
  page: [245, 247, 249], side: [247, 248, 248], panel: [255, 255, 255], card: [255, 255, 255],
};

const ACCENT_ROOT = ':root[data-webui-appearance][data-theme][data-color-mode]';
const TRANSLUCENT = "[data-webui-surface='translucent'][data-webui-background='active']";
const TRANSLUCENT_ROOT = `:root[data-webui-appearance]${TRANSLUCENT}[data-theme][data-color-mode]`;

export function parseColor(value: string): RGB | null {
  const text = value.trim();
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.exec(text)?.[1];
  if (hex) {
    const full = hex.length === 3 ? [...hex].map((digit) => digit + digit).join('') : hex.slice(0, 6);
    return [0, 2, 4].map((index) => Number.parseInt(full.slice(index, index + 2), 16)) as unknown as RGB;
  }
  const rgb = /^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,[^)]*)?\)$/i.exec(text);
  if (rgb) {
    const channels = rgb.slice(1, 4).map(Number);
    return channels.every((channel) => channel <= 255) ? channels as unknown as RGB : null;
  }
  const list = /^(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})$/.exec(text);
  return list ? list.slice(1, 4).map(Number) as unknown as RGB : null;
}

function linear(channel: number): number {
  const value = channel / 255;
  return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
}

/** WCAG 2 contrast ratio between two colors. */
export function contrastRatio(a: RGB, b: RGB): number {
  const luminance = (color: RGB) => 0.2126 * linear(color[0]) + 0.7152 * linear(color[1]) + 0.0722 * linear(color[2]);
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (light + 0.05) / (dark + 0.05);
}

function mix(a: RGB, b: RGB, amount: number): RGB {
  return a.map((channel, index) => Math.round(channel + (b[index] - channel) * amount)) as unknown as RGB;
}

const hex = (color: RGB) => `#${color.map((channel) => channel.toString(16).padStart(2, '0')).join('')}`;
const list = (color: RGB) => color.join(', ');
const rgba = (color: RGB, alpha: number) => `rgba(${list(color)}, ${Number(alpha.toFixed(3))})`;

/** Darken until white text on the color reaches 4.5:1. */
export function strongAccent(accent: RGB): RGB {
  for (let step = 0; step <= 15; step += 1) {
    const color = mix(accent, BLACK, step * 0.04);
    if (contrastRatio(color, WHITE) >= 4.5) return color;
  }
  return mix(accent, BLACK, 0.6);
}

/** Lighten for hover while white text keeps at least 3:1. */
export function hoverAccent(strong: RGB): RGB {
  for (let step = 4; step >= 1; step -= 1) {
    const color = mix(strong, WHITE, step * 0.04);
    if (contrastRatio(color, WHITE) >= 3) return color;
  }
  return strong;
}

export function accentVariables(accentHex: string): Record<string, string> {
  const accent = parseColor(accentHex) as RGB;
  const strong = strongAccent(accent);
  const variables: Record<string, string> = {};
  for (const name of ACCENT_STRONG_VARIABLES) variables[name] = hex(strong);
  variables['--color-action-primary-rgb'] = list(strong);
  variables['--color-action-primary-hover'] = hex(hoverAccent(strong));
  variables['--color-action-primary-subtle'] = rgba(accent, 0.12);
  variables['--color-chat-accent-subtle'] = rgba(accent, 0.14);
  variables['--color-action-primary-glow'] = rgba(accent, 0.16);
  variables['--color-border-accent'] = rgba(accent, 0.5);
  variables['--color-border-accent-rgb'] = list(accent);
  return variables;
}

/** Theme colors the translucent preset starts from, keyed by surface variable. */
export type SurfaceBases = Readonly<Record<string, RGB>>;

export function readSurfaceBases(read: (name: string) => string): SurfaceBases {
  const bases: Record<string, RGB> = {};
  for (const [group, names] of Object.entries(SURFACE_GROUPS) as Array<[keyof typeof SURFACE_GROUPS, readonly string[]]>) {
    for (const name of names) bases[name] = parseColor(read(name)) ?? FALLBACK_BASES[group];
  }
  return bases;
}

export function translucentVariables(opacity: number, bases: SurfaceBases): Record<string, string> {
  const alpha = { page: opacity * 0.42, side: opacity * 0.7, panel: Math.min(1, opacity + 0.02), card: Math.min(1, opacity + 0.08) };
  const variables: Record<string, string> = { '--color-surface-shell': 'transparent' };
  for (const [group, names] of Object.entries(SURFACE_GROUPS) as Array<[keyof typeof SURFACE_GROUPS, readonly string[]]>) {
    for (const name of names) variables[name] = rgba(bases[name] ?? FALLBACK_BASES[group], alpha[group]);
  }
  return variables;
}

const block = (selector: string, variables: Record<string, string>) =>
  `${selector}{${Object.entries(variables).map(([name, value]) => `${name}:${value}`).join(';')}}`;

/** Build the contribution style sheet for one appearance object. */
export function buildAppearanceCss(appearance: WebUIAppearance, bases: SurfaceBases): string {
  const rules: string[] = [];
  const variables = { ...(appearance.accent ? accentVariables(appearance.accent) : {}), ...appearance.tokens };
  if (Object.keys(variables).length) rules.push(block(ACCENT_ROOT, variables));
  if (appearance.surface === 'translucent') {
    const opacity = appearance.surfaceOpacity ?? DEFAULT_SURFACE_OPACITY;
    const card = bases['--color-surface-card'] ?? FALLBACK_BASES.card;
    const floating = bases['--color-sidebar-surface'] ?? FALLBACK_BASES.side;
    rules.push(
      block(TRANSLUCENT_ROOT, {
        ...translucentVariables(opacity, bases),
        // Role variables of the theme: the workspace shows the background through one layer, floating layers
        // stay readable over text and popovers stay nearly opaque. Components only consume these variables.
        '--color-surface-workspace': rgba(card, opacity * 0.7),
        '--color-surface-workspace-layer': 'transparent',
        '--color-chat-composer-context-surface': 'transparent',
        '--color-surface-floating': rgba(floating, 0.9),
        '--effect-surface-floating-backdrop': 'blur(24px) saturate(1.3)',
        '--color-surface-popover': rgba(card, 0.97),
        // The page itself shows the background host underneath; body paints this variable.
        '--effect-page-background': 'transparent',
      }),
    );
  }
  return rules.join('\n');
}
