/**
 * 受控外观的部件校验与下行快照（Gateway S_08 第 2、4 节）。
 * Gateway `webui/appearance.py` 执行同样的规则；两端共用 gatewayAppearanceCases.json。
 */

import { validMediaURL } from '../catalog/components/mediaSurface.ts';

export const WEBUI_APPEARANCE_EVENT = 'webui.appearance';

/** Every part, in snapshot order. */
export const APPEARANCE_PARTS = ['colors', 'identity', 'welcome', 'mascot'] as const;
export type WebUIAppearancePart = (typeof APPEARANCE_PARTS)[number];

/** Work modes the welcome text can address; host defaults cover the rest. */
export const WELCOME_MODES = ['work', 'code', 'design'] as const;
export type WebUIWelcomeMode = (typeof WELCOME_MODES)[number];

/** Decorative variables a package may set directly; text and surfaces stay host-owned. */
export const APPEARANCE_TOKEN_NAMES: readonly string[] = [
  '--color-scrollbar-thumb',
  '--color-sidebar-nav-active',
  '--color-sidebar-nav-active-hover',
  '--color-conversation-active-surface',
  '--color-chat-accent-subtle',
  '--color-action-primary-subtle',
  '--color-action-primary-glow',
  '--color-border-accent',
];

export const DEFAULT_SURFACE_OPACITY = 0.86;
export const MAX_NAME_CHARS = 32;
export const MAX_WELCOME_CHARS = 64;
export const MIN_HOVER_MS = 500;
export const MAX_HOVER_MS = 10_000;
export const MAX_HIGHLIGHT_COLORS = 2;
const MIN_SURFACE_OPACITY = 0.3;
const COLOR_FIELDS = ['accent', 'surface', 'surfaceOpacity', 'tokens'];
const LANGUAGES = ['zh', 'en'] as const;
const IMAGE_SUFFIXES = ['.png', '.webp', '.svg', '.gif'];
const ACCENT = /^#[0-9A-Fa-f]{6}$/;
const HEX_COLOR = /^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$/;
const CHANNEL = ' *([0-9]{1,3}) *';
const ALPHA = ' *(0(?:\\.[0-9]+)?|1(?:\\.0+)?|\\.[0-9]+) *';
const RGB = new RegExp(`^rgb\\(${CHANNEL},${CHANNEL},${CHANNEL}\\)$`);
const RGBA = new RegExp(`^rgba\\(${CHANNEL},${CHANNEL},${CHANNEL},${ALPHA}\\)$`);
// Python's unicodedata categories Cc, Zl and Zp.
const LINE_BREAKING = /[\p{Cc}\p{Zl}\p{Zp}]/u;

/** The colors part. */
export interface WebUIColors {
  accent?: string;
  surface?: 'opaque' | 'translucent';
  surfaceOpacity?: number;
  tokens?: Readonly<Record<string, string>>;
}

export type WebUILocalizedText = Readonly<Partial<Record<'zh' | 'en', string>>>;

/** Brand name, logo and tab icon; `favicon` is already filled from `logo` when omitted. */
export interface WebUIIdentity {
  name?: string;
  logo?: string;
  favicon?: string;
}

/** What the welcome highlight colours: the brand name only, or the whole line. */
export type WebUIHighlightScope = 'brand' | 'line';

/** The second half of the welcome line, and the colour of the brand name before it. */
export interface WebUIWelcome {
  default?: WebUILocalizedText;
  modes?: Readonly<Partial<Record<WebUIWelcomeMode, WebUILocalizedText>>>;
  /** One colour, or two for a left-to-right gradient; the host adjusts them for contrast. */
  highlight?: readonly string[];
  /** 'brand' (the default) colours the brand name; 'line' also the second half. Needs highlight. */
  highlightScope?: WebUIHighlightScope;
}

export interface WebUIMascot {
  src: string;
  hoverSrc?: string;
  hoverMs?: number;
}

export interface WebUIAppearanceParts {
  colors: WebUIColors;
  identity: WebUIIdentity;
  welcome: WebUIWelcome;
  mascot: WebUIMascot;
}

/** What one `ops.appearance.apply` call sets (a value) or withdraws (null) per part. */
export type WebUIAppearanceChange = { [P in WebUIAppearancePart]?: WebUIAppearanceParts[P] | null };

export interface WebUIAppearancePartValue<T> {
  packageId: string;
  value: T;
}

