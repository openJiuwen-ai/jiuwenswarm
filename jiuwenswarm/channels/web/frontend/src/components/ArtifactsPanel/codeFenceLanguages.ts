/** Common fenced-code languages for the artifact style toolbar combobox. */
export const CODE_FENCE_LANGUAGES = [
  'bash',
  'sh',
  'zsh',
  'javascript',
  'js',
  'typescript',
  'ts',
  'jsx',
  'tsx',
  'python',
  'py',
  'json',
  'yaml',
  'yml',
  'toml',
  'markdown',
  'md',
  'html',
  'css',
  'scss',
  'sql',
  'go',
  'rust',
  'java',
  'kotlin',
  'c',
  'cpp',
  'csharp',
  'php',
  'ruby',
  'swift',
  'dart',
  'r',
  'lua',
  'perl',
  'powershell',
  'dockerfile',
  'xml',
  'graphql',
  'plaintext',
] as const;

export function filterCodeFenceLanguages(query: string): string[] {
  const q = query.trim().toLowerCase();
  if (!q) return [...CODE_FENCE_LANGUAGES];
  return CODE_FENCE_LANGUAGES.filter(lang => lang.includes(q));
}
