export interface LocalizedText {
  zh: string;
  en: string;
}

export function localizedText(
  value: LocalizedText | string | undefined | null,
  language: string,
): string {
  if (!value) return '';
  if (typeof value === 'string') return value;
  const zh = value.zh || value.en;
  const en = value.en || value.zh;
  return language.toLowerCase().startsWith('en') ? en : zh;
}
