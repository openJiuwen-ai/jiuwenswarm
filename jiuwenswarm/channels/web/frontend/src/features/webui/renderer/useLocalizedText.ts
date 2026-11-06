import { useTranslation } from 'react-i18next';
import type { LocalizedText } from '../catalog/components/localizedText.ts';

/** Select plugin-owned text using the same language policy as the host UI. */
export function useLocalizedText(value: LocalizedText | undefined): string | undefined {
  const { i18n } = useTranslation();
  return resolveLocalizedText(value, i18n.resolvedLanguage ?? i18n.language ?? 'zh');
}

export function resolveLocalizedText(value: LocalizedText | undefined, language: string): string | undefined {
  if (value === undefined || typeof value === 'string') return value;
  return value[language.startsWith('zh') ? 'zh' : 'en'];
}
