/**
 * 受控外观的对象与下行帧校验（Gateway S_08 第 2、4 节）。
 * Gateway `webui/appearance.py` 执行同样的规则；两端共用 gatewayAppearanceCases.json。
 */

export const WEBUI_APPEARANCE_EVENT = 'webui.appearance';

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
const MIN_SURFACE_OPACITY = 0.3;
const FIELDS = ['accent', 'surface', 'surfaceOpacity', 'tokens'];
const ACCENT = /^#[0-9A-Fa-f]{6}$/;
const HEX_COLOR = /^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$/;
const CHANNEL = ' *([0-9]{1,3}) *';
const ALPHA = ' *(0(?:\\.[0-9]+)?|1(?:\\.0+)?|\\.[0-9]+) *';
const RGB = new RegExp(`^rgb\\(${CHANNEL},${CHANNEL},${CHANNEL}\\)$`);
const RGBA = new RegExp(`^rgba\\(${CHANNEL},${CHANNEL},${CHANNEL},${ALPHA}\\)$`);

export interface WebUIAppearance {
  accent?: string;
  surface?: 'opaque' | 'translucent';
  surfaceOpacity?: number;
  tokens?: Readonly<Record<string, string>>;
}

export interface WebUIAppearanceFrame {
  packageId: string;
  revision: number;
  /** null revokes the current contribution. */
  appearance: WebUIAppearance | null;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

export function validColor(value: unknown): boolean {
  if (typeof value !== 'string') return false;
  if (HEX_COLOR.test(value)) return true;
  const match = RGB.exec(value) ?? RGBA.exec(value);
  return !!match && match.slice(1, 4).every((channel) => Number(channel) <= 255);
}

/** Return a copy of a valid appearance object, or null when any rule fails. */
export function validateAppearance(value: unknown): WebUIAppearance | null {
  if (!isPlainObject(value)) return null;
  const keys = Object.keys(value);
  if (keys.length === 0 || keys.some((key) => !FIELDS.includes(key))) return null;
  const result: { -readonly [K in keyof WebUIAppearance]: WebUIAppearance[K] } = {};
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

/** Read one `webui.appearance` payload; malformed frames return null and are ignored. */
export function readAppearanceFrame(payload: unknown): WebUIAppearanceFrame | null {
  if (!isPlainObject(payload)) return null;
  if ('parts' in payload) return readSnapshotFrame(payload);
  if (Object.keys(payload).some((key) => !['package_id', 'revision', 'appearance', 'activation_generation'].includes(key))) {
    return null;
  }
  const { package_id: packageId, revision, appearance } = payload;
  if (typeof packageId !== 'string' || !packageId || packageId.length > 128) return null;
  if (typeof revision !== 'number' || !Number.isSafeInteger(revision) || revision < 1) return null;
  if (appearance === null) return { packageId, revision, appearance: null };
  const valid = validateAppearance(appearance);
  return valid ? { packageId, revision, appearance: valid } : null;
}

const SNAPSHOT_FIELDS = ['revision', 'parts', 'activation_generation'];
const PART_NAMES = ['colors', 'identity', 'welcome', 'mascot'] as const;

/**
 * Read the host's composed snapshot (Gateway S_08): ``parts`` holds one provider per part.
 *
 * Only the colors part is applied here. Other parts are validated by the host and ignored
 * until this client renders them; unknown part names are ignored too, so a host that adds a
 * part later does not make the colors it sends unusable.
 */
function readSnapshotFrame(payload: Record<string, unknown>): WebUIAppearanceFrame | null {
  if (Object.keys(payload).some((key) => !SNAPSHOT_FIELDS.includes(key))) return null;
  const revision = payload.revision;
  if (typeof revision !== 'number' || !Number.isSafeInteger(revision) || revision < 1) return null;
  const parts = payload.parts;
  if (!isPlainObject(parts)) return null;
  const colors = parts.colors;
  if (colors === undefined) {
    // No colors provider: the host defaults apply, and the first provider still names the splash owner.
    let packageId = '';
    for (const name of PART_NAMES) {
      const part = parts[name];
      if (isPlainObject(part) && typeof part.package_id === 'string' && part.package_id) {
        packageId = part.package_id;
        break;
      }
    }
    return { packageId, revision, appearance: null };
  }
  if (!isPlainObject(colors) || typeof colors.package_id !== 'string' || !colors.package_id
    || colors.package_id.length > 128 || !('value' in colors)) return null;
  if (colors.value === null) return { packageId: colors.package_id, revision, appearance: null };
  const valid = validateAppearance(colors.value);
  return valid ? { packageId: colors.package_id, revision, appearance: valid } : null;
}
