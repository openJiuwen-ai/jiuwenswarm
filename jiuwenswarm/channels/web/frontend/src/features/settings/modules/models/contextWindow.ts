export const DEFAULT_CONTEXT_WINDOW_TOKENS = 256 * 1024;
export const ONE_MILLION_CONTEXT_WINDOW_TOKENS = 1024 * 1024;
export const CONTEXT_WINDOW_PRESETS = ['128K', '256K', '512K', '1M'] as const;

const MAX_CONTEXT_WINDOW_TOKENS = Number.MAX_SAFE_INTEGER;
const CONTEXT_WINDOW_PATTERN = /^([0-9]+(?:\.[0-9]+)?)\s*(tokens?|k(?:i?b)?|m(?:i?b)?)?$/i;

const CONTEXT_WINDOW_UNIT_MULTIPLIERS: Record<string, number> = {
  token: 1,
  tokens: 1,
  k: 1024,
  kb: 1024,
  ki: 1024,
  kib: 1024,
  m: 1024 * 1024,
  mb: 1024 * 1024,
  mi: 1024 * 1024,
  mib: 1024 * 1024,
};

function parseContextWindowNumber(value: number): number | null {
  return Number.isSafeInteger(value) && value > 0 && value <= MAX_CONTEXT_WINDOW_TOKENS ? value : null;
}

/**
 * Parse a context-window value entered by a user.
 *
 * Plain token counts and binary K/M suffixes are accepted. The suffix is
 * case-insensitive, so values such as `256k`, `256 K`, `1m`, and `1M` all
 * resolve deterministically.
 */
export function parseContextWindowTokens(value: unknown): number | null {
  if (typeof value === 'number') return parseContextWindowNumber(value);
  if (value === null || value === undefined || typeof value === 'boolean') return null;

  const normalized = String(value).trim().replaceAll(',', '');
  const match = CONTEXT_WINDOW_PATTERN.exec(normalized);
  if (!match) return null;

  const amount = Number(match[1]);
  const multiplier = CONTEXT_WINDOW_UNIT_MULTIPLIERS[(match[2] ?? 'tokens').toLowerCase()];
  if (!Number.isFinite(amount) || multiplier === undefined) return null;
  return parseContextWindowNumber(amount * multiplier);
}

export function formatContextWindowTokens(value: unknown): string {
  const parsed = parseContextWindowTokens(value);
  if (parsed === null) return '256K';
  if (parsed % ONE_MILLION_CONTEXT_WINDOW_TOKENS === 0) {
    return `${parsed / ONE_MILLION_CONTEXT_WINDOW_TOKENS}M`;
  }
  if (parsed % 1024 === 0) return `${parsed / 1024}K`;
  return String(parsed);
}

export function normalizeContextWindowTokens(value: unknown): string {
  return parseContextWindowTokens(value) === null
    ? formatContextWindowTokens(DEFAULT_CONTEXT_WINDOW_TOKENS)
    : formatContextWindowTokens(value);
}

export function resolveDraftContextWindowTokens(value: unknown): number {
  return parseContextWindowTokens(value) ?? DEFAULT_CONTEXT_WINDOW_TOKENS;
}
