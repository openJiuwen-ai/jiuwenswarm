export const CODE_LANGUAGE_EXTENSIONS = [
  'bash',
  'c',
  'cc',
  'cjs',
  'cpp',
  'cs',
  'css',
  'go',
  'h',
  'hpp',
  'java',
  'js',
  'jsx',
  'kt',
  'kts',
  'lua',
  'mjs',
  'php',
  'pl',
  'py',
  'rb',
  'rs',
  'sh',
  'sql',
  'swift',
  'toml',
  'ts',
  'tsx',
  'vue',
  'xml',
  'yaml',
  'yml',
] as const;

export type CodeLanguageExtension = (typeof CODE_LANGUAGE_EXTENSIONS)[number];

const CODE_LANGUAGE_EXTENSION_SET: ReadonlySet<string> = new Set(CODE_LANGUAGE_EXTENSIONS);

export function fileExtension(name: string): string {
  const value = name.split('.').pop()?.trim().toLowerCase() ?? '';
  return value === name.toLowerCase() ? '' : value;
}

export function isCodeLanguageExtension(value: string): value is CodeLanguageExtension {
  return CODE_LANGUAGE_EXTENSION_SET.has(value);
}