/** The composed appearance of a Gateway connection; parts without a provider are host defaults. */
export interface WebUIAppearanceSnapshot {
  revision: number;
  parts: { readonly [P in WebUIAppearancePart]?: WebUIAppearancePartValue<WebUIAppearanceParts[P]> };
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function onlyKeys(value: Record<string, unknown>, allowed: readonly string[]): boolean {
  return Object.keys(value).every((key) => allowed.includes(key));
}

export function validColor(value: unknown): boolean {
  if (typeof value !== 'string') return false;
  if (HEX_COLOR.test(value)) return true;
  const match = RGB.exec(value) ?? RGBA.exec(value);
  return !!match && match.slice(1, 4).every((channel) => Number(channel) <= 255);
}

/** Return a copy of a valid colors part, or null when any rule fails. */
export function validateColors(value: unknown): WebUIColors | null {
  if (!isPlainObject(value)) return null;
  const keys = Object.keys(value);
  if (keys.length === 0 || !onlyKeys(value, COLOR_FIELDS)) return null;
  const result: { -readonly [K in keyof WebUIColors]: WebUIColors[K] } = {};
  if ('accent' in value) {
    if (typeof value.accent !== 'string' || !ACCENT.test(value.accent)) return null;
    result.accent = value.accent;
  }
  if ('surface' in value) {
    if (value.surface !== 'opaque' && value.surface !== 'translucent') return null;
    result.surface = value.surface;
  }
  if ('surfaceOpacity' in value) {
    const opacity = value.surfaceOpacity;
    if (typeof opacity !== 'number' || !Number.isFinite(opacity) || opacity < MIN_SURFACE_OPACITY || opacity > 1
      || value.surface !== 'translucent') return null;
    result.surfaceOpacity = opacity;
  }
  if ('tokens' in value) {
    const tokens = value.tokens;
    if (!isPlainObject(tokens)) return null;
    const names = Object.keys(tokens);
    if (names.length === 0 || names.some((name) => !APPEARANCE_TOKEN_NAMES.includes(name) || !validColor(tokens[name]))) {
      return null;
    }
    result.tokens = { ...(tokens as Record<string, string>) };
  }
  return result;
}

/** Plain single-line text of 1 to `limit` characters (code points), trimmed; null otherwise. */
function text(value: unknown, limit: number): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  const length = [...trimmed].length;
  return length > 0 && length <= limit && !LINE_BREAKING.test(trimmed) ? trimmed : null;
}

function localized(value: unknown, limit: number): WebUILocalizedText | null {
  if (!isPlainObject(value) || Object.keys(value).length === 0 || !onlyKeys(value, LANGUAGES)) return null;
  const result: Partial<Record<'zh' | 'en', string>> = {};
  for (const language of LANGUAGES) {
    if (!(language in value)) continue;
    const item = text(value[language], limit);
    if (item === null) return null;
    result[language] = item;
  }
  return result;
}

