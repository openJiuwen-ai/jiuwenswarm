export type ConfiguredLanguage = 'en' | 'zh';

export function configuredLanguage(payload?: { preferred_language?: unknown }): ConfiguredLanguage {
  return payload?.preferred_language === 'en' ? 'en' : 'zh';
}

export async function applyConfiguredLanguage(
  load: () => Promise<{ preferred_language?: unknown }>,
  changeLanguage: (language: ConfiguredLanguage) => void | Promise<unknown>,
): Promise<void> {
  let language: ConfiguredLanguage = 'zh';
  try {
    language = configuredLanguage(await load());
  } catch {
    // Missing or unreadable JiuwenSwarm configuration uses Simplified Chinese.
  }
  await changeLanguage(language);
}
