export interface LocalizedText {
  zh: string;
  en: string;
}

export function localizedText(value: LocalizedText | undefined | null, language: string): string {
  if (!value) return '';
  const zh = value.zh || value.en;
  const en = value.en || value.zh;
  return language.toLowerCase().startsWith('en') ? en : zh;
}
