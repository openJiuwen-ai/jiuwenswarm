import { formatContextWindowTokens, parseContextWindowTokens } from './contextWindow';

export const DEFAULT_ANTHROPIC_MAX_TOKENS = 32 * 1024;
export const OUTPUT_TOKEN_PRESETS = ['8K', '16K', '32K', '64K'] as const;

const LOW_ANTHROPIC_OUTPUT_TOKEN_CAPS: readonly (readonly [string, number])[] = [
  ['claude-3-5', 8 * 1024],
  ['claude-3.5', 8 * 1024],
  ['claude-3-opus', 4 * 1024],
  ['claude-3-sonnet', 4 * 1024],
  ['claude-3-haiku', 4 * 1024],
];

/** Parse a user-configured per-request output limit. */
export function parseOutputTokens(value: unknown): number | null {
  return parseContextWindowTokens(value);
}

export function normalizeOutputTokens(value: unknown): string {
  const parsed = parseOutputTokens(value);
  return parsed === null ? '' : formatContextWindowTokens(parsed);
}

export function resolveDraftOutputTokens(value: unknown): number | null {
  return parseOutputTokens(value);
}

/** Return the known Anthropic output cap for older Claude families, if any. */
export function getAnthropicOutputTokenCap(modelName: unknown): number | undefined {
  const normalizedName = String(modelName ?? '').trim().toLowerCase();
  return LOW_ANTHROPIC_OUTPUT_TOKEN_CAPS.find(([family]) => normalizedName.includes(family))?.[1];
}

export function defaultOutputTokens(protocol: 'openai' | 'anthropic', modelName: unknown): string {
  if (protocol === 'openai') return '';
  const modelCap = getAnthropicOutputTokenCap(modelName);
  return normalizeOutputTokens(Math.min(DEFAULT_ANTHROPIC_MAX_TOKENS, modelCap ?? DEFAULT_ANTHROPIC_MAX_TOKENS));
}