/** A package or data asset path naming a supported image; ownership is checked by the Gateway. */
export function validImage(value: unknown): value is string {
  if (typeof value !== 'string' || !validMediaURL(value)) return false;
  const path = value.split(/[?#]/, 1)[0].toLowerCase();
  return IMAGE_SUFFIXES.some((suffix) => path.endsWith(suffix));
}

export function validateIdentity(value: unknown): WebUIIdentity | null {
  if (!isPlainObject(value) || Object.keys(value).length === 0 || !onlyKeys(value, ['name', 'logo', 'favicon'])) return null;
  const result: { -readonly [K in keyof WebUIIdentity]: WebUIIdentity[K] } = {};
  if ('name' in value) {
    const name = text(value.name, MAX_NAME_CHARS);
    if (name === null) return null;
    result.name = name;
  }
  if ('logo' in value) {
    if (!validImage(value.logo)) return null;
    result.logo = value.logo;
  }
  if ('favicon' in value) {
    if (!validImage(value.favicon)) return null;
    result.favicon = value.favicon;
  } else if (result.logo !== undefined) {
    result.favicon = result.logo;
  }
  return result;
}

export function validateWelcome(value: unknown): WebUIWelcome | null {
  if (!isPlainObject(value) || Object.keys(value).length === 0
    || !onlyKeys(value, ['default', 'modes', 'highlight', 'highlightScope'])) {
    return null;
  }
  const result: { -readonly [K in keyof WebUIWelcome]: WebUIWelcome[K] } = {};
  if ('default' in value) {
    const item = localized(value.default, MAX_WELCOME_CHARS);
    if (item === null) return null;
    result.default = item;
  }
  if ('modes' in value) {
    const modes = value.modes;
    if (!isPlainObject(modes) || Object.keys(modes).length === 0 || !onlyKeys(modes, WELCOME_MODES)) return null;
    const items: Partial<Record<WebUIWelcomeMode, WebUILocalizedText>> = {};
    for (const mode of WELCOME_MODES) {
      if (!(mode in modes)) continue;
      const item = localized(modes[mode], MAX_WELCOME_CHARS);
      if (item === null) return null;
      items[mode] = item;
    }
    result.modes = items;
  }
  if ('highlight' in value) {
    const colors = value.highlight;
    if (!Array.isArray(colors) || colors.length === 0 || colors.length > MAX_HIGHLIGHT_COLORS
      || !colors.every((color) => typeof color === 'string' && ACCENT.test(color))) return null;
    result.highlight = [...colors];
  }
  if ('highlightScope' in value) {
    const scope = value.highlightScope;
    if ((scope !== 'brand' && scope !== 'line') || result.highlight === undefined) return null;
    result.highlightScope = scope;
  }
  return result;
}

export function validateMascot(value: unknown): WebUIMascot | null {
  if (!isPlainObject(value) || !('src' in value) || !onlyKeys(value, ['src', 'hoverSrc', 'hoverMs'])) return null;
  if (('hoverSrc' in value) !== ('hoverMs' in value) || !validImage(value.src)) return null;
  const result: WebUIMascot = { src: value.src };
  if ('hoverSrc' in value) {
    const duration = value.hoverMs;
    if (typeof duration !== 'number' || !Number.isInteger(duration) || duration < MIN_HOVER_MS || duration > MAX_HOVER_MS
      || !validImage(value.hoverSrc)) return null;
    return { ...result, hoverSrc: value.hoverSrc, hoverMs: duration };
  }
  return result;
}

const PART_VALIDATORS: { [P in WebUIAppearancePart]: (value: unknown) => WebUIAppearanceParts[P] | null } = {
  colors: validateColors,
  identity: validateIdentity,
  welcome: validateWelcome,
  mascot: validateMascot,
};

/**
 * The parts an appearance object sets or withdraws, or null when any rule fails (Gateway S_08 section 1).
 * An object made only of color fields is the colors part.
 */
export function validateAppearance(value: unknown): WebUIAppearanceChange | null {
  if (!isPlainObject(value)) return null;
  const keys = Object.keys(value);
  if (keys.length === 0 || keys.some((key) => !COLOR_FIELDS.includes(key) && !APPEARANCE_PARTS.includes(key as WebUIAppearancePart))) {
    return null;
  }
  if (keys.every((key) => COLOR_FIELDS.includes(key))) {
    const colors = validateColors(value);
    return colors ? { colors } : null;
  }
  if (keys.some((key) => COLOR_FIELDS.includes(key))) return null;
  const result: Record<string, unknown> = {};
  for (const part of APPEARANCE_PARTS) {
    if (!(part in value)) continue;
    if (value[part] === null) {
      result[part] = null;
      continue;
    }
    const valid = PART_VALIDATORS[part](value[part]);
    if (valid === null) return null;
    result[part] = valid;
  }
  return result as WebUIAppearanceChange;
}

/**
 * Read one `webui.appearance` snapshot; malformed frames return null and are ignored.
 *
 * Part names this client does not know are skipped, so a host that adds a part later does not make the
 * parts it also sends unusable; the known parts are still checked in full. `activation_generation` is
 * the transport's activation stamp and carries no appearance.
 */
export function readAppearanceFrame(payload: unknown): WebUIAppearanceSnapshot | null {
  if (!isPlainObject(payload) || !onlyKeys(payload, ['revision', 'parts', 'activation_generation'])) return null;
  const { revision, parts } = payload;
  if (typeof revision !== 'number' || !Number.isSafeInteger(revision) || revision < 1) return null;
  if (!isPlainObject(parts)) return null;
  const result: Record<string, WebUIAppearancePartValue<unknown>> = {};
  for (const part of APPEARANCE_PARTS) {
    if (!(part in parts)) continue;
    const entry = parts[part];
    if (!isPlainObject(entry) || !onlyKeys(entry, ['package_id', 'value'])) return null;
    const packageId = entry.package_id;
    if (typeof packageId !== 'string' || !packageId || packageId.length > 128) return null;
    const value = PART_VALIDATORS[part](entry.value);
    if (value === null) return null;
    result[part] = { packageId, value };
  }
  return { revision, parts: result as WebUIAppearanceSnapshot['parts'] };
}

/** Pick the text of the interface language: `zh` for languages starting with zh, `en` otherwise, then the other one. */
export function localizedText(value: WebUILocalizedText | undefined, language: string): string | null {
  if (!value) return null;
  const preferred = language.toLowerCase().startsWith('zh') ? 'zh' : 'en';
  return value[preferred] ?? value[preferred === 'zh' ? 'en' : 'zh'] ?? null;
}

/** The welcome suffix a package provides for a mode, or null for the host default (Gateway S_08 section 2.5). */
export function welcomeSuffix(welcome: WebUIWelcome | undefined, mode: string, language: string): string | null {
  if (!welcome) return null;
  const forMode = (WELCOME_MODES as readonly string[]).includes(mode)
    ? welcome.modes?.[mode as WebUIWelcomeMode] : undefined;
  return localizedText(forMode, language) ?? localizedText(welcome.default, language);
}
