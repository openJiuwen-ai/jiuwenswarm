import type { WebUIPropRule } from '../types.ts';

export type LocalizedText = string | { zh: string; en: string };

const text: WebUIPropRule = { kind: 'string', maxBytes: 16 * 1024 };
export const localizedTextRule: WebUIPropRule = {
  kind: 'union',
  choices: [text, { kind: 'object', fields: { zh: text, en: text }, required: ['zh', 'en'] }],
};

export function hasNonEmptyLocalizedText(value: unknown): boolean {
  if (typeof value === 'string') return !!value.trim();
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const translations = value as Record<string, unknown>;
  return ['zh', 'en'].every(
    (language) => typeof translations[language] === 'string' && !!translations[language].trim(),
  );
}
